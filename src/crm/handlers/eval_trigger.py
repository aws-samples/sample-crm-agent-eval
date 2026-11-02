# SPDX-License-Identifier: MIT
"""``eval_trigger`` Lambda handler: the run API for state-machine eval sweeps.

Operations, selected by a required ``action`` argument:

* ``submit {mode?: "stub"|"live", confirmed?: bool, repeats?: int}`` — start a sweep (default
  ``stub``, 1 repeat) and return its run. ``live`` requires an admin caller and ``confirmed: true``;
  ``stub`` requires full tenant visibility.
* ``estimate {repeats?: int}`` — the live-sweep cost estimate for that many repeats, and its basis.
* ``status {run_id}`` — one run, reconciled against its execution first.
* ``list {cursor?}`` — 20 runs, newest first, and the next page's cursor.
* ``cancel {run_id}`` — admin only; cooperative, never stops the execution.
* ``unlock`` — admin only; releases a lock whose holder is not running.
* ``artefact_url {run_id, key}`` — a presigned GET, valid 300 s, for one artefact the run recorded;
  admin or full tenant visibility (``eval.artefacts``).

This handler is not an action tool; it composes ``_common``'s ``decode``, ``ok``,
``error_response`` and ``is_proxy_event`` and validates its own arguments. Unauthenticated callers
get 401; ``live``/``cancel``/``unlock`` by a non-admin get 403 ``eval:admin``. A new run's id is
always allocated server-side.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from crm.authz import Identity
from crm.errors import CrmError, ValidationFailed, bounded_detail_value
from crm.handlers._common import decode, default_store, error_response, is_proxy_event, ok
from crm.store.store import Store

if TYPE_CHECKING:
    from eval.harness import EvalMode

ACTION_SUBMIT: Final[str] = "submit"
ACTION_ESTIMATE: Final[str] = "estimate"
ACTION_STATUS: Final[str] = "status"
ACTION_LIST: Final[str] = "list"
ACTION_CANCEL: Final[str] = "cancel"
ACTION_UNLOCK: Final[str] = "unlock"
ACTION_ARTEFACT_URL: Final[str] = "artefact_url"
ACTIONS: Final[tuple[str, ...]] = (
    ACTION_SUBMIT,
    ACTION_ESTIMATE,
    ACTION_STATUS,
    ACTION_LIST,
    ACTION_CANCEL,
    ACTION_UNLOCK,
    ACTION_ARTEFACT_URL,
)

#: Every argument key this handler recognises. An unrecognised key is refused.
_RECOGNISED_ARGUMENTS: Final[frozenset[str]] = frozenset(
    {"action", "run_id", "mode", "confirmed", "cursor", "key", "repeats"}
)

#: The keys each action accepts beyond ``action``.
_ACTION_ARGUMENTS: Final[dict[str, frozenset[str]]] = {
    ACTION_SUBMIT: frozenset({"mode", "confirmed", "repeats"}),
    ACTION_ESTIMATE: frozenset({"repeats"}),
    ACTION_STATUS: frozenset({"run_id"}),
    ACTION_LIST: frozenset({"cursor"}),
    ACTION_CANCEL: frozenset({"run_id"}),
    ACTION_UNLOCK: frozenset(),
    ACTION_ARTEFACT_URL: frozenset({"run_id", "key"}),
}

_MAX_CURSOR_LENGTH: Final[int] = 2048


def _require_action(arguments: dict[str, Any]) -> str:
    """Return the validated ``action``, refusing unknown actions and keys it does not accept.

    Raises:
        ValidationFailed: ``action`` is absent or unknown, or a key is unrecognised or not
            accepted by that action.
    """
    unexpected = sorted(set(arguments) - _RECOGNISED_ARGUMENTS)
    if unexpected:
        raise ValidationFailed(
            f"Unrecognised argument; recognised: {sorted(_RECOGNISED_ARGUMENTS)}.",
            source="arguments",
            detail=f"unexpected={[bounded_detail_value(key) for key in unexpected[:8]]}",
        )
    action = arguments.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValidationFailed(
            f"An action is required, one of {list(ACTIONS)}.",
            source="action",
            detail=f"received action={bounded_detail_value(action)}",
        )
    extra = sorted(set(arguments) - {"action"} - _ACTION_ARGUMENTS[action])
    if extra:
        raise ValidationFailed(
            f"The {action} action accepts only {sorted({'action'} | _ACTION_ARGUMENTS[action])}.",
            source="arguments",
            detail=f"extra={[bounded_detail_value(key) for key in extra[:8]]}",
        )
    return action


def _require_run_id(arguments: dict[str, Any]) -> str:
    """Return the validated non-empty ``run_id``.

    Raises:
        ValidationFailed: ``run_id`` is absent, not a string, empty, or carries an internal row
            prefix.
    """
    run_id = arguments.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValidationFailed(
            "A run_id is required.",
            source="run_id",
            detail=f"received run_id={run_id!r}",
        )
    if "#" in run_id:
        raise ValidationFailed(
            "A run_id must not carry an internal row prefix.",
            source="run_id",
            detail="row keys are derived internally by eval.runs",
        )
    return run_id


def _submit_arguments(arguments: dict[str, Any]) -> tuple[EvalMode, bool, int]:
    """Return ``(mode, confirmed, repeats)`` for ``submit``.

    Raises:
        ValidationFailed: ``mode`` is not ``stub``/``live``, ``confirmed`` is not a boolean, or
            ``repeats`` is not accepted by :func:`eval.runs.validate_repeats`.
    """
    from eval.runs import validate_repeats

    raw_mode = arguments.get("mode", "stub")
    if raw_mode not in ("stub", "live"):
        raise ValidationFailed("mode must be 'stub' or 'live'.", source="mode")
    mode: EvalMode = "live" if raw_mode == "live" else "stub"
    confirmed = arguments.get("confirmed", False)
    if not isinstance(confirmed, bool):
        raise ValidationFailed("confirmed must be a boolean.", source="confirmed")
    return mode, confirmed, validate_repeats(arguments.get("repeats"))


def _cursor(arguments: dict[str, Any]) -> str | None:
    cursor = arguments.get("cursor")
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
        raise ValidationFailed("The history cursor is not valid.", source="cursor")
    return cursor


def _eval_trigger(
    store: Store, sfn: Any, identity: Identity, arguments: dict[str, Any], s3: Any = None
) -> dict[str, Any]:
    """Authorize and perform one run-API action.

    Authorization runs before any write.

    Raises:
        ScopeDenied: The caller lacks the action's permission.
        ValidationFailed: The arguments are not a recognised operation.
        NotFound: ``status``/``cancel``/``artefact_url`` named an unknown run, or
            ``artefact_url`` an artefact the run did not record.
        ArtefactsNotConfiguredError: ``artefact_url`` on a deployment without the artefacts env.
        Conflict: ``submit`` while a sweep runs or at the daily live cap; ``unlock`` of a running
            sweep.
    """
    # Deferred import: `eval` imports `crm`.
    from eval.runs import (
        estimate,
        list_runs,
        reconcile,
        request_cancel,
        require_run,
        unlock,
        validate_repeats,
    )
    from eval.trigger import require_eval_admin, submit_sweep, sweep_shape

    action = _require_action(arguments)

    if action == ACTION_SUBMIT:
        mode, confirmed, repeats = _submit_arguments(arguments)
        record = submit_sweep(store, sfn, identity, mode, confirmed, repeats=repeats)
        return record.to_payload()

    if action == ACTION_ESTIMATE:
        families, task_count = sweep_shape()
        repeats = validate_repeats(arguments.get("repeats"))
        return estimate(
            store, task_count=task_count, families=families, repeats=repeats
        ).to_payload()

    if action == ACTION_STATUS:
        record = require_run(store, _require_run_id(arguments))
        return reconcile(store, sfn, record).to_payload()

    if action == ACTION_LIST:
        records, next_cursor = list_runs(store, cursor=_cursor(arguments))
        page = [reconcile(store, sfn, record).to_payload() for record in records]
        return {"runs": page, "next_cursor": next_cursor}

    if action == ACTION_ARTEFACT_URL:
        from eval.artefacts import ArtefactsConfig, artefact_url

        run_id = _require_run_id(arguments)
        config = ArtefactsConfig.from_env()
        return artefact_url(
            store,
            s3 if s3 is not None else _artefacts_s3_client(),
            identity,
            run_id=run_id,
            key=arguments.get("key"),
            config=config,
        )

    if action == ACTION_CANCEL:
        run_id = _require_run_id(arguments)
        require_eval_admin(identity, operation="eval_trigger:cancel")
        disposition, record = request_cancel(store, run_id=run_id, identity=identity)
        return {"disposition": disposition, "run": record.to_payload()}

    require_eval_admin(identity, operation="eval_trigger:unlock")
    return unlock(store, sfn)


def _artefacts_s3_client() -> Any:
    """An S3 client for presigning artefact GETs: SigV4, in the resolved region."""
    import boto3
    from botocore.config import Config

    from crm.config import resolve_aws_config

    region = resolve_aws_config(validate=False).region
    return boto3.client(
        "s3",
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


class _LazyStepFunctions:
    """A Step Functions client built on first use, so a request that never reaches it needs none.

    The region is the one :func:`crm.config.resolve_aws_config` resolves.
    """

    def __init__(self) -> None:
        self._client: Any = None

    def __getattr__(self, name: str) -> Any:
        if self._client is None:
            import boto3

            from crm.config import resolve_aws_config

            region = resolve_aws_config(validate=False).region
            self._client = boto3.client("stepfunctions", region_name=region)
        return getattr(self._client, name)


def handler(
    event: dict[str, Any],
    context: object = None,
    *,
    store: Store | None = None,
    sfn: Any | None = None,
    s3: Any | None = None,
) -> dict[str, Any]:
    """AWS Lambda entry point.

    A raised :class:`crm.errors.CrmError` is rendered as its own status code and user-safe message on
    either channel.

    Args:
        event: The raw Lambda event, from API Gateway's proxy envelope or a direct invoke.
        context: The Lambda context, unused.
        store: Injectable persistence layer; constructed from the ambient configuration when absent.
        sfn: Injectable Step Functions client; constructed when absent.
        s3: Injectable S3 client for ``artefact_url`` presigning; constructed on use when absent.
    """
    del context
    proxy = is_proxy_event(event)
    try:
        identity, arguments = decode(event)
        resolved_store = store if store is not None else default_store()
        resolved_sfn = sfn if sfn is not None else _LazyStepFunctions()
        result = _eval_trigger(resolved_store, resolved_sfn, identity, arguments, s3)
    except CrmError as exc:
        return error_response(exc, proxy=proxy)
    return ok(result, proxy=proxy)


__all__ = [
    "ACTIONS",
    "ACTION_ARTEFACT_URL",
    "ACTION_CANCEL",
    "ACTION_ESTIMATE",
    "ACTION_LIST",
    "ACTION_STATUS",
    "ACTION_SUBMIT",
    "ACTION_UNLOCK",
    "handler",
]

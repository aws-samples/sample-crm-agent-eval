# SPDX-License-Identifier: MIT
"""Layer 4 smoke for the Runtime role's conversation-history grant on `reps`.

Simulates the deployed `crm-agent-eval-runtime-role` (`agentcore_runtime_role_arn` output) and reads
the `reps` table. Targets come from the `agentcore_runtime_role_arn` and `dynamodb_table_names`
outputs; table and key ARNs come from `describe-table`. Rows, all read/simulate-only:

* ALLOW: `GetItem` and `PutItem` on `reps` with `dynamodb:LeadingKeys` = a run-scoped
  `history#demo#<run>` key; `kms:Decrypt` and `kms:GenerateDataKey` on the table's CMK through
  DynamoDB for `reps`.
* EXPLICIT DENY: `GetItem`/`PutItem` on `usersettings#`, `conversation#`, a bare rep id and
  `history#canary#` keys; every other DynamoDB action on `reps` and its indexes; `GetItem` on every
  other table.
* DENY: `GetItem` with no `dynamodb:LeadingKeys` context; `PutItem` writing a `tenant` attribute;
  `kms:Decrypt` for another table or without `kms:ViaService`.
* TTL: `describe-time-to-live` on `reps` is ENABLED on `expires_at`.

Each Get/Put/KMS deny row shares its action with an ALLOW row in the same run, because
`SimulatePrincipalPolicy` returns `implicitDeny` for a misspelled action too. The other-action rows
require `explicitDeny`, which only a matching Deny statement produces.

Usage:
    python3 -m scripts.smoke.runtime_history_iam [--profile P] [--region R] [--outputs PATH] [--json]
"""

from __future__ import annotations

import sys
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from scripts.smoke.harness import (
    CheckResult,
    ExpectedCheckCount,
    SmokeContext,
    exception_detail,
    run_main,
)
from scripts.smoke.iam_roles import _is_throttle  # one spelling of the throttle predicate

from agents.conversation_history import HISTORY_ITEM_ATTRIBUTES as _WRITTEN_ATTRIBUTES
from crm.store.keys import conversation_key, history_key, user_settings_key

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "runtime_history_iam"

HISTORY_TABLE: Final[str] = "reps"
HISTORY_KEY_PREFIX: Final[str] = "history#"
TTL_ATTRIBUTE: Final[str] = "expires_at"

GET_ITEM: Final[str] = "dynamodb:GetItem"
PUT_ITEM: Final[str] = "dynamodb:PutItem"
GRANTED_ACTIONS: Final[tuple[str, ...]] = (GET_ITEM, PUT_ITEM)

#: DynamoDB actions on `reps` the role must be explicitly denied.
DENIED_TABLE_ACTIONS: Final[tuple[str, ...]] = (
    "dynamodb:DeleteItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:BatchWriteItem",
    "dynamodb:UpdateItem",
    "dynamodb:BatchGetItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:PartiQLSelect",
)

#: Attributes a history item carries; a write naming any other attribute must be denied.
HISTORY_ITEM_ATTRIBUTES: Final[tuple[str, ...]] = tuple(sorted(_WRITTEN_ATTRIBUTES))

KMS_ACTIONS: Final[tuple[str, ...]] = ("kms:Decrypt", "kms:GenerateDataKey")

_LEADING_KEYS: Final[str] = "dynamodb:LeadingKeys"
_ATTRIBUTES: Final[str] = "dynamodb:Attributes"
_VIA_SERVICE: Final[str] = "kms:ViaService"
_TABLE_NAME_CONTEXT: Final[str] = "kms:EncryptionContext:aws:dynamodb:tableName"

_ALLOWED: Final[frozenset[str]] = frozenset({"allowed"})
_EXPLICIT_DENY: Final[frozenset[str]] = frozenset({"explicitDeny"})
_DENIED: Final[frozenset[str]] = frozenset({"explicitDeny", "implicitDeny"})
_NO_RESULT: Final[str] = "no_result"
_DEFER_NO_EVALUATION: Final[str] = "simulate_returned_no_evaluation_result"
_DEFER_TTL_ENABLING: Final[str] = "ttl_enabling"
_SIMULATE_MAX_ATTEMPTS: Final[int] = 4
_SIMULATE_BACKOFF_SECONDS: Final[float] = 1.0

__all__ = [
    "DENIED_TABLE_ACTIONS",
    "GRANTED_ACTIONS",
    "HISTORY_ITEM_ATTRIBUTES",
    "HISTORY_KEY_PREFIX",
    "KMS_ACTIONS",
    "LAYER",
    "RESOURCE_CLASS",
    "checks",
    "expected_check_count",
    "main",
]


@dataclass(frozen=True)
class _Targets:
    role_arn: str
    table_names: Mapping[str, str]
    table_arns: Mapping[str, str]
    kms_key_arn: str


def _context(name: str, values: Sequence[str], kind: str) -> dict[str, Any]:
    return {"ContextKeyName": name, "ContextKeyValues": list(values), "ContextKeyType": kind}


def _leading_keys(key: str) -> list[dict[str, Any]]:
    return [_context(_LEADING_KEYS, [key], "stringList")]


def _simulate(
    ctx: SmokeContext,
    role_arn: str,
    action: str,
    resource_arn: str,
    context_entries: list[dict[str, Any]] | None = None,
) -> str:
    """Return the simulate decision for one action/resource, retrying throttles only."""
    iam = ctx.client("iam")
    kwargs: dict[str, Any] = {
        "PolicySourceArn": role_arn,
        "ActionNames": [action],
        "ResourceArns": [resource_arn],
    }
    if context_entries:
        kwargs["ContextEntries"] = context_entries
    for attempt in range(1, _SIMULATE_MAX_ATTEMPTS + 1):
        try:
            response = iam.simulate_principal_policy(**kwargs)
            break
        except Exception as exc:  # noqa: BLE001 - re-raised unless a retryable throttle
            if attempt == _SIMULATE_MAX_ATTEMPTS or not _is_throttle(exc):
                raise
            time.sleep(_SIMULATE_BACKOFF_SECONDS * attempt)
    results = response.get("EvaluationResults", [])
    if not results:
        return _NO_RESULT
    return str(results[0]["EvalDecision"])


def _row(
    resource: str, check: str, decision: str, expected: frozenset[str], why: str
) -> CheckResult:
    if decision == _NO_RESULT:
        return CheckResult(
            resource=resource,
            check=check,
            status="defer",
            detail=f"{check}: {resource} returned no evaluation result, so this row asserts nothing",
            defer_reason=_DEFER_NO_EVALUATION,
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="pass" if decision in expected else "fail",
        detail=f"evaluated {decision}; expected one of {sorted(expected)} -- {why}",
    )


def _resolve_targets(ctx: SmokeContext) -> _Targets | CheckResult:
    try:
        role_arn = ctx.outputs.require_str("agentcore_runtime_role_arn")
        table_names = dict(ctx.outputs.require_mapping("dynamodb_table_names"))
    except Exception as exc:  # noqa: BLE001 - reported as a failing check, not a crash
        return CheckResult(
            resource="terraform_outputs",
            check="terraform_output_present",
            status="fail",
            detail=(
                "required outputs 'agentcore_runtime_role_arn' and 'dynamodb_table_names' are this "
                f"module's subject ({exception_detail(exc)})"
            ),
        )
    if HISTORY_TABLE not in table_names or len(table_names) < 2:
        return CheckResult(
            resource="dynamodb_table_names",
            check="history_table_present",
            status="fail",
            detail=f"dynamodb_table_names lacks '{HISTORY_TABLE}' or any other table",
        )
    dynamodb = ctx.client("dynamodb")
    table_arns: dict[str, str] = {}
    kms_key_arn = ""
    for logical, physical in sorted(table_names.items()):
        try:
            table = dynamodb.describe_table(TableName=str(physical))["Table"]
        except Exception as exc:  # noqa: BLE001 - reported as a failing check
            return CheckResult(
                resource=str(physical),
                check="describe_table",
                status="fail",
                detail=exception_detail(exc, "DescribeTable"),
            )
        table_arns[logical] = str(table["TableArn"])
        if logical == HISTORY_TABLE:
            kms_key_arn = str((table.get("SSEDescription") or {}).get("KMSMasterKeyArn") or "")
    if not kms_key_arn:
        return CheckResult(
            resource=table_arns[HISTORY_TABLE],
            check="history_table_cmk",
            status="fail",
            detail="reps reports no SSEDescription.KMSMasterKeyArn, so the KMS rows have no subject",
        )
    return _Targets(role_arn, table_names, table_arns, kms_key_arn)


def _ttl_row(ctx: SmokeContext, physical: str, table_arn: str) -> CheckResult:
    check = f"ttl_enabled:{TTL_ATTRIBUTE}"
    try:
        description = ctx.client("dynamodb").describe_time_to_live(TableName=physical)
    except Exception as exc:  # noqa: BLE001 - reported as a failing check
        return CheckResult(table_arn, check, "fail", exception_detail(exc, "DescribeTimeToLive"))
    ttl = description.get("TimeToLiveDescription") or {}
    status, attribute = ttl.get("TimeToLiveStatus"), ttl.get("AttributeName")
    if attribute == TTL_ATTRIBUTE and status == "ENABLED":
        return CheckResult(table_arn, check, "pass", f"TTL ENABLED on {attribute}")
    if attribute == TTL_ATTRIBUTE and status == "ENABLING":
        return CheckResult(
            table_arn,
            check,
            "defer",
            "TTL is ENABLING on expires_at; re-run once DynamoDB reports ENABLED",
            defer_reason=_DEFER_TTL_ENABLING,
        )
    return CheckResult(
        table_arn,
        check,
        "fail",
        f"TTL status={status!r} attribute={attribute!r}; expected ENABLED on {TTL_ATTRIBUTE!r}",
    )


def checks(ctx: SmokeContext) -> Iterator[CheckResult]:
    """Yield the allow, deny, KMS and TTL rows for the Runtime role's history grant."""
    targets = _resolve_targets(ctx)
    if isinstance(targets, CheckResult):
        yield targets
        return
    run = uuid.uuid4().hex[:12]
    role = targets.role_arn
    reps_arn = targets.table_arns[HISTORY_TABLE]
    own_key = history_key("demo", f"smoke-{run}")

    def simulate(action: str, resource: str, entries: list[dict[str, Any]] | None = None) -> str:
        return _simulate(ctx, role, action, resource, entries)

    for action in GRANTED_ACTIONS:
        entries = _leading_keys(own_key)
        if action == PUT_ITEM:
            entries.append(_context(_ATTRIBUTES, HISTORY_ITEM_ATTRIBUTES, "stringList"))
        yield _row(
            reps_arn,
            f"allow:{action}@history#",
            simulate(action, reps_arn, entries),
            _ALLOWED,
            "the Runtime reads and writes its conversation-history items",
        )

    foreign_keys = {
        "usersettings#": user_settings_key("demo", f"smoke-{run}"),
        "conversation#": conversation_key(f"smoke-{run}"),
        "rep_id": f"rep-smoke-{run}",
        "history#canary#": history_key("canary", f"smoke-{run}"),
    }
    for label, key in foreign_keys.items():
        for action in GRANTED_ACTIONS:
            yield _row(
                reps_arn,
                f"explicit_deny:{action}@{label}",
                simulate(action, reps_arn, _leading_keys(key)),
                _EXPLICIT_DENY,
                "a key outside the history prefix, or the canary tenant's history, is denied by "
                "a Deny statement, not only by the Allow's absence",
            )

    yield _row(
        reps_arn,
        f"deny_without_leading_keys:{GET_ITEM}",
        simulate(GET_ITEM, reps_arn),
        _DENIED,
        "with no dynamodb:LeadingKeys context the Allow's Null condition must not match",
    )
    yield _row(
        reps_arn,
        f"deny_tenant_attribute:{PUT_ITEM}",
        simulate(
            PUT_ITEM,
            reps_arn,
            [
                *_leading_keys(own_key),
                _context(_ATTRIBUTES, [*HISTORY_ITEM_ATTRIBUTES, "tenant"], "stringList"),
            ],
        ),
        _DENIED,
        "a history write carrying `tenant` would surface in gsi_tenant reads",
    )

    for action in DENIED_TABLE_ACTIONS:
        yield _row(
            reps_arn,
            f"explicit_deny:{action}@reps",
            simulate(action, reps_arn, _leading_keys(own_key)),
            _EXPLICIT_DENY,
            "only GetItem and PutItem are granted on reps; every other action is denied",
        )
    index_arn = f"{reps_arn}/index/gsi_tenant"
    yield _row(
        index_arn,
        "explicit_deny:dynamodb:Query@reps/index",
        simulate("dynamodb:Query", index_arn, _leading_keys(own_key)),
        _EXPLICIT_DENY,
        "the reps indexes are denied",
    )

    for logical, table_arn in sorted(targets.table_arns.items()):
        if logical == HISTORY_TABLE:
            continue
        yield _row(
            table_arn,
            f"explicit_deny:{GET_ITEM}@{logical}",
            simulate(GET_ITEM, table_arn, _leading_keys(own_key)),
            _EXPLICIT_DENY,
            "the Runtime role is on every table's data-plane allowlist, so its identity policy "
            "must deny every table but reps",
        )

    via_dynamodb = _context(_VIA_SERVICE, [f"dynamodb.{ctx.region}.amazonaws.com"], "string")
    reps_name = str(targets.table_names[HISTORY_TABLE])
    other_name = str(next(v for k, v in sorted(targets.table_names.items()) if k != HISTORY_TABLE))
    for action in KMS_ACTIONS:
        yield _row(
            targets.kms_key_arn,
            f"allow:{action}@reps",
            simulate(
                action,
                targets.kms_key_arn,
                [via_dynamodb, _context(_TABLE_NAME_CONTEXT, [reps_name], "string")],
            ),
            _ALLOWED,
            "DynamoDB uses the CMK on the Runtime's behalf for reps",
        )
    yield _row(
        targets.kms_key_arn,
        "deny:kms:Decrypt@other_table",
        simulate(
            "kms:Decrypt",
            targets.kms_key_arn,
            [via_dynamodb, _context(_TABLE_NAME_CONTEXT, [other_name], "string")],
        ),
        _DENIED,
        "the CMK is shared by every table; the Runtime may use it for reps only",
    )
    yield _row(
        targets.kms_key_arn,
        "deny:kms:Decrypt@without_via_service",
        simulate(
            "kms:Decrypt",
            targets.kms_key_arn,
            [_context(_TABLE_NAME_CONTEXT, [reps_name], "string")],
        ),
        _DENIED,
        "the CMK is usable only through DynamoDB",
    )

    yield _ttl_row(ctx, reps_name, reps_arn)


def expected_check_count(ctx: SmokeContext) -> ExpectedCheckCount:
    """Derived from the action tuples and the `dynamodb_table_names` output."""
    try:
        other_tables = len(ctx.outputs.require_mapping("dynamodb_table_names")) - 1
    except Exception:  # noqa: BLE001 - an absent output yields the single failing row
        other_tables = 0
    granted = len(GRANTED_ACTIONS)
    minimum = (
        granted  # allow rows
        + 4 * granted  # four foreign key prefixes
        + 2  # no-context and tenant-attribute denies
        + len(DENIED_TABLE_ACTIONS)
        + 1  # index Query
        + max(other_tables, 0)
        + len(KMS_ACTIONS)
        + 2  # KMS denies
        + 1  # TTL
    )
    return ExpectedCheckCount(
        minimum=minimum,
        basis=(
            f"{granted} allow + {4 * granted} foreign-key denies + 2 condition denies + "
            f"{len(DENIED_TABLE_ACTIONS)} other-action denies + 1 index deny + {other_tables} "
            f"other-table denies + {len(KMS_ACTIONS)} KMS allow + 2 KMS denies + 1 TTL row"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: emit the Runtime history grant inventory, non-zero on any fail."""
    return run_main(
        argv,
        description=__doc__ or "",
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    sys.exit(main())

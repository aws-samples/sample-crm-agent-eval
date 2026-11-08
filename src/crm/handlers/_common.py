# SPDX-License-Identifier: MIT
"""Shared plumbing every action Lambda handler in this package builds on.

Each handler decodes the caller identity with :func:`crm.envelope.decode_identity`, extracts and
validates the tool arguments, calls into the domain, store, proposal, audit, or search modules, and
renders a success payload or a :class:`crm.errors.CrmError` as its response.

* **Arguments.** On the flat Gateway/agent path the arguments are the event with every
  :data:`crm.envelope.IDENTITY_CHANNEL_KEYS` member removed. On the API Gateway proxy path (payload
  format 1.0 or 2.0, see :func:`is_proxy_event`) they are the JSON ``body``, with the same keys
  removed.
* **Validation.** :func:`build_handler` resolves the action's
  :class:`~crm.toolspec.definitions.ToolDefinition` at import time, and :func:`run_handler`
  validates the arguments against its ``input_schema`` before the action body runs.
* **Responses.** :func:`ok` and :func:`error_response` render the flat shape the Gateway and
  direct-invoke planes consume, or on the proxy path a structured proxy response with a string
  ``body``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Final

from crm.audit import build_audit_record
from crm.authz import Identity
from crm.envelope import REQUEST_CONTEXT_KEY, decode_identity, strip_identity
from crm.errors import Conflict, CrmError, ValidationFailed
from crm.handlers.wire import put_operation
from crm.store.keys import derive
from crm.toolspec.definitions import ALL_TOOLS, ToolDefinition
from crm.toolspec.validation import validate_arguments

logger = logging.getLogger(__name__)

#: Upper bound on the characters of an error's ``detail`` carried into its log line.
ERROR_LOG_DETAIL_MAX_CHARS: Final[int] = 300

#: Value of the format-2.0 proxy envelope's ``version`` field.
PROXY_PAYLOAD_VERSION_V2: Final[str] = "2.0"

#: Keys a format-1.0 REST proxy event always carries as strings and no tool argument uses;
#: :func:`is_proxy_event` recognises format 1.0 by them.
PROXY_V1_ENVELOPE_KEYS: Final[tuple[str, ...]] = ("resource", "path", "httpMethod")

#: Response headers on the proxy path.
PROXY_RESPONSE_HEADERS: Final[dict[str, str]] = {"content-type": "application/json"}


def _is_v2_proxy_event(event: dict[str, Any]) -> bool:
    """Return whether ``event`` is an API Gateway HTTP API format-2.0 proxy envelope."""
    return (
        event.get("version") == PROXY_PAYLOAD_VERSION_V2
        and "routeKey" in event
        and REQUEST_CONTEXT_KEY in event
    )


def _is_v1_proxy_event(event: dict[str, Any]) -> bool:
    """Return whether ``event`` is an API Gateway format-1.0 (REST ``AWS_PROXY``) envelope."""
    return REQUEST_CONTEXT_KEY in event and all(
        isinstance(event.get(key), str) for key in PROXY_V1_ENVELOPE_KEYS
    )


def is_proxy_event(event: Any) -> bool:
    """Return whether ``event`` is an API Gateway proxy envelope, in either payload format.

    Format 2.0 is recognised by ``version == "2.0"`` plus ``routeKey``, and format 1.0 by every key in
    :data:`PROXY_V1_ENVELOPE_KEYS` holding a string; both also require ``requestContext``.
    ``requestContext`` alone is not sufficient, since a flat event may carry it and ``body`` is a real
    argument of some tools.
    """
    if not isinstance(event, dict):
        return False
    return _is_v1_proxy_event(event) or _is_v2_proxy_event(event)


def proxy_arguments(event: dict[str, Any]) -> dict[str, Any]:
    """Return the tool arguments carried in a proxy event's ``body``.

    Args:
        event: An event :func:`is_proxy_event` accepts.

    Returns:
        The decoded JSON object with every :data:`crm.envelope.IDENTITY_CHANNEL_KEYS` member
        removed. An absent or empty body yields an empty mapping, so a tool taking no arguments
        works whether or not the caller sent a body at all.

    Raises:
        ValidationFailed: The body is not base64-decodable when flagged as such, is not valid JSON,
            or is valid JSON that is not an object.
    """
    raw: Any = event.get("body")
    if raw is None or raw == "":
        return {}
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise ValidationFailed(
                "The request body was not valid.",
                source="body",
                detail=f"isBase64Encoded body did not decode: {type(exc).__name__}: {exc}",
            ) from exc
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(
            "The request body was not valid JSON.",
            source="body",
            detail=f"json.loads rejected the request body: {type(exc).__name__}: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise ValidationFailed(
            "The request body must be a JSON object.",
            source="body",
            detail=f"request body decoded to {type(parsed).__name__}, expected object",
        )
    return strip_identity(parsed)


def decode(event: dict[str, Any]) -> tuple[Identity, dict[str, Any]]:
    """Decode the caller identity and the tool arguments from a raw Lambda event.

    Args:
        event: The raw event a Gateway Lambda target, API Gateway, or an in-app agent delivers.

    Returns:
        A tuple of ``(identity, arguments)``. On the flat Gateway/agent path ``arguments`` is
        ``event`` with every identity channel key removed; on the API Gateway proxy path it is the
        JSON ``body`` with the same keys removed (:func:`proxy_arguments`).

    Raises:
        AuthenticationFailed: Propagated unchanged from :func:`crm.envelope.decode_identity` when
            no recognised identity channel is present or its payload is malformed.
        ValidationFailed: Propagated from :func:`proxy_arguments` for an undecodable body.
    """
    identity = decode_identity(event)
    if is_proxy_event(event):
        return identity, proxy_arguments(event)
    return identity, strip_identity(event)


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    """Return a fresh ``<prefix>-<uuid4>`` identifier for a newly created record."""
    return f"{prefix}-{uuid.uuid4()}"


def idempotent_id(prefix: str, transaction_key: str) -> str:
    """Return a stable ``<prefix>-<uuid5>`` identifier derived from ``transaction_key``.

    A retry with the same ``transaction_key`` resolves to the same primary key, which is what
    :meth:`crm.store.store.Store.put_idempotent` compares against.
    """
    return f"{prefix}-{uuid.uuid5(uuid.NAMESPACE_OID, f'{prefix}:{transaction_key}')}"


#: Attributes excluded from the payload comparison :func:`create_idempotent` performs: the
#: per-call timestamps and the store's own idempotency bookkeeping.
_VOLATILE_ITEM_ATTRS: Final[frozenset[str]] = frozenset(
    {"created_at", "updated_at", "_idempotency_key", "_idempotency_fingerprint"}
)

#: The attribute a created item carries naming the ``transaction_key`` that wrote it.
_IDEMPOTENCY_KEY_ATTR: Final[str] = "_idempotency_key"


def _stable_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Return ``item`` with :data:`_VOLATILE_ITEM_ATTRS` removed, for a genuine payload comparison."""
    return {key: value for key, value in item.items() if key not in _VOLATILE_ITEM_ATTRS}


def create_idempotent(
    store: Any,
    table: str,
    key_attr: str,
    item: dict[str, Any],
    *,
    transaction_key: str,
    identity: Identity,
    audit_account_id: str | None = None,
) -> dict[str, Any]:
    """Create ``item`` and its ``create`` audit record in one transaction, idempotent under retry.

    A retry whose item already exists under the same ``transaction_key`` with a matching payload,
    excluding :data:`_VOLATILE_ITEM_ATTRS`, returns the existing item and writes nothing; the first
    write's audit record stands. Otherwise ``item`` is written with a ``Put`` conditioned on its key
    being absent, together with the :func:`crm.audit.build_audit_record` ``Put``, so either both
    persist or neither does. The condition and the replay check carry the idempotency, so no
    ``ClientRequestToken`` is sent; a write cancelled by a concurrent identical create returns that
    create's item.

    Args:
        store: The persistence layer.
        table: One of :data:`crm.store.schema.TABLE_NAMES`.
        key_attr: The table's primary partition-key attribute name.
        item: The full item to write, including its primary key attribute.
        transaction_key: Caller-supplied idempotency token.
        identity: The caller; becomes the audit record's actor and tenant.
        audit_account_id: The account the audit record is scoped by, for an item that carries no
            ``account_id`` of its own; ``None`` derives it from the item.

    Returns:
        ``item`` itself on a first write, or the pre-existing item (with its original timestamps) on a
        retry with a matching payload.

    Raises:
        ValidationFailed: ``transaction_key`` is empty.
        Conflict: ``transaction_key`` was already used for a payload whose stable content (per
            :func:`_stable_payload`) differs from the caller's, or an item already exists at the key.
    """
    if not transaction_key:
        raise ValidationFailed(
            "A transaction key is required for an idempotent write.", source="transaction_key"
        )
    key = {key_attr: str(item[key_attr])}
    replay = _replayed_item(store.get_item(table, key), item, transaction_key)
    if replay is not None:
        return replay

    persisted = derive(table, item)
    audit = build_audit_record(
        identity=identity,
        target_table=table,
        target_id=key[key_attr],
        before_snapshot={},
        after_snapshot=persisted,
        action="create",
        account_id=audit_account_id,
    )
    insert = put_operation(table, {**persisted, _IDEMPOTENCY_KEY_ATTR: transaction_key})
    insert["Put"]["ConditionExpression"] = f"attribute_not_exists({key_attr})"
    try:
        store.transact_write([insert, put_operation("audit_records", audit.to_item())])
    except Conflict:
        replay = _replayed_item(store.get_item(table, key, consistent=True), item, transaction_key)
        if replay is not None:
            return replay
        raise
    return persisted


def _replayed_item(
    existing: object, item: dict[str, Any], transaction_key: str
) -> dict[str, Any] | None:
    """Return ``existing`` when it is ``item`` written earlier under ``transaction_key``.

    Raises:
        Conflict: ``existing`` is some other item at the same key.
    """
    if existing is None:
        return None
    if (
        isinstance(existing, dict)
        and existing.get(_IDEMPOTENCY_KEY_ATTR) == transaction_key
        and _stable_payload(existing) == _stable_payload(item)
    ):
        return existing
    raise Conflict(
        "This transaction key was already used for a different payload.",
        detail=f"transaction_key={transaction_key}",
    )


def _proxy_response(status_code: int, payload: Any) -> dict[str, Any]:
    """Render ``payload`` as an API Gateway structured proxy response, valid in both payload formats.

    ``body`` is a JSON string; API Gateway rejects a proxy response whose ``body`` is an object.
    """
    return {
        "statusCode": status_code,
        "headers": dict(PROXY_RESPONSE_HEADERS),
        "body": json.dumps(payload, default=str),
        "isBase64Encoded": False,
    }


def ok(body: Any, *, status_code: int = 200, proxy: bool = False) -> dict[str, Any]:
    """Render a successful handler response.

    Args:
        body: The success payload.
        status_code: HTTP status to report.
        proxy: Render the API Gateway structured proxy shape instead of the flat
            ``{"statusCode", "body"}`` shape the Gateway and direct-invoke planes consume.
    """
    if proxy:
        return _proxy_response(status_code, body)
    return {"statusCode": status_code, "body": body}


def error_response(exc: CrmError, *, proxy: bool = False) -> dict[str, Any]:
    """Render a :class:`crm.errors.CrmError` as the handler's JSON response.

    The payload is :meth:`~crm.errors.CrmError.to_api_response`, which excludes ``detail``.

    Args:
        exc: The raised taxonomy member.
        proxy: Render the API Gateway structured proxy shape, carrying the error's ``http_status`` and
            the same payload as a JSON string body, instead of the flat shape.
    """
    payload = exc.to_api_response()
    if proxy:
        return _proxy_response(exc.http_status, payload)
    return payload


def run_handler(event: dict[str, Any], action: Any, *, tool: ToolDefinition) -> dict[str, Any]:
    """Decode identity and arguments, validate the arguments, invoke ``action``, translate errors.

    Validation runs after identity keys are stripped and before ``action`` is called. ``tool`` is
    keyword-required, so no call shape reaches ``action`` with unvalidated arguments.

    Args:
        event: The raw Lambda event.
        action: A callable taking ``(identity, arguments)`` and returning the success body.
        tool: The invoked action's definition, supplying the ``input_schema`` to validate against.

    Returns:
        ``ok(result)`` if ``action`` returns normally, or the error's own API response if validation
        or ``action`` raises a :class:`CrmError`, each in the shape the event's channel requires. A
        :class:`CrmError` is first logged by :func:`log_crm_error`.
    """
    proxy = is_proxy_event(event)
    identity: Identity | None = None
    try:
        identity, arguments = decode(event)
        validate_arguments(tool, arguments)
        result = action(identity, arguments)
    except CrmError as exc:
        log_crm_error(exc, identity)
        return error_response(exc, proxy=proxy)
    return ok(result, proxy=proxy)


def log_crm_error(exc: CrmError, identity: Identity | None) -> None:
    """Emit one ``crm_error`` WARNING line: the error code and its bounded ``detail``.

    ``rep_id`` is included when the caller's identity was decoded. Nothing from the event, its
    headers or its arguments is logged.
    """
    reason = f"{exc.code}: {(exc.detail or '')[:ERROR_LOG_DETAIL_MAX_CHARS]}"
    if identity is None:
        logger.warning(json.dumps({"event": "crm_error", "reason": reason}))
    else:
        logger.warning(
            json.dumps({"event": "crm_error", "reason": reason, "rep_id": identity.rep_id})
        )


def default_store() -> Any:
    """Construct a :class:`crm.store.store.Store` from the ambient AWS configuration.

    Called only when no store was injected. The table prefix is read from ``CRM_TABLE_PREFIX`` and
    defaults to none.
    """
    import os

    from crm.store.store import Store

    return Store(table_prefix=os.environ.get("CRM_TABLE_PREFIX", ""))


#: Every action's definition keyed by the dotted handler path it declares.
_TOOLS_BY_HANDLER: Final[dict[str, ToolDefinition]] = {tool.handler: tool for tool in ALL_TOOLS}


def tool_for_action(action: Any) -> ToolDefinition:
    """Return the :class:`ToolDefinition` whose declared ``handler`` path is ``action``'s module.

    Args:
        action: The module-level action function a handler module passes to :func:`build_handler`.

    Returns:
        The matching definition from :data:`crm.toolspec.definitions.ALL_TOOLS`.

    Raises:
        RuntimeError: No definition declares ``<action module>.handler``. Raised at import time, when
            :func:`build_handler` runs.
    """
    handler_path = f"{action.__module__}.handler"
    tool = _TOOLS_BY_HANDLER.get(handler_path)
    if tool is None:
        raise RuntimeError(
            f"{handler_path} is not declared by any crm.toolspec.definitions.ToolDefinition, so no "
            "input schema exists to validate its arguments against"
        )
    return tool


def build_handler(action: Any) -> Any:
    """Return a Lambda entry point ``handler(event, context=None, *, store=None)`` for ``action``.

    ``action`` takes ``(store, identity, arguments)`` and returns the success body or raises a
    :class:`CrmError`. Every handler module in this package is one call to this function.

    Args:
        action: A callable taking ``(store, identity, arguments)``.

    Returns:
        A callable matching the AWS Lambda handler signature, with an injectable ``store`` keyword.

    Raises:
        RuntimeError: Propagated from :func:`tool_for_action` when ``action``'s module is not the
            declared handler of any tool.
    """
    tool = tool_for_action(action)

    def handler(
        event: dict[str, Any], context: object = None, *, store: Any | None = None
    ) -> dict[str, Any]:
        del context
        resolved_store = store if store is not None else default_store()
        return run_handler(
            event,
            lambda identity, arguments: _scoped_action(
                action, resolved_store, identity, arguments, tool
            ),
            tool=tool,
        )

    return handler


def _scoped_action(
    action: Any, store: Any, identity: Identity, arguments: dict[str, Any], tool: ToolDefinition
) -> Any:
    """Run ``action`` with transaction tokens scoped to ``(tenant, rep_id, tool)``."""
    from crm.store.store import token_scope

    with token_scope(identity.tenant, identity.rep_id, tool.name):
        return action(store, identity, arguments)

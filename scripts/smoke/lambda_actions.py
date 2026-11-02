# SPDX-License-Identifier: MIT
"""Layer 4 per-Lambda direct-invoke smoke checks for every action Lambda.

Enumerates action Lambdas from ``crm.toolspec.definitions.ALL_TOOLS`` (never a hand-maintained
list), invokes each physical function (``crm-agent-eval-<tool_name>``, matching
``infra/main.tf``'s ``module.lambda_action`` naming) via ``lambda:Invoke`` with a synthetic
in-scope identity envelope and asserts a successful result, then invokes it again with an
out-of-scope target and asserts the typed ``scope_denied`` error rather than a 500 or a
``not_found``. Also asserts no response leaks an internal ``detail`` key. Needs a live deployment.

``create_opportunity`` additionally gets two checks for its optional ``primary_contact_id``
argument, which makes the handler read ``contacts``: supplied validly, the call must succeed and
persist the contact id; supplied with a contact under another account, it must be refused with
``validation_failed``, never the ``401 authentication_failed`` a missing ``dynamodb:GetItem`` grant
produces.

The listing tools in ``_EMPTY_BOOK_DENIAL_TOOLS`` are denied ``scope_denied`` / ``account:*`` for an
empty book, and ``search_opportunities`` with an explicit out-of-book ``account_id`` is denied
naming that account. ``update_contact`` on PD-01's departed contact is refused
``contact_departed`` with the phone unchanged on re-read, a ``fields.status = "active"`` update
succeeds, and the contact is then set back to ``departed``. Every check reads the payload's error
code, never the invoke status.

Operates under its own smoke tenant (`scripts/smoke/tenant.py`), never ``demo``. With no
``--tenant`` it allocates and seeds that tenant and tears it down when ``checks()`` returns; with
``--tenant`` the tenant is used as-is and the orchestrator owns teardown.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final, Literal

from scripts.smoke.harness import CheckResult, SmokeContext, run_main
from scripts.smoke.tenant import (
    SmokeTenant,
    TenantTeardownError,
    allocate_smoke_tenant,
    seed_smoke_tenant,
    teardown_smoke_tenant,
)

from crm.store.store import Store
from crm.toolspec.definitions import ALL_TOOLS, ToolDefinition

LAYER = 4
RESOURCE_CLASS = "lambda_actions"

#: Physical Lambda function name prefix `infra/main.tf`'s `module.lambda_action` applies to every
#: tool name (`function_name = "crm-agent-eval-${each.key}"`).
FUNCTION_NAME_PREFIX = "crm-agent-eval-"

#: Index of the first seeded account under any tenant (`fixtures.generate._tenant_id(tenant, "acct",
#: 0)`), owned by `REPS[0]` (`rep-ashford`). The in-scope target.
_SEEDED_ACCOUNT_INDEX = 0
_SEEDED_ACCOUNT_OWNER_REP_ID = "rep-ashford"

#: Index of a second seeded account under the same tenant, never in the out-of-scope identity's
#: book. A real record, so the handler reaches its scope check rather than returning `NotFound`.
_OUT_OF_SCOPE_ACCOUNT_INDEX = 1

#: `fixtures.generate._generate_contacts` numbers contacts with one counter across accounts in
#: seeded order, 2-4 per account. So contact index 0 is always under the first seeded account, and
#: index 4 is always a seeded contact under another account.
_IN_ACCOUNT_CONTACT_INDEX = 0
_OTHER_ACCOUNT_CONTACT_INDEX = 4

#: Maps a tool's required identifying property name to the `fixtures.generate` `kind` prefix whose
#: index-0 record is seeded under every tenant.
_SEEDED_ENTITY_ID_KIND_BY_PROPERTY: Final[dict[str, str]] = {
    "opportunity_id": "opp",
    "contact_id": "cont",
    "activity_id": "act",
}

#: Tools acting on a proposal or audit record `fixtures.generate` never seeds, which
#: `scripts/smoke/integration_proposal_chain.py` exercises at Layer 5. Reported `skip`.
_LAYER5_CHAIN_TOOLS: Final[frozenset[str]] = frozenset(
    {"submit_proposal", "approve_proposal", "reject_proposal", "revert_audit_record"}
)

#: Tools acting on an unseeded proposal or audit record that no smoke module exercises yet.
#: Reported `skip` with :data:`T3_PENDING_DETAIL`.
_T3_PENDING_TOOLS: Final[frozenset[str]] = frozenset({"get_audit_record", "rebase_proposal"})

#: The skip reason for a :data:`_T3_PENDING_TOOLS` member.
T3_PENDING_DETAIL: Final[str] = "T3 pending: no smoke module exercises {tool} yet"

#: Every tool whose required identifying property names a record `fixtures.generate` never seeds.
_NO_STATIC_SEED_TOOLS: Final[frozenset[str]] = _LAYER5_CHAIN_TOOLS | _T3_PENDING_TOOLS

#: Tools the out-of-scope `scope_denied` assertion does not apply to, reported `skip`:
#: - `create_account` has `scope_arg=None` and calls no `require_scope`;
#: - `list_proposals`/`list_audit_records` filter their result set with
#:   `crm.authz.filter_to_scope` rather than denying the call.
_NO_OUT_OF_SCOPE_DENIAL_TOOLS: Final[frozenset[str]] = frozenset(
    {"create_account", "list_proposals", "list_audit_records"}
)

#: Listing tools that deny an empty-book caller via `crm.authz.require_listing_scope`. Their
#: out-of-scope check sends an empty book and no explicit filter, and expects
#: :data:`EMPTY_BOOK_MISSING_SCOPE`.
_EMPTY_BOOK_DENIAL_TOOLS: Final[frozenset[str]] = frozenset(
    {"search_opportunities", "search_accounts", "pipeline_aggregate"}
)
EMPTY_BOOK_MISSING_SCOPE: Final[str] = "account:*"

#: The explicit-filter denial: `search_opportunities(account_id=<out-of-book id>)` from a caller
#: whose book is non-empty.
_EXPLICIT_FILTER_TOOL: Final[str] = "search_opportunities"
EXPLICIT_FILTER_CHECK: Final[str] = "explicit_out_of_book_filter_denied"

#: The departed-contact refusal and its reactivation path, run on the PD-01 departed contact.
#: `restored` sets the contact back to `departed` so a shared tenant is left as seeded.
_DEPARTED_CONTACT_TOOL: Final[str] = "update_contact"
_CONTACT_READ_TOOL: Final[str] = "get_contact"
DEPARTED_REFUSED_CHECK: Final[str] = "departed_contact_update_refused"
DEPARTED_REACTIVATED_CHECK: Final[str] = "departed_contact_reactivation_allowed"
DEPARTED_RESTORED_CHECK: Final[str] = "departed_contact_restored"
DEPARTED_CONTACT_CHECKS: Final[tuple[str, str, str]] = (
    DEPARTED_REFUSED_CHECK,
    DEPARTED_REACTIVATED_CHECK,
    DEPARTED_RESTORED_CHECK,
)
CONTACT_DEPARTED_MISSING_SCOPE: Final[str] = "status:active"

#: Never produced by `fixtures.generate` (its exchange is 100-999), so a persisted change is visible.
SMOKE_PHONE: Final[str] = "+1-555-010-9999"

#: `fixtures.generate` plants PD-01's departed contact as the lowest-numbered contact of the first
#: seeded account, and the next one as an active successor.
_DEPARTED_CONTACT_INDEX = 0
_ACTIVE_CONTACT_INDEX = 1


def seeded_in_scope_account_id(tenant: str) -> str:
    """Return the deterministic account id `fixtures.generate` seeds first under `tenant`."""
    return f"acct-{tenant}-{_SEEDED_ACCOUNT_INDEX:04d}"


def seeded_out_of_scope_account_id(tenant: str) -> str:
    """Return a real, seeded second account under `tenant`, never in the out-of-scope book."""
    return f"acct-{tenant}-{_OUT_OF_SCOPE_ACCOUNT_INDEX:04d}"


def seeded_in_account_contact_id(tenant: str) -> str:
    """Return a seeded contact that belongs to `seeded_in_scope_account_id(tenant)`.

    See `_IN_ACCOUNT_CONTACT_INDEX`.
    """
    return f"cont-{tenant}-{_IN_ACCOUNT_CONTACT_INDEX:04d}"


def seeded_other_account_contact_id(tenant: str) -> str:
    """Return a seeded contact that never belongs to `seeded_in_scope_account_id(tenant)`.

    See `_OTHER_ACCOUNT_CONTACT_INDEX`.
    """
    return f"cont-{tenant}-{_OTHER_ACCOUNT_CONTACT_INDEX:04d}"


def seeded_departed_contact_id(tenant: str) -> str:
    """Return PD-01's seeded departed contact under `tenant`."""
    return f"cont-{tenant}-{_DEPARTED_CONTACT_INDEX:04d}"


def seeded_active_contact_id(tenant: str) -> str:
    """Return an active seeded contact under `seeded_in_scope_account_id(tenant)`.

    `update_contact`'s generic in-scope call targets it, since the departed contact refuses a
    non-status update.
    """
    return f"cont-{tenant}-{_ACTIVE_CONTACT_INDEX:04d}"


def seeded_entity_id(tenant: str, property_name: str) -> str | None:
    """Return the deterministic id `fixtures.generate` seeds first under `tenant` for `property_name`.

    Returns `None` when `property_name` names no entity `fixtures.generate` seeds statically (for
    example `proposal_id`/`audit_id`, which only exist once a prior write created one).
    """
    kind = _SEEDED_ENTITY_ID_KIND_BY_PROPERTY.get(property_name)
    if kind is None:
        return None
    return f"{kind}-{tenant}-0000"


def function_name_for(tool: ToolDefinition) -> str:
    """Return the physical Lambda function name Terraform assigns to ``tool``."""
    return f"{FUNCTION_NAME_PREFIX}{tool.name}"


def _identity_envelope(
    *, tenant: str, full_visibility: bool, book: Sequence[str] = ()
) -> dict[str, Any]:
    """Build a synthetic `_identity` payload matching what the Gateway interceptor injects.

    `tenant` is always the caller-supplied smoke tenant.
    """
    return {
        "rep_id": _SEEDED_ACCOUNT_OWNER_REP_ID,
        "book": list(book),
        "full_visibility": full_visibility,
        "principal_type": "user",
        "session_id": "smoke-session",
        "source": "gateway",
        "tenant": tenant,
    }


#: The one argument whose schema is an open `type: object` hiding a closed discriminated union, so
#: `_placeholder_for`'s generic `{}` cannot satisfy it.
PAGE_CONTEXT_ARGUMENT: Final[str] = "page_context"

#: The `PageContext` variant this module drives: `entity` is the only kind `get_page_context`
#: scope-checks.
_PAGE_CONTEXT_KIND: Final[str] = "entity"

#: `accounts`, because the two scope arms differ by `account_id` and both are real seeded records.
_PAGE_CONTEXT_ENTITY_TYPE: Final[str] = "accounts"


def entity_page_context(account_id: str) -> dict[str, Any]:
    """Build a well-formed `entity` `PageContext` naming the real seeded account ``account_id``.

    Every key is required by `crm.toolspec.page_context.EntityPageContext`, and the union is closed,
    so an extra key would be rejected as an undeclared one.
    """
    return {
        "kind": _PAGE_CONTEXT_KIND,
        "route": f"/{_PAGE_CONTEXT_ENTITY_TYPE}/{account_id}",
        "entity_type": _PAGE_CONTEXT_ENTITY_TYPE,
        "entity_id": account_id,
    }


def in_scope_event(tool: ToolDefinition, *, tenant: str) -> dict[str, Any]:
    """Build a synthetic event for ``tool`` an in-scope identity is permitted to call.

    Targets the real, seeded account `seeded_in_scope_account_id(tenant)` under `tenant`, so a
    handler that actually looks up the record (rather than trusting the envelope alone) finds it.
    """
    account_id = seeded_in_scope_account_id(tenant)
    return {
        **_scoped_arguments(
            tool, tenant=tenant, account_id=account_id, transaction_key_scope=f"{tenant}-in"
        ),
        "_identity": _identity_envelope(tenant=tenant, full_visibility=True, book=[account_id]),
    }


def out_of_scope_event(tool: ToolDefinition, *, tenant: str) -> dict[str, Any]:
    """Build a synthetic event for ``tool`` an out-of-scope identity must be denied for.

    Every entity `seeded_entity_id` points at belongs to `seeded_in_scope_account_id(tenant)`, so the
    identity's book excludes that account. An account-scoped tool (``tool.scope_arg ==
    "account_id"``) instead targets `seeded_out_of_scope_account_id(tenant)`, which the book also
    excludes.
    """
    return {
        **_scoped_arguments(
            tool,
            tenant=tenant,
            account_id=seeded_out_of_scope_account_id(tenant),
            transaction_key_scope=f"{tenant}-out",
        ),
        "_identity": _identity_envelope(tenant=tenant, full_visibility=False, book=[]),
    }


def _transaction_key(transaction_key_scope: str, tool_name: str) -> str:
    """Build a `transaction_key` unique per (tenant, direction, tool), never colliding, never long.

    DynamoDB's ``TransactWriteItems`` forwards this value as ``ClientRequestToken``, capped at 36
    characters, so `(transaction_key_scope, tool_name)` is hashed into a fixed-width digest.
    """
    digest = hashlib.sha256(f"{transaction_key_scope}:{tool_name}".encode()).hexdigest()[:24]
    return f"smk-{digest}"


def _scoped_arguments(
    tool: ToolDefinition, *, tenant: str, account_id: str, transaction_key_scope: str
) -> dict[str, Any]:
    """Build minimal, schema-satisfying arguments for ``tool`` scoped to ``account_id``.

    Every required property is filled with a placeholder, except a property `seeded_entity_id`
    recognises, which gets the real seeded record id. The scope argument (``tool.scope_arg``, or
    ``account_id`` when none is declared) is set to ``account_id``. `transaction_key_scope` and the
    tool name build a `transaction_key` unique per (tenant, tool, direction).
    """
    required = tool.input_schema.get("required", [])
    properties = tool.input_schema.get("properties", {})
    arguments: dict[str, Any] = {}
    for name in required:
        arguments[name] = _placeholder_for(
            name,
            properties.get(name, {}),
            tenant=tenant,
            transaction_key_scope=transaction_key_scope,
            tool=tool,
        )
    scope_key = tool.scope_arg or "account_id"
    if scope_key in properties or scope_key == tool.scope_arg:
        arguments[scope_key] = account_id
    if PAGE_CONTEXT_ARGUMENT in arguments:
        # `page_context` needs a well-formed variant, and carries this tool's scope target since
        # `get_page_context` declares no `scope_arg`.
        arguments[PAGE_CONTEXT_ARGUMENT] = entity_page_context(account_id)
    return arguments


def _placeholder_for(
    name: str,
    schema: Mapping[str, Any],
    *,
    tenant: str,
    transaction_key_scope: str,
    tool: ToolDefinition,
) -> Any:
    """Return a schema-satisfying placeholder value for one required property.

    Prefers a real seeded record id whenever `seeded_entity_id` recognises `name`.
    """
    if name == "transaction_key":
        return _transaction_key(transaction_key_scope, tool.name)
    if tool.name == _DEPARTED_CONTACT_TOOL and name == "contact_id":
        return seeded_active_contact_id(tenant)
    seeded_id = seeded_entity_id(tenant, name)
    if seeded_id is not None:
        return seeded_id
    schema_type = schema.get("type")
    enum = schema.get("enum")
    if enum:
        return enum[0]
    if schema_type == "number":
        return 0
    if schema_type == "integer":
        return 1
    if schema_type == "boolean":
        return False
    if schema_type == "object":
        return {}
    return f"smoke-{name}"


def _invoke(ctx: SmokeContext, function_name: str, event: Mapping[str, Any]) -> dict[str, Any]:
    """Invoke ``function_name`` synchronously with ``event`` and return the decoded response body.

    Raises `ValueError` when the invocation reports a `FunctionError`, or when the response
    payload is not valid JSON.
    """
    client = ctx.client("lambda")
    response = client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(dict(event)).encode("utf-8"),
    )
    function_error = response.get("FunctionError")
    payload_stream = response["Payload"]
    raw_payload = payload_stream.read()
    if function_error:
        raise ValueError(
            f"FunctionError={function_error}: "
            f"{raw_payload.decode('utf-8', errors='replace') if raw_payload else ''}"
        )
    try:
        decoded = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response payload is not valid JSON: {raw_payload!r}") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"response payload is not a JSON object: {decoded!r}")
    return decoded


def _error_code(response: Mapping[str, Any]) -> str | None:
    """Return the taxonomy `code` of an error response, or `None` when it is not error-shaped."""
    error_block = response.get("error")
    if isinstance(error_block, Mapping):
        code = error_block.get("code")
        return str(code) if code is not None else None
    return None


def _success_body_of(response: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the success payload `crm.handlers._common.ok` nests under `body`.

    The direct-invoke plane's success shape is ``{"statusCode": 200, "body": <payload>}``, so the
    payload a handler returned is one level down; an error response carries `error` at the top
    level instead and never reaches here. Returns an empty mapping when `body` is absent or is not
    a mapping.
    """
    body = response.get("body")
    return body if isinstance(body, Mapping) else {}


def _leaks_detail(response: Mapping[str, Any]) -> bool:
    """Return whether ``response`` (or its nested `error` block) carries an internal `detail`."""
    if "detail" in response:
        return True
    error_block = response.get("error")
    return isinstance(error_block, Mapping) and "detail" in error_block


def _check_in_scope(
    ctx: SmokeContext, tool: ToolDefinition, function_name: str, *, tenant: str
) -> CheckResult:
    """Assert an in-scope call to ``tool`` succeeds and leaks no internal detail."""
    resource = tool.name
    try:
        response = _invoke(ctx, function_name, in_scope_event(tool, tenant=tenant))
    except ValueError as exc:
        return CheckResult(
            resource=resource, check="in_scope_invoke", status="fail", detail=str(exc)
        )

    code = _error_code(response)
    if code is not None:
        return CheckResult(
            resource=resource,
            check="in_scope_invoke",
            status="fail",
            detail=f"expected success, got error code={code!r}: {response!r}",
        )
    if _leaks_detail(response):
        return CheckResult(
            resource=resource,
            check="in_scope_invoke",
            status="fail",
            detail=f"response leaks an internal 'detail' key: {response!r}",
        )
    return CheckResult(resource=resource, check="in_scope_invoke", status="pass")


def empty_book_listing_event(tool: ToolDefinition, *, tenant: str) -> dict[str, Any]:
    """Build a listing call from an identity with an empty book, carrying no explicit filter."""
    arguments = _scoped_arguments(
        tool,
        tenant=tenant,
        account_id=seeded_out_of_scope_account_id(tenant),
        transaction_key_scope=f"{tenant}-empty-book",
    )
    arguments.pop("account_id", None)
    return {
        **arguments,
        "_identity": _identity_envelope(tenant=tenant, full_visibility=False, book=[]),
    }


def explicit_filter_event(*, tenant: str) -> dict[str, Any]:
    """Build a `search_opportunities` call naming an account outside a non-empty book."""
    return {
        "account_id": seeded_out_of_scope_account_id(tenant),
        "_identity": _identity_envelope(
            tenant=tenant, full_visibility=False, book=[seeded_in_scope_account_id(tenant)]
        ),
    }


def _missing_scope(response: Mapping[str, Any]) -> object:
    """Return the error block's `missing_scope`, or `None` when absent."""
    error_block = response.get("error")
    return error_block.get("missing_scope") if isinstance(error_block, Mapping) else None


def _check_out_of_scope(
    ctx: SmokeContext,
    tool: ToolDefinition,
    function_name: str,
    *,
    tenant: str,
    event: Mapping[str, Any] | None = None,
    expected_missing_scope: str | None = None,
    check: str = "out_of_scope_invoke",
) -> CheckResult:
    """Assert an out-of-scope call to ``tool`` is denied with the typed `scope_denied` error.

    `event` defaults to `out_of_scope_event`. When `expected_missing_scope` is set, the error's
    `missing_scope` must equal it.
    """
    resource = tool.name
    sent = event if event is not None else out_of_scope_event(tool, tenant=tenant)
    try:
        response = _invoke(ctx, function_name, sent)
    except ValueError as exc:
        return CheckResult(resource=resource, check=check, status="fail", detail=str(exc))

    code = _error_code(response)
    if code == "scope_denied":
        if _leaks_detail(response):
            return CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail=f"scope_denied response leaks an internal 'detail' key: {response!r}",
            )
        missing_scope = _missing_scope(response)
        if expected_missing_scope is not None and missing_scope != expected_missing_scope:
            return CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail=(
                    f"scope_denied names missing_scope={missing_scope!r}, expected "
                    f"{expected_missing_scope!r}: {response!r}"
                ),
            )
        return CheckResult(
            resource=resource,
            check=check,
            status="pass",
            detail=(
                f"scope_denied, missing_scope={expected_missing_scope!r}"
                if expected_missing_scope is not None
                else ""
            ),
        )
    if code == "not_found":
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                "expected typed error code='scope_denied', got 'not_found' — an authorization "
                "bug surfacing as a lookup miss rather than a denial"
            ),
        )
    if code is not None:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"expected typed error code='scope_denied', got code={code!r}: {response!r}",
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="fail",
        detail=f"expected typed error code='scope_denied', got a non-error response: {response!r}",
    )


#: The check name asserting a `page_context` naming no `kind` is refused.
MALFORMED_PAGE_CONTEXT_CHECK: Final[str] = "malformed_page_context_rejected"


#: Every tool carrying a `page_context` argument, derived from the toolspec.
def page_context_tools() -> tuple[str, ...]:
    """Return the names of tools declaring a `page_context` argument, in toolspec order."""
    return tuple(
        tool.name
        for tool in ALL_TOOLS
        if PAGE_CONTEXT_ARGUMENT in tool.input_schema.get("properties", {})
    )


def _check_malformed_page_context_is_rejected(
    ctx: SmokeContext, tool: ToolDefinition, function_name: str, *, tenant: str
) -> CheckResult:
    """Assert a `page_context` that names no `kind` is rejected as `validation_failed`.

    The union is closed and the discriminator has no default, so a payload naming no `kind` is
    refused.
    """
    event = {
        **in_scope_event(tool, tenant=tenant),
        PAGE_CONTEXT_ARGUMENT: {},
    }
    resource = tool.name
    check = MALFORMED_PAGE_CONTEXT_CHECK
    try:
        response = _invoke(ctx, function_name, event)
    except ValueError as exc:
        return CheckResult(resource=resource, check=check, status="fail", detail=str(exc))

    code = _error_code(response)
    if code == "validation_failed":
        if _leaks_detail(response):
            return CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail=f"validation_failed response leaks an internal 'detail' key: {response!r}",
            )
        return CheckResult(
            resource=resource,
            check=check,
            status="pass",
            detail="an empty page_context naming no 'kind' is refused as validation_failed",
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="fail",
        detail=(
            f"expected typed error code='validation_failed' for a page_context naming no 'kind', "
            f"got code={code!r}: {response!r}"
        ),
    )


#: The one tool whose optional-argument path reaches a table its required-arguments-only path never
#: touches. `_run_all_tool_checks` adds `_PRIMARY_CONTACT_CHECKS` for it.
_PRIMARY_CONTACT_TOOL = "create_opportunity"

#: The check names the two `primary_contact_id` checks publish, in the order they run.
_ACCEPTED_CHECK: Final[str] = "in_scope_invoke_with_primary_contact"
_REFUSED_CHECK: Final[str] = "foreign_primary_contact_refused_with_validation_failed"
_PRIMARY_CONTACT_CHECKS: Final[tuple[str, str]] = (_ACCEPTED_CHECK, _REFUSED_CHECK)


def primary_contact_event(
    tool: ToolDefinition, *, tenant: str, contact_id: str, transaction_key_scope: str
) -> dict[str, Any]:
    """Build an in-scope `create_opportunity` event carrying the optional `primary_contact_id`.

    Identical to `in_scope_event` except for the added optional argument and a distinct
    `transaction_key_scope`, since `create_opportunity` is idempotent on `transaction_key`.
    """
    account_id = seeded_in_scope_account_id(tenant)
    return {
        **_scoped_arguments(
            tool, tenant=tenant, account_id=account_id, transaction_key_scope=transaction_key_scope
        ),
        "primary_contact_id": contact_id,
        "_identity": _identity_envelope(tenant=tenant, full_visibility=True, book=[account_id]),
    }


def _check_primary_contact_accepted(
    ctx: SmokeContext, tool: ToolDefinition, function_name: str, *, tenant: str
) -> CheckResult:
    """Assert `create_opportunity` succeeds when a valid in-account `primary_contact_id` is supplied.

    Supplying the optional argument makes the handler read `contacts`; a missing grant surfaces as
    `401 authentication_failed`, reported with that diagnosis. Asserts the created opportunity
    carries the contact id.
    """
    resource = tool.name
    check = _ACCEPTED_CHECK
    contact_id = seeded_in_account_contact_id(tenant)
    event = primary_contact_event(
        tool, tenant=tenant, contact_id=contact_id, transaction_key_scope=f"{tenant}-in-pc"
    )
    try:
        response = _invoke(ctx, function_name, event)
    except ValueError as exc:
        return CheckResult(resource=resource, check=check, status="fail", detail=str(exc))

    code = _error_code(response)
    if code == "authentication_failed":
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"supplying primary_contact_id={contact_id!r} returned "
                "code='authentication_failed' — the shape a missing dynamodb:GetItem grant on "
                "'contacts' produces via crm.errors._AWS_CODE_MAP's AccessDeniedException "
                "mapping, not a genuine authentication failure"
            ),
        )
    if code is not None:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"expected success, got error code={code!r}: {response!r}",
        )
    if _leaks_detail(response):
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"response leaks an internal 'detail' key: {response!r}",
        )
    opportunity = _success_body_of(response).get("opportunity")
    if not isinstance(opportunity, Mapping):
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"expected an 'opportunity' mapping in the response body: {response!r}",
        )
    persisted = opportunity.get("primary_contact_id")
    if persisted != contact_id:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"opportunity persisted primary_contact_id={persisted!r}, expected "
                f"{contact_id!r} — the optional argument was dropped rather than honoured"
            ),
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="pass",
        detail=(
            f"create_opportunity with primary_contact_id={contact_id!r} (a contact under the same "
            f"account {seeded_in_scope_account_id(tenant)!r}) returned opportunity "
            f"{str(opportunity.get('opportunity_id'))!r} carrying that contact id, so the "
            "handler's read of 'contacts' ran under a grant that permits it"
        ),
    )


def _check_foreign_primary_contact_refused(
    ctx: SmokeContext, tool: ToolDefinition, function_name: str, *, tenant: str
) -> CheckResult:
    """Assert a `primary_contact_id` under a *different* account is refused with `validation_failed`.

    Asserts that specific taxonomy code, so a `401 authentication_failed` from a missing `contacts`
    grant does not pass. The target contact exists (`seeded_other_account_contact_id`), so the
    refusal comes from the handler's `account_id` comparison.
    """
    resource = tool.name
    check = _REFUSED_CHECK
    contact_id = seeded_other_account_contact_id(tenant)
    event = primary_contact_event(
        tool,
        tenant=tenant,
        contact_id=contact_id,
        transaction_key_scope=f"{tenant}-in-pc-foreign",
    )
    try:
        response = _invoke(ctx, function_name, event)
    except ValueError as exc:
        return CheckResult(resource=resource, check=check, status="fail", detail=str(exc))

    code = _error_code(response)
    if code == "validation_failed":
        if _leaks_detail(response):
            return CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail=f"validation_failed response leaks an internal 'detail' key: {response!r}",
            )
        return CheckResult(
            resource=resource,
            check=check,
            status="pass",
            detail=(
                f"primary_contact_id={contact_id!r}, a seeded contact under an account other than "
                f"{seeded_in_scope_account_id(tenant)!r}, was refused with "
                "code='validation_failed' — the handler's own cross-account rule, reachable only "
                "because the read of 'contacts' returned the row and its account_id was compared"
            ),
        )
    if code == "authentication_failed":
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"expected code='validation_failed', got 'authentication_failed' for "
                f"primary_contact_id={contact_id!r} — the shape a missing dynamodb:GetItem grant "
                "on 'contacts' produces via crm.errors._AWS_CODE_MAP's AccessDeniedException "
                "mapping, which is a broken read masquerading as a refusal, not the "
                "handler's cross-account rule"
            ),
        )
    if code is not None:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"expected code='validation_failed', got code={code!r}: {response!r}",
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="fail",
        detail=(
            f"expected code='validation_failed' for a contact under another account, got a "
            f"non-error response: {response!r}"
        ),
    )


def _check_explicit_filter_denied(
    ctx: SmokeContext, tool: ToolDefinition, *, tenant: str
) -> CheckResult:
    """Assert an explicit out-of-book `account_id` filter is denied naming that account."""
    return _check_out_of_scope(
        ctx,
        tool,
        function_name_for(tool),
        tenant=tenant,
        event=explicit_filter_event(tenant=tenant),
        expected_missing_scope=f"account:{seeded_out_of_scope_account_id(tenant)}",
        check=EXPLICIT_FILTER_CHECK,
    )


def _book_identity(tenant: str) -> dict[str, Any]:
    """The rep identity owning `seeded_in_scope_account_id(tenant)`, without full visibility."""
    return _identity_envelope(
        tenant=tenant, full_visibility=False, book=[seeded_in_scope_account_id(tenant)]
    )


def contact_update_event(
    *, tenant: str, contact_id: str, fields: Mapping[str, Any], step: str
) -> dict[str, Any]:
    """Build an `update_contact` event from the book identity, keyed uniquely per `step`."""
    return {
        "contact_id": contact_id,
        "fields": dict(fields),
        "transaction_key": _transaction_key(f"{tenant}-departed-{step}", _DEPARTED_CONTACT_TOOL),
        "_identity": _book_identity(tenant),
    }


def _read_contact(ctx: SmokeContext, *, tenant: str, contact_id: str) -> Mapping[str, Any] | str:
    """Return the contact `get_contact` reads as the book identity, or a failure reason."""
    event = {"contact_id": contact_id, "_identity": _book_identity(tenant)}
    try:
        response = _invoke(ctx, f"{FUNCTION_NAME_PREFIX}{_CONTACT_READ_TOOL}", event)
    except ValueError as exc:
        return f"get_contact failed: {exc}"
    code = _error_code(response)
    if code is not None:
        return f"get_contact returned error code={code!r}: {response!r}"
    contact = _success_body_of(response).get("contact")
    if not isinstance(contact, Mapping):
        return f"get_contact returned no 'contact' mapping: {response!r}"
    return contact


def _update_contact(
    ctx: SmokeContext, *, tenant: str, contact_id: str, fields: Mapping[str, Any], step: str
) -> dict[str, Any] | str:
    """Invoke `update_contact`, returning the decoded response or a failure reason."""
    event = contact_update_event(tenant=tenant, contact_id=contact_id, fields=fields, step=step)
    try:
        return _invoke(ctx, f"{FUNCTION_NAME_PREFIX}{_DEPARTED_CONTACT_TOOL}", event)
    except ValueError as exc:
        return f"update_contact failed: {exc}"


def _departed_result(check: str, status: Literal["pass", "fail"], detail: str) -> CheckResult:
    return CheckResult(resource=_DEPARTED_CONTACT_TOOL, check=check, status=status, detail=detail)


def _check_departed_refused(ctx: SmokeContext, *, tenant: str, contact_id: str) -> CheckResult:
    """Assert a phone update on the departed contact is `contact_departed` and writes nothing."""
    check = DEPARTED_REFUSED_CHECK
    before = _read_contact(ctx, tenant=tenant, contact_id=contact_id)
    if isinstance(before, str):
        return _departed_result(check, "fail", f"pre-read: {before}")
    if before.get("status") != "departed":
        return _departed_result(
            check,
            "fail",
            f"precondition: {contact_id!r} has status={before.get('status')!r}, expected "
            "'departed' (PD-01)",
        )
    response = _update_contact(
        ctx, tenant=tenant, contact_id=contact_id, fields={"phone": SMOKE_PHONE}, step="refused"
    )
    if isinstance(response, str):
        return _departed_result(check, "fail", response)
    code = _error_code(response)
    if code != "contact_departed":
        return _departed_result(
            check, "fail", f"expected code='contact_departed', got code={code!r}: {response!r}"
        )
    if _leaks_detail(response):
        return _departed_result(
            check, "fail", f"contact_departed response leaks an internal 'detail': {response!r}"
        )
    missing_scope = _missing_scope(response)
    if missing_scope != CONTACT_DEPARTED_MISSING_SCOPE:
        return _departed_result(
            check,
            "fail",
            f"contact_departed names missing_scope={missing_scope!r}, expected "
            f"{CONTACT_DEPARTED_MISSING_SCOPE!r}",
        )
    after = _read_contact(ctx, tenant=tenant, contact_id=contact_id)
    if isinstance(after, str):
        return _departed_result(check, "fail", f"re-read: {after}")
    if after.get("phone") != before.get("phone") or after.get("status") != "departed":
        return _departed_result(
            check,
            "fail",
            f"refused update still wrote: phone {before.get('phone')!r} -> "
            f"{after.get('phone')!r}, status {after.get('status')!r}",
        )
    return _departed_result(
        check,
        "pass",
        f"update_contact on departed {contact_id!r} refused as contact_departed "
        f"(missing_scope={CONTACT_DEPARTED_MISSING_SCOPE!r}); re-read phone unchanged",
    )


def _check_status_update(
    ctx: SmokeContext, *, tenant: str, contact_id: str, status: str, check: str
) -> CheckResult:
    """Assert a `fields.status` update on the contact succeeds and persists `status`."""
    response = _update_contact(
        ctx, tenant=tenant, contact_id=contact_id, fields={"status": status}, step=check
    )
    if isinstance(response, str):
        return _departed_result(check, "fail", response)
    code = _error_code(response)
    if code is not None:
        return _departed_result(
            check, "fail", f"expected success, got error code={code!r}: {response!r}"
        )
    if _leaks_detail(response):
        return _departed_result(check, "fail", f"response leaks an internal 'detail': {response!r}")
    contact = _success_body_of(response).get("contact")
    persisted = contact.get("status") if isinstance(contact, Mapping) else None
    if persisted != status:
        return _departed_result(
            check, "fail", f"expected contact status={status!r}, response carries {persisted!r}"
        )
    return _departed_result(
        check, "pass", f"update_contact fields.status={status!r} on {contact_id!r} succeeded"
    )


def _departed_contact_checks(ctx: SmokeContext, *, tenant: str) -> Iterable[CheckResult]:
    """Yield the refusal, reactivation and restore checks on PD-01's departed contact, in order.

    The restore runs only once the refusal established the departed precondition.
    """
    contact_id = seeded_departed_contact_id(tenant)
    refused = _check_departed_refused(ctx, tenant=tenant, contact_id=contact_id)
    yield refused
    if refused.status != "pass" and refused.detail.startswith(("pre-read:", "precondition:")):
        not_run = f"not run: {DEPARTED_REFUSED_CHECK} could not establish the departed precondition"
        yield _departed_result(DEPARTED_REACTIVATED_CHECK, "fail", not_run)
        yield CheckResult(
            resource=_DEPARTED_CONTACT_TOOL,
            check=DEPARTED_RESTORED_CHECK,
            status="skip",
            detail=f"{not_run}; nothing was reactivated",
        )
        return
    yield _check_status_update(
        ctx,
        tenant=tenant,
        contact_id=contact_id,
        status="active",
        check=DEPARTED_REACTIVATED_CHECK,
    )
    yield _check_status_update(
        ctx,
        tenant=tenant,
        contact_id=contact_id,
        status="departed",
        check=DEPARTED_RESTORED_CHECK,
    )


def checks(
    ctx: SmokeContext,
    *,
    tenant_allocator: Callable[[Store], SmokeTenant] | None = None,
    seeder: Callable[[Store, SmokeTenant], None] | None = None,
    teardown: Callable[[Store, SmokeTenant], dict[str, int]] | None = None,
) -> Iterable[CheckResult]:
    """Yield an in-scope and an out-of-scope `CheckResult` for every tool in `ALL_TOOLS`.

    Every tool is checked even when an earlier tool's check fails.

    When `ctx.tenant` is set, that tenant is used as-is and not torn down here. When it is `None`,
    this function allocates and seeds its own smoke tenant via `scripts.smoke.tenant` and tears it
    down once every check has run, reporting a teardown failure as its own failing check.
    """
    allocate = tenant_allocator if tenant_allocator is not None else allocate_smoke_tenant
    do_seed = seeder if seeder is not None else seed_smoke_tenant
    do_teardown = teardown if teardown is not None else teardown_smoke_tenant

    if ctx.tenant is not None:
        yield from _run_all_tool_checks(ctx, tenant=ctx.tenant)
        return

    store = Store(client=ctx.client("dynamodb"), table_prefix=_live_table_prefix(ctx))
    smoke_tenant = allocate(store)
    do_seed(store, smoke_tenant)
    try:
        yield from _run_all_tool_checks(ctx, tenant=smoke_tenant.tenant)
    finally:
        yield from _teardown_check(store, smoke_tenant, do_teardown)


def _live_table_prefix(ctx: SmokeContext) -> str:
    """Derive the live DynamoDB table-name prefix from the `dynamodb_table_names` Terraform output.

    Mirrors `scripts/verify_deployment.sh`: the physical name for the `eval_runs` logical table is
    `<table_prefix>eval_runs`, so stripping that suffix recovers the prefix.
    """
    table_names = ctx.outputs.require_mapping("dynamodb_table_names")
    return str(table_names["eval_runs"]).removesuffix("eval_runs")


def _run_all_tool_checks(ctx: SmokeContext, *, tenant: str) -> Iterable[CheckResult]:
    """Yield the in-scope/out-of-scope check pair for every declared tool, under `tenant`.

    A tool in `_NO_STATIC_SEED_TOOLS` is reported `skip`, naming Layer 5 or T3-pending.
    `_PRIMARY_CONTACT_TOOL` additionally gets the two `primary_contact_id` checks. A tool in
    `_EMPTY_BOOK_DENIAL_TOOLS` gets an empty-book out-of-scope check expecting
    `EMPTY_BOOK_MISSING_SCOPE`, and `_EXPLICIT_FILTER_TOOL` also gets `EXPLICIT_FILTER_CHECK`.
    The `DEPARTED_CONTACT_CHECKS` run once every tool has been checked.
    """
    for tool in ALL_TOOLS:
        function_name = function_name_for(tool)
        if tool.name in _NO_STATIC_SEED_TOOLS:
            detail = (
                T3_PENDING_DETAIL.format(tool=tool.name)
                if tool.name in _T3_PENDING_TOOLS
                else (
                    f"{tool.name} acts on a proposal/audit record fixtures.generate never seeds "
                    "statically; exercised at Layer 5 (integration_proposal_chain.py) instead"
                )
            )
            yield CheckResult(
                resource=tool.name, check="in_scope_invoke", status="skip", detail=detail
            )
            yield CheckResult(
                resource=tool.name, check="out_of_scope_invoke", status="skip", detail=detail
            )
            continue
        yield _check_in_scope(ctx, tool, function_name, tenant=tenant)
        if PAGE_CONTEXT_ARGUMENT in tool.input_schema.get("properties", {}):
            yield _check_malformed_page_context_is_rejected(ctx, tool, function_name, tenant=tenant)
        if tool.name == _PRIMARY_CONTACT_TOOL:
            yield _check_primary_contact_accepted(ctx, tool, function_name, tenant=tenant)
            yield _check_foreign_primary_contact_refused(ctx, tool, function_name, tenant=tenant)
        if tool.name in _NO_OUT_OF_SCOPE_DENIAL_TOOLS:
            yield CheckResult(
                resource=tool.name,
                check="out_of_scope_invoke",
                status="skip",
                detail=(
                    f"{tool.name} has no single target account to deny access to (a create, or a "
                    "list read that filters via crm.authz.filter_to_scope rather than denying the "
                    "whole call) -- the scope_denied assertion does not apply"
                ),
            )
        elif tool.name in _EMPTY_BOOK_DENIAL_TOOLS:
            yield _check_out_of_scope(
                ctx,
                tool,
                function_name,
                tenant=tenant,
                event=empty_book_listing_event(tool, tenant=tenant),
                expected_missing_scope=EMPTY_BOOK_MISSING_SCOPE,
            )
        else:
            yield _check_out_of_scope(ctx, tool, function_name, tenant=tenant)
        if tool.name == _EXPLICIT_FILTER_TOOL:
            yield _check_explicit_filter_denied(ctx, tool, tenant=tenant)
    yield from _departed_contact_checks(ctx, tenant=tenant)


def _teardown_check(
    store: Store,
    smoke_tenant: SmokeTenant,
    do_teardown: Callable[[Store, SmokeTenant], dict[str, int]],
) -> Iterable[CheckResult]:
    """Tear down `smoke_tenant`, reporting a `TenantTeardownError` as a failing check."""
    try:
        deleted = do_teardown(store, smoke_tenant)
    except TenantTeardownError as exc:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="tenant_left_clean",
            status="fail",
            detail=f"teardown of tenant {smoke_tenant.tenant!r} failed: {exc}",
        )
        return
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="tenant_left_clean",
        status="pass",
        detail=f"tenant {smoke_tenant.tenant!r} torn down: {deleted!r}",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run every action-Lambda smoke check and return 0 pass / 1 fail."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

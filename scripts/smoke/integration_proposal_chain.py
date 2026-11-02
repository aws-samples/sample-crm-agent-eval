# SPDX-License-Identifier: MIT
"""Layer 5 smoke script: the full proposal -> approval -> audit -> revert chain against a live
deployment.

Drives `submit_proposal`, `approve_proposal`, `reject_proposal` and `revert_audit_record` through
the CloudFront `/api/*` behaviour with a real Cognito token, as
`scripts/smoke/integration_ui_api_ddb.py` drives the low-risk write path, and reads DynamoDB
directly to check each claim. These four tools are recorded `skip` at Layer 4 because
`fixtures.generate` seeds neither a proposal nor an audit record; this script creates the
prerequisite record before acting on it.

Assertions:

* **a proposal mutates nothing** - the target row is read back before and after
  `submit_proposal` and compared as canonical bytes, and the audit table is counted over the same
  window.
* **approval commits and audits atomically** - the committed change and the audit record must
  both exist; each half-state is its own distinct failure. The audit record must carry
  before/after snapshots that byte-match the target row on either side of the approval, plus the
  acting rep and a timestamp inside the run window. A second approval of the now-approved proposal
  must be refused and leave neither half behind.
* **a stale snapshot is refused with `Conflict`** - a second target is mutated out from under its
  own pending proposal through the low-risk write path, and the approval must come back `409`
  with `error.code == "conflict"` specifically, with the target unchanged and the proposal still
  `pending`.
* **revert restores once and refuses twice** - the first revert must restore the pre-approval row
  as canonical bytes; the second must be refused with `conflict`, leaving the row and the audit
  record's own `reversed_at` untouched.

`reject_proposal` is exercised on a third target: a reject leaves the target byte-identical, is
terminal against a second reject, and makes a subsequent approval impossible.

A fourth leg covers the delete proposal path. `submit_proposal` with `operation: "delete"` runs
`crm.handlers.cascade_rules`, which queries `opportunities.gsi_account`; a missing `dynamodb:Query`
grant surfaces as a `401`. A delete proposal on an unreferenced contact must be accepted, and one
on the account its opportunities belong to must be refused with `validation_failed`. Both targets
are the acting identity's own in-book records, since the token's `custom:book` claim is fixed at
sign-in.

A fifth leg covers what an `after_payload` may carry: it is confined to
`UPDATE_ALLOWLISTS[target_table] | high_risk_fields_for(target_table)`, so a payload carrying the
target table's partition key, a `tenant`, or a derived index attribute must be refused with
`validation_failed` at submit. These are driven against two created targets (the declared one and
the one a redirecting payload points at), both read back byte-for-byte. A payload naming one
high-risk field and one update-allowlisted field must still be accepted.

Two revert legs follow: a note create's audit reverts by deleting the note, and an approved
delete of an unreferenced contact reverts by restoring the contact from the audit's
before-snapshot. Each marks its audit record reversed.

An out-of-book revert leg follows. Two audit records are seeded directly under the disposable tenant
for an account outside the acting identity's book: a note create and a contact delete. Each revert
must be refused with `403 scope_denied`, and a read-back must show the note, the absent contact and
both audit records unchanged.

Tenant safety: the script never writes the `demo` tenant. By default it provisions its own Cognito
user under a disposable `eval-smoke-<hex>` tenant via
`scripts.smoke.cognito_claims.LiveSmokeUserProvisioner`, and removes every resource it created. A
caller-supplied `--token` is honoured instead, with the tenant taken from its `custom:tenant` claim
and refused via `scripts.smoke.api_routes._tenant_refusal` if it is `demo`, absent, or not
disposable. The `demo` tenant's row counts are captured before the chain and re-read after it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from scripts.smoke.api_routes import TENANT_CLAIM, _tenant_refusal, verified_token_claims
from scripts.smoke.cognito_claims import (
    LiveSmokeUserProvisioner,
    ProvisionedSmokeUser,
    _live_table_prefix,
)
from scripts.smoke.harness import CheckResult, SmokeContext, open_https, run_main
from scripts.smoke.integration_ui_api_ddb import in_book_accounts

from crm.audit import build_audit_record
from crm.authz import DEMO_TENANT, Identity
from crm.envelope import CLAIM_BOOK, CLAIM_REP_ID
from crm.store.keys import derive, derived_attribute_names, tenant_prefixed
from crm.store.store import Store
from crm.toolspec.definitions import BY_NAME

LAYER = 5
RESOURCE_CLASS = "integration_proposal_chain"

_OPPORTUNITY_TABLE: Final[str] = "opportunities"
_ACCOUNTS_TABLE: Final[str] = "accounts"
_CONTACTS_TABLE: Final[str] = "contacts"
_PROPOSALS_TABLE: Final[str] = "proposals"
_AUDIT_TABLE: Final[str] = "audit_records"
_NOTES_TABLE: Final[str] = "notes"

#: The stage a `create_opportunity` call lands in with no explicit stage, and the stage every
#: proposal in this script asks for. Discovery -> Negotiation is an `ADVANCE`, the one transition
#: kind `crm.domain.stages.PERMITTED_TRANSITION_KINDS` accepts for a forward move.
_CREATED_STAGE: Final[str] = "Discovery"
_PROPOSED_STAGE: Final[str] = "Negotiation"

#: Every target opportunity is created with a close date, so `stage_close_date` — the composite
#: sort key of `gsi_owner_stage`, derived from `stage` and `close_date` — exists on the row.
_CLOSE_DATE: Final[str] = "2026-12-31"

#: Tables whose `demo`-tenant row counts are captured before the chain and re-read after it.
_DEMO_WATCHED_TABLES: Final[tuple[str, ...]] = (
    _ACCOUNTS_TABLE,
    _CONTACTS_TABLE,
    _OPPORTUNITY_TABLE,
    _PROPOSALS_TABLE,
    _AUDIT_TABLE,
)

#: `(status_code, response_body)` for a completed HTTP call, or a `str` transport-failure message
#: when no HTTP response was received at all. Mirrors `integration_ui_api_ddb.py`'s `HttpResult`.
HttpResult = tuple[int, str] | str

HttpCaller = Callable[[str, str, Mapping[str, str], str], HttpResult]


def default_http_caller(method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
    """Perform one HTTP request using only `urllib.request` (no `requests` dependency).

    Returns `(status_code, response_body)` for any received response, including 4xx/5xx. Returns
    a `str` describing the failure only when no HTTP response was received at all.
    """
    request = urllib.request.Request(  # noqa: S310 - internal seam, no untrusted URL
        url, data=body.encode("utf-8") if body else None, headers=dict(headers), method=method
    )
    try:
        with open_https(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return f"transport error calling {url}: {exc}"


def _flag_or_env(argv: Sequence[str] | None, flag: str, env_var: str) -> str | None:
    """Resolve a value from ``flag`` in ``argv``, else ``env_var``, else ``None``.

    Parsed out of `argv` ahead of `run_main`; each flag is declared to `run_main` via
    `extra_arguments`.
    """
    args = list(argv) if argv is not None else sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(f"{flag}="):
            return arg.split("=", 1)[1]
    return os.environ.get(env_var)


def _tool_invoke_url(cloudfront_domain_name: str, tool_name: str) -> str:
    """Build the invoke URL for one tool call: `https://<domain>/api/<tool_name>`."""
    return f"https://{cloudfront_domain_name}/api/{tool_name}"


def _tenant_marker(tenant: str) -> str:
    """Return the write marker identifying ``tenant``, as it appears in every `transaction_key`.

    A digest of the whole tenant name, identical in form to `integration_ui_api_ddb._tenant_marker`.
    """
    return f"smoke-{hashlib.sha256(tenant.encode()).hexdigest()[:8]}"


def _make_transaction_key(tenant: str, step: str) -> str:
    """Build a `transaction_key` unique per (tenant, step) and within DynamoDB's 36-character cap.

    `Store.transact_write` forwards this value as `ClientRequestToken`, which AWS rejects above 36
    characters, so the value is hashed.
    """
    digest = hashlib.sha256(f"{tenant}:{step}".encode()).hexdigest()[:20]
    return f"{_tenant_marker(tenant)}-{digest}"


def _spying_caller(caller: HttpCaller, calls: list[tuple[str, str, str]]) -> HttpCaller:
    """Wrap `caller`, recording every `(method, url, body)` triple issued through it."""

    def spy(method: str, url: str, headers: Mapping[str, str], body: str) -> HttpResult:
        calls.append((method, url, body))
        return caller(method, url, headers, body)

    return spy


def _call_tool(
    caller: HttpCaller,
    cloudfront_domain_name: str,
    tool_name: str,
    token: str,
    arguments: Mapping[str, Any],
) -> HttpResult:
    url = _tool_invoke_url(cloudfront_domain_name, tool_name)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    return caller("POST", url, headers, json.dumps(dict(arguments)))


def _parse_json_body(body: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _error_code(response_body: str) -> str | None:
    """Extract `error.code` from an error response body, or `None` if absent/unparseable."""
    parsed = _parse_json_body(response_body) or {}
    error_block = parsed.get("error")
    if isinstance(error_block, Mapping):
        code = error_block.get("code")
        return str(code) if code is not None else None
    return None


def _error_reason(response_body: str) -> str | None:
    """Extract `error.reason` from an error response body, or `None` if absent/unparseable."""
    parsed = _parse_json_body(response_body) or {}
    error_block = parsed.get("error")
    if isinstance(error_block, Mapping):
        reason = error_block.get("reason")
        return str(reason) if reason is not None else None
    return None


def canonical_row_bytes(row: Mapping[str, Any] | None) -> bytes:
    """Render one persisted row as canonical bytes: sorted keys, no incidental whitespace.

    An absent row renders as empty, distinct from any present row.
    """
    if row is None:
        return b""
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def row_fingerprint(row: Mapping[str, Any] | None) -> str:
    """Return a short, publishable fingerprint of ``row``'s canonical bytes."""
    raw = canonical_row_bytes(row)
    if not raw:
        return "absent"
    return f"{len(raw)} bytes sha256[:16]={hashlib.sha256(raw).hexdigest()[:16]}"


# -- live reads ------------------------------------------------------------------------------


def _store(ctx: SmokeContext) -> Store:
    """Build a `Store` addressing the live, prefixed physical tables."""
    return Store(client=ctx.client("dynamodb"), table_prefix=_live_table_prefix(ctx))


def _read_row(
    ctx: SmokeContext, table: str, key: Mapping[str, str], *, consistent: bool = False
) -> dict[str, Any] | None:
    return _store(ctx).get_item(table, key, consistent=consistent)


def _read_opportunity(ctx: SmokeContext, opportunity_id: str) -> dict[str, Any] | None:
    return _read_row(ctx, _OPPORTUNITY_TABLE, {"opportunity_id": opportunity_id})


def _read_proposal(ctx: SmokeContext, proposal_id: str) -> dict[str, Any] | None:
    return _read_row(ctx, _PROPOSALS_TABLE, {"proposal_id": proposal_id})


def _audit_ids_for_target(ctx: SmokeContext, *, tenant: str, target_id: str) -> list[str]:
    """Return every audit record id targeting ``target_id`` under ``tenant``, oldest first.

    Read through the `audit_records.gsi_target` index on the tenant-prefixed `tenant_target_id`
    partition key; the index's `created_at` sort key gives the order.
    """
    items, cursor = _store(ctx).query_gsi(
        _AUDIT_TABLE,
        "gsi_target",
        partition_value=tenant_prefixed(tenant, target_id),
    )
    while cursor is not None:
        page, cursor = _store(ctx).query_gsi(
            _AUDIT_TABLE,
            "gsi_target",
            partition_value=tenant_prefixed(tenant, target_id),
            exclusive_start_key=cursor,
        )
        items.extend(page)
    return [str(item["audit_id"]) for item in items if item.get("audit_id") is not None]


def _read_audit(
    ctx: SmokeContext, audit_id: str, *, consistent: bool = False
) -> dict[str, Any] | None:
    """Read the full audit row by primary key (the `gsi_target` projection omits the snapshots)."""
    return _read_row(ctx, _AUDIT_TABLE, {"audit_id": audit_id}, consistent=consistent)


def _tenant_row_count(ctx: SmokeContext, table: str, tenant: str) -> int:
    """Count every row in ``table`` under ``tenant`` via the tenant-wide index."""
    store = _store(ctx)
    total = 0
    cursor: dict[str, object] | None = None
    while True:
        items, cursor = store.query_tenant_scan(table, tenant=tenant, exclusive_start_key=cursor)
        total += len(items)
        if cursor is None:
            return total


# -- identity ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SmokeIdentity:
    """The acting identity for one run of the chain.

    Attributes:
        token: Bearer token every call carries.
        tenant: The tenant every write lands in - never `demo`.
        rep_id: The rep the token names, asserted against each audit record's `actor_rep_id`.
        account_id: An in-book account to create the chain's target opportunities under.
        detail: How this identity was obtained, for the inventory.
        book: Every account the token's verified identity owns; empty when unknown.
    """

    token: str
    tenant: str
    rep_id: str
    account_id: str
    detail: str
    book: frozenset[str] = frozenset()


class IdentityLease(Protocol):
    """An acting identity plus the removal of everything obtaining it created."""

    def acquire(self) -> SmokeIdentity:
        """Return the acting identity, provisioning whatever it requires."""

    def release(self) -> Iterable[CheckResult]:
        """Remove everything `acquire` created, yielding one check per removal."""


class ProvisionedIdentityLease:
    """Mints a Cognito user under its own disposable tenant, then removes it.

    A thin adapter over `scripts.smoke.cognito_claims.LiveSmokeUserProvisioner`.
    """

    def __init__(self, ctx: SmokeContext) -> None:
        self._provisioner = LiveSmokeUserProvisioner(ctx)

    def acquire(self) -> SmokeIdentity:
        """Provision the user and return its identity, with the lowest-numbered in-book account."""
        user: ProvisionedSmokeUser = self._provisioner.provision()
        if not user.book:
            raise RuntimeError(
                "the provisioned rep's book is empty, so no account exists to create the chain's "
                "target opportunities under; refusing to proceed"
            )
        return SmokeIdentity(
            token=user.id_token,
            tenant=user.tenant,
            rep_id=user.rep_id,
            account_id=sorted(user.book)[0],
            detail=f"provisioned smoke user; {user.detail}",
            book=frozenset(user.book),
        )

    def release(self) -> Iterable[CheckResult]:
        """Delete the Cognito user, its `reps` row, and tear the tenant down."""
        return self._provisioner.cleanup()


class SuppliedTokenLease:
    """Uses a caller-supplied token, tearing its tenant down on release.

    The tenant is the token's own `custom:tenant` claim; `scripts.smoke.api_routes._tenant_refusal`
    refuses `demo`, an absent claim, or a non-disposable tenant. The account is the supplied
    `account_id`, else the lowest entry of the token's own `custom:book` claim.
    """

    def __init__(self, ctx: SmokeContext, *, token: str, account_id: str | None) -> None:
        self._ctx = ctx
        self._token = token
        self._account_id = account_id
        self._tenant: str | None = None

    def acquire(self) -> SmokeIdentity:
        """Validate the token's tenant claim and return the identity it names."""
        claims, unverified = verified_token_claims(self._ctx, self._token)
        claimed_tenant = str(claims.get(TENANT_CLAIM) or "")
        tenant = self._ctx.tenant or claimed_tenant
        refusal = unverified or _tenant_refusal(
            tenant=tenant, claimed_tenant=claimed_tenant, ctx_tenant=self._ctx.tenant
        )
        if refusal:
            raise RuntimeError(refusal)
        account_id = self._account_id
        account_source = "--account-id/CRM_SMOKE_ACCOUNT_ID"
        try:
            book = frozenset(in_book_accounts(claims))
        except ValueError:
            book = frozenset()
        if not account_id:
            try:
                account_id = in_book_accounts(claims)[0]
            except ValueError as exc:
                raise RuntimeError(
                    "a supplied --token with no --account-id/CRM_SMOKE_ACCOUNT_ID needs an in-book "
                    f"account to create the chain's target opportunities under, but {exc}"
                ) from exc
            account_source = f"the token's {CLAIM_BOOK} claim"
        self._tenant = tenant
        return SmokeIdentity(
            token=self._token,
            tenant=tenant,
            rep_id=str(claims.get(CLAIM_REP_ID) or ""),
            account_id=account_id,
            detail=(
                f"caller-supplied token naming {TENANT_CLAIM}={tenant!r}; account "
                f"{account_id!r} from {account_source}"
            ),
            book=book,
        )

    def release(self) -> Iterable[CheckResult]:
        """Tear down the tenant the supplied token names, reporting failure as a failing check."""
        tenant = self._tenant
        if tenant is None:
            return
        from eval.tenancy import teardown_tenant

        try:
            deleted = teardown_tenant(_store(self._ctx), tenant=tenant)
        except Exception as exc:  # noqa: BLE001 - a failed teardown must never read as clean
            yield CheckResult(
                resource="smoke_tenant",
                check="tenant_left_clean",
                status="fail",
                detail=f"teardown of {tenant!r} failed: {type(exc).__name__}: {exc}",
            )
            return
        yield CheckResult(
            resource="smoke_tenant",
            check="tenant_left_clean",
            status="pass",
            detail=f"tenant {tenant!r} torn down ({sum(deleted.values())} items)",
        )


# -- one step of the chain -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Call:
    """One completed tool call: its HTTP status, body, and parsed success payload."""

    status_code: int
    body: str
    payload: dict[str, Any]


def _ok_call(
    caller: HttpCaller,
    domain: str,
    tool_name: str,
    identity: SmokeIdentity,
    arguments: Mapping[str, Any],
) -> tuple[_Call | None, str]:
    """Issue one call expected to succeed. Returns `(call, "")` or `(None, why_it_failed)`."""
    result = _call_tool(caller, domain, tool_name, identity.token, arguments)
    if isinstance(result, str):
        return None, result
    status_code, body = result
    if status_code >= 400:
        return None, f"status={status_code} body={body[:400]}"
    return _Call(status_code, body, _parse_json_body(body) or {}), ""


def _refused_with(
    result: HttpResult,
    *,
    resource: str,
    check: str,
    what: str,
    member: str,
    expected_status: int,
    expected_code: str,
) -> CheckResult:
    """Assert ``result`` is a refusal carrying one named taxonomy member's own status/code pair.

    A success is reported as a failure, and so is a refusal under any other taxonomy member.
    """
    if isinstance(result, str):
        return CheckResult(resource=resource, check=check, status="fail", detail=result)
    status_code, body = result
    if status_code < 400:  # noqa: PLR2004 - the HTTP client/server error boundary
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"{what} SUCCEEDED (status={status_code} body={body[:300]}) - a success here is "
                "the dangerous outcome this check exists to catch"
            ),
        )
    code = _error_code(body)
    if status_code != expected_status or code != expected_code:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"{what} was refused, but not with {member}: expected status={expected_status} and "
                f"error.code={expected_code!r}, got status={status_code} code={code!r} "
                f"body={body[:300]}"
            ),
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="pass",
        detail=f"{what} refused with {expected_status} error.code={expected_code!r}",
    )


def _refused_with_conflict(
    result: HttpResult, *, resource: str, check: str, what: str
) -> CheckResult:
    """Assert ``result`` is a refusal carrying `Conflict`'s own 409/`conflict` pair specifically."""
    return _refused_with(
        result,
        resource=resource,
        check=check,
        what=what,
        member="Conflict",
        expected_status=409,
        expected_code="conflict",
    )


def _refused_with_validation_failed(
    result: HttpResult, *, resource: str, check: str, what: str
) -> CheckResult:
    """Assert ``result`` carries `ValidationFailed`'s own 400/`validation_failed` pair specifically.

    Distinct from the `401 authentication_failed` a missing `dynamodb:Query` grant produces.
    """
    return _refused_with(
        result,
        resource=resource,
        check=check,
        what=what,
        member="ValidationFailed",
        expected_status=400,
        expected_code="validation_failed",
    )


def _unchanged_check(
    *,
    resource: str,
    check: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    what: str,
) -> CheckResult:
    """Assert two reads of the same row are byte-identical, publishing both fingerprints."""
    before_print = row_fingerprint(before)
    after_print = row_fingerprint(after)
    if canonical_row_bytes(before) == canonical_row_bytes(after):
        return CheckResult(
            resource=resource,
            check=check,
            status="pass",
            detail=f"{what}: byte-identical, {before_print}",
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="fail",
        detail=f"{what}: row CHANGED, before={before_print} after={after_print}",
    )


def _identical_check(
    *,
    resource: str,
    check: str,
    left: Mapping[str, Any] | None,
    right: Mapping[str, Any] | None,
    what: str,
) -> CheckResult:
    """Assert two rows/snapshots are byte-identical, publishing both fingerprints."""
    return _unchanged_check(resource=resource, check=check, before=left, after=right, what=what)


# -- the chain -------------------------------------------------------------------------------


def checks(
    ctx: SmokeContext,
    *,
    http_caller: HttpCaller | None = None,
    token: str | None = None,
    account_id: str | None = None,
    lease: IdentityLease | None = None,
) -> Iterable[CheckResult]:
    """Yield one `CheckResult` per assertion of the proposal chain.

    Never raises for an individual check failure; after a failed prerequisite step every later step
    reports `skip` naming what failed. Acquires and releases its own identity (and therefore its own
    tenant), reporting a release failure as a failing check.
    """
    caller = http_caller if http_caller is not None else default_http_caller
    resolved_lease: IdentityLease
    if lease is not None:
        resolved_lease = lease
    elif token is not None:
        resolved_lease = SuppliedTokenLease(ctx, token=token, account_id=account_id)
    else:
        resolved_lease = ProvisionedIdentityLease(ctx)

    try:
        identity = resolved_lease.acquire()
    except Exception as exc:  # noqa: BLE001 - reported as a failing check, never a skip
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="identity_acquired",
            status="fail",
            detail=f"{type(exc).__name__}: {exc}",
        )
        yield from resolved_lease.release()
        return

    if identity.tenant == DEMO_TENANT or not identity.tenant:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="identity_acquired",
            status="fail",
            detail=(
                f"refusing to run: the acting identity names tenant {identity.tenant!r}; smoke runs "
                "never write the demo tenant"
            ),
        )
        yield from resolved_lease.release()
        return

    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="identity_acquired",
        status="pass",
        detail=(
            f"acting as rep {identity.rep_id!r} under disposable tenant {identity.tenant!r} "
            f"({identity.detail})"
        ),
    )

    domain = ctx.outputs.require_str("cloudfront_domain_name")
    recorded_calls: list[tuple[str, str, str]] = []
    spy = _spying_caller(caller, recorded_calls)
    demo_before = {
        table: _tenant_row_count(ctx, table, DEMO_TENANT) for table in _DEMO_WATCHED_TABLES
    }

    try:
        yield from _run_chain(ctx=ctx, caller=spy, domain=domain, identity=identity)
    finally:
        yield _demo_tenant_untouched_check(ctx, demo_before)
        yield from _writes_only_under_smoke_tenant_check(recorded_calls, identity.tenant)
        created = _created_row_keys(recorded_calls)
        yield from resolved_lease.release()
        yield from _teardown_removed_everything_checks(ctx, identity.tenant, created)


def _run_chain(
    *, ctx: SmokeContext, caller: HttpCaller, domain: str, identity: SmokeIdentity
) -> Iterable[CheckResult]:
    """Run the five independent legs of the chain against independent target records."""
    yield from _approval_leg(ctx=ctx, caller=caller, domain=domain, identity=identity)
    yield from _stale_leg(ctx=ctx, caller=caller, domain=domain, identity=identity)
    yield from _reject_leg(ctx=ctx, caller=caller, domain=domain, identity=identity)
    yield from _delete_leg(ctx=ctx, caller=caller, domain=domain, identity=identity)
    yield from _payload_allowlist_leg(ctx=ctx, caller=caller, domain=domain, identity=identity)
    yield from _note_revert_leg(ctx=ctx, caller=caller, domain=domain, identity=identity)
    yield from _delete_revert_leg(ctx=ctx, caller=caller, domain=domain, identity=identity)
    yield from _out_of_book_revert_leg(ctx=ctx, caller=caller, domain=domain, identity=identity)


def _create_target(
    *,
    caller: HttpCaller,
    domain: str,
    identity: SmokeIdentity,
    step: str,
    name: str,
    account_id: str | None = None,
) -> tuple[str | None, str]:
    """Create one target opportunity via the low-risk write path. Returns `(id, why_not)`.

    ``account_id`` defaults to the acting identity's own in-book account; the delete-proposal leg
    passes the account it created so the cascade check has an opportunity under that account.
    """
    call, why = _ok_call(
        caller,
        domain,
        BY_NAME["create_opportunity"].name,
        identity,
        {
            "account_id": account_id if account_id is not None else identity.account_id,
            "name": name,
            "close_date": _CLOSE_DATE,
            "transaction_key": _make_transaction_key(identity.tenant, step),
        },
    )
    if call is None:
        return None, why
    opportunity = call.payload.get("opportunity")
    if not (isinstance(opportunity, dict) and isinstance(opportunity.get("opportunity_id"), str)):
        return None, f"response body carried no opportunity id: {call.body[:300]}"
    return str(opportunity["opportunity_id"]), ""


def _submit(
    *,
    caller: HttpCaller,
    domain: str,
    identity: SmokeIdentity,
    step: str,
    target_id: str,
    reason: str,
    extra_payload: Mapping[str, object] | None = None,
) -> tuple[str | None, str]:
    """Submit one stage-change proposal against ``target_id``. Returns `(proposal_id, why_not)`.

    ``extra_payload`` adds fields to the proposal's ``after_payload`` beside ``stage``.
    """
    call, why = _ok_call(
        caller,
        domain,
        BY_NAME["submit_proposal"].name,
        identity,
        {
            "target_table": _OPPORTUNITY_TABLE,
            "target_id": target_id,
            "operation": "update",
            "after_payload": {"stage": _PROPOSED_STAGE, **(extra_payload or {})},
            "reason": reason,
            "transaction_key": _make_transaction_key(identity.tenant, step),
        },
    )
    if call is None:
        return None, why
    proposal = call.payload.get("proposal")
    if not (isinstance(proposal, dict) and isinstance(proposal.get("proposal_id"), str)):
        return None, f"response body carried no proposal id: {call.body[:300]}"
    return str(proposal["proposal_id"]), ""


_APPROVAL_LEG_CHECKS: Final[tuple[tuple[str, str], ...]] = (
    ("submit", "proposal_mutates_nothing"),
    ("submit", "proposal_writes_no_audit_record"),
    ("submit", "proposal_row_is_pending"),
    ("approve", "commits_the_change"),
    ("approve", "derived_index_attributes_recomputed"),
    ("approve", "writes_exactly_one_audit_record"),
    ("approve", "commit_and_audit_are_atomic"),
    ("approve", "audit_carries_before_and_after_snapshots"),
    ("approve", "audit_carries_actor_and_timestamp"),
    ("approve", "proposal_row_is_approved"),
    ("approve", "proposal_reachable_by_approved_status_index"),
    ("approve_twice", "refused_with_conflict"),
    ("approve_twice", "left_neither_half_behind"),
    ("revert", "restores_the_pre_approval_row"),
    ("revert", "audit_record_marked_reversed"),
    ("revert_twice", "refused_with_conflict"),
    ("revert_twice", "restored_nothing_a_second_time"),
)

_STALE_LEG_CHECKS: Final[tuple[tuple[str, str], ...]] = (
    ("stale_approval", "refused_with_conflict"),
    ("stale_approval", "conflict_reason_is_fields_changed"),
    ("stale_approval", "target_unchanged_by_the_refusal"),
    ("stale_approval", "proposal_still_pending"),
    ("stale_approval", "refusal_wrote_no_audit_record"),
)

_REJECT_LEG_CHECKS: Final[tuple[tuple[str, str], ...]] = (
    ("reject", "proposal_row_is_rejected"),
    ("reject", "target_mutates_nothing"),
    ("reject_twice", "refused_with_conflict"),
    ("reject", "approval_after_rejection_refused_with_conflict"),
)

_DELETE_LEG_CHECKS: Final[tuple[tuple[str, str], ...]] = (
    ("delete_proposal", "accepted_on_an_unreferenced_contact"),
    ("delete_proposal", "proposal_row_records_the_delete"),
    ("delete_proposal", "target_mutates_nothing"),
    ("delete_proposal", "wrote_no_audit_record"),
    ("delete_proposal", "refused_on_an_account_with_an_open_opportunity"),
)


_PAYLOAD_ALLOWLIST_LEG_CHECKS: Final[tuple[tuple[str, str], ...]] = (
    ("payload_allowlist", "key_redirection_refused"),
    ("payload_allowlist", "re_tenanting_refused"),
    ("payload_allowlist", "derived_attribute_supply_refused"),
    ("payload_allowlist", "redirect_victim_row_unchanged"),
    ("payload_allowlist", "declared_target_unchanged"),
    ("payload_allowlist", "no_proposal_stored_for_a_refused_payload"),
    ("payload_allowlist", "permitted_union_payload_accepted"),
)


_NOTE_REVERT_LEG_CHECKS: Final[tuple[tuple[str, str], ...]] = (
    ("note_revert", "create_note_wrote_one_after_only_audit"),
    ("note_revert", "revert_succeeds_in_scope"),
    ("note_revert", "note_is_deleted"),
    ("note_revert", "audit_record_marked_reversed"),
)


_DELETE_REVERT_LEG_CHECKS: Final[tuple[tuple[str, str], ...]] = (
    ("delete_revert", "approved_delete_removes_the_record"),
    ("delete_revert", "delete_wrote_one_before_only_audit"),
    ("delete_revert", "revert_succeeds_in_scope"),
    ("delete_revert", "record_restored_from_the_before_snapshot"),
    ("delete_revert", "audit_record_marked_reversed"),
)


_OUT_OF_BOOK_REVERT_LEG_CHECKS: Final[tuple[tuple[str, str], ...]] = (
    ("out_of_book_revert", "fixture_seeded_outside_the_book"),
    ("out_of_book_revert", "note_revert_refused_with_scope_denied"),
    ("out_of_book_revert", "note_revert_changed_nothing"),
    ("out_of_book_revert", "delete_revert_refused_with_scope_denied"),
    ("out_of_book_revert", "delete_revert_changed_nothing"),
)

#: The rep the seeded out-of-book audit records name as their author. Never a real rep.
_OUT_OF_BOOK_AUTHOR: Final[str] = "smoke-out-of-book-author"


def _skip_leg(leg: tuple[tuple[str, str], ...], detail: str) -> Iterable[CheckResult]:
    """Yield `skip` for every assertion in ``leg``, naming the failed prerequisite."""
    for resource, check in leg:
        yield CheckResult(resource=resource, check=check, status="skip", detail=detail)


def _leg_from(
    leg: tuple[tuple[str, str], ...], resource: str, check: str
) -> tuple[tuple[str, str], ...]:
    """Return the tail of ``leg`` starting at ``(resource, check)``.

    Every prerequisite failure inside a leg skips "everything from this assertion onward".

    Raises:
        KeyError: ``leg`` declares no such assertion, which would mean a skip covering nothing.
    """
    for index, entry in enumerate(leg):
        if entry == (resource, check):
            return leg[index:]
    raise KeyError(f"{resource}:{check} is not declared in this leg")


def _approval_leg(  # noqa: C901, PLR0911, PLR0912, PLR0915 - one cohesive chain over shared state
    *, ctx: SmokeContext, caller: HttpCaller, domain: str, identity: SmokeIdentity
) -> Iterable[CheckResult]:
    """Submit -> nothing mutates -> approve -> atomic commit+audit -> revert once, refuse twice."""
    tenant = identity.tenant
    target_id, why = _create_target(
        caller=caller,
        domain=domain,
        identity=identity,
        step="approval-leg-target",
        name="Layer 5 proposal-chain approval target",
    )
    if target_id is None:
        yield CheckResult(
            resource="seed", check="approval_leg_target_created", status="fail", detail=why
        )
        yield from _skip_leg(_APPROVAL_LEG_CHECKS, f"target could not be created: {why}")
        return
    yield CheckResult(
        resource="seed",
        check="approval_leg_target_created",
        status="pass",
        detail=f"opportunity {target_id!r} created at stage {_CREATED_STAGE!r}",
    )

    before_submit = _read_opportunity(ctx, target_id)
    audits_before_submit = _audit_ids_for_target(ctx, tenant=tenant, target_id=target_id)

    proposal_id, why = _submit(
        caller=caller,
        domain=domain,
        identity=identity,
        step="approval-leg-submit",
        target_id=target_id,
        reason="Layer 5 smoke: proposal chain",
    )
    if proposal_id is None:
        yield CheckResult(resource="submit", check="http_call", status="fail", detail=why)
        yield from _skip_leg(_APPROVAL_LEG_CHECKS, f"submit_proposal failed: {why}")
        return
    yield CheckResult(
        resource="submit",
        check="http_call",
        status="pass",
        detail=f"proposal {proposal_id!r} created",
    )

    # -- assertion 1: a proposal mutates nothing ---------------------------------------------
    after_submit = _read_opportunity(ctx, target_id)
    yield _unchanged_check(
        resource="submit",
        check="proposal_mutates_nothing",
        before=before_submit,
        after=after_submit,
        what="target opportunity read back either side of submit_proposal",
    )
    audits_after_submit = _audit_ids_for_target(ctx, tenant=tenant, target_id=target_id)
    yield CheckResult(
        resource="submit",
        check="proposal_writes_no_audit_record",
        status="pass" if audits_after_submit == audits_before_submit else "fail",
        detail=(
            f"audit rows for the target: {len(audits_before_submit)} before submit, "
            f"{len(audits_after_submit)} after"
        ),
    )

    proposal_row = _read_proposal(ctx, proposal_id)
    pending_ok = (
        proposal_row is not None
        and proposal_row.get("status") == "pending"
        and proposal_row.get("tenant") == tenant
        and isinstance(proposal_row.get("before_snapshot"), Mapping)
    )
    yield CheckResult(
        resource="submit",
        check="proposal_row_is_pending",
        status="pass" if pending_ok else "fail",
        detail=(
            f"proposals row status={None if proposal_row is None else proposal_row.get('status')!r} "
            f"tenant={None if proposal_row is None else proposal_row.get('tenant')!r} "
            f"before_snapshot "
            f"{'present' if proposal_row and proposal_row.get('before_snapshot') else 'ABSENT'}"
        ),
    )

    # -- assertion 2: approval commits and audits atomically ----------------------------------
    before_approve = _read_opportunity(ctx, target_id)
    audits_before_approve = _audit_ids_for_target(ctx, tenant=tenant, target_id=target_id)
    approve_call, why = _ok_call(
        caller,
        domain,
        BY_NAME["approve_proposal"].name,
        identity,
        {
            "proposal_id": proposal_id,
            "transaction_key": _make_transaction_key(tenant, "approval-leg-approve"),
        },
    )
    if approve_call is None:
        yield CheckResult(resource="approve", check="http_call", status="fail", detail=why)
        yield from _skip_leg(
            _leg_from(_APPROVAL_LEG_CHECKS, "approve", "commits_the_change"),
            f"approve_proposal failed: {why}",
        )
        return
    yield CheckResult(
        resource="approve", check="http_call", status="pass", detail="approve_proposal returned 200"
    )

    after_approve = _read_opportunity(ctx, target_id)
    change_committed = after_approve is not None and after_approve.get("stage") == _PROPOSED_STAGE
    yield CheckResult(
        resource="approve",
        check="commits_the_change",
        status="pass" if change_committed else "fail",
        detail=(
            "target stage before="
            f"{None if before_approve is None else before_approve.get('stage')!r}"
            f" after={None if after_approve is None else after_approve.get('stage')!r}, "
            f"expected {_PROPOSED_STAGE!r}; the same row's derived index attributes read back as "
            f"tenant_stage={None if after_approve is None else after_approve.get('tenant_stage')!r} "
            "stage_close_date="
            f"{None if after_approve is None else after_approve.get('stage_close_date')!r} "
            "(asserted separately by derived_index_attributes_recomputed)"
        ),
    )
    yield _derived_index_attributes_check(ctx, tenant=tenant, row=after_approve)

    audits_after_approve = _audit_ids_for_target(ctx, tenant=tenant, target_id=target_id)
    new_audit_ids = [a for a in audits_after_approve if a not in audits_before_approve]
    audit_written = len(new_audit_ids) == 1
    yield CheckResult(
        resource="approve",
        check="writes_exactly_one_audit_record",
        status="pass" if audit_written else "fail",
        detail=(
            f"{len(audits_before_approve)} audit row(s) before approval, "
            f"{len(audits_after_approve)} after; {len(new_audit_ids)} new"
        ),
    )
    yield _atomicity_check(change_committed=change_committed, audit_written=audit_written)

    if not (change_committed and audit_written):
        yield from _skip_leg(
            _leg_from(_APPROVAL_LEG_CHECKS, "approve", "audit_carries_before_and_after_snapshots"),
            "approval did not both commit the change and write exactly one audit record",
        )
        return

    audit_id = new_audit_ids[0]
    audit_row = _read_audit(ctx, audit_id)
    if audit_row is None:
        yield from _skip_leg(
            _leg_from(_APPROVAL_LEG_CHECKS, "approve", "audit_carries_before_and_after_snapshots"),
            f"audit row {audit_id!r} was indexed but could not be read back by primary key",
        )
        return

    yield _audit_snapshots_check(
        audit_row=audit_row, before_approve=before_approve, after_approve=after_approve
    )
    yield _audit_actor_and_timestamp_check(audit_row=audit_row, identity=identity)

    approved_row = _read_proposal(ctx, proposal_id)
    yield CheckResult(
        resource="approve",
        check="proposal_row_is_approved",
        status="pass"
        if approved_row is not None and approved_row.get("status") == "approved"
        else "fail",
        detail=(
            "proposals row status="
            f"{None if approved_row is None else approved_row.get('status')!r}, expected 'approved'"
        ),
    )
    yield _proposal_status_index_check(ctx, tenant=tenant, proposal_id=proposal_id)

    # The negative half of the atomicity claim: a refused approval must leave neither half.
    second_approve = _call_tool(
        caller,
        domain,
        BY_NAME["approve_proposal"].name,
        identity.token,
        {
            "proposal_id": proposal_id,
            "transaction_key": _make_transaction_key(tenant, "approval-leg-approve-2"),
        },
    )
    yield _refused_with_conflict(
        second_approve,
        resource="approve_twice",
        check="refused_with_conflict",
        what="approving an already-approved proposal",
    )
    after_second_approve = _read_opportunity(ctx, target_id)
    audits_after_second = _audit_ids_for_target(ctx, tenant=tenant, target_id=target_id)
    both_halves_absent = (
        canonical_row_bytes(after_second_approve) == canonical_row_bytes(after_approve)
        and audits_after_second == audits_after_approve
    )
    yield CheckResult(
        resource="approve_twice",
        check="left_neither_half_behind",
        status="pass" if both_halves_absent else "fail",
        detail=(
            f"after the refused approval: target {row_fingerprint(after_second_approve)} "
            f"(was {row_fingerprint(after_approve)}), {len(audits_after_second)} audit row(s) "
            f"(was {len(audits_after_approve)})"
        ),
    )

    # -- assertion 4: revert restores once and refuses twice ----------------------------------
    revert_call, why = _ok_call(
        caller,
        domain,
        BY_NAME["revert_audit_record"].name,
        identity,
        {
            "audit_id": audit_id,
            "transaction_key": _make_transaction_key(tenant, "approval-leg-revert-1"),
        },
    )
    if revert_call is None:
        yield CheckResult(
            resource="revert", check="first_revert_http_call", status="fail", detail=why
        )
        yield from _skip_leg(
            _leg_from(_APPROVAL_LEG_CHECKS, "revert", "restores_the_pre_approval_row"),
            f"the first revert failed: {why}",
        )
        return
    yield CheckResult(
        resource="revert",
        check="first_revert_http_call",
        status="pass",
        detail="revert_audit_record returned 200",
    )

    after_revert = _read_opportunity(ctx, target_id)
    yield _identical_check(
        resource="revert",
        check="restores_the_pre_approval_row",
        left=before_approve,
        right=after_revert,
        what="target opportunity after revert vs its pre-approval state",
    )
    reverted_audit = _read_audit(ctx, audit_id)
    reversed_ok = (
        reverted_audit is not None
        and reverted_audit.get("status") == "reversed"
        and bool(reverted_audit.get("reversed_at"))
        and reverted_audit.get("reversed_by") == identity.rep_id
    )
    yield CheckResult(
        resource="revert",
        check="audit_record_marked_reversed",
        status="pass" if reversed_ok else "fail",
        detail=(
            f"audit row status={None if reverted_audit is None else reverted_audit.get('status')!r} "
            f"reversed_at={None if reverted_audit is None else reverted_audit.get('reversed_at')!r} "
            f"reversed_by={None if reverted_audit is None else reverted_audit.get('reversed_by')!r}"
            f" (expected 'reversed', a timestamp, and {identity.rep_id!r})"
        ),
    )

    second_revert = _call_tool(
        caller,
        domain,
        BY_NAME["revert_audit_record"].name,
        identity.token,
        {
            "audit_id": audit_id,
            "transaction_key": _make_transaction_key(tenant, "approval-leg-revert-2"),
        },
    )
    yield _refused_with_conflict(
        second_revert,
        resource="revert_twice",
        check="refused_with_conflict",
        what="reverting an already-reverted audit record",
    )
    after_second_revert = _read_opportunity(ctx, target_id)
    audit_after_second_revert = _read_audit(ctx, audit_id)
    unchanged = canonical_row_bytes(after_second_revert) == canonical_row_bytes(after_revert)
    timestamp_untouched = (
        audit_after_second_revert is not None
        and reverted_audit is not None
        and audit_after_second_revert.get("reversed_at") == reverted_audit.get("reversed_at")
    )
    yield CheckResult(
        resource="revert_twice",
        check="restored_nothing_a_second_time",
        status="pass" if unchanged and timestamp_untouched else "fail",
        detail=(
            f"target after the refused second revert {row_fingerprint(after_second_revert)} "
            f"(was {row_fingerprint(after_revert)}); audit reversed_at "
            f"{'unchanged' if timestamp_untouched else 'OVERWRITTEN'}"
        ),
    )


def _derived_index_attributes_check(
    ctx: SmokeContext, *, tenant: str, row: Mapping[str, Any] | None
) -> CheckResult:
    """Assert the committed row's derived index keys name the post-approval stage.

    The composites are compared against what :func:`crm.store.keys.derive` computes for the
    committed row, and `gsi_stage` is queried for the new stage and for the old one — the new stage
    must return this row and the old stage must not.
    """
    if row is None:
        return CheckResult(
            resource="approve",
            check="derived_index_attributes_recomputed",
            status="fail",
            detail="the committed row could not be read back, so nothing was observed",
        )
    expected = derive(_OPPORTUNITY_TABLE, row)
    mismatched = {
        name: (row.get(name), expected.get(name))
        for name in sorted(derived_attribute_names(_OPPORTUNITY_TABLE))
        if row.get(name) != expected.get(name)
    }
    try:
        at_new_stage = _stage_index_ids(ctx, tenant=tenant, stage=_PROPOSED_STAGE)
        at_old_stage = _stage_index_ids(ctx, tenant=tenant, stage=_CREATED_STAGE)
    except Exception as exc:  # noqa: BLE001 - an unreadable index is a failure, never a skip
        return CheckResult(
            resource="approve",
            check="derived_index_attributes_recomputed",
            status="fail",
            detail=f"gsi_stage could not be queried: {type(exc).__name__}: {exc}",
        )
    target_id = str(row.get("opportunity_id"))
    indexed_correctly = target_id in at_new_stage and target_id not in at_old_stage
    if mismatched or not indexed_correctly:
        return CheckResult(
            resource="approve",
            check="derived_index_attributes_recomputed",
            status="fail",
            detail=(
                f"the approved row's derived index attributes are stale: {mismatched!r} "
                f"(observed, expected); gsi_stage {_PROPOSED_STAGE!r} returned "
                f"{sorted(at_new_stage)!r} and {_CREATED_STAGE!r} returned {sorted(at_old_stage)!r}"
            ),
        )
    return CheckResult(
        resource="approve",
        check="derived_index_attributes_recomputed",
        status="pass",
        detail=(
            f"tenant_stage={row.get('tenant_stage')!r} "
            f"stage_close_date={row.get('stage_close_date')!r} both name the committed stage "
            f"{_PROPOSED_STAGE!r}; gsi_stage returns the row under {_PROPOSED_STAGE!r} "
            f"({len(at_new_stage)} row(s)) and not under {_CREATED_STAGE!r} "
            f"({len(at_old_stage)} row(s))"
        ),
    )


def _stage_index_ids(ctx: SmokeContext, *, tenant: str, stage: str) -> set[str]:
    """Return every opportunity id `gsi_stage` reports for ``tenant`` at ``stage``."""
    found: set[str] = set()
    cursor: dict[str, Any] | None = None
    while True:
        items, cursor = _store(ctx).query_gsi(
            _OPPORTUNITY_TABLE,
            "gsi_stage",
            partition_value=tenant_prefixed(tenant, stage),
            exclusive_start_key=cursor,
        )
        found.update(str(item["opportunity_id"]) for item in items)
        if cursor is None:
            return found


def _proposal_status_index_check(
    ctx: SmokeContext, *, tenant: str, proposal_id: str
) -> CheckResult:
    """Assert the approved proposal is reachable on `gsi_status` under `approved`.

    `list_proposals(status=...)` queries `proposals.gsi_status`, whose partition key is the derived
    `tenant_status`, which the approval's `UpdateExpression` must assign.
    """
    try:
        approved = _proposals_by_status(ctx, tenant=tenant, status="approved")
        pending = _proposals_by_status(ctx, tenant=tenant, status="pending")
    except Exception as exc:  # noqa: BLE001 - an unreadable index is a failure, never a skip
        return CheckResult(
            resource="approve",
            check="proposal_reachable_by_approved_status_index",
            status="fail",
            detail=f"gsi_status could not be queried: {type(exc).__name__}: {exc}",
        )
    if proposal_id not in approved or proposal_id in pending:
        return CheckResult(
            resource="approve",
            check="proposal_reachable_by_approved_status_index",
            status="fail",
            detail=(
                f"proposal {proposal_id!r} is indexed under approved={proposal_id in approved} "
                f"pending={proposal_id in pending}; gsi_status 'approved' returned "
                f"{len(approved)} row(s) and 'pending' returned {len(pending)} row(s)"
            ),
        )
    return CheckResult(
        resource="approve",
        check="proposal_reachable_by_approved_status_index",
        status="pass",
        detail=(
            f"proposal {proposal_id!r} is reachable on gsi_status under 'approved' and absent "
            "under 'pending', so a status-filtered list_proposals reports it correctly"
        ),
    )


def _proposals_by_status(ctx: SmokeContext, *, tenant: str, status: str) -> set[str]:
    """Return every proposal id `gsi_status` reports for ``tenant`` at ``status``."""
    found: set[str] = set()
    cursor: dict[str, Any] | None = None
    while True:
        items, cursor = _store(ctx).query_gsi(
            _PROPOSALS_TABLE,
            "gsi_status",
            partition_value=tenant_prefixed(tenant, status),
            exclusive_start_key=cursor,
        )
        found.update(str(item["proposal_id"]) for item in items)
        if cursor is None:
            return found


def _atomicity_check(*, change_committed: bool, audit_written: bool) -> CheckResult:
    """Assert approval committed the change AND wrote the audit record - never only one."""
    resource, check = "approve", "commit_and_audit_are_atomic"
    if change_committed and audit_written:
        return CheckResult(
            resource=resource,
            check=check,
            status="pass",
            detail="both the committed change and its audit record are present",
        )
    if change_committed:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail="HALF-COMMITTED: the target record changed but no audit record was written",
        )
    if audit_written:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail="HALF-COMMITTED: an audit record was written but the target record did not change",
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="fail",
        detail="approval returned 200 but committed neither the change nor an audit record",
    )


def _audit_snapshots_check(
    *,
    audit_row: Mapping[str, Any],
    before_approve: Mapping[str, Any] | None,
    after_approve: Mapping[str, Any] | None,
) -> CheckResult:
    """Assert the audit record's snapshots byte-match the target row either side of the approval."""
    resource, check = "approve", "audit_carries_before_and_after_snapshots"
    before_snapshot = audit_row.get("before_snapshot")
    after_snapshot = audit_row.get("after_snapshot")
    if not isinstance(before_snapshot, Mapping) or not isinstance(after_snapshot, Mapping):
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"audit record carries before_snapshot={type(before_snapshot).__name__} and "
                f"after_snapshot={type(after_snapshot).__name__}; both must be mappings"
            ),
        )
    before_matches = canonical_row_bytes(before_snapshot) == canonical_row_bytes(before_approve)
    after_matches = canonical_row_bytes(after_snapshot) == canonical_row_bytes(after_approve)
    if before_matches and after_matches:
        return CheckResult(
            resource=resource,
            check=check,
            status="pass",
            detail=(
                f"before_snapshot {row_fingerprint(before_snapshot)} matches the pre-approval row; "
                f"after_snapshot {row_fingerprint(after_snapshot)} matches the committed row"
            ),
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="fail",
        detail=(
            f"before_snapshot {row_fingerprint(before_snapshot)} vs pre-approval row "
            f"{row_fingerprint(before_approve)} ({'match' if before_matches else 'MISMATCH'}); "
            f"after_snapshot {row_fingerprint(after_snapshot)} vs committed row "
            f"{row_fingerprint(after_approve)} ({'match' if after_matches else 'MISMATCH'})"
        ),
    )


def _audit_actor_and_timestamp_check(
    *, audit_row: Mapping[str, Any], identity: SmokeIdentity
) -> CheckResult:
    """Assert the audit record names the acting rep and carries a parseable UTC timestamp."""
    resource, check = "approve", "audit_carries_actor_and_timestamp"
    actor = str(audit_row.get("actor_rep_id") or "")
    created_at = str(audit_row.get("created_at") or "")
    actor_ok = actor == identity.rep_id if identity.rep_id else bool(actor)
    try:
        parsed = datetime.fromisoformat(created_at)
        timestamp_ok = parsed.tzinfo is not None or created_at.endswith("Z")
    except ValueError:
        timestamp_ok = False
    if actor_ok and timestamp_ok:
        return CheckResult(
            resource=resource,
            check=check,
            status="pass",
            detail=f"actor_rep_id={actor!r} created_at={created_at!r}",
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="fail",
        detail=(
            f"actor_rep_id={actor!r} (expected {identity.rep_id!r}, "
            f"{'ok' if actor_ok else 'MISMATCH'}); created_at={created_at!r} "
            f"({'parseable' if timestamp_ok else 'NOT A PARSEABLE TIMESTAMP'})"
        ),
    )


def _stale_leg(  # noqa: C901 - one cohesive probe over shared state
    *, ctx: SmokeContext, caller: HttpCaller, domain: str, identity: SmokeIdentity
) -> Iterable[CheckResult]:
    """Mutate a target out from under its own pending proposal, then approve it.

    Runs against its own target record, separate from the approval leg's.
    """
    tenant = identity.tenant
    target_id, why = _create_target(
        caller=caller,
        domain=domain,
        identity=identity,
        step="stale-leg-target",
        name="Layer 5 proposal-chain stale-snapshot target",
    )
    if target_id is None:
        yield CheckResult(
            resource="stale_approval", check="setup_target", status="fail", detail=why
        )
        yield from _skip_leg(_STALE_LEG_CHECKS, f"target could not be created: {why}")
        return

    proposal_id, why = _submit(
        caller=caller,
        domain=domain,
        identity=identity,
        step="stale-leg-submit",
        target_id=target_id,
        reason="Layer 5 smoke: stale-snapshot conflict probe",
        extra_payload={"name": "Layer 5 proposal-chain target (proposed name)"},
    )
    if proposal_id is None:
        yield CheckResult(
            resource="stale_approval", check="setup_proposal", status="fail", detail=why
        )
        yield from _skip_leg(_STALE_LEG_CHECKS, f"submit_proposal failed: {why}")
        return

    # `name` is on `crm.domain.allowlist_for("opportunities")`; `stage` is not, so this low-risk
    # write can change the record without itself being a high-risk mutation. The proposal also
    # touches `name`, so the change is to a field it writes.
    mutation_call, why = _ok_call(
        caller,
        domain,
        BY_NAME["update_opportunity"].name,
        identity,
        {
            "opportunity_id": target_id,
            "fields": {"name": "Layer 5 proposal-chain target (mutated to go stale)"},
            "transaction_key": _make_transaction_key(tenant, "stale-leg-mutation"),
        },
    )
    if mutation_call is None:
        yield CheckResult(
            resource="stale_approval", check="setup_stale_mutation", status="fail", detail=why
        )
        yield from _skip_leg(_STALE_LEG_CHECKS, f"the target could not be made stale: {why}")
        return
    yield CheckResult(
        resource="stale_approval",
        check="setup_stale_mutation",
        status="pass",
        detail=(
            f"target {target_id!r} mutated through update_opportunity after proposal "
            f"{proposal_id!r} captured its before-snapshot"
        ),
    )

    before_stale_approve = _read_opportunity(ctx, target_id)
    audits_before = _audit_ids_for_target(ctx, tenant=tenant, target_id=target_id)
    stale_result = _call_tool(
        caller,
        domain,
        BY_NAME["approve_proposal"].name,
        identity.token,
        {
            "proposal_id": proposal_id,
            "transaction_key": _make_transaction_key(tenant, "stale-leg-approve"),
        },
    )
    yield _refused_with_conflict(
        stale_result,
        resource="stale_approval",
        check="refused_with_conflict",
        what="approving a proposal whose before-snapshot is stale",
    )
    stale_reason = None if isinstance(stale_result, str) else _error_reason(stale_result[1])
    yield CheckResult(
        resource="stale_approval",
        check="conflict_reason_is_fields_changed",
        status="pass" if stale_reason == "fields_changed" else "fail",
        detail=f"error.reason={stale_reason!r}, expected 'fields_changed'",
    )
    after_stale_approve = _read_opportunity(ctx, target_id)
    yield _unchanged_check(
        resource="stale_approval",
        check="target_unchanged_by_the_refusal",
        before=before_stale_approve,
        after=after_stale_approve,
        what="stale target read back either side of the refused approval",
    )
    still_pending = _read_proposal(ctx, proposal_id)
    yield CheckResult(
        resource="stale_approval",
        check="proposal_still_pending",
        status="pass"
        if still_pending is not None and still_pending.get("status") == "pending"
        else "fail",
        detail=(
            "proposals row status="
            f"{None if still_pending is None else still_pending.get('status')!r} after the refused "
            "approval, expected 'pending'"
        ),
    )
    audits_after = _audit_ids_for_target(ctx, tenant=tenant, target_id=target_id)
    yield CheckResult(
        resource="stale_approval",
        check="refusal_wrote_no_audit_record",
        status="pass" if audits_after == audits_before else "fail",
        detail=(
            f"audit rows for the stale target: {len(audits_before)} before the refused approval, "
            f"{len(audits_after)} after"
        ),
    )


def _reject_leg(
    *, ctx: SmokeContext, caller: HttpCaller, domain: str, identity: SmokeIdentity
) -> Iterable[CheckResult]:
    """Reject a pending proposal: terminal, mutates nothing, and forecloses approval."""
    tenant = identity.tenant
    target_id, why = _create_target(
        caller=caller,
        domain=domain,
        identity=identity,
        step="reject-leg-target",
        name="Layer 5 proposal-chain reject target",
    )
    if target_id is None:
        yield CheckResult(resource="reject", check="setup_target", status="fail", detail=why)
        yield from _skip_leg(_REJECT_LEG_CHECKS, f"target could not be created: {why}")
        return

    before_submit = _read_opportunity(ctx, target_id)
    proposal_id, why = _submit(
        caller=caller,
        domain=domain,
        identity=identity,
        step="reject-leg-submit",
        target_id=target_id,
        reason="Layer 5 smoke: reject probe",
    )
    if proposal_id is None:
        yield CheckResult(resource="reject", check="setup_proposal", status="fail", detail=why)
        yield from _skip_leg(_REJECT_LEG_CHECKS, f"submit_proposal failed: {why}")
        return

    reject_call, why = _ok_call(
        caller,
        domain,
        BY_NAME["reject_proposal"].name,
        identity,
        {
            "proposal_id": proposal_id,
            "reason": "Layer 5 smoke: rejected on purpose",
            "transaction_key": _make_transaction_key(tenant, "reject-leg-reject"),
        },
    )
    if reject_call is None:
        yield CheckResult(resource="reject", check="http_call", status="fail", detail=why)
        yield from _skip_leg(_REJECT_LEG_CHECKS, f"reject_proposal failed: {why}")
        return
    yield CheckResult(
        resource="reject", check="http_call", status="pass", detail="reject_proposal returned 200"
    )

    rejected_row = _read_proposal(ctx, proposal_id)
    yield CheckResult(
        resource="reject",
        check="proposal_row_is_rejected",
        status="pass"
        if rejected_row is not None and rejected_row.get("status") == "rejected"
        else "fail",
        detail=(
            "proposals row status="
            f"{None if rejected_row is None else rejected_row.get('status')!r}, expected 'rejected'"
        ),
    )
    yield _unchanged_check(
        resource="reject",
        check="target_mutates_nothing",
        before=before_submit,
        after=_read_opportunity(ctx, target_id),
        what="reject target read back either side of submit+reject",
    )

    yield _refused_with_conflict(
        _call_tool(
            caller,
            domain,
            BY_NAME["reject_proposal"].name,
            identity.token,
            {
                "proposal_id": proposal_id,
                "transaction_key": _make_transaction_key(tenant, "reject-leg-reject-2"),
            },
        ),
        resource="reject_twice",
        check="refused_with_conflict",
        what="rejecting an already-rejected proposal",
    )
    yield _refused_with_conflict(
        _call_tool(
            caller,
            domain,
            BY_NAME["approve_proposal"].name,
            identity.token,
            {
                "proposal_id": proposal_id,
                "transaction_key": _make_transaction_key(tenant, "reject-leg-approve"),
            },
        ),
        resource="reject",
        check="approval_after_rejection_refused_with_conflict",
        what="approving a rejected proposal",
    )


def _create_contact(
    *, caller: HttpCaller, domain: str, identity: SmokeIdentity, step: str, name: str
) -> tuple[str | None, str]:
    """Create one contact under the acting identity's own in-book account. Returns `(id, why)`."""
    call, why = _ok_call(
        caller,
        domain,
        BY_NAME["create_contact"].name,
        identity,
        {
            "account_id": identity.account_id,
            "name": name,
            "transaction_key": _make_transaction_key(identity.tenant, step),
        },
    )
    if call is None:
        return None, why
    contact = call.payload.get("contact")
    if not (isinstance(contact, dict) and isinstance(contact.get("contact_id"), str)):
        return None, f"response body carried no contact id: {call.body[:300]}"
    return str(contact["contact_id"]), ""


def _submit_delete(
    *,
    caller: HttpCaller,
    domain: str,
    identity: SmokeIdentity,
    step: str,
    target_table: str,
    target_id: str,
) -> HttpResult:
    """Submit one delete proposal against ``target_id``, returning the raw HTTP result."""
    return _call_tool(
        caller,
        domain,
        BY_NAME["submit_proposal"].name,
        identity.token,
        {
            "target_table": target_table,
            "target_id": target_id,
            "operation": "delete",
            "reason": "Layer 5 smoke: delete-proposal cascade probe",
            "transaction_key": _make_transaction_key(identity.tenant, step),
        },
    )


def _delete_leg(  # noqa: C901 - one cohesive probe over shared state
    *, ctx: SmokeContext, caller: HttpCaller, domain: str, identity: SmokeIdentity
) -> Iterable[CheckResult]:
    """Exercise the delete-proposal cascade query in both of its outcomes.

    `submit_proposal(operation="delete")` on a contact runs
    `crm.handlers.cascade_rules.reject_contact_delete_if_referenced` and on an account
    `reject_account_delete_with_open_opportunities`; both query `opportunities.gsi_account`.

    * a contact nothing references is proposed for deletion successfully;
    * the account those opportunities belong to is refused with `validation_failed`.

    Both targets are the acting identity's own in-book records, since the token's `custom:book`
    claim is fixed at sign-in.
    """
    tenant = identity.tenant
    blocker_id, why = _create_target(
        caller=caller,
        domain=domain,
        identity=identity,
        step="delete-leg-open-opportunity",
        name="Layer 5 proposal-chain delete blocker",
    )
    if blocker_id is None:
        yield CheckResult(
            resource="delete_proposal", check="setup_open_opportunity", status="fail", detail=why
        )
        yield from _skip_leg(_DELETE_LEG_CHECKS, f"create_opportunity failed: {why}")
        return
    contact_id, why = _create_contact(
        caller=caller,
        domain=domain,
        identity=identity,
        step="delete-leg-contact",
        name="Layer 5 proposal-chain delete target",
    )
    if contact_id is None:
        yield CheckResult(
            resource="delete_proposal", check="setup_open_opportunity", status="fail", detail=why
        )
        yield from _skip_leg(_DELETE_LEG_CHECKS, f"create_contact failed: {why}")
        return
    yield CheckResult(
        resource="delete_proposal",
        check="setup_open_opportunity",
        status="pass",
        detail=(
            f"account {identity.account_id!r} now has open opportunity {blocker_id!r} at stage "
            f"{_CREATED_STAGE!r}, and unreferenced contact {contact_id!r}"
        ),
    )

    before_submit = _read_row(ctx, _CONTACTS_TABLE, {"contact_id": contact_id})
    audits_before = _audit_ids_for_target(ctx, tenant=tenant, target_id=contact_id)

    result = _submit_delete(
        caller=caller,
        domain=domain,
        identity=identity,
        step="delete-leg-submit",
        target_table=_CONTACTS_TABLE,
        target_id=contact_id,
    )
    if isinstance(result, str) or result[0] >= 400:  # noqa: PLR2004 - HTTP error boundary
        detail = result if isinstance(result, str) else f"status={result[0]} body={result[1][:400]}"
        yield CheckResult(
            resource="delete_proposal",
            check="accepted_on_an_unreferenced_contact",
            status="fail",
            detail=(
                "a delete proposal on an unreferenced contact was refused: "
                f"{detail}. A 401 authentication_failed here is the missing-`dynamodb:Query`-grant "
                "shape (crm.errors._AWS_CODE_MAP maps AccessDeniedException onto it)"
            ),
        )
        yield from _skip_leg(
            _leg_from(_DELETE_LEG_CHECKS, "delete_proposal", "proposal_row_records_the_delete"),
            f"the accepted delete proposal never happened: {detail}",
        )
        return
    payload = _parse_json_body(result[1]) or {}
    proposal = payload.get("proposal")
    proposal_id = (
        str(proposal["proposal_id"])
        if isinstance(proposal, dict) and isinstance(proposal.get("proposal_id"), str)
        else None
    )
    yield CheckResult(
        resource="delete_proposal",
        check="accepted_on_an_unreferenced_contact",
        status="pass" if proposal_id else "fail",
        detail=(
            "submit_proposal(operation='delete', target_table='contacts') returned 200 and created "
            f"proposal {proposal_id!r}; the cascade query over opportunities.gsi_account therefore "
            "ran under a grant that permits it"
            if proposal_id
            else f"200 but no proposal id in the body: {result[1][:300]}"
        ),
    )

    proposal_row = _read_proposal(ctx, proposal_id) if proposal_id else None
    records_delete = (
        proposal_row is not None
        and proposal_row.get("operation") == "delete"
        and proposal_row.get("status") == "pending"
        and proposal_row.get("target_table") == _CONTACTS_TABLE
        and proposal_row.get("target_id") == contact_id
        and proposal_row.get("after_payload") is None
    )
    yield CheckResult(
        resource="delete_proposal",
        check="proposal_row_records_the_delete",
        status="pass" if records_delete else "fail",
        detail=(
            "proposals row operation="
            f"{None if proposal_row is None else proposal_row.get('operation')!r} status="
            f"{None if proposal_row is None else proposal_row.get('status')!r} target="
            f"{None if proposal_row is None else proposal_row.get('target_table')!r}:"
            f"{None if proposal_row is None else proposal_row.get('target_id')!r} after_payload="
            f"{None if proposal_row is None else proposal_row.get('after_payload')!r}, expected a "
            "pending delete against this contact carrying no after_payload"
        ),
    )
    yield _unchanged_check(
        resource="delete_proposal",
        check="target_mutates_nothing",
        before=before_submit,
        after=_read_row(ctx, _CONTACTS_TABLE, {"contact_id": contact_id}),
        what="delete-proposal target contact read back either side of submit_proposal",
    )
    audits_after = _audit_ids_for_target(ctx, tenant=tenant, target_id=contact_id)
    yield CheckResult(
        resource="delete_proposal",
        check="wrote_no_audit_record",
        status="pass" if audits_after == audits_before else "fail",
        detail=(
            f"{len(audits_before)} audit row(s) for this contact before the delete proposal, "
            f"{len(audits_after)} after; a proposal commits nothing and so audits nothing"
        ),
    )

    yield _refused_with_validation_failed(
        _submit_delete(
            caller=caller,
            domain=domain,
            identity=identity,
            step="delete-leg-submit-blocked",
            target_table=_ACCOUNTS_TABLE,
            target_id=identity.account_id,
        ),
        resource="delete_proposal",
        check="refused_on_an_account_with_an_open_opportunity",
        what=(
            f"a delete proposal on account {identity.account_id!r}, which has open opportunity "
            f"{blocker_id!r} at stage {_CREATED_STAGE!r}"
        ),
    )


def _submit_payload(
    *,
    caller: HttpCaller,
    domain: str,
    identity: SmokeIdentity,
    step: str,
    target_id: str,
    after_payload: Mapping[str, Any],
) -> HttpResult:
    """Submit one update proposal carrying an arbitrary ``after_payload``, returning the raw result.

    Unlike :func:`_submit` this does not assume success.
    """
    return _call_tool(
        caller,
        domain,
        BY_NAME["submit_proposal"].name,
        identity.token,
        {
            "target_table": _OPPORTUNITY_TABLE,
            "target_id": target_id,
            "operation": "update",
            "after_payload": dict(after_payload),
            "reason": "Layer 5 smoke: after_payload allowlist probe",
            "transaction_key": _make_transaction_key(identity.tenant, step),
        },
    )


def _payload_allowlist_leg(
    *, ctx: SmokeContext, caller: HttpCaller, domain: str, identity: SmokeIdentity
) -> Iterable[CheckResult]:
    """Assert live what a proposal's ``after_payload`` may and may not carry.

    Two targets are created: the declared target the proposal names, and a second row the
    redirecting payload points at. Every probe must be refused, and both rows are read back
    byte-for-byte afterwards. A payload naming one field from `UPDATE_ALLOWLISTS` and one high-risk
    field must still be accepted.
    """
    tenant = identity.tenant
    target_id, why = _create_target(
        caller=caller,
        domain=domain,
        identity=identity,
        step="payload-leg-target",
        name="Layer 5 proposal-chain payload-allowlist declared target",
    )
    victim_id, victim_why = _create_target(
        caller=caller,
        domain=domain,
        identity=identity,
        step="payload-leg-victim",
        name="Layer 5 proposal-chain payload-allowlist redirect target",
    )
    if target_id is None or victim_id is None:
        detail = why or victim_why
        yield CheckResult(
            resource="seed", check="payload_allowlist_targets_created", status="fail", detail=detail
        )
        yield from _skip_leg(
            _PAYLOAD_ALLOWLIST_LEG_CHECKS, f"targets could not be created: {detail}"
        )
        return
    yield CheckResult(
        resource="seed",
        check="payload_allowlist_targets_created",
        status="pass",
        detail=f"declared target {target_id!r}, redirect target {victim_id!r}",
    )

    target_before = _read_opportunity(ctx, target_id)
    victim_before = _read_opportunity(ctx, victim_id)
    proposals_before = _tenant_row_count(ctx, _PROPOSALS_TABLE, tenant)

    yield _refused_with_validation_failed(
        _submit_payload(
            caller=caller,
            domain=domain,
            identity=identity,
            step="payload-leg-redirect",
            target_id=target_id,
            after_payload={"opportunity_id": victim_id, "name": "REDIRECTED_BY_PAYLOAD"},
        ),
        resource="payload_allowlist",
        check="key_redirection_refused",
        what=(
            f"an update proposal declaring target {target_id!r} whose after_payload carries the "
            f"partition key of {victim_id!r}"
        ),
    )

    yield _refused_with_validation_failed(
        _submit_payload(
            caller=caller,
            domain=domain,
            identity=identity,
            step="payload-leg-retenant",
            target_id=target_id,
            after_payload={"tenant": DEMO_TENANT},
        ),
        resource="payload_allowlist",
        check="re_tenanting_refused",
        what=f"an after_payload moving {target_id!r} into the {DEMO_TENANT!r} tenant",
    )

    yield _refused_with_validation_failed(
        _submit_payload(
            caller=caller,
            domain=domain,
            identity=identity,
            step="payload-leg-derived",
            target_id=target_id,
            after_payload={"stage": _PROPOSED_STAGE, "tenant_stage": "poisoned"},
        ),
        resource="payload_allowlist",
        check="derived_attribute_supply_refused",
        what=f"an after_payload supplying the derived attribute tenant_stage on {target_id!r}",
    )

    yield _unchanged_check(
        resource="payload_allowlist",
        check="redirect_victim_row_unchanged",
        before=victim_before,
        after=_read_opportunity(ctx, victim_id),
        what=f"redirect target {victim_id!r} read back either side of all three refused payloads",
    )
    yield _unchanged_check(
        resource="payload_allowlist",
        check="declared_target_unchanged",
        before=target_before,
        after=_read_opportunity(ctx, target_id),
        what=f"declared target {target_id!r} read back either side of all three refused payloads",
    )

    proposals_after = _tenant_row_count(ctx, _PROPOSALS_TABLE, tenant)
    yield CheckResult(
        resource="payload_allowlist",
        check="no_proposal_stored_for_a_refused_payload",
        status="pass" if proposals_after == proposals_before else "fail",
        detail=(
            f"proposals rows under {tenant!r}: {proposals_before} before the three refused "
            f"payloads, {proposals_after} after"
        ),
    )

    accepted = _submit_payload(
        caller=caller,
        domain=domain,
        identity=identity,
        step="payload-leg-permitted",
        target_id=target_id,
        after_payload={"stage": _PROPOSED_STAGE, "description": "permitted-union probe"},
    )
    if isinstance(accepted, str):
        yield CheckResult(
            resource="payload_allowlist",
            check="permitted_union_payload_accepted",
            status="fail",
            detail=accepted,
        )
        return
    accepted_status, accepted_body = accepted
    yield CheckResult(
        resource="payload_allowlist",
        check="permitted_union_payload_accepted",
        status="pass" if accepted_status < 400 else "fail",  # noqa: PLR2004
        detail=(
            "after_payload naming the high-risk field 'stage' and the update-allowlisted field "
            f"'description' returned status={accepted_status}"
            + ("" if accepted_status < 400 else f" body={accepted_body[:300]}")  # noqa: PLR2004
        ),
    )


# -- hygiene ---------------------------------------------------------------------------------


def _demo_tenant_untouched_check(ctx: SmokeContext, demo_before: Mapping[str, int]) -> CheckResult:
    """Assert this run changed no `demo`-tenant row count in any table it writes.

    A delta rather than an absolute zero.
    """
    try:
        demo_after = {
            table: _tenant_row_count(ctx, table, DEMO_TENANT) for table in _DEMO_WATCHED_TABLES
        }
    except Exception as exc:  # noqa: BLE001 - an unobservable boundary is not a passing one
        return CheckResult(
            resource=RESOURCE_CLASS,
            check="demo_tenant_untouched",
            status="fail",
            detail=f"could not re-read the demo tenant's row counts: {type(exc).__name__}: {exc}",
        )
    drift = {
        t: (demo_before.get(t), demo_after[t])
        for t in demo_after
        if demo_before.get(t) != demo_after[t]
    }
    if drift:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check="demo_tenant_untouched",
            status="fail",
            detail=f"demo-tenant row counts changed during this run: {drift!r}",
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check="demo_tenant_untouched",
        status="pass",
        detail=f"demo-tenant row counts unchanged across {dict(demo_after)!r}",
    )


def _writes_only_under_smoke_tenant_check(
    recorded_calls: list[tuple[str, str, str]], tenant: str
) -> Iterable[CheckResult]:
    """Assert every recorded write carries the smoke tenant marker and never `demo`'s.

    Over the spy's recorded calls: every call body carrying a `transaction_key` must carry
    :func:`_tenant_marker`'s digest of this run's tenant, and never `demo`'s.
    """
    writes = [
        (method, url, body)
        for method, url, body in recorded_calls
        if (_parse_json_body(body) or {}).get("transaction_key")
    ]
    if not writes:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="writes_only_under_smoke_tenant",
            status="fail",
            detail=(
                "no write call was issued at all, so the tenant every write lands in was never "
                "observed; an unobserved boundary is not a passing one"
            ),
        )
        return

    demo_marker = f"{_tenant_marker(DEMO_TENANT)}-"
    smoke_marker = f"{_tenant_marker(tenant)}-"
    violations = []
    for _method, url, body in writes:
        transaction_key = str((_parse_json_body(body) or {}).get("transaction_key") or "")
        if demo_marker in transaction_key:
            violations.append(f"url={url} transaction_key={transaction_key!r} carries demo marker")
        elif not transaction_key.startswith(smoke_marker):
            violations.append(
                f"url={url} transaction_key={transaction_key!r} does not carry the smoke tenant"
            )
    if violations:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="writes_only_under_smoke_tenant",
            status="fail",
            detail="; ".join(violations),
        )
        return
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="writes_only_under_smoke_tenant",
        status="pass",
        detail=f"{len(writes)} write call(s) all carried the smoke tenant marker, none carried demo",
    )


def _created_row_keys(recorded_calls: list[tuple[str, str, str]]) -> dict[str, set[str]]:
    """Return the ids this run's write calls are known to have created, per logical table.

    Derived from the recorded call bodies: every `create_opportunity` names its target account and
    derives a deterministic id, and every proposal id is derived from the same `transaction_key` by
    `crm.handlers._common.idempotent_id`.
    """
    from crm.handlers._common import idempotent_id

    created: dict[str, set[str]] = {
        _CONTACTS_TABLE: set(),
        _OPPORTUNITY_TABLE: set(),
        _PROPOSALS_TABLE: set(),
    }
    for _method, url, body in recorded_calls:
        arguments = _parse_json_body(body) or {}
        transaction_key = str(arguments.get("transaction_key") or "")
        if not transaction_key:
            continue
        tool_name = url.rsplit("/", 1)[-1]
        if tool_name == "create_contact":
            created[_CONTACTS_TABLE].add(idempotent_id("cont", transaction_key))
        elif tool_name == "create_opportunity":
            created[_OPPORTUNITY_TABLE].add(idempotent_id("opp", transaction_key))
        elif tool_name == "submit_proposal":
            created[_PROPOSALS_TABLE].add(idempotent_id("prop", transaction_key))
    return created


def _teardown_removed_everything_checks(
    ctx: SmokeContext, tenant: str, created: Mapping[str, set[str]]
) -> Iterable[CheckResult]:
    """Assert teardown removed every row this run created, including proposals and audit records.

    Two independent proofs per table: every id this run is known to have created is absent on a
    primary-key read, and the tenant-wide index reports zero remaining rows. The audit table has
    no predictable id (`crm.audit.build_audit_record` mints a UUID4), so it is checked by the
    tenant-wide count alone.
    """
    for table in (_CONTACTS_TABLE, _OPPORTUNITY_TABLE, _PROPOSALS_TABLE):
        key_attr = {
            _CONTACTS_TABLE: "contact_id",
            _OPPORTUNITY_TABLE: "opportunity_id",
            _PROPOSALS_TABLE: "proposal_id",
        }[table]
        ids = sorted(created.get(table, set()))
        try:
            remaining = [i for i in ids if _read_row(ctx, table, {key_attr: i}) is not None]
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            yield CheckResult(
                resource="smoke_tenant",
                check=f"{table}_rows_removed",
                status="fail",
                detail=f"could not re-read {table} rows: {type(exc).__name__}: {exc}",
            )
            continue
        if not ids:
            yield CheckResult(
                resource="smoke_tenant",
                check=f"{table}_rows_removed",
                status="fail",
                detail=(
                    f"no {table} row id was recorded for this run, so teardown of that table was "
                    "never actually observed"
                ),
            )
            continue
        yield CheckResult(
            resource="smoke_tenant",
            check=f"{table}_rows_removed",
            status="fail" if remaining else "pass",
            detail=(
                f"{len(remaining)} of {len(ids)} {table} row(s) this run created still resolve by "
                f"primary key: {remaining!r}"
                if remaining
                else f"all {len(ids)} {table} row(s) this run created are absent on read-back"
            ),
        )

    for table in (
        _ACCOUNTS_TABLE,
        _CONTACTS_TABLE,
        _OPPORTUNITY_TABLE,
        _PROPOSALS_TABLE,
        _AUDIT_TABLE,
    ):
        try:
            count = _tenant_row_count(ctx, table, tenant)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            yield CheckResult(
                resource="smoke_tenant",
                check=f"{table}_empty_for_tenant",
                status="fail",
                detail=f"could not count {table} rows for {tenant!r}: {type(exc).__name__}: {exc}",
            )
            continue
        yield CheckResult(
            resource="smoke_tenant",
            check=f"{table}_empty_for_tenant",
            status="pass" if count == 0 else "fail",
            detail=f"{count} row(s) remain in {table} under tenant {tenant!r} after teardown",
        )


def _note_revert_leg(
    *, ctx: SmokeContext, caller: HttpCaller, domain: str, identity: SmokeIdentity
) -> Iterable[CheckResult]:
    """A note create is revertable: the revert deletes the note and marks its audit reversed.

    Exercises revert_audit_record's `dynamodb:DeleteItem` grant on `notes` with the application's
    own identity path; a missing grant surfaces as a failed revert call.
    """
    target_id, why = _create_target(
        caller=caller,
        domain=domain,
        identity=identity,
        step="note-revert-leg-target",
        name="Layer 5 proposal-chain note-revert target",
    )
    if target_id is None:
        yield from _skip_leg(_NOTE_REVERT_LEG_CHECKS, f"target could not be created: {why}")
        return
    note_call, why = _ok_call(
        caller,
        domain,
        BY_NAME["create_note"].name,
        identity,
        {
            "opportunity_id": target_id,
            "body": "Layer 5 note-revert leg",
            "transaction_key": _make_transaction_key(identity.tenant, "note-revert-leg-note"),
        },
    )
    note = note_call.payload.get("note") if note_call is not None else None
    note_id = note.get("note_id") if isinstance(note, dict) else None
    if not isinstance(note_id, str):
        yield from _skip_leg(_NOTE_REVERT_LEG_CHECKS, f"create_note failed: {why or 'no note_id'}")
        return
    audit_ids = _audit_ids_for_target(ctx, tenant=identity.tenant, target_id=note_id)
    audit = _read_audit(ctx, audit_ids[0]) if len(audit_ids) == 1 else None
    after_only = (
        audit is not None
        and audit.get("target_table") == _NOTES_TABLE
        and not audit.get("before_snapshot")
        and bool(audit.get("after_snapshot"))
    )
    yield CheckResult(
        resource="note_revert",
        check="create_note_wrote_one_after_only_audit",
        status="pass" if after_only else "fail",
        detail=f"{len(audit_ids)} audit row(s) for note {note_id}; expected one after-only notes audit",
    )
    if not after_only:
        yield from _skip_leg(
            _leg_from(_NOTE_REVERT_LEG_CHECKS, "note_revert", "revert_succeeds_in_scope"),
            "no single after-only audit record to revert",
        )
        return
    audit_id = audit_ids[0]
    revert_call, why = _ok_call(
        caller,
        domain,
        BY_NAME["revert_audit_record"].name,
        identity,
        {
            "audit_id": audit_id,
            "transaction_key": _make_transaction_key(identity.tenant, "note-revert-leg-revert"),
        },
    )
    yield CheckResult(
        resource="note_revert",
        check="revert_succeeds_in_scope",
        status="pass" if revert_call is not None else "fail",
        detail="revert_audit_record returned 200" if revert_call is not None else why,
    )
    if revert_call is None:
        yield from _skip_leg(
            _leg_from(_NOTE_REVERT_LEG_CHECKS, "note_revert", "note_is_deleted"),
            f"the revert failed: {why}",
        )
        return
    remaining = _read_row(ctx, _NOTES_TABLE, {"note_id": note_id})
    yield CheckResult(
        resource="note_revert",
        check="note_is_deleted",
        status="pass" if remaining is None else "fail",
        detail=f"note {note_id} {'is gone' if remaining is None else 'still exists after the revert'}",
    )
    reverted = _read_audit(ctx, audit_id)
    reversed_ok = (
        reverted is not None
        and reverted.get("status") == "reversed"
        and reverted.get("reversed_by") == identity.rep_id
    )
    yield CheckResult(
        resource="note_revert",
        check="audit_record_marked_reversed",
        status="pass" if reversed_ok else "fail",
        detail=f"audit status={None if reverted is None else reverted.get('status')!r}",
    )


def _delete_revert_leg(  # noqa: PLR0911 - one cohesive chain over shared state
    *, ctx: SmokeContext, caller: HttpCaller, domain: str, identity: SmokeIdentity
) -> Iterable[CheckResult]:
    """An approved delete is revertable: the revert restores the record and reverses the audit.

    Deletes an unreferenced in-book contact through submit/approve, then reverts the delete's
    before-only audit record with the application's own identity path.
    """
    leg = _DELETE_REVERT_LEG_CHECKS
    tenant = identity.tenant
    contact_id, why = _create_contact(
        caller=caller,
        domain=domain,
        identity=identity,
        step="delete-revert-leg-contact",
        name="Layer 5 proposal-chain delete-revert target",
    )
    if contact_id is None:
        yield from _skip_leg(leg, f"create_contact failed: {why}")
        return
    before = _read_row(ctx, _CONTACTS_TABLE, {"contact_id": contact_id})
    submitted = _submit_delete(
        caller=caller,
        domain=domain,
        identity=identity,
        step="delete-revert-leg-submit",
        target_table=_CONTACTS_TABLE,
        target_id=contact_id,
    )
    payload = (
        _parse_json_body(submitted[1]) or {}
        if not isinstance(submitted, str) and submitted[0] < 400  # noqa: PLR2004 - HTTP error boundary
        else {}
    )
    proposal = payload.get("proposal")
    proposal_id = proposal.get("proposal_id") if isinstance(proposal, dict) else None
    approved, why = (
        _ok_call(
            caller,
            domain,
            BY_NAME["approve_proposal"].name,
            identity,
            {
                "proposal_id": proposal_id,
                "transaction_key": _make_transaction_key(tenant, "delete-revert-leg-approve"),
            },
        )
        if isinstance(proposal_id, str)
        else (None, f"submit_proposal(operation='delete') gave no proposal: {submitted!r:.300}")
    )
    removed = _read_row(ctx, _CONTACTS_TABLE, {"contact_id": contact_id}) is None
    yield CheckResult(
        resource="delete_revert",
        check="approved_delete_removes_the_record",
        status="pass" if approved is not None and before is not None and removed else "fail",
        detail=(
            f"contact {contact_id} existed={before is not None}, approve ok={approved is not None}"
            f"{'' if approved is not None else f' ({why})'}, gone after approval={removed}"
        ),
    )
    if approved is None or not removed:
        yield from _skip_leg(leg[1:], "the delete did not commit")
        return
    audits = [
        _read_audit(ctx, a) for a in _audit_ids_for_target(ctx, tenant=tenant, target_id=contact_id)
    ]
    deletes = [
        a
        for a in audits
        if a is not None and a.get("before_snapshot") and not a.get("after_snapshot")
    ]
    yield CheckResult(
        resource="delete_revert",
        check="delete_wrote_one_before_only_audit",
        status="pass" if len(deletes) == 1 else "fail",
        detail=f"{len(deletes)} before-only audit row(s) of {len(audits)} for contact {contact_id}",
    )
    if len(deletes) != 1:
        yield from _skip_leg(leg[2:], "no single before-only audit record to revert")
        return
    audit_id = str(deletes[0]["audit_id"])
    reverted_call, why = _ok_call(
        caller,
        domain,
        BY_NAME["revert_audit_record"].name,
        identity,
        {
            "audit_id": audit_id,
            "transaction_key": _make_transaction_key(tenant, "delete-revert-leg-revert"),
        },
    )
    yield CheckResult(
        resource="delete_revert",
        check="revert_succeeds_in_scope",
        status="pass" if reverted_call is not None else "fail",
        detail="revert_audit_record returned 200" if reverted_call is not None else why,
    )
    if reverted_call is None:
        yield from _skip_leg(leg[3:], f"the revert failed: {why}")
        return
    restored = _read_row(ctx, _CONTACTS_TABLE, {"contact_id": contact_id})
    yield CheckResult(
        resource="delete_revert",
        check="record_restored_from_the_before_snapshot",
        status="pass"
        if restored is not None and restored == deletes[0]["before_snapshot"]
        else "fail",
        detail=(
            f"contact {contact_id} "
            + ("absent after the revert" if restored is None else "read back after the revert")
            + f"; equals the audit's before_snapshot={restored == deletes[0]['before_snapshot']}"
        ),
    )
    audit = _read_audit(ctx, audit_id)
    reversed_ok = (
        audit is not None
        and audit.get("status") == "reversed"
        and audit.get("reversed_by") == identity.rep_id
    )
    yield CheckResult(
        resource="delete_revert",
        check="audit_record_marked_reversed",
        status="pass" if reversed_ok else "fail",
        detail=f"audit status={None if audit is None else audit.get('status')!r}",
    )


def _out_of_book_account(ctx: SmokeContext, identity: SmokeIdentity) -> str:
    """An account in the tenant outside ``identity.book``; a fixture account is written if none is.

    Raises:
        RuntimeError: ``identity.book`` is empty, so no account can be shown to lie outside it.
    """
    if not identity.book:
        raise RuntimeError("the acting identity's book is unknown, so out-of-book is undefined")
    store = _store(ctx)
    cursor: dict[str, object] | None = None
    while True:
        items, cursor = store.query_tenant_scan(
            _ACCOUNTS_TABLE, tenant=identity.tenant, exclusive_start_key=cursor
        )
        for item in items:
            account_id = item.get("account_id")
            if isinstance(account_id, str) and account_id not in identity.book:
                return account_id
        if cursor is None:
            break
    account_id = f"acct-{identity.tenant}-out-of-book"
    store.put_item(
        _ACCOUNTS_TABLE,
        {
            "account_id": account_id,
            "tenant": identity.tenant,
            "owner_rep_id": _OUT_OF_BOOK_AUTHOR,
            "name": "Layer 5 out-of-book account",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "created_by": _OUT_OF_BOOK_AUTHOR,
            "schema_version": 1,
        },
    )
    return account_id


def _seed_out_of_book_audits(
    ctx: SmokeContext, identity: SmokeIdentity, account_id: str
) -> tuple[str, str, str, str]:
    """Write a note and its create audit, and a contact delete audit, scoped to ``account_id``.

    The contact itself is not written: a delete audit's target is absent. Returns
    ``(note_id, note_audit_id, contact_id, contact_audit_id)``.
    """
    store = _store(ctx)
    tenant = identity.tenant
    suffix = hashlib.sha256(f"{tenant}:out-of-book".encode()).hexdigest()[:12]
    author = Identity(rep_id=_OUT_OF_BOOK_AUTHOR, tenant=tenant)
    created_at = "2026-01-01T00:00:00Z"
    note: dict[str, object] = {
        "note_id": f"note-oob-{suffix}",
        "tenant": tenant,
        "opportunity_id": f"opp-oob-{suffix}",
        "body": "Layer 5 out-of-book note",
        "created_at": created_at,
        "created_by": _OUT_OF_BOOK_AUTHOR,
    }
    contact: dict[str, object] = {
        "contact_id": f"contact-oob-{suffix}",
        "tenant": tenant,
        "account_id": account_id,
        "name": "Layer 5 out-of-book contact",
        "created_at": created_at,
        "updated_at": created_at,
        "created_by": _OUT_OF_BOOK_AUTHOR,
        "schema_version": 1,
    }
    note_audit = build_audit_record(
        identity=author,
        target_table=_NOTES_TABLE,
        target_id=str(note["note_id"]),
        before_snapshot={},
        after_snapshot=note,
        account_id=account_id,
    )
    contact_audit = build_audit_record(
        identity=author,
        target_table=_CONTACTS_TABLE,
        target_id=str(contact["contact_id"]),
        before_snapshot=contact,
        after_snapshot={},
    )
    store.put_item(_NOTES_TABLE, note)
    store.put_item(_AUDIT_TABLE, note_audit.to_item())
    store.put_item(_AUDIT_TABLE, contact_audit.to_item())
    return (
        str(note["note_id"]),
        note_audit.audit_id,
        str(contact["contact_id"]),
        contact_audit.audit_id,
    )


def _out_of_book_revert_leg(
    *, ctx: SmokeContext, caller: HttpCaller, domain: str, identity: SmokeIdentity
) -> Iterable[CheckResult]:
    """A revert of an out-of-book note create or contact delete is refused and changes nothing."""
    leg = _OUT_OF_BOOK_REVERT_LEG_CHECKS
    try:
        account_id = _out_of_book_account(ctx, identity)
        note_id, note_audit_id, contact_id, contact_audit_id = _seed_out_of_book_audits(
            ctx, identity, account_id
        )
    except Exception as exc:  # noqa: BLE001 - one actionable failing check, then the leg skips
        yield CheckResult(
            resource="out_of_book_revert",
            check="fixture_seeded_outside_the_book",
            status="fail",
            detail=f"{type(exc).__name__}: {exc}",
        )
        yield from _skip_leg(leg[1:], "the out-of-book fixture could not be seeded")
        return
    yield CheckResult(
        resource="out_of_book_revert",
        check="fixture_seeded_outside_the_book",
        status="pass",
        detail=f"account {account_id!r} is outside the book {sorted(identity.book)!r}",
    )
    cases = (
        ("note", note_audit_id, _NOTES_TABLE, {"note_id": note_id}),
        ("delete", contact_audit_id, _CONTACTS_TABLE, {"contact_id": contact_id}),
    )
    for name, audit_id, table, key in cases:
        target_before = _read_row(ctx, table, key, consistent=True)
        audit_before = _read_audit(ctx, audit_id, consistent=True)
        result = _call_tool(
            caller,
            domain,
            BY_NAME["revert_audit_record"].name,
            identity.token,
            {
                "audit_id": audit_id,
                "transaction_key": _make_transaction_key(
                    identity.tenant, f"out-of-book-{name}-revert"
                ),
            },
        )
        yield _refused_with(
            result,
            resource="out_of_book_revert",
            check=f"{name}_revert_refused_with_scope_denied",
            what=f"an out-of-book {name} revert",
            member="ScopeDenied",
            expected_status=403,
            expected_code="scope_denied",
        )
        target_after = _read_row(ctx, table, key, consistent=True)
        audit_after = _read_audit(ctx, audit_id, consistent=True)
        unchanged = canonical_row_bytes(target_before) == canonical_row_bytes(
            target_after
        ) and canonical_row_bytes(audit_before) == canonical_row_bytes(audit_after)
        yield CheckResult(
            resource="out_of_book_revert",
            check=f"{name}_revert_changed_nothing",
            status="pass" if unchanged and audit_after is not None else "fail",
            detail=(
                f"{table} target {row_fingerprint(target_before)} -> "
                f"{row_fingerprint(target_after)}; audit {row_fingerprint(audit_before)} -> "
                f"{row_fingerprint(audit_after)}"
            ),
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: resolve any supplied token/account id, then run every check.

    With neither supplied, the script provisions its own Cognito user under its own disposable
    tenant and removes it afterwards.
    """
    token = _flag_or_env(argv, "--token", "CRM_SMOKE_TOKEN")
    account_id = _flag_or_env(argv, "--account-id", "CRM_SMOKE_ACCOUNT_ID")
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=lambda ctx: checks(ctx, token=token, account_id=account_id),
        extra_arguments=["--token", "--account-id"],
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

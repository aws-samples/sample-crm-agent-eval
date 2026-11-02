# SPDX-License-Identifier: MIT
"""Layer 6 smoke check: a high-risk write reaches the user as a structured proposal card.

Drives the deployed CloudFront `/agent/*` behaviour onto the AgentCore Runtime and asserts:

1. A structured `proposal` event with `proposal_summary.before`/`.after` objects arrives;
   `prose_only_rejected_check` requires the same discriminator to fail on a prose rendition.
2. Nothing has mutated at that point: the target row's canonical bytes are unchanged and raising
   the proposal wrote no audit record. Audit records are compared against the target's audit ids
   read immediately before the turn that raises the proposal, so an earlier record on the same
   target is not attributed to it.
3. The card's `before` matches the live row, and every `after` field lies inside
   `crm.domain.allowlists.proposal_payload_allowlist`.
4. Approving commits the change and writes exactly one new audit record, the approval's own
   `idempotent_id("audit", <approve transaction_key>)`, asserted separately.

The card is the one the model-driven chat turn raises through the relay: clauses 1 and 3 read that
turn's own `proposal` event, and the approval acts on its proposal id. A turn shaped like the stub
provider's (no `completed` text, or text carrying the stub marker) cannot request a high-risk write,
so its card rows are a `defer` with reason `stub_provider_cannot_request_a_high_risk_write`; a turn
that answers in prose fails. When the turn raised no card, the proposal is raised through the API's
`submit_proposal` route, so clauses 2 and 4 still run and their rows say so.

A disposable `eval-smoke-<run_id>` tenant and one MFA-enrolled Cognito user are created and removed;
the `demo` tenant is proved untouched. Network hops sit behind the `ApiCaller`, `RuntimeCaller` and
`RowReader` seams. Access-token claims are read only after `scripts.smoke.verified_claims` verifies
the token against the pool's JWKS.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Protocol

# Supports both `python3 scripts/smoke_proposal_card.py` and `python3 -m scripts.smoke_proposal_card`;
# the path form needs the repository root on `sys.path` to import `scripts.smoke.*`.
if __package__ in (None, ""):  # pragma: no cover - only taken by the path-form invocation
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.smoke.cognito_claims import (  # noqa: E402
    _live_table_prefix,
    auth_factor_detail,
    totp_code,
)
from scripts.smoke.demo_tenant_guard import (  # noqa: E402
    TenantSnapshot,
    demo_tenant_untouched_check,
    read_tenant_snapshot,
    store_row_reader,
    writes_only_under_smoke_tenant_check,
)
from scripts.smoke.harness import (  # noqa: E402
    DEFER,
    CheckResult,
    ExpectedCheckCount,
    SmokeContext,
    open_https,
    run_main,
)
from scripts.smoke.integration_proposal_chain import (  # noqa: E402
    HttpCaller,
    HttpResult,
    canonical_row_bytes,
    default_http_caller,
    row_fingerprint,
)
from scripts.smoke.runtime_invoke import verified_access_claims  # noqa: E402
from scripts.smoke.tenant import (  # noqa: E402
    SmokeTenant,
    allocate_smoke_tenant,
    teardown_smoke_tenant,
)

from crm.authz import DEMO_TENANT  # noqa: E402
from crm.domain.allowlists import proposal_payload_allowlist  # noqa: E402
from crm.envelope import CLAIM_TENANT  # noqa: E402
from crm.handlers._common import idempotent_id  # noqa: E402
from crm.store.keys import tenant_prefixed  # noqa: E402
from crm.store.store import Store  # noqa: E402
from crm.toolspec.definitions import BY_NAME, allowlist_for, high_risk_fields_for  # noqa: E402

LAYER = 6
RESOURCE_CLASS = "proposal_card"

_OPPORTUNITY_TABLE: Final[str] = "opportunities"
_AUDIT_TABLE: Final[str] = "audit_records"

#: The high-risk field under test: in `high_risk_fields_for("opportunities")` and absent from
#: `allowlist_for("opportunities")`, so only the proposal/approval gate can change it.
_TARGET_FIELD: Final[str] = "stage"

#: The stage `create_opportunity` lands in with no explicit stage, and the stage every proposal here
#: asks for (a permitted forward transition).
_CREATED_STAGE: Final[str] = "Discovery"
_PROPOSED_STAGE: Final[str] = "Negotiation"

#: Every target carries a close date so the derived `stage_close_date` index attribute exists.
_CLOSE_DATE: Final[str] = "2026-12-31"

#: TOTP step, and how many sign-in attempts are made before the token's tenant claim is rejected.
_TOTP_STEP_SECONDS: Final[float] = 30.0
_AUTH_ATTEMPTS: Final[int] = 3

#: Socket timeout for a Runtime invocation.
_RUNTIME_TIMEOUT_SECONDS: Final[float] = 90.0

#: The machine-readable `defer_reason` for the card rows of a stub-shaped turn.
_STUB_CANNOT_PROPOSE: Final[str] = "stub_provider_cannot_request_a_high_risk_write"

#: The prefix the stub provider's synthetic reply text carries (`src/providers/stub.py`).
_STUB_TEXT_MARKER: Final[str] = "[stub:"

#: How much of the emitted proposal event is quoted verbatim into the passing check's `detail`.
_EMITTED_EVENT_DETAIL_CHARS: Final[int] = 1600

#: Every `(resource, check)` row the probe publishes on a complete run. `_deferrals_for_unemitted`
#: turns each one a run did not emit into a named `defer`.
_PROBE_CHECKS: Final[tuple[tuple[str, str], ...]] = (
    ("proposal_card", "high_risk_field_is_gated_by_construction"),
    ("proposal_card", "target_created"),
    ("direct_update", "direct_high_risk_update_refused"),
    ("direct_update", "target_unchanged_by_the_refused_update"),
    ("chat_turn", "turn_reached_the_runtime"),
    ("chat_turn", "model_driven_turn_emits_proposal_card"),
    ("chat_turn", "turn_mutated_nothing"),
    ("proposal_card", "card_before_after_matches_live_state"),
    ("proposal_card", "prose_only_confirmation_is_rejected"),
    ("proposal_card", "nothing_mutated_at_proposal_time"),
    ("proposal_card", "proposal_wrote_no_audit_record"),
    ("approve", "commits_the_change"),
    ("approve", "writes_the_audit_record"),
)

#: The probe rows only the model-driven turn's own card can answer.
_CARD_CHECKS: Final[tuple[tuple[str, str], ...]] = (
    ("proposal_card", "card_before_after_matches_live_state"),
    ("proposal_card", "prose_only_confirmation_is_rejected"),
)

#: Every row `checks` publishes on a complete live run: the probe's, plus the principal, tenant-delta
#: and cleanup rows.
_EXPECTED_CHECKS: Final[tuple[tuple[str, str], ...]] = (
    ("smoke_principal", "mfa_tokens_issued"),
    *_PROBE_CHECKS,
    ("proposal_card", "demo_tenant_untouched"),
    ("proposal_card", "writes_only_under_smoke_tenant"),
    ("smoke_principal", "cognito_user_deleted"),
    ("smoke_principal", "reps_row_deleted"),
    ("smoke_tenant", "tenant_left_clean"),
)

#: `defer_reason` for a row that did not run because an upstream step failed or never ran.
_UNEMITTED_DEFER_REASON: Final[str] = "upstream_check_did_not_run"

#: Detail suffix for a row exercised on the API fallback rather than the turn's own proposal.
_API_FALLBACK_NOTE: Final[str] = (
    " [the model-driven turn raised no proposal card, so this proposal was raised through the API's "
    "submit_proposal route]"
)


def _deferrals_for_unemitted(
    results: Sequence[CheckResult],
    expected: Sequence[tuple[str, str]],
    *,
    reason: str = _UNEMITTED_DEFER_REASON,
    cause: str = "",
) -> list[CheckResult]:
    """Return a named `defer` for every expected `(resource, check)` row `results` lacks.

    The detail names `cause`, or else the failures that preceded the row.
    """
    emitted = {(result.resource, result.check) for result in results}
    blockers = sorted({result.check for result in results if result.status == "fail"})
    because = cause or (
        ", ".join(blockers) if blockers else "an upstream step that published no failure"
    )
    return [
        CheckResult(
            resource=resource,
            check=check,
            status=DEFER,
            defer_reason=reason,
            detail=f"did not run, so this property is unverified rather than absent; blocked by: {because}",
        )
        for resource, check in expected
        if (resource, check) not in emitted
    ]


def expected_check_count(_ctx: SmokeContext) -> ExpectedCheckCount:
    """The fewest checks a run publishes: every row in `_EXPECTED_CHECKS`, ran or deferred."""
    return ExpectedCheckCount(
        minimum=len(_EXPECTED_CHECKS),
        basis=(
            f"every row in _EXPECTED_CHECKS ({len(_EXPECTED_CHECKS)}); a row that could not run is "
            "published as a named defer"
        ),
    )


__all__ = [
    "LAYER",
    "RESOURCE_CLASS",
    "ApiCaller",
    "Principal",
    "RowReader",
    "RuntimeCaller",
    "card_matches_live_state_check",
    "checks",
    "checks_with_seams",
    "find_proposal_event",
    "main",
    "prose_only_rejected_check",
    "prose_rendition",
    "structured_proposal_check",
    "target_unchanged_check",
]


# -- seams ------------------------------------------------------------------------------------


class ApiCaller(Protocol):
    """One CRM tool call through the deployed API front door (`POST /api/<tool_name>`)."""

    def __call__(self, tool_name: str, arguments: Mapping[str, Any]) -> HttpResult:
        """Return `(status_code, body)`, or a `str` when no HTTP response arrived at all."""
        ...


class RuntimeCaller(Protocol):
    """One AgentCore Runtime invocation through the CloudFront `/agent/*` behaviour."""

    def __call__(self, payload: Mapping[str, Any]) -> HttpResult:
        """Return `(status_code, body)`, or a `str` when no HTTP response arrived at all."""
        ...


class RowReader(Protocol):
    """Reads one persisted row back out of the deployed tables by primary key."""

    def __call__(self, table: str, key: Mapping[str, str]) -> dict[str, Any] | None:
        """Return the row, or `None` when it does not exist."""
        ...


@dataclass(frozen=True, slots=True)
class Principal:
    """The acting identity for one run.

    Attributes:
        access_token: Token the relay verifies on the chat turn.
        id_token: Token the CRM API is called with.
        rep_id: The rep the tokens name, asserted against the audit record's `actor_rep_id`.
        tenant: The disposable tenant every write lands under, never `demo`.
        detail: How the identity was obtained, for the inventory.
    """

    access_token: str
    id_token: str
    rep_id: str
    tenant: str
    detail: str


# -- discriminators (no network, no AWS) ---------------------------------------------------------


def find_proposal_event(events: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Return the first event whose `kind` is exactly `"proposal"`, or `None`."""
    for event in events:
        if isinstance(event, Mapping) and event.get("kind") == "proposal":
            return event
    return None


def structured_proposal_check(
    events: Sequence[Mapping[str, Any]], *, resource: str, check: str
) -> CheckResult:
    """Assert `events` carries a structured `proposal` event a UI could render as a card.

    Each requirement fails distinctly:

    * an event of kind `proposal` exists (a prose reply mentioning one does not count);
    * `proposal_id` is a non-empty string, so the card has something to approve;
    * `proposal_summary` is an object; and
    * `proposal_summary.before` and `.after` are both non-empty objects, so there are real values
      to render either side of the change.

    Args:
        events: The decoded events observed, in arrival order.
        resource: Inventory resource name for the emitted `CheckResult`.
        check: Inventory check name for the emitted `CheckResult`.

    Returns:
        A `pass` naming the proposal id and the before/after fields, or a `fail` naming the defect
        and the event kinds that were actually observed.
    """
    observed = [str(event.get("kind")) for event in events if isinstance(event, Mapping)]
    event = find_proposal_event(events)
    if event is None:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                "no event of kind 'proposal' was emitted for the high-risk change; observed "
                f"kinds={observed!r}. A prose confirmation is not a proposal card"
            ),
        )
    proposal_id = event.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"proposal event carries proposal_id={proposal_id!r}; a non-empty string is required",
        )
    summary = event.get("proposal_summary")
    if not isinstance(summary, Mapping):
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"proposal event carries proposal_summary={type(summary).__name__}; an object with "
                "before/after values is required for a card to render"
            ),
        )
    malformed = [
        f"{name}={summary.get(name)!r}"
        for name in ("before", "after")
        if not isinstance(summary.get(name), Mapping) or not summary.get(name)
    ]
    if malformed:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"proposal_summary lacks renderable before/after values: {malformed}",
        )
    before = dict(summary["before"])
    after = dict(summary["after"])
    return CheckResult(
        resource=resource,
        check=check,
        status="pass",
        detail=(
            f"structured proposal event: proposal_id={proposal_id!r}, "
            f"before fields={sorted(before)!r}, after={after!r}; as emitted: "
            f"{json.dumps(event, sort_keys=True, default=str)[:_EMITTED_EVENT_DETAIL_CHARS]}"
        ),
    )


def prose_rendition(
    events: Sequence[Mapping[str, Any]], *, reply_text: str = ""
) -> list[dict[str, Any]]:
    """Render `events` as the prose-only reply the same system could have sent instead.

    The text names the proposal id, the field, both values and the word "proposal", but carries no
    structured `proposal` event.

    Args:
        events: The real events observed, used to build a faithful prose paraphrase of them.
        reply_text: The system's own synthesized reply text, appended verbatim when present.

    Returns:
        A one-event `completed` stream carrying only text.
    """
    proposal = find_proposal_event(events)
    summary = proposal.get("proposal_summary") if proposal is not None else None
    before = (
        dict(summary["before"])
        if isinstance(summary, Mapping) and isinstance(summary.get("before"), Mapping)
        else {}
    )
    after = (
        dict(summary["after"])
        if isinstance(summary, Mapping) and isinstance(summary.get("after"), Mapping)
        else {}
    )
    proposal_id = str(proposal.get("proposal_id")) if proposal is not None else "unknown"
    text = (
        f"I have raised proposal {proposal_id} to change {_TARGET_FIELD} from "
        f"{before.get(_TARGET_FIELD)!r} to {after.get(_TARGET_FIELD)!r}. "
        "Reply 'approve' and I will apply it. before/after values are shown above."
    )
    if reply_text:
        text = f"{text} {reply_text}"
    return [{"kind": "completed", "text": text}]


def prose_only_rejected_check(
    events: Sequence[Mapping[str, Any]], *, resource: str, reply_text: str = ""
) -> CheckResult:
    """Assert `structured_proposal_check` fails on a prose rendition of the same response."""
    check = "prose_only_confirmation_is_rejected"
    prose = prose_rendition(events, reply_text=reply_text)
    verdict = structured_proposal_check(prose, resource=resource, check=check)
    quoted = str(prose[0].get("text", ""))[:300]
    if verdict.status == "fail":
        return CheckResult(
            resource=resource,
            check=check,
            status="pass",
            detail=(
                "the same discriminator rejects a prose-only confirmation naming the proposal id "
                f"and both values: {quoted!r} -> fail ({verdict.detail})"
            ),
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="fail",
        detail=(
            f"the discriminator ACCEPTED a prose-only confirmation ({verdict.status}): {quoted!r}. "
            "A check that passes on prose cannot prove a proposal card exists"
        ),
    )


def card_matches_live_state_check(
    event: Mapping[str, Any],
    *,
    live_row: Mapping[str, Any] | None,
    resource: str,
    table: str = _OPPORTUNITY_TABLE,
) -> CheckResult:
    """Assert the card's before/after describe live state and stay inside the payload allowlist.

    * every field of `before` equals the value the deployed table holds;
    * `before` names the high-risk field under test;
    * every field of `after` lies inside `proposal_payload_allowlist(table)`; a field outside it is
      reported as a finding;
    * `after` proposes the requested new value for the high-risk field.
    """
    check = "card_before_after_matches_live_state"
    summary = event.get("proposal_summary")
    if not isinstance(summary, Mapping):
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail="proposal event carries no proposal_summary object to compare against live state",
        )
    if live_row is None:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail="the target row could not be read back, so the card cannot be compared to live state",
        )
    before = summary.get("before")
    after = summary.get("after")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"before={type(before).__name__} after={type(after).__name__}; both must be objects",
        )
    if _TARGET_FIELD not in before:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"the card's before block does not carry {_TARGET_FIELD!r} "
                f"(fields={sorted(before)!r}), so it shows nothing about the field being changed"
            ),
        )
    stale = {
        name: (value, live_row.get(name))
        for name, value in before.items()
        if live_row.get(name) != value
    }
    if stale:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=f"the card's before block disagrees with live state (card, live): {stale!r}",
        )
    permitted = proposal_payload_allowlist(table)
    outside = sorted(set(after) - permitted)
    if outside:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"the card's after payload names field(s) outside the {table} proposal allowlist: "
                f"{outside!r}; permitted={sorted(permitted)!r}"
            ),
        )
    if after.get(_TARGET_FIELD) != _PROPOSED_STAGE:
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"the card proposes {_TARGET_FIELD}={after.get(_TARGET_FIELD)!r}, expected "
                f"{_PROPOSED_STAGE!r}"
            ),
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="pass",
        detail=(
            f"before {row_fingerprint(dict(before))} matches live state on {sorted(before)!r}; "
            f"after={dict(after)!r} lies inside the {table} proposal allowlist"
        ),
    )


def target_unchanged_check(
    *,
    resource: str,
    check: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    what: str,
) -> CheckResult:
    """Assert two reads of the target row are byte-identical, publishing both fingerprints.

    Compares canonical bytes, so an added attribute is detected as well as a changed one.
    """
    before_print, after_print = row_fingerprint(before), row_fingerprint(after)
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


def high_risk_field_is_gated_check(*, resource: str) -> CheckResult:
    """Assert, from the toolspec itself, that the field under test can only move by proposal.

    The field must be classified high-risk and absent from the direct `update_*` allowlist.
    """
    check = "high_risk_field_is_gated_by_construction"
    high_risk = high_risk_fields_for(_OPPORTUNITY_TABLE)
    direct = allowlist_for(_OPPORTUNITY_TABLE)
    if _TARGET_FIELD in high_risk and _TARGET_FIELD not in direct:
        return CheckResult(
            resource=resource,
            check=check,
            status="pass",
            detail=(
                f"{_TARGET_FIELD!r} is high-risk ({sorted(high_risk)!r}) and absent from "
                f"update_opportunity's allowlist ({sorted(direct)!r})"
            ),
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="fail",
        detail=(
            f"{_TARGET_FIELD!r} is not gated: high_risk={sorted(high_risk)!r} "
            f"direct_allowlist={sorted(direct)!r}"
        ),
    )


def _refused_with_validation_failed(
    result: HttpResult, *, resource: str, check: str, what: str
) -> CheckResult:
    """Assert `result` is a refusal carrying `ValidationFailed`'s own 400/`validation_failed` pair."""
    if isinstance(result, str):
        return CheckResult(resource=resource, check=check, status="fail", detail=result)
    status_code, body = result
    if status_code < 400:  # noqa: PLR2004 - the HTTP client/server error boundary
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"{what} SUCCEEDED (status={status_code} body={body[:300]}) - a success here would "
                "mean the high-risk field is directly writable and the approval gate is bypassable"
            ),
        )
    code = _error_code(body)
    if status_code != 400 or code != "validation_failed":  # noqa: PLR2004 - the taxonomy's own pair
        return CheckResult(
            resource=resource,
            check=check,
            status="fail",
            detail=(
                f"{what} was refused, but not with ValidationFailed: expected status=400 and "
                f"error.code='validation_failed', got status={status_code} code={code!r} "
                f"body={body[:300]}"
            ),
        )
    return CheckResult(
        resource=resource,
        check=check,
        status="pass",
        detail=f"{what} refused with 400 error.code='validation_failed'",
    )


# -- wire helpers -----------------------------------------------------------------------------


def _error_code(body: str) -> str | None:
    """Extract `error.code` from an error response body, or `None` if absent/unparseable."""
    parsed = _parse_json(body) or {}
    error_block = parsed.get("error")
    if isinstance(error_block, Mapping):
        code = error_block.get("code")
        return str(code) if code is not None else None
    return None


def _parse_json(body: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _completed_text(events: Sequence[Mapping[str, Any]]) -> str:
    """The text of the last `completed` event, or `""` when none carries text."""
    for event in reversed(events):
        if event.get("kind") == "completed" and isinstance(event.get("text"), str):
            return str(event["text"])
    return ""


def _transaction_key(tenant: str, step: str) -> str:
    """Build a `transaction_key` unique per (tenant, step) within DynamoDB's 36-character cap.

    The value becomes `ClientRequestToken`, which AWS rejects above 36 characters, so it is hashed.
    """
    marker = hashlib.sha256(tenant.encode()).hexdigest()[:8]
    digest = hashlib.sha256(f"{tenant}:{step}".encode()).hexdigest()[:20]
    return f"smoke-{marker}-{digest}"


def decode_sse_events(body: str) -> list[dict[str, Any]]:
    """Decode an SSE response body into the JSON objects its `data:` lines carry.

    One object per `data:` line, in arrival order; a line that is not valid JSON is skipped.
    """
    events: list[dict[str, Any]] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        parsed = _parse_json(line[len("data:") :].strip())
        if parsed is not None:
            events.append(parsed)
    return events


def live_api_caller(caller: HttpCaller, domain: str, token: str) -> ApiCaller:
    """Return an `ApiCaller` posting to the deployed CRM API through CloudFront."""

    def _call(tool_name: str, arguments: Mapping[str, Any]) -> HttpResult:
        return caller(
            "POST",
            f"https://{domain}/api/{tool_name}",
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json.dumps(dict(arguments)),
        )

    return _call


def live_runtime_caller(ctx: SmokeContext, token: str) -> RuntimeCaller:
    """Return a `RuntimeCaller` posting to the Runtime through the CloudFront `/agent/*` behaviour.

    The viewer path carries the url-encoded runtime ARN, and the `/agent/*` behaviour forwards it
    unchanged to the relay's Function URL. The JWT travels in `X-Crm-Authorization`, since
    `Authorization` carries CloudFront's OAC signature, and `x-amz-content-sha256` is the digest of
    the exact bytes sent. The session header repeats the body's `session_id` when it has one. Reads
    the whole body before returning.
    """
    domain = ctx.outputs.require_str("cloudfront_domain_name")
    runtime_arn = ctx.outputs.require_str("agentcore_runtime_arn")
    url = f"https://{domain}/agent/{urllib.parse.quote(runtime_arn, safe='')}"

    def _call(payload: Mapping[str, Any]) -> HttpResult:
        session_id = str(payload.get("session_id") or f"smoke-proposal-card-{uuid.uuid4().hex}")
        data = json.dumps(dict(payload)).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - fixed https CloudFront host
            url,
            data=data,
            method="POST",
            headers={
                "X-Crm-Authorization": f"Bearer {token}",
                "x-amz-content-sha256": hashlib.sha256(data).hexdigest(),
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Accept-Encoding": "identity",
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
            },
        )
        try:
            with open_https(request, timeout=_RUNTIME_TIMEOUT_SECONDS) as response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            return f"transport error invoking the runtime: {exc}"

    return _call


# -- the acting principal ---------------------------------------------------------------------


def _generated_password() -> str:
    """Return a throwaway password satisfying the pool's 14-character all-classes policy."""
    return "Aa1!" + secrets.token_urlsafe(16)


class LiveProposalCardPrincipal:
    """Creates a disposable tenant and one MFA-enrolled Cognito user, and removes both.

    Mints both tokens from one sign-in: an access token for the Runtime and an ID token for the CRM
    API. The rep has `full_visibility=True` over its own disposable tenant. The pool's MFA
    configuration is left as deployed and any MFA challenge is completed. The tenant comes from
    `scripts.smoke.tenant.allocate_smoke_tenant`, which refuses `demo`. The access token is
    verified against `jwks_url` (the pool's JWKS by default) before its tenant claim is read.
    """

    def __init__(
        self,
        ctx: SmokeContext,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        jwks_url: str | None = None,
    ) -> None:
        self._ctx = ctx
        self._jwks_url = jwks_url
        self._clock = clock if clock is not None else time.time
        self._sleeper = sleeper if sleeper is not None else time.sleep
        self._smoke_tenant: SmokeTenant | None = None
        self._username: str | None = None

    def store(self) -> Store:
        """Return a `Store` addressing the live, prefixed physical tables."""
        return Store(
            client=self._ctx.client("dynamodb"), table_prefix=_live_table_prefix(self._ctx)
        )

    def tenant(self) -> str:
        """Return the disposable tenant allocated so far, or `""` before allocation."""
        return self._smoke_tenant.tenant if self._smoke_tenant is not None else ""

    def _idp(self) -> Any:
        return self._ctx.client("cognito-idp")

    def _user_pool_id(self) -> str:
        return self._ctx.outputs.require_str("cognito_user_pool_id")

    def _client_id(self) -> str:
        return self._ctx.outputs.require_str("cognito_spa_client_id")

    def provision(self) -> Principal:
        """Allocate the tenant, create the MFA-enrolled user, and return both issued tokens.

        Raises:
            RuntimeError: Any step could not be completed, or the issued tokens name a tenant other
                than the disposable one allocated here (the pre-token-generation trigger's
                fail-closed default), in which case they must not be used.
        """
        store = self.store()
        smoke_tenant = allocate_smoke_tenant(store)
        self._smoke_tenant = smoke_tenant
        if not smoke_tenant.tenant or smoke_tenant.tenant == DEMO_TENANT:
            raise RuntimeError(
                f"refusing to run: allocated tenant is {smoke_tenant.tenant!r}; smoke runs never write "
                "the demo tenant"
            )

        idp = self._idp()
        email = f"smoke-proposal-card-{smoke_tenant.run_id}@example.test"
        password = _generated_password()
        created = idp.admin_create_user(
            UserPoolId=self._user_pool_id(),
            Username=email,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
        )
        username = str((created.get("User") or {}).get("Username") or email)
        self._username = username
        idp.admin_set_user_password(
            UserPoolId=self._user_pool_id(), Username=username, Password=password, Permanent=True
        )
        store.put_item(
            "reps",
            {
                "rep_id": username,
                "tenant": smoke_tenant.tenant,
                "book": [],
                "full_visibility": True,
                "name": "Layer 6 proposal card smoke",
                "status": "active",
            },
        )
        return self._authenticate_with_mfa(
            username=username, password=password, expected_tenant=smoke_tenant.tenant
        )

    def _authenticate_with_mfa(
        self, *, username: str, password: str, expected_tenant: str
    ) -> Principal:
        """Sign in, completing MFA enrollment, and return tokens naming `expected_tenant`."""
        secret: str | None = None
        last_tenant = ""
        for attempt in range(1, _AUTH_ATTEMPTS + 1):
            if attempt > 1:
                self._wait_for_next_totp_step()
            result, secret = self._one_auth_attempt(
                username=username, password=password, secret=secret
            )
            access_token = str(result.get("AccessToken") or "")
            id_token = str(result.get("IdToken") or "")
            if not access_token or not id_token:
                raise RuntimeError(
                    f"sign-in for {username!r} issued AccessToken={bool(access_token)} "
                    f"IdToken={bool(id_token)}; both are required"
                )
            claims = verified_access_claims(self._ctx, access_token, jwks_url=self._jwks_url)
            last_tenant = str(claims.get(CLAIM_TENANT) or "")
            if last_tenant == expected_tenant:
                return Principal(
                    access_token=access_token,
                    id_token=id_token,
                    rep_id=username,
                    tenant=expected_tenant,
                    detail=(
                        f"{auth_factor_detail(mfa_completed=bool(secret))} access and ID "
                        f"tokens issued on attempt {attempt}"
                    ),
                )
        raise RuntimeError(
            f"issued tokens name tenant {last_tenant!r}, expected the disposable tenant "
            f"{expected_tenant!r} after {_AUTH_ATTEMPTS} attempt(s); the pre-token-generation "
            "trigger's reps lookup did not resolve, so the tokens must not be used"
        )

    def _one_auth_attempt(
        self, *, username: str, password: str, secret: str | None
    ) -> tuple[Mapping[str, Any], str]:
        """Run one `ADMIN_USER_PASSWORD_AUTH` sign-in, answering whichever MFA challenge arrives."""
        idp = self._idp()
        response = idp.admin_initiate_auth(
            UserPoolId=self._user_pool_id(),
            ClientId=self._client_id(),
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        challenge = str(response.get("ChallengeName") or "")

        if challenge == "MFA_SETUP":
            associated = idp.associate_software_token(Session=response["Session"])
            secret = str(associated["SecretCode"])
            verified = idp.verify_software_token(
                Session=associated["Session"],
                UserCode=totp_code(secret, timestamp=self._clock()),
                FriendlyDeviceName="crm-eval-smoke",
            )
            if str(verified.get("Status")) != "SUCCESS":
                raise RuntimeError(
                    f"verify_software_token returned Status={verified.get('Status')!r}, "
                    "expected 'SUCCESS'"
                )
            response = idp.admin_respond_to_auth_challenge(
                UserPoolId=self._user_pool_id(),
                ClientId=self._client_id(),
                ChallengeName="MFA_SETUP",
                Session=verified["Session"],
                ChallengeResponses={"USERNAME": username},
            )
        elif challenge == "SOFTWARE_TOKEN_MFA":
            if secret is None:
                raise RuntimeError(
                    "pool challenged SOFTWARE_TOKEN_MFA before any software token was associated"
                )
            response = idp.admin_respond_to_auth_challenge(
                UserPoolId=self._user_pool_id(),
                ClientId=self._client_id(),
                ChallengeName="SOFTWARE_TOKEN_MFA",
                Session=response["Session"],
                ChallengeResponses={
                    "USERNAME": username,
                    "SOFTWARE_TOKEN_MFA_CODE": totp_code(secret, timestamp=self._clock()),
                },
            )
        elif challenge:
            raise RuntimeError(
                f"unexpected challenge {challenge!r} from admin_initiate_auth; this script answers "
                "MFA_SETUP and SOFTWARE_TOKEN_MFA only and never weakens the pool to avoid one"
            )

        result = response.get("AuthenticationResult") or {}
        if not isinstance(result, Mapping):
            raise RuntimeError(
                f"AuthenticationResult for {username!r} is not an object: {result!r}"
            )
        return result, secret or ""

    def _wait_for_next_totp_step(self) -> None:
        """Sleep into the next TOTP window, so a retry never replays an already-consumed code."""
        now = self._clock()
        self._sleeper(_TOTP_STEP_SECONDS - (now % _TOTP_STEP_SECONDS) + 1.0)

    def cleanup(self) -> Iterable[CheckResult]:
        """Remove the Cognito user, its `reps` row, and the tenant, proving each removal by re-read."""
        yield from self._cleanup_cognito_user()
        yield from self._cleanup_reps_row()
        yield from self._cleanup_tenant()

    def _cleanup_cognito_user(self) -> Iterable[CheckResult]:
        username = self._username
        if username is None:
            return
        try:
            idp = self._idp()
            idp.admin_delete_user(UserPoolId=self._user_pool_id(), Username=username)
            try:
                idp.admin_get_user(UserPoolId=self._user_pool_id(), Username=username)
            except Exception as exc:  # noqa: BLE001 - the expected outcome is an exception
                if "UserNotFoundException" not in f"{type(exc).__name__}: {exc}":
                    raise
                yield CheckResult(
                    resource="smoke_principal",
                    check="cognito_user_deleted",
                    status="pass",
                    detail="admin_get_user after deletion raised UserNotFoundException",
                )
                return
            yield CheckResult(
                resource="smoke_principal",
                check="cognito_user_deleted",
                status="fail",
                detail="admin_get_user still resolves the smoke user after admin_delete_user",
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
            yield CheckResult(
                resource="smoke_principal",
                check="cognito_user_deleted",
                status="fail",
                detail=f"could not confirm smoke user deletion: {type(exc).__name__}: {exc}",
            )

    def _cleanup_reps_row(self) -> Iterable[CheckResult]:
        username = self._username
        if username is None:
            return
        try:
            store = self.store()
            store.delete_item("reps", {"rep_id": username})
            remaining = store.get_item("reps", {"rep_id": username})
            yield CheckResult(
                resource="smoke_principal",
                check="reps_row_deleted",
                status="pass" if remaining is None else "fail",
                detail=(
                    "reps row absent on read-back"
                    if remaining is None
                    else "reps row still present after delete_item"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
            yield CheckResult(
                resource="smoke_principal",
                check="reps_row_deleted",
                status="fail",
                detail=f"could not delete the smoke reps row: {type(exc).__name__}: {exc}",
            )

    def _cleanup_tenant(self) -> Iterable[CheckResult]:
        smoke_tenant = self._smoke_tenant
        if smoke_tenant is None:
            return
        try:
            deleted = teardown_smoke_tenant(self.store(), smoke_tenant)
        except Exception as exc:  # noqa: BLE001 - a failed teardown must never read as clean
            yield CheckResult(
                resource="smoke_tenant",
                check="tenant_left_clean",
                status="fail",
                detail=f"could not tear down {smoke_tenant.tenant}: {type(exc).__name__}: {exc}",
            )
            return
        yield CheckResult(
            resource="smoke_tenant",
            check="tenant_left_clean",
            status="pass",
            detail=(
                f"tenant {smoke_tenant.tenant} torn down; rows deleted: {sum(deleted.values())} "
                f"({deleted!r})"
            ),
        )


# -- the probe --------------------------------------------------------------------------------


@dataclass(slots=True)
class _ProposalCardProbe:
    """Runs the chat-initiated high-risk write and yields one `CheckResult` per assertion.

    Never raises for an individual failure: a row a failed prerequisite kept from running is
    published as a `defer` naming what failed.
    """

    call_api: ApiCaller
    call_runtime: RuntimeCaller
    read_row: RowReader
    audit_ids_for: Callable[[str], list[str]]
    principal: Principal
    results: list[CheckResult] = field(default_factory=list)
    opportunity_id: str = ""
    #: Why the turn yielded no card: `(defer_reason, cause)`, or `None` when it yielded one.
    card_unavailable: tuple[str, str] | None = None
    #: The target's audit ids read just before the proposal is raised; `None` when unreadable.
    audit_baseline: frozenset[str] | None = None
    audit_baseline_error: str = ""

    def run(self) -> list[CheckResult]:
        """Run every step in order, returning the accumulated results plus a `defer` per row not run.

        When the model-driven turn yields no card, the card rows defer naming why, and the proposal
        is raised through the API's `submit_proposal` route so the mutation, audit and approval rows
        still run.
        """
        self._steps()
        if self.card_unavailable is not None:
            reason, cause = self.card_unavailable
            self.results.extend(
                _deferrals_for_unemitted(self.results, _CARD_CHECKS, reason=reason, cause=cause)
            )
        self.results.extend(_deferrals_for_unemitted(self.results, _PROBE_CHECKS))
        return self.results

    def _steps(self) -> None:
        self.results.append(high_risk_field_is_gated_check(resource="proposal_card"))
        if not self._create_target():
            return
        created = self._read_target()
        self.results.append(self._direct_high_risk_update_refused())
        self.results.append(
            target_unchanged_check(
                resource="direct_update",
                check="target_unchanged_by_the_refused_update",
                before=created,
                after=self._read_target(),
                what="the target row across the refused direct high-risk update",
            )
        )

        before_turn = self._read_target()
        self._snapshot_audit_baseline()
        proposal_event, reply_text = self._model_driven_turn(before_turn)
        if proposal_event is not None:
            proposal_id = str(proposal_event.get("proposal_id") or "")
            self.results.append(
                card_matches_live_state_check(
                    proposal_event, live_row=before_turn, resource="proposal_card"
                )
            )
            self.results.append(
                prose_only_rejected_check(
                    [proposal_event], resource="proposal_card", reply_text=reply_text
                )
            )
        fallback_from = len(self.results)
        if proposal_event is None:
            proposal_id = self._api_submitted_proposal_id()
            if not proposal_id:
                return
        self.results.append(
            target_unchanged_check(
                resource="proposal_card",
                check="nothing_mutated_at_proposal_time",
                before=before_turn,
                after=self._read_target(),
                what="the target row from before the turn to after the proposal was raised",
            )
        )
        self.results.append(self._no_audit_record_yet())
        self._approve(proposal_id, before_approve=self._read_target())
        if proposal_event is None:
            self.results[fallback_from:] = [
                replace(row, detail=row.detail + _API_FALLBACK_NOTE)
                for row in self.results[fallback_from:]
            ]

    # -- steps ---------------------------------------------------------------------------------

    def _create_target(self) -> bool:
        """Create the account and opportunity this run acts on, through the low-risk write path."""
        account = self._ok_call(
            "create_account",
            {
                "name": f"Layer 6 proposal card {self.principal.tenant}",
                "transaction_key": _transaction_key(self.principal.tenant, "create_account"),
            },
        )
        if account is None:
            return False
        account_id = str((account.get("account") or {}).get("account_id") or "")
        opportunity = self._ok_call(
            "create_opportunity",
            {
                "account_id": account_id,
                "name": f"Proposal card target {self.principal.tenant}",
                "close_date": _CLOSE_DATE,
                "transaction_key": _transaction_key(self.principal.tenant, "create_opportunity"),
            },
        )
        if opportunity is None:
            return False
        self.opportunity_id = str(
            (opportunity.get("opportunity") or {}).get("opportunity_id") or ""
        )
        row = self._read_target()
        if not self.opportunity_id or row is None:
            self.results.append(
                CheckResult(
                    resource="proposal_card",
                    check="target_created",
                    status="fail",
                    detail=(
                        f"created opportunity id={self.opportunity_id!r} could not be read back "
                        "from the deployed table"
                    ),
                )
            )
            return False
        if row.get(_TARGET_FIELD) != _CREATED_STAGE:
            self.results.append(
                CheckResult(
                    resource="proposal_card",
                    check="target_created",
                    status="fail",
                    detail=(
                        f"target opportunity starts at {_TARGET_FIELD}="
                        f"{row.get(_TARGET_FIELD)!r}, expected {_CREATED_STAGE!r}, so a change to "
                        f"{_PROPOSED_STAGE!r} would not be observable"
                    ),
                )
            )
            return False
        self.results.append(
            CheckResult(
                resource="proposal_card",
                check="target_created",
                status="pass",
                detail=(
                    f"opportunity {self.opportunity_id} under account {account_id} at "
                    f"{_TARGET_FIELD}={_CREATED_STAGE!r}; {row_fingerprint(row)}"
                ),
            )
        )
        return True

    def _direct_high_risk_update_refused(self) -> CheckResult:
        """Assert the direct updater refuses the high-risk field, live, through the real API."""
        result = self.call_api(
            "update_opportunity",
            {
                "opportunity_id": self.opportunity_id,
                "fields": {_TARGET_FIELD: _PROPOSED_STAGE},
                "transaction_key": _transaction_key(self.principal.tenant, "direct_update"),
            },
        )
        return _refused_with_validation_failed(
            result,
            resource="direct_update",
            check="direct_high_risk_update_refused",
            what=f"update_opportunity setting the high-risk field {_TARGET_FIELD!r}",
        )

    def _model_driven_turn(
        self, before: Mapping[str, Any] | None
    ) -> tuple[Mapping[str, Any] | None, str]:
        """Run one real chat turn asking for the high-risk change, and return its card and reply.

        Returns the turn's `proposal` event when `model_driven_turn_emits_proposal_card` passed,
        else `None` with `card_unavailable` set; the reply is the turn's `completed` text.
        """
        prompt = (
            f"Move opportunity {self.opportunity_id} to the {_PROPOSED_STAGE} stage. "
            f"Its {_TARGET_FIELD} is currently {_CREATED_STAGE}."
        )
        session_id = f"smoke-proposal-card-{uuid.uuid4().hex}"
        result = self.call_runtime({"prompt": prompt, "session_id": session_id, "resume_from": 0})
        if isinstance(result, str) or result[0] >= 400:  # noqa: PLR2004 - HTTP error boundary
            detail = (
                result if isinstance(result, str) else f"status={result[0]} body={result[1][:300]}"
            )
            self.results.append(
                CheckResult(
                    resource="chat_turn",
                    check="turn_reached_the_runtime",
                    status="fail",
                    detail=f"the high-risk chat turn did not complete: {detail}",
                )
            )
            self.card_unavailable = (_UNEMITTED_DEFER_REASON, "turn_reached_the_runtime failed")
            return None, ""
        events = decode_sse_events(result[1])
        kinds = [str(event.get("kind")) for event in events]
        tools = [str(event.get("tool_name")) for event in events if event.get("tool_name")]
        if not events:
            self.results.append(
                CheckResult(
                    resource="chat_turn",
                    check="turn_reached_the_runtime",
                    status="fail",
                    detail=f"the turn returned no decodable SSE event: body={result[1][:300]!r}",
                )
            )
            self.card_unavailable = (_UNEMITTED_DEFER_REASON, "turn_reached_the_runtime failed")
            return None, ""
        self.results.append(
            CheckResult(
                resource="chat_turn",
                check="turn_reached_the_runtime",
                status="pass",
                detail=f"{len(events)} event(s) kinds={kinds!r} tools={tools!r}",
            )
        )

        reply_text = _completed_text(events)
        verdict = structured_proposal_check(
            events, resource="chat_turn", check="model_driven_turn_emits_proposal_card"
        )
        if verdict.status == "pass":
            self.results.append(verdict)
        elif not reply_text or _STUB_TEXT_MARKER in reply_text:
            self.results.append(
                CheckResult(
                    resource="chat_turn",
                    check="model_driven_turn_emits_proposal_card",
                    status=DEFER,
                    defer_reason=_STUB_CANNOT_PROPOSE,
                    detail=(
                        "the turn is shaped like the zero-cost stub provider's (no completed text, "
                        "or text carrying the stub marker), which cannot request a high-risk write, "
                        f"so no proposal could arise from the model's own choice: kinds={kinds!r} "
                        f"tools={tools!r}. The card rows need a live model"
                    ),
                )
            )
            self.card_unavailable = (
                _STUB_CANNOT_PROPOSE,
                "model_driven_turn_emits_proposal_card deferred: a stub-shaped turn raises no card",
            )
        else:
            self.results.append(verdict)
            self.card_unavailable = (
                _UNEMITTED_DEFER_REASON,
                "model_driven_turn_emits_proposal_card failed",
            )
        self.results.append(
            target_unchanged_check(
                resource="chat_turn",
                check="turn_mutated_nothing",
                before=before,
                after=self._read_target(),
                what="the target row across the model-driven high-risk turn",
            )
        )
        if verdict.status != "pass":
            return None, reply_text
        return find_proposal_event(events), reply_text

    def _proposal_input(self, step: str) -> dict[str, Any]:
        """Return the `submit_proposal` arguments raising the stage change, keyed to `step`."""
        return {
            "target_table": _OPPORTUNITY_TABLE,
            "target_id": self.opportunity_id,
            "operation": "update",
            "after_payload": {_TARGET_FIELD: _PROPOSED_STAGE},
            "reason": "Layer 6 proposal card smoke: advance the stage for approval",
            "transaction_key": _transaction_key(self.principal.tenant, step),
        }

    def _api_submitted_proposal_id(self) -> str:
        """Raise the proposal through the API's `submit_proposal` route; `""` when it failed."""
        submitted = self._ok_call(
            BY_NAME["submit_proposal"].name,
            self._proposal_input("submit_proposal_api"),
            check="nothing_mutated_at_proposal_time",
        )
        if submitted is None:
            return ""
        proposal_id = str((submitted.get("proposal") or {}).get("proposal_id") or "")
        if not proposal_id:
            self.results.append(
                CheckResult(
                    resource="proposal_card",
                    check="nothing_mutated_at_proposal_time",
                    status="fail",
                    detail=f"submit_proposal through the API returned no proposal id: {submitted!r}",
                )
            )
        return proposal_id

    def _snapshot_audit_baseline(self) -> None:
        """Record the target's audit ids immediately before the turn that raises the proposal."""
        try:
            self.audit_baseline = frozenset(self.audit_ids_for(self.opportunity_id))
        except Exception as exc:  # noqa: BLE001 - surfaced by every row that needs the baseline
            self.audit_baseline_error = f"{type(exc).__name__}: {exc}"

    def _new_audit_ids(self) -> tuple[list[str], str]:
        """The target's audit ids absent from the baseline, oldest first, and `""`; or `[]` and
        why they could not be determined."""
        if self.audit_baseline is None:
            return [], (
                "the target's audit ids could not be read before the proposal was raised "
                f"({self.audit_baseline_error or 'no baseline taken'}), so no record can be "
                "attributed to it"
            )
        try:
            audit_ids = self.audit_ids_for(self.opportunity_id)
        except Exception as exc:  # noqa: BLE001 - an unobservable boundary is not a passing one
            return [], f"could not read audit records for the target: {type(exc).__name__}: {exc}"
        return [a for a in audit_ids if a not in self.audit_baseline], ""

    def _no_audit_record_yet(self) -> CheckResult:
        """Assert raising the proposal wrote no audit record on the target."""
        resource, check = "proposal_card", "proposal_wrote_no_audit_record"
        new_ids, error = self._new_audit_ids()
        if error:
            return CheckResult(resource=resource, check=check, status="fail", detail=error)
        baseline = len(self.audit_baseline or ())
        if new_ids:
            return CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail=(
                    f"{len(new_ids)} audit record(s) written since the proposal was raised and "
                    f"before any approval: {new_ids!r}"
                ),
            )
        return CheckResult(
            resource=resource,
            check=check,
            status="pass",
            detail=(
                "no audit record was written for the target while the proposal is merely pending "
                f"({baseline} record(s) predating the proposal ignored)"
            ),
        )

    def _approve(self, proposal_id: str, *, before_approve: Mapping[str, Any] | None) -> None:
        """Approve through the deployed API and assert the commit and the audit record separately."""
        if not proposal_id:
            self.results.append(
                CheckResult(
                    resource="approve",
                    check="commits_the_change",
                    status="skip",
                    detail="no proposal id was obtained, so nothing could be approved",
                )
            )
            return
        approved = self._ok_call(
            "approve_proposal",
            {
                "proposal_id": proposal_id,
                "transaction_key": _transaction_key(self.principal.tenant, "approve"),
            },
            resource="approve",
            check="commits_the_change",
        )
        if approved is None:
            return
        committed = self._read_target()
        self.results.append(self._commit_check(committed))
        self.results.append(
            self._audit_check(before_approve=before_approve, after_approve=committed)
        )

    def _commit_check(self, committed: Mapping[str, Any] | None) -> CheckResult:
        resource, check = "approve", "commits_the_change"
        if committed is None:
            return CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail="the target row could not be read back after approval",
            )
        actual = committed.get(_TARGET_FIELD)
        if actual != _PROPOSED_STAGE:
            return CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail=(
                    f"approval did not commit: {_TARGET_FIELD}={actual!r}, expected "
                    f"{_PROPOSED_STAGE!r}"
                ),
            )
        return CheckResult(
            resource=resource,
            check=check,
            status="pass",
            detail=(
                f"{_TARGET_FIELD} committed as {actual!r} on the deployed row; "
                f"{row_fingerprint(committed)}"
            ),
        )

    def _audit_check(
        self,
        *,
        before_approve: Mapping[str, Any] | None,
        after_approve: Mapping[str, Any] | None,
    ) -> CheckResult:
        """Assert approval wrote exactly one new audit record, the approval's own, and that its
        snapshots bracket the committed change."""
        resource, check = "approve", "writes_the_audit_record"
        audit_ids, error = self._new_audit_ids()
        if error:
            return CheckResult(resource=resource, check=check, status="fail", detail=error)
        expected_id = idempotent_id("audit", _transaction_key(self.principal.tenant, "approve"))
        if audit_ids != [expected_id]:
            return CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail=(
                    "expected exactly one new audit record after approval, the approval's "
                    f"{expected_id!r}; found {audit_ids!r}"
                ),
            )
        audit_row = self.read_row(_AUDIT_TABLE, {"audit_id": audit_ids[0]})
        if audit_row is None:
            return CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail=f"audit record {audit_ids[0]!r} is indexed but not readable by primary key",
            )
        before_snapshot = audit_row.get("before_snapshot")
        after_snapshot = audit_row.get("after_snapshot")
        if not isinstance(before_snapshot, Mapping) or not isinstance(after_snapshot, Mapping):
            return CheckResult(
                resource=resource,
                check=check,
                status="fail",
                detail=(
                    f"audit record carries before_snapshot={type(before_snapshot).__name__} and "
                    f"after_snapshot={type(after_snapshot).__name__}; both must be objects"
                ),
            )
        problems: list[str] = []
        if canonical_row_bytes(before_snapshot) != canonical_row_bytes(before_approve):
            problems.append(
                f"before_snapshot {row_fingerprint(dict(before_snapshot))} != pre-approval row "
                f"{row_fingerprint(before_approve)}"
            )
        if canonical_row_bytes(after_snapshot) != canonical_row_bytes(after_approve):
            problems.append(
                f"after_snapshot {row_fingerprint(dict(after_snapshot))} != committed row "
                f"{row_fingerprint(after_approve)}"
            )
        if after_snapshot.get(_TARGET_FIELD) != _PROPOSED_STAGE:
            problems.append(
                f"after_snapshot records {_TARGET_FIELD}={after_snapshot.get(_TARGET_FIELD)!r}, "
                f"expected the approved {_PROPOSED_STAGE!r} — an audit record that does not record "
                "the approved change is not evidence the change was audited"
            )
        actor = str(audit_row.get("actor_rep_id") or "")
        if actor != self.principal.rep_id:
            problems.append(f"actor_rep_id={actor!r} != acting rep {self.principal.rep_id!r}")
        created_at = str(audit_row.get("created_at") or "")
        try:
            parsed = datetime.fromisoformat(created_at)
            timestamp_ok = parsed.tzinfo is not None or created_at.endswith("Z")
        except ValueError:
            timestamp_ok = False
        if not timestamp_ok:
            problems.append(f"created_at={created_at!r} is not a parseable UTC timestamp")
        if problems:
            return CheckResult(
                resource=resource, check=check, status="fail", detail="; ".join(problems)
            )
        return CheckResult(
            resource=resource,
            check=check,
            status="pass",
            detail=(
                f"one audit record {audit_ids[0]!r} by {actor!r} at {created_at!r}; snapshots "
                f"byte-match the row either side of the approval "
                f"({row_fingerprint(dict(before_snapshot))} -> "
                f"{row_fingerprint(dict(after_snapshot))})"
            ),
        )

    # -- helpers -------------------------------------------------------------------------------

    def _read_target(self) -> dict[str, Any] | None:
        if not self.opportunity_id:
            return None
        return self.read_row(_OPPORTUNITY_TABLE, {"opportunity_id": self.opportunity_id})

    def _ok_call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        resource: str = "proposal_card",
        check: str = "target_created",
    ) -> dict[str, Any] | None:
        """Issue one call expected to succeed, recording a failing check when it does not."""
        result = self.call_api(tool_name, arguments)
        if isinstance(result, str):
            self.results.append(
                CheckResult(
                    resource=resource,
                    check=check,
                    status="fail",
                    detail=f"{tool_name}: {result}",
                )
            )
            return None
        status_code, body = result
        if status_code >= 400:  # noqa: PLR2004 - the HTTP client/server error boundary
            self.results.append(
                CheckResult(
                    resource=resource,
                    check=check,
                    status="fail",
                    detail=f"{tool_name} failed: status={status_code} body={body[:400]}",
                )
            )
            return None
        return _parse_json(body) or {}


# -- entry points -----------------------------------------------------------------------------


def checks_with_seams(
    *,
    call_api: ApiCaller,
    call_runtime: RuntimeCaller,
    read_row: RowReader,
    audit_ids_for: Callable[[str], list[str]],
    principal: Principal,
) -> list[CheckResult]:
    """Run every assertion against already-built seams. The offline self-test's entry point.

    `call_runtime` carries the chat turn through the relay.
    """
    probe = _ProposalCardProbe(
        call_api=call_api,
        call_runtime=call_runtime,
        read_row=read_row,
        audit_ids_for=audit_ids_for,
        principal=principal,
    )
    return probe.run()


def checks(
    ctx: SmokeContext, *, http_caller: HttpCaller | None = None, jwks_url: str | None = None
) -> Iterable[CheckResult]:
    """Provision a disposable principal, run the proposal-card round trip, and clean up.

    The `demo` baseline is sampled before anything is provisioned, and both tenant-delta assertions
    run before teardown. Access tokens are verified against `jwks_url`, the pool's JWKS by default.
    """
    caller = http_caller if http_caller is not None else default_http_caller
    results: list[CheckResult] = []
    provisioner = LiveProposalCardPrincipal(ctx, jwks_url=jwks_url)
    store = provisioner.store()
    reader = store_row_reader(store)
    demo_before: TenantSnapshot = read_tenant_snapshot(reader, DEMO_TENANT)

    try:
        principal = provisioner.provision()
        results.append(
            CheckResult(
                resource="smoke_principal",
                check="mfa_tokens_issued",
                status="pass",
                detail=(
                    f"acting as rep {principal.rep_id!r} under disposable tenant "
                    f"{principal.tenant!r}; {principal.detail}"
                ),
            )
        )
        results.extend(
            checks_with_seams(
                call_api=live_api_caller(
                    caller, ctx.outputs.require_str("cloudfront_domain_name"), principal.id_token
                ),
                call_runtime=live_runtime_caller(ctx, principal.access_token),
                read_row=lambda table, key: store.get_item(table, dict(key)),
                audit_ids_for=lambda target_id: _audit_ids_for_target(
                    store, tenant=principal.tenant, target_id=target_id
                ),
                principal=principal,
            )
        )
    except Exception as exc:  # noqa: BLE001 - reported as a failing check, never a crash
        results.append(
            CheckResult(
                resource="proposal_card",
                check="round_trip_completed",
                status="fail",
                detail=f"could not complete the proposal-card round trip: {type(exc).__name__}: {exc}",
            )
        )
    finally:
        results.append(
            demo_tenant_untouched_check(resource="proposal_card", before=demo_before, reader=reader)
        )
        if provisioner.tenant():
            results.append(
                writes_only_under_smoke_tenant_check(
                    resource="proposal_card", tenant=provisioner.tenant(), reader=reader
                )
            )
        results.extend(provisioner.cleanup())
        results.extend(_deferrals_for_unemitted(results, _EXPECTED_CHECKS))
    return results


def _audit_ids_for_target(store: Store, *, tenant: str, target_id: str) -> list[str]:
    """Return every audit record id targeting `target_id` under `tenant`, oldest first.

    Queries the `audit_records.gsi_target` index on the tenant-prefixed partition key.
    """
    partition_value = tenant_prefixed(tenant, target_id)
    items, cursor = store.query_gsi(_AUDIT_TABLE, "gsi_target", partition_value=partition_value)
    while cursor is not None:
        page, cursor = store.query_gsi(
            _AUDIT_TABLE,
            "gsi_target",
            partition_value=partition_value,
            exclusive_start_key=cursor,
        )
        items.extend(page)
    return [str(item["audit_id"]) for item in items if item.get("audit_id") is not None]


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the proposal-card round trip and return 0 pass / 1 fail."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

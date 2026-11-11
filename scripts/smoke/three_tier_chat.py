# SPDX-License-Identifier: MIT
"""Layer 5 T3 smoke: three-tier delegation attribution + conversation-history cross-session denial.

**Check 1 — three-tier CloudTrail attribution.** One delegation-forcing turn must drive Bedrock
invocations at all three tiers, attributed by CloudTrail to the Runtime role on each tier's assigned
model profile. CloudTrail tells tiers apart only by profile, so the turn runs under the first
mapping of :data:`ATTRIBUTION_CANDIDATES` whose three assigned profiles are distinct; when none is,
the check is one `fail` row naming the collision and the turn is not sent. The supervisor-tier
invocation, present on every turn, is the positive control before a missing subagent- or tool-tier
invocation is reported. It covers the Runtime role, the window and the supervisor family's
CloudTrail event name only; a lower tier on the other family is queried under that family's event
name, which the control does not cover. The turn's `completed` frame also gets check 2's rows, each
suffixed :data:`ATTRIBUTION_ROW_SUFFIX`, for its mapping's models and tier efforts.

**Check 2 — relay turn cost.** The turn that seeds check 4's session is a
delegation-forcing turn under a mixed-family wire tuning; its `completed` frame must carry non-empty
`cost_lines`, each carrying a non-negative integer `long_context_calls`, a positive `cost_usd` with
`cost_withheld` not set, `resolved_tiers` naming each
tier's requested family at its `config/models.yaml` model and tier effort, and a priced cost
line for each tier's `config/models.yaml` model.

**Check 3 — temperature refusal.** A schema-2 tuning carrying a `temperature` is refused
by the Runtime as one SSE `error` frame (:data:`REFUSAL_ERROR_KIND`) whose message names
`tuning.tiers.<tier>.temperature`, with no `usage` or `completed` frame. It makes no model call and is not counted.

**Check 4 — session owner-binding / cross-session denial.** User A takes a turn under session S,
then user B reusing S is refused as a session collision. The Runtime raises
`crm.errors.ScopeDenied` (403) before any history read; AgentCore discards the container body and
the relay maps the container 4xx to an HTTP 400 whose message is `GENERIC_VALIDATION_MESSAGE`, which
is what this check asserts. The control is user A reusing its own session S and getting a 200.

**Check 5 — openai-supervisor turn.** One delegation-forcing turn under
:data:`OPENAI_FAMILIES` gets check 2's rows, each suffixed :data:`OPENAI_ROW_SUFFIX`, so
`supervisor_resolved_effort_is_<effort>` is proven for an openai supervisor as well.

**Check 6 — prompt-cache read on the wire.** For each family, the second of two
supervisor turns sharing the stable prefix must report `cache_read_tokens > 0` on its supervisor
cost line: the anthropic pair is check 2's seed and owner-reuse turns, the openai pair is check 5's
turn and :data:`_OPENAI_CACHE_PROBE_PROMPT`. A read of zero when neither turn reported a cache
write is a `defer` (:data:`CACHE_UNREPORTED_REASON`); a read of zero after a reported write fails.

**Check 7 — page-context route refusal.** The openai cache probe carries a conforming
`page_context` and is the control. Two turns then plant :data:`page_context_marker` in an
over-bound and an off-charset `route`; each must be refused with HTTP 400 carrying the relay's
generic validation message, with the marker absent from the response body. They make no model call
and are not counted.

Runtime-role Bedrock IAM is checked by `scripts/smoke/runtime_bedrock_iam.py`, not here.

Tokens come from disposable smoke users removed in a `finally`; never the `demo` or `canary`
tenant. Billable: a live turn makes real model calls. Model-backed turns are counted against this
module's share of the post-deploy envelope (:data:`MODEL_TURN_ALLOTMENTS`) and the count is printed;
a turn past the share is not issued.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, NamedTuple

from scripts.smoke.harness import DEFER, CheckResult, SmokeContext, open_https, run_main

from crm.toolspec.page_context import ROUTE_MAX_LENGTH
from providers.capabilities import CapabilityMatrix, load_capability_matrix
from providers.tier_tuning import WIRE_SCHEMA_VERSION

LAYER = 5
RESOURCE_CLASS = "three_tier_chat"

#: The family whose three tiers this smoke drives; every turn names it explicitly per tier.
_FAMILY: Final[str] = "anthropic"

#: The tiers a wire tuning block names (`providers.tier_tuning.TIERS`).
_TIERS: Final[tuple[str, ...]] = ("supervisor", "subagent", "tool")

#: The mixed-family tuning check 2's turn runs under: one tier on the other family.
MIXED_FAMILIES: Final[Mapping[str, str]] = {
    "supervisor": "anthropic",
    "subagent": "openai",
    "tool": "anthropic",
}

#: Check 5's tuning: every tier on openai, so the supervisor is the openai one.
OPENAI_FAMILIES: Final[Mapping[str, str]] = dict.fromkeys(_TIERS, "openai")

#: An openai supervisor delegating to anthropic subagent and tool tiers: when the all-anthropic
#: candidate's profiles collide, this keeps anthropic on check 1's subagent tier, which no other
#: model-backed turn runs.
ANTHROPIC_DELEGATES_FAMILIES: Final[Mapping[str, str]] = {
    "supervisor": "openai",
    "subagent": "anthropic",
    "tool": "anthropic",
}

#: Check 1's candidate `{tier: family}` mappings, in preference order.
ATTRIBUTION_CANDIDATES: Final[tuple[Mapping[str, str], ...]] = (
    dict.fromkeys(_TIERS, _FAMILY),
    ANTHROPIC_DELEGATES_FAMILIES,
    MIXED_FAMILIES,
)

#: Check 1's row when no candidate mapping assigns three distinct profiles.
ATTRIBUTION_COLLISION_CHECK: Final[str] = "attribution_mapping_profiles_distinct"

#: Appended to each of check 5's row names, keeping them apart from check 2's.
OPENAI_ROW_SUFFIX: Final[str] = ":openai_supervisor"

#: Appended to each of check 1's turn-cost row names, keeping them apart from check 2's.
ATTRIBUTION_ROW_SUFFIX: Final[str] = ":attribution"

#: Post-deploy model-backed turns, split between the modules that spend them.
MODEL_TURN_ENVELOPE: Final[int] = 20
MODEL_TURN_ALLOTMENTS: Final[Mapping[str, int]] = {
    "three_tier_chat": 5,
    "mcp_chat_gateway": 2,
    "agents": 1,
}

#: The `error_kind` of the Runtime's turn-level validation refusal (`agents.runtime_server`).
REFUSAL_ERROR_KIND: Final[str] = "invalid_request"

#: The tier whose request carries the refused `temperature` in check 3.
_REFUSAL_TIER: Final[str] = "supervisor"

#: The retired wire schema a stale client sends with its temperature.
_STALE_WIRE_SCHEMA: Final[int] = 2

#: Check 6's `defer_reason` when neither turn of a pair reported any prompt-cache token.
CACHE_UNREPORTED_REASON: Final[str] = "platform_reported_no_cache_tokens"

#: Check 6's second openai turn: no tool needed, so it is one supervisor round.
_OPENAI_CACHE_PROBE_PROMPT: Final[str] = "Reply with the single word: ready. Do not call any tool."

#: Check 7's prompt; the turn is refused before any model call.
_PAGE_REFUSAL_PROMPT: Final[str] = "Reply with the single word: ready."

#: The `route` of check 7's conforming control `page_context`.
_CONTROL_ROUTE: Final[str] = "/accounts"


def page_context_for(route: str) -> dict[str, Any]:
    """An entity `page_context` whose only variable field is `route`."""
    return {"kind": "entity", "route": route, "entity_type": "accounts", "entity_id": "a-1"}


def page_context_marker() -> str:
    """A run-unique, route-charset-safe token planted in check 7's refused routes."""
    return f"zqplant{_suffix()[:16]}"


def refused_routes(marker: str) -> dict[str, str]:
    """Check 7's `{row_name: route}`: one longer than `ROUTE_MAX_LENGTH`, one off the charset."""
    return {
        "page_context_over_bound_route_refused": (f"/accounts/{marker}" + "a" * ROUTE_MAX_LENGTH),
        "page_context_off_charset_route_refused": f"/accounts/{marker}<script>",
    }


def tuning_block(families: Mapping[str, str]) -> dict[str, Any]:
    """A current-schema wire tuning block selecting `families[tier]` for every tier."""
    return {
        "schema": WIRE_SCHEMA_VERSION,
        "source": "smoke.three_tier_chat",
        "tiers": {tier: {"family": families[tier]} for tier in _TIERS},
    }


def stale_temperature_block() -> dict[str, Any]:
    """A schema-2 block whose :data:`_REFUSAL_TIER` request still carries a `temperature`."""
    tiers: dict[str, dict[str, Any]] = {tier: {"family": _FAMILY} for tier in _TIERS}
    tiers[_REFUSAL_TIER]["temperature"] = 0.5
    return {"schema": _STALE_WIRE_SCHEMA, "source": "smoke.three_tier_chat", "tiers": tiers}


def expected_tier_models(families: Mapping[str, str]) -> dict[str, str]:
    """`{tier: model_id}` that `config/models.yaml` assigns to `families[tier]` for each tier."""
    matrix = load_capability_matrix()
    return {
        tier: matrix.model_for(family, tier).model_id  # type: ignore[arg-type]
        for tier, family in families.items()
    }


def expected_tier_efforts(families: Mapping[str, str]) -> dict[str, str]:
    """`{tier: effort}` that `config/models.yaml` configures for `families[tier]`."""
    matrix = load_capability_matrix()
    return {
        tier: matrix.tiers[family][tier].tuning["effort"]  # type: ignore[index]
        for tier, family in families.items()
    }


class ModelTurnBudget:
    """Counts one module's model-backed turns against its :data:`MODEL_TURN_ALLOTMENTS` share."""

    def __init__(self, module: str) -> None:
        self.module = module
        self.allotment = MODEL_TURN_ALLOTMENTS[module]
        self.used: list[str] = []

    def spend(self, label: str) -> bool:
        """Record one model-backed turn; `False` (nothing recorded) once the share is spent."""
        if len(self.used) >= self.allotment:
            return False
        self.used.append(label)
        print(
            f"[{self.module}] model-backed turn {len(self.used)}/{self.allotment} "
            f"(envelope {MODEL_TURN_ENVELOPE}): {label}",
            file=sys.stderr,
        )
        return True

    def result(self) -> CheckResult:
        """The row reporting the count; `fail` only when a turn was refused for the share."""
        return CheckResult(
            resource=self.module,
            check="model_backed_turns_within_envelope",
            status="pass" if len(self.used) <= self.allotment else "fail",
            detail=(
                f"{len(self.used)} model-backed turn(s) of this module's {self.allotment} "
                f"(envelope {MODEL_TURN_ENVELOPE} across {dict(MODEL_TURN_ALLOTMENTS)}): "
                f"{self.used}"
            ),
        )


def budget_refused(budget: ModelTurnBudget, label: str) -> CheckResult:
    """The row for a model-backed turn not issued because the module's share is spent."""
    return CheckResult(
        resource=budget.module,
        check="model_backed_turns_within_envelope",
        status="fail",
        detail=(
            f"{label!r} was NOT issued: {len(budget.used)} of {budget.allotment} model-backed "
            f"turn(s) already spent ({budget.used}); the deploy's envelope is "
            f"{MODEL_TURN_ENVELOPE}"
        ),
    )


#: The tool the delegation prompt commands: a mutation-subagent tool outside the supervisor's direct
#: tools (`agents.supervisor.DIRECT_TOOL_NAMES`), so the supervisor must delegate, and one that needs
#: no pre-existing rows in the unseeded smoke tenant.
_DELEGATION_FORCING_TOOL: Final[str] = "create_account"

#: A prompt that forces the supervisor to delegate a write it cannot perform directly, so all three
#: tiers fire even against an empty tenant.
_DELEGATION_PROMPT: Final[str] = (
    "Create a new account called 'Northwind Traders' and record a brief note that the "
    "introductory call went well."
)

#: Check 2's delegation-forcing prompt, so the subagent and tool tiers are priced under the mixed
#: tuning; a different account name from check 1's.
_MIXED_DELEGATION_PROMPT: Final[str] = (
    "Create a new account called 'Fabrikam Holdings' and record a brief note that the "
    "renewal is due in Q3."
)

#: Check 5's delegation-forcing prompt; a different account name from checks 1 and 2.
_OPENAI_DELEGATION_PROMPT: Final[str] = (
    "Create a new account called 'Contoso Analytics' and record a brief note that the "
    "pilot starts next month."
)

#: CloudTrail event names of the vendor-SDK transport: the Anthropic Messages stream for every
#: Anthropic tier, and the OpenAI Responses path. Any other invoke event is not queried.
_ANTHROPIC_INVOKE_EVENT: Final[str] = "InvokeModelWithResponseStream"
_OPENAI_INVOKE_EVENT: Final[str] = "Responses"
_BEDROCK_INVOKE_EVENTS: Final[frozenset[str]] = frozenset(
    {_ANTHROPIC_INVOKE_EVENT, _OPENAI_INVOKE_EVENT}
)

#: CloudTrail ingestion lag; the attribution lookup polls up to this long for every tier's event to
#: appear (twice the typical ~5-minute delivery latency).
_CLOUDTRAIL_POLL_SECONDS: Final[float] = 600.0
_CLOUDTRAIL_POLL_INTERVAL: Final[float] = 20.0
#: The tiers one delegation turn invokes, in the order the attribution rows are emitted.
_ATTRIBUTION_TIERS: Final[tuple[str, ...]] = ("supervisor", "subagent", "tool")
#: LookupEvents page size (the API maximum) and the page cap per event name.
_CLOUDTRAIL_PAGE_SIZE: Final[int] = 50
_CLOUDTRAIL_MAX_PAGES: Final[int] = 20
_TURN_TIMEOUT_SECONDS: Final[float] = 150.0

#: The relay's generic message for a Runtime container-4xx rejection, asserted by check 4. Must equal
#: `GENERIC_VALIDATION_MESSAGE` in `src/relay/src/errorClassification.ts`.
_GENERIC_VALIDATION_MESSAGE: Final[str] = (
    "The assistant could not accept this request. Adjust your selection and try again."
)


@dataclass(frozen=True, slots=True)
class TurnResult:
    """The observable outcome of one relay turn.

    `status` is the HTTP status the relay returned (200 for a streamed turn; 400 for a session
    collision). On a non-2xx, `message` is the text of the relay's pre-stream rejection body
    `{"message": <text>}` (`rejectBeforeStream` in `src/relay/src/index.ts`) and `raw_body` is that
    body as received.
    """

    status: int
    kinds: tuple[str, ...]
    answer: str
    message: str = ""
    completed: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    raw_body: str = ""


@dataclass(frozen=True, slots=True)
class CloudTrailEvent:
    """The two fields the attribution check reads from a CloudTrail record."""

    role_name: str
    model_id: str


#: `(ctx, token, session_id, prompt, tuning) -> TurnResult`, plus an optional `page_context=`
#: keyword. One relay turn. Live default POSTs through CloudFront; the offline test injects a fake.
RunTurn = Callable[..., TurnResult]
#: `(ctx, start_ms, end_ms) -> list[CloudTrailEvent]`. Live default calls CloudTrail LookupEvents.
CloudTrailLookup = Callable[[SmokeContext, int, int], "list[CloudTrailEvent]"]
#: `(ctx) -> provisioner`. Builds a fresh disposable-user provisioner. Injected by the test.
ProvisionerFactory = Callable[[SmokeContext], Any]


def _tier_profiles(
    families: Mapping[str, str], matrix: CapabilityMatrix | None = None
) -> dict[str, str]:
    """`{tier: inference_profile}` of the model `config/models.yaml` assigns to `families[tier]`."""
    loaded = matrix if matrix is not None else load_capability_matrix()
    return {
        tier: loaded.model_for(family, tier).inference_profile  # type: ignore[arg-type]
        for tier, family in families.items()
    }


def mapping_label(families: Mapping[str, str]) -> str:
    """`families` rendered as `tier=family,...` for row details."""
    return ",".join(f"{tier}={family}" for tier, family in families.items())


class AttributionMapping(NamedTuple):
    """Check 1's selected mapping and its profiles, or `None` with every candidate's profiles."""

    families: Mapping[str, str] | None
    profiles: dict[str, str]
    tried: tuple[tuple[Mapping[str, str], dict[str, str]], ...]


def select_attribution_mapping(matrix: CapabilityMatrix | None = None) -> AttributionMapping:
    """The first :data:`ATTRIBUTION_CANDIDATES` mapping whose assigned profiles are distinct."""
    loaded = matrix if matrix is not None else load_capability_matrix()
    tried: list[tuple[Mapping[str, str], dict[str, str]]] = []
    for families in ATTRIBUTION_CANDIDATES:
        profiles = _tier_profiles(families, loaded)
        tried.append((families, profiles))
        if len(set(profiles.values())) == len(profiles):
            return AttributionMapping(families, profiles, tuple(tried))
    return AttributionMapping(None, {}, tuple(tried))


def attribution_collision_row(selection: AttributionMapping) -> CheckResult:
    """Check 1's `fail` row when no candidate mapping has three distinct profiles."""
    collisions = "; ".join(
        f"{mapping_label(families)} -> {profiles}" for families, profiles in selection.tried
    )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=ATTRIBUTION_COLLISION_CHECK,
        status="fail",
        detail=(
            "no candidate mapping assigns three distinct inference profiles, so CloudTrail cannot "
            f"attribute each tier; the delegation turn was not sent: {collisions}"
        ),
    )


def _runtime_role_name(ctx: SmokeContext) -> str:
    """The Runtime role's name, from its ARN output (the `role/<name>` tail)."""
    arn = ctx.outputs.require_str("agentcore_runtime_role_arn")
    return arn.rsplit("/", 1)[-1]


def _live_run_turn(
    ctx: SmokeContext,
    token: str,
    session_id: str,
    prompt: str,
    tuning: Mapping[str, Any],
    *,
    page_context: Mapping[str, Any] | None = None,
) -> TurnResult:
    """Issue one authenticated turn through CloudFront -> relay -> Runtime, returning status + reason.

    On a 2xx the body is an SSE stream, parsed to frame kinds and the completed answer, `status=200`.
    On a non-2xx (the session-collision **400** check 4 asserts) the relay's pre-stream rejection body
    is `{"message": <text>}`, read from `HTTPError` into `status`/`message`.

    The request headers mirror `scripts.smoke_agent_stream.live_source`: `X-Crm-Authorization`
    carries the JWT because `Authorization` holds CloudFront's OAC SigV4 signature, and
    `x-amz-content-sha256` is the digest of the exact bytes sent.
    """
    from scripts.smoke_agent_stream import parse_sse_bytes

    domain = ctx.outputs.require_str("cloudfront_domain_name")
    runtime_arn = ctx.outputs.require_str("agentcore_runtime_arn")
    url = f"https://{domain}/agent/{urllib.parse.quote(runtime_arn, safe='')}"
    body_fields: dict[str, Any] = {
        "prompt": prompt,
        "session_id": session_id,
        "resume_from": 0,
        "tuning": dict(tuning),
    }
    if page_context is not None:
        body_fields["page_context"] = dict(page_context)
    payload = json.dumps(body_fields).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed https CloudFront host, internal seam
        url,
        data=payload,
        method="POST",
        headers={
            "X-Crm-Authorization": f"Bearer {token}",
            "x-amz-content-sha256": hashlib.sha256(payload).hexdigest(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Accept-Encoding": "identity",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        },
    )
    try:
        with open_https(request, timeout=_TURN_TIMEOUT_SECONDS) as response:
            kinds: list[str] = []
            answer = ""
            completed: Mapping[str, Any] | None = None
            error: Mapping[str, Any] | None = None
            for event in parse_sse_bytes(iter(lambda: response.read1(65536), b"")):
                try:
                    parsed = json.loads(event.data)
                except json.JSONDecodeError:
                    continue
                kind = str(parsed.get("kind") or "")
                kinds.append(kind)
                if kind == "completed":
                    answer = str(parsed.get("text") or "")
                    completed = parsed
                elif kind == "error":
                    error = parsed
            return TurnResult(
                status=response.status,
                kinds=tuple(kinds),
                answer=answer,
                completed=completed,
                error=error,
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        body: dict[str, Any] = {}
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            body = json.loads(raw)
        return TurnResult(
            status=exc.code,
            kinds=(),
            answer="",
            message=str(body.get("message", "")) if isinstance(body, dict) else "",
            raw_body=raw.decode("utf-8", errors="replace"),
        )


def _live_cloudtrail_lookup(ctx: SmokeContext, start_ms: int, end_ms: int) -> list[CloudTrailEvent]:
    """LookupEvents for Bedrock model-invocation events in the window, parsed to (role, model_id).

    Follows ``NextToken`` for every event name, up to ``_CLOUDTRAIL_MAX_PAGES`` pages per name.
    Raises ``RuntimeError`` if a name still has pages left at the cap.
    """
    client = ctx.client("cloudtrail")
    events: list[CloudTrailEvent] = []
    for event_name in sorted(_BEDROCK_INVOKE_EVENTS):
        request: dict[str, Any] = {
            "LookupAttributes": [{"AttributeKey": "EventName", "AttributeValue": event_name}],
            "StartTime": _ms_to_seconds(start_ms),
            "EndTime": _ms_to_seconds(end_ms),
            "MaxResults": _CLOUDTRAIL_PAGE_SIZE,
        }
        for _page in range(_CLOUDTRAIL_MAX_PAGES):
            response = client.lookup_events(**request)
            for record in response.get("Events", []):
                events.append(_parse_cloudtrail_record(record))
            next_token = response.get("NextToken")
            if not next_token:
                break
            request["NextToken"] = next_token
        else:
            raise RuntimeError(
                f"CloudTrail LookupEvents for {event_name} exceeded {_CLOUDTRAIL_MAX_PAGES} pages; "
                "the attribution window was not read in full"
            )
    return events


def _ms_to_seconds(ms: int) -> float:
    return ms / 1000.0


def _parse_cloudtrail_record(record: Mapping[str, Any]) -> CloudTrailEvent:
    """Extract the invoking role name and the requested modelId from one CloudTrail record."""
    detail = record.get("CloudTrailEvent")
    parsed: Mapping[str, Any] = json.loads(detail) if isinstance(detail, str) else {}
    identity = parsed.get("userIdentity") or {}
    session_issuer = (identity.get("sessionContext") or {}).get("sessionIssuer") or {}
    role_name = str(session_issuer.get("userName") or identity.get("userName") or "")
    request = parsed.get("requestParameters") or {}
    model_id = str(request.get("modelId") or "")
    return CloudTrailEvent(role_name=role_name, model_id=model_id)


def _tier_attributed(
    events: Sequence[CloudTrailEvent],
    tier: str,
    *,
    runtime_role: str,
    tier_profiles: Mapping[str, str],
) -> bool:
    """Whether some event in ``events`` is ``tier``'s inference profile invoked by ``runtime_role``."""
    profile = tier_profiles.get(tier, "")
    if not profile:
        return False
    profile_id = profile.removeprefix("global.")
    return any(
        e.role_name == runtime_role and (profile in e.model_id or profile_id in e.model_id)
        for e in events
    )


class CloudTrailPoll(NamedTuple):
    """The last LookupEvents snapshot of a bounded poll, and how the poll ended."""

    events: list[CloudTrailEvent]
    polls: int
    elapsed_seconds: float

    def summary(self) -> str:
        """Render the poll's counts and the distinct ``(role, model_id)`` pairs it saw."""
        pairs = sorted({(e.role_name, e.model_id) for e in self.events})
        return (
            f"polls={self.polls} elapsed={self.elapsed_seconds:.0f}s events={len(self.events)} "
            f"pairs={pairs}"
        )


def evaluate_three_tier_attribution(
    events: Sequence[CloudTrailEvent],
    *,
    runtime_role: str,
    tier_profiles: Mapping[str, str],
    poll_summary: str = "",
    families: Mapping[str, str] | None = None,
) -> Iterable[CheckResult]:
    """Check 1: supervisor-tier control, then subagent- and tool-tier attribution assertions.

    The control covers the Runtime role, the window and the supervisor family's event name; a
    lower tier on the other family is not covered by it. ``families`` (the turn's mapping) and
    ``poll_summary``, when given, are appended to every row's detail.
    """
    notes = [f"mapping {mapping_label(families)}"] if families else []
    notes += [poll_summary] if poll_summary else []
    suffix = f" [{'; '.join(notes)}]" if notes else ""

    def attributed(tier: str) -> bool:
        return _tier_attributed(
            events, tier, runtime_role=runtime_role, tier_profiles=tier_profiles
        )

    control_ok = attributed("supervisor")
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="supervisor_tier_attribution_control",
        status="pass" if control_ok else "fail",
        detail=(
            f"a supervisor-tier invocation ({tier_profiles.get('supervisor')}) is attributed to the "
            f"Runtime role {runtime_role} in the window — the CloudTrail query sees this event type, "
            "role, and window"
            if control_ok
            else f"NO supervisor-tier invocation ({tier_profiles.get('supervisor')}) from the Runtime "
            f"role {runtime_role} in the window: every turn invokes the supervisor, so this is a blind "
            "query (wrong role, window or supervisor-family event name) — the tier assertions below "
            "cannot be trusted"
        )
        + suffix,
    )
    if not control_ok:
        return
    for tier in ("subagent", "tool"):
        ok = attributed(tier)
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check=f"{tier}_tier_attribution",
            status="pass" if ok else "fail",
            detail=(
                f"a {tier}-tier invocation ({tier_profiles.get(tier)}) is attributed to the Runtime "
                "role — delegation reached this tier"
                if ok
                else f"no {tier}-tier invocation ({tier_profiles.get(tier)}) attributed to the "
                "Runtime role in the window: delegation did not reach this tier, or this tier's family "
                "event name is not queried correctly (the supervisor control above covers the role, "
                "the window and the supervisor family's event name only)"
            )
            + suffix,
        )


def evaluate_history_cross_session(
    owner_reuse: TurnResult, foreign: TurnResult
) -> Iterable[CheckResult]:
    """Check 4: owner-reuse control (session reuse is permitted), then the foreign-reuse denial.

    The foreign reuse must return HTTP 400 carrying the relay's `GENERIC_VALIDATION_MESSAGE`; a 400
    with a different message does not pass.
    """
    control_ok = owner_reuse.status == 200
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="owner_session_reuse_control",
        status="pass" if control_ok else "fail",
        detail=(
            "user A reusing A's own session_id succeeded (200) — session-id reuse is permitted for "
            "the owner, so B's refusal below is specifically the owner-binding, not a blanket "
            "rejection of reuse"
            if control_ok
            else f"user A reusing A's own session_id did NOT succeed (status={owner_reuse.status}, "
            f"message={owner_reuse.message!r}): session reuse is not working for the owner, so the "
            "foreign-denial check below is untrustworthy"
        ),
    )
    if not control_ok:
        return
    denied = foreign.status == 400 and foreign.message == _GENERIC_VALIDATION_MESSAGE
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check="foreign_session_reuse_refused",
        status="pass" if denied else "fail",
        detail=(
            "user B reusing A's session_id was refused with HTTP 400 + the relay's generic "
            "validation message — the session owner-binding raised 403 in the Runtime, which AgentCore "
            "surfaces as a container-4xx 400 through the relay"
            if denied
            else "user B reusing A's session_id was NOT refused as a session collision: expected "
            f"HTTP 400 with the generic validation message, got status={foreign.status} "
            f"message={foreign.message!r}"
        ),
    )


def evaluate_turn_cost(
    result: TurnResult,
    requested_families: Mapping[str, str],
    expected_models: Mapping[str, str],
    expected_efforts: Mapping[str, str],
    *,
    row_suffix: str = "",
    turn_label: str = "mixed-family",
) -> Iterable[CheckResult]:
    """Checks 1, 2 and 5: the `completed` frame's `cost_lines`, `cost_usd`, `resolved_tiers` and models.

    `expected_models` is `{tier: model_id}`; each needs a cost line on that tier naming that model
    with a numeric `cost_usd`. `expected_efforts` is `{tier: effort}`; each tier's `resolved_tiers`
    entry must name that tier's expected model at that effort. Every row name ends in `row_suffix`;
    `turn_label` names the turn in the no-frame detail.
    """
    frame = result.completed
    lines_check, long_context_check, cost_check, tiers_check = (
        f"{name}{row_suffix}"
        for name in (
            "cost_lines_present",
            "cost_lines_carry_long_context_calls",
            "cost_usd_positive",
            "resolved_tiers_match_tuning",
        )
    )
    model_checks = [
        f"{tier}_cost_line_names_{model}{row_suffix}" for tier, model in expected_models.items()
    ]
    effort_checks = [
        f"{tier}_resolved_effort_is_{effort}{row_suffix}"
        for tier, effort in expected_efforts.items()
    ]
    if frame is None:
        for check in (
            lines_check,
            long_context_check,
            cost_check,
            tiers_check,
            *model_checks,
            *effort_checks,
        ):
            yield CheckResult(
                resource=RESOURCE_CLASS,
                check=check,
                status="fail",
                detail=f"the {turn_label} turn produced no completed frame "
                f"(status={result.status} kinds={list(result.kinds)})",
            )
        return
    lines = frame.get("cost_lines")
    lines_ok = isinstance(lines, list) and bool(lines)
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check=lines_check,
        status="pass" if lines_ok else "fail",
        detail=f"completed.cost_lines={lines!r}"
        + ("" if lines_ok else ", expected a non-empty list of priced (tier, role, model) groups"),
    )
    counts = [
        line.get("long_context_calls") if isinstance(line, Mapping) else None
        for line in (lines if isinstance(lines, list) else [])
    ]
    counts_ok = bool(counts) and all(
        isinstance(c, int) and not isinstance(c, bool) and c >= 0 for c in counts
    )
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check=long_context_check,
        status="pass" if counts_ok else "fail",
        detail=f"cost_lines long_context_calls={counts!r}"
        + ("" if counts_ok else ", expected a non-negative integer on every cost line"),
    )
    cost = frame.get("cost_usd")
    withheld = frame.get("cost_withheld")
    cost_ok = (
        isinstance(cost, int | float) and not isinstance(cost, bool) and cost > 0 and not withheld
    )
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check=cost_check,
        status="pass" if cost_ok else "fail",
        detail=f"completed.cost_usd={cost!r} cost_withheld={withheld!r}"
        + (
            ""
            if cost_ok
            else ", expected a positive cost and no withholding (config/pricing.yaml is not "
            "provisional, so a withheld or zero cost means the frame is not priced)"
        ),
    )
    resolved = frame.get("resolved_tiers")
    observed: dict[str, object] = {}
    entries: dict[str, Mapping[str, object]] = {}
    if isinstance(resolved, list):
        for entry in resolved:
            if isinstance(entry, Mapping):
                observed[str(entry.get("tier"))] = entry.get("family")
                entries[str(entry.get("tier"))] = entry
    tiers_ok = observed == dict(requested_families)
    yield CheckResult(
        resource=RESOURCE_CLASS,
        check=tiers_check,
        status="pass" if tiers_ok else "fail",
        detail=f"resolved_tiers families {observed!r}, requested {dict(requested_families)!r}",
    )
    priced = (
        [line for line in lines if isinstance(line, Mapping)] if isinstance(lines, list) else []
    )
    for (tier, model), check in zip(expected_models.items(), model_checks, strict=True):
        matched = [
            line for line in priced if line.get("tier") == tier and line.get("model_id") == model
        ]
        model_ok = any(
            isinstance(line.get("cost_usd"), int | float) and not isinstance(line["cost_usd"], bool)
            for line in matched
        )
        seen = sorted({(str(line.get("tier")), str(line.get("model_id"))) for line in priced})
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="pass" if model_ok else "fail",
            detail=f"{tier} cost line for {model}: {matched!r}"
            + ("" if model_ok else f"; expected a priced line, (tier, model_id) seen: {seen}"),
        )
    for (tier, effort), check in zip(expected_efforts.items(), effort_checks, strict=True):
        entry = entries.get(tier, {})
        got = (entry.get("model_id"), entry.get("effort"))
        want = (expected_models.get(tier), effort)
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="pass" if got == want else "fail",
            detail=f"{tier} resolved (model_id, effort)={got!r}, expected {want!r} "
            "(config/models.yaml tier effort)",
        )


def evaluate_temperature_refusal(result: TurnResult) -> CheckResult:
    """Check 3: a stale temperature tuning is one `error` frame and nothing model-backed."""
    error = result.error or {}
    message = str(error.get("error_message") or "")
    names_field = f"tuning.tiers.{_REFUSAL_TIER}.temperature" in message
    model_frames = sorted({"usage", "completed", "text_delta"} & set(result.kinds))
    ok = (
        result.status == 200
        and error.get("error_kind") == REFUSAL_ERROR_KIND
        and names_field
        and not model_frames
    )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check="stale_temperature_tuning_refused",
        status="pass" if ok else "fail",
        detail=(
            f"status={result.status} kinds={list(result.kinds)} "
            f"error_kind={error.get('error_kind')!r} error_message={message!r}"
            + (
                "; refused before any model call"
                if ok
                else f"; expected 200 with one {REFUSAL_ERROR_KIND!r} error frame naming "
                f"tuning.tiers.{_REFUSAL_TIER}.temperature, and no {model_frames or 'model'} frame"
            )
        ),
    )


def _supervisor_cache_counts(result: TurnResult, model: str) -> tuple[int, int] | None:
    """`(cache_read, cache_write)` summed over `result`'s supervisor cost lines on `model`.

    `None` when there is no completed frame, no such line, or a count is not an integer.
    """
    lines = (result.completed or {}).get("cost_lines")
    if not isinstance(lines, list):
        return None
    matched = [
        line
        for line in lines
        if isinstance(line, Mapping)
        and line.get("tier") == "supervisor"
        and line.get("model_id") == model
    ]
    if not matched:
        return None
    read = write = 0
    for line in matched:
        values = (line.get("cache_read_tokens"), line.get("cache_write_tokens"))
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            return None
        read += int(values[0])  # type: ignore[arg-type]
        write += int(values[1])  # type: ignore[arg-type]
    return read, write


def evaluate_cache_read(
    first: TurnResult, second: TurnResult, *, family: str, model: str
) -> CheckResult:
    """Check 6: the second supervisor turn on `model` read the prompt cache the first could write.

    `pass` when the second turn's supervisor `cache_read_tokens > 0`; `defer` with
    :data:`CACHE_UNREPORTED_REASON` when both turns report zero reads and zero writes; `fail` when
    either turn's supervisor line on `model` is missing or malformed, or a write was reported and
    the second turn read nothing.
    """
    check = f"supervisor_prompt_cache_read_on_second_turn:{family}"
    counts = [_supervisor_cache_counts(turn, model) for turn in (first, second)]
    if counts[0] is None or counts[1] is None:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="fail",
            detail=(
                f"no readable supervisor cost line on {model} (first: {counts[0]!r}, second: "
                f"{counts[1]!r}; statuses {first.status}/{second.status}); expected integer "
                "cache_read_tokens and cache_write_tokens on each completed frame"
            ),
        )
    (_, first_write), (second_read, second_write) = counts[0], counts[1]
    observed = f"{model}: first turn (read, write)={counts[0]}, second turn={counts[1]}"
    if second_read > 0:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status="pass",
            detail=f"{observed}; the second turn read the cached stable prefix",
        )
    if first_write == 0 and second_write == 0 and counts[0][0] == 0:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=check,
            status=DEFER,
            detail=f"{observed}; the platform reported no prompt-cache token on either turn",
            defer_reason=CACHE_UNREPORTED_REASON,
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check,
        status="fail",
        detail=f"{observed}; cache tokens were reported but the second turn read none",
    )


def evaluate_page_context_refusal(
    check: str, result: TurnResult, *, marker: str, control: TurnResult
) -> CheckResult:
    """Check 7: a planted route is refused with 400 + the generic message and not echoed.

    `fail` unless the control (the same request shape with a conforming `page_context`) completed
    with 200, so a refusal is attributable to the route.
    """
    control_ok = control.status == 200 and "completed" in control.kinds
    echoed = marker in result.raw_body or marker in result.message
    refused = result.status == 400 and result.message == _GENERIC_VALIDATION_MESSAGE
    ok = control_ok and refused and not echoed
    if not control_ok:
        reason = (
            f"the conforming-page_context control did not complete (status={control.status} "
            f"kinds={list(control.kinds)}), so a refusal is not attributable to the route"
        )
    elif ok:
        reason = "refused with HTTP 400 and the relay's generic message; the marker is not echoed"
    else:
        reason = (
            f"expected HTTP 400 with the generic validation message and no echo; got "
            f"status={result.status} message={result.message!r} marker_echoed={echoed} "
            f"kinds={list(result.kinds)}"
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=check,
        status="pass" if ok else "fail",
        detail=f"{reason} (body {len(result.raw_body)} bytes)",
    )


def checks(
    ctx: SmokeContext,
    *,
    provisioner_factory: ProvisionerFactory | None = None,
    run_turn: RunTurn | None = None,
    cloudtrail_lookup: CloudTrailLookup | None = None,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> Iterable[CheckResult]:
    """Provision users, drive the live turns of checks 1-7, assert, and clean up in a `finally`."""
    factory = provisioner_factory if provisioner_factory is not None else _default_provisioner
    turn = run_turn if run_turn is not None else _live_run_turn
    lookup = cloudtrail_lookup if cloudtrail_lookup is not None else _live_cloudtrail_lookup
    now = clock if clock is not None else time.time
    attribution = select_attribution_mapping()
    mixed_models = expected_tier_models(MIXED_FAMILIES)
    mixed_efforts = expected_tier_efforts(MIXED_FAMILIES)
    openai_models = expected_tier_models(OPENAI_FAMILIES)
    openai_efforts = expected_tier_efforts(OPENAI_FAMILIES)
    runtime_role = _runtime_role_name(ctx)

    budget = ModelTurnBudget(RESOURCE_CLASS)
    provisioners: list[Any] = []
    try:
        # --- Check 1: three-tier CloudTrail attribution ---
        prov_a = factory(ctx)
        provisioners.append(prov_a)
        token_a = prov_a.provision()
        if attribution.families is None:
            yield attribution_collision_row(attribution)
        else:
            if not budget.spend("delegation turn"):
                yield budget_refused(budget, "delegation turn")
                return
            start_ms = int(now() * 1000) - 5_000
            result = turn(
                ctx,
                token_a,
                f"t3-3tier-{_suffix()}",
                _DELEGATION_PROMPT,
                tuning_block(attribution.families),
            )
            end_ms = int(now() * 1000) + 5_000
            yield CheckResult(
                resource=RESOURCE_CLASS,
                check="delegation_turn_completed",
                status="pass" if result.status == 200 and "completed" in result.kinds else "fail",
                detail=(
                    f"delegation turn status={result.status} kinds={list(result.kinds)} "
                    f"[mapping {mapping_label(attribution.families)}]"
                ),
            )
            poll = _poll_cloudtrail(
                lookup,
                ctx,
                start_ms,
                end_ms,
                clock=now,
                sleeper=sleeper,
                runtime_role=runtime_role,
                tier_profiles=attribution.profiles,
            )
            yield from evaluate_three_tier_attribution(
                poll.events,
                runtime_role=runtime_role,
                tier_profiles=attribution.profiles,
                poll_summary=poll.summary(),
                families=attribution.families,
            )
            yield from evaluate_turn_cost(
                result,
                attribution.families,
                expected_tier_models(attribution.families),
                expected_tier_efforts(attribution.families),
                row_suffix=ATTRIBUTION_ROW_SUFFIX,
                turn_label="attribution",
            )

        # --- Check 3: a stale temperature tuning is refused; not model-backed ---
        refused = turn(
            ctx,
            token_a,
            f"t3-temp-{_suffix()}",
            "Reply with the single word: ready.",
            stale_temperature_block(),
        )
        yield evaluate_temperature_refusal(refused)

        # --- Check 4: session owner-binding (foreign reuse refused) ---
        prov_b = factory(ctx)
        provisioners.append(prov_b)
        token_b = prov_b.provision()
        shared_session = f"t3-hist-{_suffix()}"
        # A seeds the session under the mixed-family tuning (check 2), then reuses it (control:
        # owner reuse is permitted). B reusing A's session_id must be refused as a session
        # collision before any history read, so B's turn makes no model call.
        mixed = tuning_block(MIXED_FAMILIES)
        if not budget.spend("mixed-family seed turn"):
            yield budget_refused(budget, "mixed-family seed turn")
            return
        seeded = turn(ctx, token_a, shared_session, _MIXED_DELEGATION_PROMPT, mixed)
        yield from evaluate_turn_cost(seeded, MIXED_FAMILIES, mixed_models, mixed_efforts)
        if not budget.spend("owner reuse turn"):
            yield budget_refused(budget, "owner reuse turn")
            return
        owner_reuse = turn(ctx, token_a, shared_session, "What did I just ask you to do?", mixed)
        foreign = turn(
            ctx, token_b, shared_session, "What did the previous user ask you to note?", mixed
        )
        yield from evaluate_history_cross_session(owner_reuse, foreign)

        # --- Check 6, anthropic: the owner-reuse turn reads the seed turn's cached prefix ---
        yield evaluate_cache_read(
            seeded, owner_reuse, family="anthropic", model=mixed_models["supervisor"]
        )

        # --- Check 5: an openai-supervisor turn runs each tier at its configured effort ---
        if not budget.spend("openai-supervisor turn"):
            yield budget_refused(budget, "openai-supervisor turn")
            return
        openai_turn = turn(
            ctx,
            token_a,
            f"t3-openai-{_suffix()}",
            _OPENAI_DELEGATION_PROMPT,
            tuning_block(OPENAI_FAMILIES),
        )
        yield from evaluate_turn_cost(
            openai_turn,
            OPENAI_FAMILIES,
            openai_models,
            openai_efforts,
            row_suffix=OPENAI_ROW_SUFFIX,
            turn_label="openai-supervisor",
        )

        # --- Check 6, openai: a second openai turn reads check 5's cached prefix; it carries a
        # conforming page_context and is check 7's control ---
        if not budget.spend("openai cache probe turn"):
            yield budget_refused(budget, "openai cache probe turn")
            return
        probe = turn(
            ctx,
            token_a,
            f"t3-cache-{_suffix()}",
            _OPENAI_CACHE_PROBE_PROMPT,
            tuning_block(OPENAI_FAMILIES),
            page_context=page_context_for(_CONTROL_ROUTE),
        )
        yield evaluate_cache_read(
            openai_turn, probe, family="openai", model=openai_models["supervisor"]
        )

        # --- Check 7: planted routes are refused before any model call, with no echo ---
        marker = page_context_marker()
        for check, route in refused_routes(marker).items():
            refusal = turn(
                ctx,
                token_a,
                f"t3-route-{_suffix()}",
                _PAGE_REFUSAL_PROMPT,
                tuning_block(dict.fromkeys(_TIERS, _FAMILY)),
                page_context=page_context_for(route),
            )
            yield evaluate_page_context_refusal(check, refusal, marker=marker, control=probe)
    finally:
        yield budget.result()
        for prov in provisioners:
            yield from prov.cleanup()


def _poll_cloudtrail(
    lookup: CloudTrailLookup,
    ctx: SmokeContext,
    start_ms: int,
    end_ms: int,
    *,
    clock: Callable[[], float],
    sleeper: Callable[[float], None] | None,
    runtime_role: str,
    tier_profiles: Mapping[str, str],
) -> CloudTrailPoll:
    """Poll LookupEvents until every tier in :data:`_ATTRIBUTION_TIERS` is attributed or
    :data:`_CLOUDTRAIL_POLL_SECONDS` elapses, returning the last snapshot.

    CloudTrail delivers events independently and out of order, so a partial snapshot never ends the
    poll early; only a complete one does.
    """
    wait = sleeper if sleeper is not None else time.sleep
    started = clock()
    deadline = started + _CLOUDTRAIL_POLL_SECONDS
    polls = 0
    while True:
        events = lookup(ctx, start_ms, end_ms)
        polls += 1
        complete = all(
            _tier_attributed(events, tier, runtime_role=runtime_role, tier_profiles=tier_profiles)
            for tier in _ATTRIBUTION_TIERS
        )
        if complete or clock() >= deadline:
            return CloudTrailPoll(events=events, polls=polls, elapsed_seconds=clock() - started)
        wait(_CLOUDTRAIL_POLL_INTERVAL)


def _default_provisioner(ctx: SmokeContext) -> Any:
    """A disposable-user provisioner for a relay turn."""
    from scripts.smoke_agent_stream import LiveStreamPrincipalProvisioner

    return LiveStreamPrincipalProvisioner(ctx)


def _suffix() -> str:
    """A unique session-id suffix: full `uuid4().hex` (32 chars), so the prefixed session id meets
    InvokeAgentRuntime's 33-256 char rule.
    """
    import uuid

    return uuid.uuid4().hex


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Live path provisions users and drives checks 1-7."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
    )


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))

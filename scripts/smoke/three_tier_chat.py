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

**Check 8 — latest-message prompt caching.** Three tool-free anthropic-supervisor turns with one
prompt from :func:`cache_history_prompt`: **C** in a fresh session, then **A** and **B** in one
shared session S. The prompt carries a nonce drawn once per run, so a re-run within the cache TTL
cannot read an earlier run's cached copy of it. `pass` when no turn emitted a `tool_call` frame and
B's supervisor `cache_read_tokens` exceeds C's: a cache point on the latest message is read back by
a later turn. The row does not show that the session's history reached the model; check 9 does. A
`tool_call` frame on any of the three is a `defer` (:data:`CACHE_HISTORY_TOOL_CALL_REASON`); C
reading zero with no write reported on any turn is a `defer` (:data:`CACHE_UNREPORTED_REASON`);
otherwise B not exceeding C fails. The three turns are issued only when the module's share still has
room for all of them.

**Check 9 — a follow-up turn carries the session's history.** Four tool-free anthropic-supervisor
turns: **A** in a fresh session S states a run-unique code word (:func:`history_fact_prompt`), **B**
in S asks for it, and a control in another fresh session asks the same. `pass` when B's answer
contains the code word, B's supervisor `cache_write_tokens > 0` with `cache_read_tokens` at least
A's, and the control completed without it. A fourth turn in S omits `resume_from`, as an MCP
client's follow-up does, and repeats the question in other words: it must answer with the code word
and read more cached prompt than B, which a replay of B cannot. The Runtime log streams must show
every check-9 turn, and no stream of any of this module's sessions may carry a
`history_read_failed`, `history_write_failed` or `history_write_conflict` line. The operator then
reads S's history item in `reps` (:func:`crm.store.keys.history_key` under user A's tenant): it must
exist, name user A's tenant as its owner, and expire within the store's TTL. Every history item this
module's sessions could have written is deleted in a `finally` and proved absent by re-read. The
four turns are issued only when the module's share still has room for all of them.

Runtime-role Bedrock IAM is checked by `scripts/smoke/runtime_bedrock_iam.py`, not here.

Every live turn sends its session's recorded-frame count as `resume_from`
(:class:`ResumeCursor`), so a follow-up turn in a session runs rather than replaying the session's
previous turn.

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
from decimal import Decimal
from typing import Any, Final, NamedTuple, Protocol

from scripts.smoke.harness import (
    DEFER,
    CheckResult,
    ExpectedCheckCount,
    SmokeContext,
    exception_detail,
    open_https,
    run_main,
)

from agents.conversation_history import DEFAULT_TTL_SECONDS, HISTORY_ITEM_ATTRIBUTES
from crm.store.keys import history_key
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
    "three_tier_chat": 12,
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

#: Check 8's instruction, which :func:`cache_history_prompt` follows with the run's nonce.
_CACHE_HISTORY_INSTRUCTION: Final[str] = (
    "Reply with one short sentence confirming you are ready. Do not call any tool."
)


def cache_history_prompt(nonce: str) -> str:
    """Check 8's prompt for one run: the instruction plus `nonce`, sent unchanged on C, A and B."""
    return f"{_CACHE_HISTORY_INSTRUCTION} Ignore this cache-busting nonce: {nonce}."


#: Check 8's model-backed turns: C (fresh session), then A and B (one shared session).
CACHE_HISTORY_TURNS: Final[int] = 3

#: Check 8's defer reason when a turn called a tool, so it was not a single supervisor call.
CACHE_HISTORY_TOOL_CALL_REASON: Final[str] = "cache_history_turn_called_a_tool"

CACHE_HISTORY_CHECK: Final[str] = "supervisor_prompt_cache_reads_latest_message:anthropic"

#: Check 9's model-backed turns: A and B in one session, a fresh-session control, then a follow-up
#: in A's session that omits `resume_from`.
HISTORY_RECALL_TURNS: Final[int] = 4

HISTORY_RECALL_CHECK: Final[str] = "followup_turn_carries_history:anthropic"
HISTORY_ITEM_CHECK: Final[str] = "history_item_owned_and_expiring"
HISTORY_ITEMS_DELETED_CHECK: Final[str] = "history_items_deleted"
HISTORY_OMITTED_RESUME_CHECK: Final[str] = "followup_without_resume_from_carries_history:anthropic"
HISTORY_LOG_CHECK: Final[str] = "history_store_failures_absent_from_runtime_logs"

#: The Runtime's log events for a history store error or a write lost after its retries; the turn's
#: answer is delivered regardless.
HISTORY_FAILURE_EVENTS: Final[tuple[str, ...]] = (
    "history_read_failed",
    "history_write_failed",
    "history_write_conflict",
)

#: How long check 9 polls the Runtime log group for its turns' `invocation_started` lines.
_HISTORY_LOG_POLL_SECONDS: Final[float] = 180.0
_HISTORY_LOG_POLL_INTERVAL: Final[float] = 10.0

#: The `reps` table, which holds the Runtime's conversation-history items.
_HISTORY_TABLE: Final[str] = "reps"

#: Check 9's question, sent unchanged on B and on the control; it never carries the code word.
HISTORY_RECALL_PROMPT: Final[str] = (
    "What is my project code word? Reply with the code word only. Do not call any tool."
)

#: Check 9's fourth turn, sent without `resume_from`; worded apart from B's so it is a new request.
HISTORY_REPEAT_PROMPT: Final[str] = (
    "Repeat my project code word once more, with no other text. Do not call any tool."
)


def history_fact_prompt(code_word: str) -> str:
    """Check 9's turn A: states `code_word` as a fact for a later turn in the session to recall."""
    return f"My project code word is {code_word}. Reply OK. Do not call any tool."


def history_code_word() -> str:
    """A run-unique code word for check 9; it occurs in no prompt but turn A's."""
    return f"zq{_suffix()[:12]}"


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


class HistoryItems(Protocol):
    """Operator access to the `reps` history items, by their `rep_id` key."""

    def get(self, rep_id: str) -> Mapping[str, Any] | None:
        """The item at `rep_id`, strongly consistent, or `None`."""
        ...

    def delete(self, rep_id: str) -> None:
        """Delete the item at `rep_id`; absent is not an error."""
        ...


#: `(ctx) -> HistoryItems`. Live default addresses the deployed `reps` table.
HistoryItemsFactory = Callable[[SmokeContext], HistoryItems]


class _LiveHistoryItems:
    """`HistoryItems` over the deployed `reps` table, as the operator credential."""

    def __init__(self, ctx: SmokeContext) -> None:
        from crm.store.store import Store

        names = ctx.outputs.require_mapping("dynamodb_table_names")
        accounts = str(names["accounts"])
        self._store = Store(
            client=ctx.client("dynamodb"), table_prefix=accounts[: -len("accounts")]
        )

    def get(self, rep_id: str) -> Mapping[str, Any] | None:
        return self._store.get_item(_HISTORY_TABLE, {"rep_id": rep_id}, consistent=True)

    def delete(self, rep_id: str) -> None:
        self._store.delete_item(_HISTORY_TABLE, {"rep_id": rep_id})


def delete_history_items(items: HistoryItems, keys: Sequence[str]) -> CheckResult:
    """Delete every history item in `keys` and prove each absent by a consistent re-read."""
    remaining: list[str] = []
    try:
        for key in keys:
            items.delete(key)
            if items.get(key) is not None:
                remaining.append(key)
    except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=HISTORY_ITEMS_DELETED_CHECK,
            status="fail",
            detail=f"could not delete the history items: {exception_detail(exc)}",
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=HISTORY_ITEMS_DELETED_CHECK,
        status="fail" if remaining else "pass",
        detail=(
            f"{len(keys)} history key(s) deleted, {len(remaining)} still present on re-read"
            + (f": {remaining}" if remaining else "")
        ),
    )


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
    resume_from: int | None = 0,
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
    body_fields: dict[str, Any] = {"prompt": prompt, "session_id": session_id}
    if resume_from is not None:
        body_fields["resume_from"] = resume_from
    body_fields["tuning"] = dict(tuning)
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


class ResumeCursor:
    """Per-session count of the frames the Runtime records (every kind but `heartbeat`).

    A turn in a session that already has recorded frames must send that count as `resume_from`;
    a lower value is a reconnect, and the Runtime replays the earlier frames instead of running it.
    """

    def __init__(self) -> None:
        self._recorded: dict[str, int] = {}

    def resume_from(self, session_id: str) -> int:
        """The `resume_from` the next turn in `session_id` sends."""
        return self._recorded.get(session_id, 0)

    def advance(self, session_id: str, kinds: Iterable[str]) -> None:
        """Count the recorded frames of a turn in `session_id` that streamed `kinds`."""
        recorded = sum(1 for kind in kinds if kind != "heartbeat")
        self._recorded[session_id] = self.resume_from(session_id) + recorded


class _LiveTurns:
    """The live `RunTurn`: :func:`_live_run_turn` with each session's :class:`ResumeCursor` value.

    `omit_resume_from=True` sends no `resume_from` at all, as an MCP client's follow-up does.
    """

    def __init__(self) -> None:
        self.cursor = ResumeCursor()

    def __call__(
        self,
        ctx: SmokeContext,
        token: str,
        session_id: str,
        prompt: str,
        tuning: Mapping[str, Any],
        *,
        page_context: Mapping[str, Any] | None = None,
        omit_resume_from: bool = False,
    ) -> TurnResult:
        result = _live_run_turn(
            ctx,
            token,
            session_id,
            prompt,
            tuning,
            page_context=page_context,
            resume_from=None if omit_resume_from else self.cursor.resume_from(session_id),
        )
        self.cursor.advance(session_id, result.kinds)
        return result


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


def evaluate_cache_history(
    fresh: TurnResult, seed: TurnResult, followup: TurnResult, *, model: str
) -> CheckResult:
    """Check 8: the follow-up turn in a session reads more cached prompt than a fresh session.

    `fresh` is C, `seed` is A and `followup` is B (A's session). `fail` when any turn's supervisor
    line on `model` is missing or malformed; `defer` when any turn emitted a `tool_call` frame, or
    when C read nothing and no turn reported a cache write; `pass` when B's read exceeds C's; `fail`
    otherwise.
    """
    turns = {"C": fresh, "A": seed, "B": followup}
    counts = {label: _supervisor_cache_counts(turn, model) for label, turn in turns.items()}
    observed = (
        f"{model}: supervisor (read, write) C={counts['C']} A={counts['A']} B={counts['B']}; "
        f"statuses {'/'.join(str(t.status) for t in turns.values())}"
    )
    tool_turns = [label for label, turn in turns.items() if "tool_call" in turn.kinds]
    if any(value is None for value in counts.values()):
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=CACHE_HISTORY_CHECK,
            status="fail",
            detail=(
                f"{observed}; expected a completed frame with integer cache_read_tokens and "
                "cache_write_tokens on each turn's supervisor cost line"
            ),
        )
    if tool_turns:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=CACHE_HISTORY_CHECK,
            status=DEFER,
            detail=f"{observed}; turn(s) {tool_turns} called a tool, so the reads are not comparable",
            defer_reason=CACHE_HISTORY_TOOL_CALL_REASON,
        )
    read = {label: value[0] for label, value in counts.items() if value is not None}
    written = sum(value[1] for value in counts.values() if value is not None)
    if read["B"] > read["C"]:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=CACHE_HISTORY_CHECK,
            status="pass",
            detail=f"{observed}; B read cached session history beyond C's prefix",
        )
    if read["C"] == 0 and written == 0:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=CACHE_HISTORY_CHECK,
            status=DEFER,
            detail=f"{observed}; the platform reported no prompt-cache write on any turn",
            defer_reason=CACHE_UNREPORTED_REASON,
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=CACHE_HISTORY_CHECK,
        status="fail",
        detail=f"{observed}; B read no more cached prompt than the fresh-session turn C",
    )


def evaluate_history_recall(
    seed: TurnResult,
    followup: TurnResult,
    control: TurnResult,
    *,
    code_word: str,
    model: str,
) -> CheckResult:
    """Check 9: B recalls A's code word from the session's history, and a fresh session does not.

    `seed` is A, `followup` is B (A's session) and `control` is the fresh-session turn. `pass` only
    when B's answer contains `code_word`, B's supervisor line on `model` reports
    `cache_write_tokens > 0` with `cache_read_tokens` at least A's, and the control completed with
    an answer that does not contain `code_word`. Anything else fails.
    """
    counts = {
        "A": _supervisor_cache_counts(seed, model),
        "B": _supervisor_cache_counts(followup, model),
    }
    recalled = code_word in followup.answer
    control_completed = control.status == 200 and control.completed is not None
    leaked = code_word in control.answer
    observed = (
        f"{model}: supervisor (read, write) A={counts['A']} B={counts['B']}; statuses "
        f"{seed.status}/{followup.status}/{control.status}; B recalled the code word: {recalled}; "
        f"control completed: {control_completed}, control carried it: {leaked}"
    )
    seed_counts, followup_counts = counts["A"], counts["B"]
    if seed_counts is None or followup_counts is None:
        reason = "expected integer cache tokens on A's and B's supervisor cost lines"
    elif not recalled:
        reason = "B's answer does not contain the code word A stated in the same session"
    elif followup_counts[1] <= 0:
        reason = "B reported no cache write, so no new history tail was sent to the model"
    elif followup_counts[0] < seed_counts[0]:
        reason = "B read less cached prompt than A, so B's prompt does not extend A's"
    elif not control_completed:
        reason = "the fresh-session control did not complete, so a leak cannot be ruled out"
    elif leaked:
        reason = "the fresh-session control answered with the code word: history crossed sessions"
    else:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=HISTORY_RECALL_CHECK,
            status="pass",
            detail=f"{observed}; B recalled the session's history and the control did not",
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=HISTORY_RECALL_CHECK,
        status="fail",
        detail=f"{observed}; {reason}",
    )


def evaluate_history_item(
    item: Mapping[str, Any] | None, *, tenant: str, now: float, ttl_seconds: int
) -> CheckResult:
    """Check 9's operator read: the history item exists, is owned by `tenant`, and expires in TTL.

    `pass` when `item` carries exactly :data:`HISTORY_ITEM_ATTRIBUTES` (so no `tenant`), with
    `kind == "history"`, `owner_tenant == tenant`, a non-empty `owner_rep_id`, and a numeric
    `expires_at` in `(now, now + ttl_seconds]`.
    """
    if item is None:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=HISTORY_ITEM_CHECK,
            status="fail",
            detail=f"no history item for the session under tenant {tenant!r}",
        )
    expires_at = item.get("expires_at")
    numeric = isinstance(expires_at, int | float | Decimal) and not isinstance(expires_at, bool)
    in_window = numeric and now < float(expires_at) <= now + ttl_seconds  # type: ignore[arg-type]
    owner_rep = item.get("owner_rep_id")
    extra = sorted(set(item) - HISTORY_ITEM_ATTRIBUTES)
    missing = sorted(HISTORY_ITEM_ATTRIBUTES - set(item))
    ok = (
        not extra
        and not missing
        and item.get("kind") == "history"
        and item.get("owner_tenant") == tenant
        and isinstance(owner_rep, str)
        and bool(owner_rep)
        and in_window
    )
    turns = item.get("turns")
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=HISTORY_ITEM_CHECK,
        status="pass" if ok else "fail",
        detail=(
            f"kind={item.get('kind')!r} owner_tenant matches: {item.get('owner_tenant') == tenant} "
            f"owner_rep_id set: {isinstance(owner_rep, str) and bool(owner_rep)} "
            f"turns={len(turns) if isinstance(turns, list) else turns!r} expires_at={expires_at!r} "
            f"window=({now:.0f}, {now + ttl_seconds:.0f}] extra={extra} missing={missing}"
            + ("" if ok else "; expected a history item owned by user A's tenant within the TTL")
        ),
    )


def evaluate_omitted_resume_recall(
    followup: TurnResult, repeat: TurnResult, *, code_word: str, model: str
) -> CheckResult:
    """Check 9's fourth turn: a same-session follow-up without `resume_from` still recalls.

    `followup` is B and `repeat` the fourth turn. `pass` when the repeat's answer contains
    `code_word` and its supervisor `cache_read_tokens` exceeds B's. A replay of B carries B's
    answer and B's counts unchanged, so it fails.
    """
    counts = {
        "B": _supervisor_cache_counts(followup, model),
        "R": _supervisor_cache_counts(repeat, model),
    }
    recalled = code_word in repeat.answer
    observed = (
        f"{model}: supervisor (read, write) B={counts['B']} repeat={counts['R']}; statuses "
        f"{followup.status}/{repeat.status}; repeat recalled the code word: {recalled}"
    )
    b_counts, r_counts = counts["B"], counts["R"]
    if b_counts is None or r_counts is None:
        reason = "expected integer cache tokens on B's and the repeat's supervisor cost lines"
    elif not recalled:
        reason = "the follow-up sent without resume_from does not carry the code word"
    elif r_counts[0] <= b_counts[0]:
        reason = (
            "the follow-up read no more cached prompt than B, so it did not extend the session "
            "(a replay of B reports B's counts)"
        )
    else:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=HISTORY_OMITTED_RESUME_CHECK,
            status="pass",
            detail=f"{observed}; a follow-up without resume_from ran and carried the history",
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=HISTORY_OMITTED_RESUME_CHECK,
        status="fail",
        detail=f"{observed}; {reason}",
    )


@dataclass(frozen=True, slots=True)
class RuntimeLogLine:
    """One Runtime log event: the stream it was written to and its message."""

    stream: str
    message: str


#: `(ctx, start_ms, end_ms) -> list[RuntimeLogLine]`. Live default reads the Runtime log group.
RuntimeLogLookup = Callable[[SmokeContext, int, int], "list[RuntimeLogLine]"]


def _live_runtime_log_lookup(ctx: SmokeContext, start_ms: int, end_ms: int) -> list[RuntimeLogLine]:
    """Every event in the Runtime log group between `start_ms` and `end_ms`, following pages."""
    client = ctx.client("logs")
    request: dict[str, Any] = {
        "logGroupName": ctx.outputs.require_str("agentcore_runtime_log_group_name"),
        "startTime": start_ms,
        "endTime": end_ms,
    }
    lines: list[RuntimeLogLine] = []
    while True:
        response = client.filter_log_events(**request)
        lines.extend(
            RuntimeLogLine(
                stream=str(e.get("logStreamName", "")), message=str(e.get("message", ""))
            )
            for e in response.get("events", [])
        )
        token = response.get("nextToken")
        if not token:
            return lines
        request["nextToken"] = token


def _invocations_by_session(
    lines: Sequence[RuntimeLogLine], sessions: Sequence[str]
) -> dict[str, list[str]]:
    """`{session: [stream, ...]}` of the `invocation_started` lines naming each session."""
    found: dict[str, list[str]] = {session: [] for session in sessions}
    for line in lines:
        if '"invocation_started"' not in line.message:
            continue
        for session in sessions:
            if f'"session_id": "{session}"' in line.message:
                found[session].append(line.stream)
    return found


def evaluate_history_logs(
    lines: Sequence[RuntimeLogLine],
    turns_by_session: Mapping[str, int],
    *,
    module_sessions: Sequence[str] = (),
    poll_summary: str = "",
) -> CheckResult:
    """Check 9's log read: no history store failure on any of the module's turns.

    A history failure line carries no session id, so it is attributed by its log stream: a stream
    whose name contains one of `module_sessions` or of `turns_by_session`'s sessions. The control
    is that each of `turns_by_session`'s sessions has at least that many `invocation_started`
    lines, all in streams whose name contains the session id; without it a missing failure line
    proves nothing.
    """
    sessions = list(turns_by_session)
    invocations = _invocations_by_session(lines, sessions)
    seen = {session: len(streams) for session, streams in invocations.items()}
    attributable = {*sessions, *module_sessions}
    session_streams = {
        line.stream for line in lines if any(session in line.stream for session in attributable)
    }
    attributed = all(
        seen[session] >= turns_by_session[session]
        and all(session in stream for stream in invocations[session])
        for session in sessions
    )
    failures = [
        f"{line.stream}: {event}"
        for line in lines
        if line.stream in session_streams
        for event in HISTORY_FAILURE_EVENTS
        if f'"{event}"' in line.message
    ]
    observed = (
        f"invocation_started per session {seen} (expected {dict(turns_by_session)}); "
        f"{len(session_streams)} session stream(s); history failure lines {failures}"
        + (f" [{poll_summary}]" if poll_summary else "")
    )
    if not attributed:
        reason = (
            "the log read did not find every turn's invocation_started line in a stream naming "
            "its session, so the absence of a failure line is not evidence"
        )
    elif failures:
        reason = "the history store failed on a module turn; the answer is delivered either way"
    else:
        return CheckResult(
            resource=RESOURCE_CLASS,
            check=HISTORY_LOG_CHECK,
            status="pass",
            detail=f"{observed}; every turn logged and no history store failure",
        )
    return CheckResult(
        resource=RESOURCE_CLASS,
        check=HISTORY_LOG_CHECK,
        status="fail",
        detail=f"{observed}; {reason}",
    )


def _poll_history_logs(
    lookup: RuntimeLogLookup,
    ctx: SmokeContext,
    start_ms: int,
    turns_by_session: Mapping[str, int],
    *,
    clock: Callable[[], float],
    sleeper: Callable[[float], None] | None,
) -> tuple[list[RuntimeLogLine], str]:
    """Poll until every session's `invocation_started` lines are in, or the bound elapses."""
    wait = sleeper if sleeper is not None else time.sleep
    started = clock()
    polls = 0
    while True:
        lines = lookup(ctx, start_ms, int(clock() * 1000) + 60_000)
        polls += 1
        seen = _invocations_by_session(lines, list(turns_by_session))
        complete = all(len(seen[s]) >= n for s, n in turns_by_session.items())
        if complete or clock() - started >= _HISTORY_LOG_POLL_SECONDS:
            return lines, f"polls={polls} elapsed={clock() - started:.0f}s lines={len(lines)}"
        wait(_HISTORY_LOG_POLL_INTERVAL)


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
    history_items: HistoryItemsFactory | None = None,
    runtime_logs: RuntimeLogLookup | None = None,
) -> Iterable[CheckResult]:
    """Provision users, drive the live turns of checks 1-9, assert, and clean up in a `finally`.

    Every session id a token is sent with is recorded, and each provisioned user's history items
    for those sessions are deleted before the users are removed.
    """
    factory = provisioner_factory if provisioner_factory is not None else _default_provisioner
    send = run_turn if run_turn is not None else _LiveTurns()
    items_factory = history_items if history_items is not None else _LiveHistoryItems
    log_lookup = runtime_logs if runtime_logs is not None else _live_runtime_log_lookup
    sessions_by_token: dict[str, list[str]] = {}

    def turn(
        ctx: SmokeContext, token: str, session_id: str, *args: Any, **kwargs: Any
    ) -> TurnResult:
        sessions = sessions_by_token.setdefault(token, [])
        if session_id not in sessions:
            sessions.append(session_id)
        return send(ctx, token, session_id, *args, **kwargs)

    lookup = cloudtrail_lookup if cloudtrail_lookup is not None else _live_cloudtrail_lookup
    now = clock if clock is not None else time.time
    attribution = select_attribution_mapping()
    mixed_models = expected_tier_models(MIXED_FAMILIES)
    mixed_efforts = expected_tier_efforts(MIXED_FAMILIES)
    openai_models = expected_tier_models(OPENAI_FAMILIES)
    openai_efforts = expected_tier_efforts(OPENAI_FAMILIES)
    runtime_role = _runtime_role_name(ctx)

    budget = ModelTurnBudget(RESOURCE_CLASS)
    logs_from_ms = int(now() * 1000) - 60_000
    provisioners: list[Any] = []
    tokens: dict[int, str] = {}
    try:
        # --- Check 1: three-tier CloudTrail attribution ---
        prov_a = factory(ctx)
        provisioners.append(prov_a)
        token_a = prov_a.provision()
        tokens[id(prov_a)] = token_a
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
        tokens[id(prov_b)] = token_b
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

        # --- Check 8: a same-session follow-up reads cached history beyond the stable prefix ---
        if budget.allotment - len(budget.used) < CACHE_HISTORY_TURNS:
            yield budget_refused(budget, "cache-history turns")
            return
        history_tuning = tuning_block(dict.fromkeys(_TIERS, _FAMILY))
        history_model = expected_tier_models(dict.fromkeys(_TIERS, _FAMILY))["supervisor"]
        history_session = f"t3-chist-{_suffix()}"
        history_prompt = cache_history_prompt(_suffix())
        history: list[TurnResult] = []
        for label, session in (
            ("cache-history turn C", f"t3-cfresh-{_suffix()}"),
            ("cache-history turn A", history_session),
            ("cache-history turn B", history_session),
        ):
            budget.spend(label)
            history.append(turn(ctx, token_a, session, history_prompt, history_tuning))
        yield evaluate_cache_history(*history, model=history_model)

        # --- Check 9: a same-session follow-up recalls a fact from the session's history ---
        if budget.allotment - len(budget.used) < HISTORY_RECALL_TURNS:
            yield budget_refused(budget, "history-recall turns")
            return
        code_word = history_code_word()
        recall_session = f"t3-recall-{_suffix()}"
        control_session = f"t3-rfresh-{_suffix()}"
        recall: list[TurnResult] = []
        for label, session, prompt in (
            ("history-recall turn A", recall_session, history_fact_prompt(code_word)),
            ("history-recall turn B", recall_session, HISTORY_RECALL_PROMPT),
            ("history-recall control", control_session, HISTORY_RECALL_PROMPT),
        ):
            budget.spend(label)
            recall.append(turn(ctx, token_a, session, prompt, history_tuning))
        yield evaluate_history_recall(*recall, code_word=code_word, model=history_model)
        budget.spend("history-recall follow-up without resume_from")
        repeat = turn(
            ctx,
            token_a,
            recall_session,
            HISTORY_REPEAT_PROMPT,
            history_tuning,
            omit_resume_from=True,
        )
        yield evaluate_omitted_resume_recall(
            recall[1], repeat, code_word=code_word, model=history_model
        )
        turns_by_session = {recall_session: 3, control_session: 1}
        try:
            lines, poll_summary = _poll_history_logs(
                log_lookup, ctx, logs_from_ms, turns_by_session, clock=now, sleeper=sleeper
            )
            yield evaluate_history_logs(
                lines,
                turns_by_session,
                module_sessions=[sid for sids in sessions_by_token.values() for sid in sids],
                poll_summary=poll_summary,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
            yield CheckResult(
                resource=RESOURCE_CLASS,
                check=HISTORY_LOG_CHECK,
                status="fail",
                detail=f"could not read the Runtime log group: {exception_detail(exc)}",
            )
        tenant_a = str(prov_a.tenant())
        try:
            item = items_factory(ctx).get(history_key(tenant_a, recall_session))
            yield evaluate_history_item(
                item, tenant=tenant_a, now=now(), ttl_seconds=DEFAULT_TTL_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
            yield CheckResult(
                resource=RESOURCE_CLASS,
                check=HISTORY_ITEM_CHECK,
                status="fail",
                detail=f"could not read the session's history item: {exception_detail(exc)}",
            )
    finally:
        yield budget.result()
        yield from _delete_module_history(
            ctx, items_factory, provisioners, tokens, sessions_by_token
        )
        for prov in provisioners:
            yield from prov.cleanup()


def _delete_module_history(
    ctx: SmokeContext,
    items_factory: HistoryItemsFactory,
    provisioners: Sequence[Any],
    tokens: Mapping[int, str],
    sessions_by_token: Mapping[str, Sequence[str]],
) -> Iterable[CheckResult]:
    """Delete each provisioned user's history items for every session its token was sent with."""
    keys: list[str] = []
    for prov in provisioners:
        token = tokens.get(id(prov))
        sessions = sessions_by_token.get(token, ()) if token is not None else ()
        tenant = str(prov.tenant()) if sessions else ""
        keys.extend(history_key(tenant, session) for session in sessions if tenant)
    if not keys:
        return
    try:
        items = items_factory(ctx)
    except Exception as exc:  # noqa: BLE001 - surfaced as a failing check, never swallowed
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check=HISTORY_ITEMS_DELETED_CHECK,
            status="fail",
            detail=f"could not address the history items: {exception_detail(exc)}",
        )
        return
    yield delete_history_items(items, keys)


def expected_check_count(_ctx: SmokeContext) -> ExpectedCheckCount:
    """The rows a run that issues every turn emits whatever each turn's outcome."""
    cost_rows = {
        label: 4 + len(expected_tier_models(families)) + len(expected_tier_efforts(families))
        for label, families in (("mixed-family", MIXED_FAMILIES), ("openai", OPENAI_FAMILIES))
    }
    single_rows = {
        "check 1 collision or attribution control": 1,
        "stale-tuning refusal": 1,
        "owner session reuse control": 1,
        "cache read per family": 2,
        "page-context route refusals": len(refused_routes("m")),
        "latest-message caching": 1,
        "history recall, omitted-resume recall, logs and history item": 4,
        "model-backed turn count": 1,
        "history items deleted": 1,
    }
    return ExpectedCheckCount(
        minimum=sum(cost_rows.values()) + sum(single_rows.values()),
        basis=(
            f"turn-cost rows {cost_rows} (4 plus one per tier model and tier effort from "
            f"config/models.yaml) and one-row checks {single_rows}; provisioner cleanup rows are "
            "not counted"
        ),
    )


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
    """Entry point. Live path provisions users and drives checks 1-9."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))

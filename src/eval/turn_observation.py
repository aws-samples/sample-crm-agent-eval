# SPDX-License-Identifier: MIT
"""Observe one eval turn and carry what it did across a process boundary.

:class:`ObservingDispatch` wraps a turn's identity-bound dispatch and records, for every tool call,
the scoring-relevant facts the :class:`eval.scoring.TurnOutcome` is built from, plus the arguments
the agent supplied (:class:`eval.scoring.ObservedToolArguments`). :func:`observe_turn` drives one
turn through :func:`agents.turn_driver.drive_turn` under :data:`agents.budget.EVAL_BUDGET`, with
no history, page context or user settings, and returns an :class:`ObservedTurn`.
:func:`serialise_turn` and :func:`deserialise_turn` are the strict JSON form of an
:class:`ObservedTurn`, which the Runtime's ``eval_turn`` mode returns and the sweep-task Lambda
reads, so the in-process harness and the state machine score the same observation.

This module imports no vendor SDK and constructs no AWS client.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, cast, get_args, runtime_checkable

from agents.budget import EVAL_BUDGET
from agents.registry import RegisteredTool
from agents.supervisor import classify_tool_result, describe_truncation
from agents.tier_attribution import (
    CacheCounts,
    InvocationAttribution,
    OutcomeKind,
    TokenCounts,
    ToolCallAttribution,
)
from agents.tool_actor import ToolDispatch
from agents.turn_driver import ProviderResolver, TurnRecords, drive_turn
from crm.toolspec.definitions import HIGH_RISK_OPPORTUNITY_FIELDS, UPDATE_ALLOWLISTS
from eval.fault_injection import with_injected_faults
from eval.metrics import Exclusion, ExclusionCategory, retried_failure_positions
from eval.scoring import ObservedProposal, ObservedToolArguments, ToolCall, TurnOutcome
from providers.base import ModelProvider, ResolvedInvocation
from providers.capabilities import CapabilityMatrix, Family, Tier
from providers.tier_tuning import TurnTuning

#: Version of the :func:`serialise_turn` wire shape.
SERIALISED_TURN_SCHEMA: Final[int] = 2

#: The tool-result payload key a bounded read sets to report truncation; the same value as
#: :data:`agents.supervisor._SEARCH_TRUNCATED_KEY`.
TOOL_OUTPUT_TRUNCATED_KEY: Final[str] = "truncated"


def tool_output_reports_truncation(tool_output: Mapping[str, object]) -> bool:
    """Return whether ``tool_output`` reports truncation, at its top level or under ``body``."""
    body = tool_output.get("body", tool_output)
    payload = body if isinstance(body, Mapping) else tool_output
    return bool(payload.get(TOOL_OUTPUT_TRUNCATED_KEY))


def _target_id(tool_input: Mapping[str, object]) -> str | None:
    candidate = (
        tool_input.get("target_id")
        or tool_input.get("opportunity_id")
        or tool_input.get("account_id")
        or tool_input.get("contact_id")
    )
    return str(candidate) if candidate is not None else None


class ObservingDispatch:
    """Dispatch each tool call through ``base`` and record what scoring needs to know about it.

    Records truncation and its disclosure, the denial outcome (``denied`` overrides; otherwise the
    first ``no_match``/``tool_failure``; ``data`` only if none occurred), created proposals, the
    proposal-versus-direct-commit labels, and each call's name, target and arguments.
    """

    def __init__(self, base: ToolDispatch) -> None:
        """Wrap ``base``, the identity-bound dispatch the turn would otherwise call directly."""
        self._base = base
        self._tool_calls: list[ToolCall] = []
        self._tool_arguments: list[ObservedToolArguments] = []
        self._proposals: list[ObservedProposal] = []
        self._observed_outcomes: set[str] = set()
        self._denial_outcome: str | None = None
        self._denial_missing_scope: str | None = None
        self._saw_data = False
        self._truncated = False
        self._truncation_disclosed = False

    def __call__(self, tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        """Dispatch one tool call and record its effects; returns the output unchanged."""
        self._tool_arguments.append(
            ObservedToolArguments(tool_name=tool.name, arguments=dict(tool_input))
        )
        tool_output = self._base(tool, tool_input)
        tool_name = tool.name

        if tool_output_reports_truncation(tool_output):
            self._truncated = True
            if describe_truncation(tool_output, event_truncated=False) is not None:
                self._truncation_disclosed = True

        self._tool_calls.append(
            ToolCall(
                tool_name=tool_name,
                target_table=(
                    str(tool_input["target_table"])
                    if tool_input.get("target_table") is not None
                    else None
                ),
                target_id=_target_id(tool_input),
            )
        )

        outcome = classify_tool_result(tool_output)
        denial_outcome = self._denial_outcome
        if outcome.kind == "denied":
            denial_outcome = "denied"
            self._denial_missing_scope = outcome.missing_scope
        elif outcome.kind == "no_match" and denial_outcome is None:
            denial_outcome = "no_match"
        elif outcome.kind == "tool_failure" and denial_outcome is None:
            denial_outcome = "tool_failure"
        elif outcome.kind == "data":
            self._saw_data = True
        self._denial_outcome = denial_outcome

        observed_outcomes = self._observed_outcomes
        if tool_name == "submit_proposal" and "error" not in tool_output:
            body = tool_output.get("body", tool_output)
            payload = body if isinstance(body, dict) else {}
            proposal = payload.get("proposal")
            if isinstance(proposal, dict):
                proposal_id = proposal.get("proposal_id")
                if isinstance(proposal_id, str) and proposal_id:
                    self._proposals.append(ObservedProposal(proposal_id=proposal_id))
                target_table = str(proposal.get("target_table") or "")
                after_payload = proposal.get("after_payload")
                changed_fields = set(after_payload) if isinstance(after_payload, dict) else set()
                if target_table == "opportunities" and changed_fields.issubset(
                    UPDATE_ALLOWLISTS.get("opportunities", frozenset())
                ):
                    observed_outcomes.add("proposal_instead_of_direct_commit")
        elif tool_name.startswith("update_"):
            fields_arg = tool_input.get("fields")
            changed_fields = set(fields_arg) if isinstance(fields_arg, dict) else set()
            if changed_fields & HIGH_RISK_OPPORTUNITY_FIELDS:
                observed_outcomes.add("direct_commit_of_high_risk_field")

        return tool_output

    @property
    def tool_arguments(self) -> tuple[ObservedToolArguments, ...]:
        """Every dispatched call's name and the arguments the agent supplied, in call order."""
        return tuple(self._tool_arguments)

    def outcome(
        self,
        *,
        tenant: str,
        budget_exhausted: bool,
        tool_calls: Sequence[ToolCallAttribution] = (),
    ) -> TurnOutcome:
        """Build the turn's :class:`eval.scoring.TurnOutcome` from everything observed so far.

        With ``tool_calls`` (the turn's role-attributed calls, in call order) the outcome follows
        those records, so a tool-tier error that dispatched nothing counts as a ``tool_failure``
        and a self-corrected ``tool_failure`` (:func:`eval.metrics.retried_failure_positions`)
        does not set it; ``denied`` overrides. The records are used only when the dispatched
        calls' names are a subsequence of theirs; otherwise, or without them, the outcome follows
        the dispatch order alone.
        """
        denial_outcome = self._denial_outcome
        if (
            tool_calls
            and denial_outcome != "denied"
            and _is_subsequence(
                [call.tool_name for call in self._tool_calls],
                [call.tool_name for call in tool_calls],
            )
        ):
            retried = retried_failure_positions(tool_calls)
            denial_outcome = None
            for position, call in enumerate(tool_calls):
                if call.outcome_kind == "no_match":
                    denial_outcome = "no_match"
                    break
                if call.outcome_kind == "tool_failure" and position not in retried:
                    denial_outcome = "tool_failure"
                    break
        if denial_outcome is None and self._saw_data:
            denial_outcome = "data"
        return TurnOutcome(
            tenant=tenant,
            tool_calls=tuple(self._tool_calls),
            proposals=tuple(self._proposals),
            denial_outcome=denial_outcome,
            denial_missing_scope=self._denial_missing_scope,
            budget_exhausted=budget_exhausted,
            truncated=self._truncated,
            truncation_disclosed=self._truncation_disclosed,
            observed_outcomes=frozenset(self._observed_outcomes),
        )


def _is_subsequence(names: Sequence[str], within: Sequence[str]) -> bool:
    """Whether ``names`` occur in ``within`` in the same order, other entries interleaved."""
    remaining = iter(within)
    return all(name in remaining for name in names)


@dataclass(frozen=True, slots=True)
class ObservedTurn:
    """Everything one eval turn produced that scoring and metering read.

    Attributes:
        outcome: The scoring view of the turn.
        tool_arguments: Every dispatched call's name and agent-supplied arguments, in call order.
        invocations: One attribution per model invocation, every tier.
        tool_calls: One attribution per dispatched tool call.
        stream_calls: ``provider.stream`` calls made inside this turn's driver.
        exclusions: Exclusions the turn's providers recorded, drained after the turn.
        turn_latency_ms: The turn's wall-clock duration, or ``None`` when not measured.
        usage_lower_bound: Whether some model call of the turn ended before reporting token usage
            (:attr:`agents.turn_driver.TurnRecords.calls_without_usage`), so ``invocations``
            under-count the turn's usage and cost.
        resolved_efforts: The effort each tier ran at, from the ``resolved_tiers`` of the turn's
            last ``usage``/``completed`` event (``None`` for a tier without effort control), or
            ``None`` when the turn reported none.
        prompt_bundle_sha256: :func:`agents.prompt_bundle.prompt_bundle_sha256` in the process that
            ran the turn, or ``None`` when the turn was recorded without one.
    """

    outcome: TurnOutcome
    tool_arguments: tuple[ObservedToolArguments, ...] = field(default_factory=tuple)
    invocations: tuple[InvocationAttribution, ...] = field(default_factory=tuple)
    tool_calls: tuple[ToolCallAttribution, ...] = field(default_factory=tuple)
    stream_calls: int = 0
    exclusions: tuple[Exclusion, ...] = field(default_factory=tuple)
    turn_latency_ms: float | None = None
    usage_lower_bound: bool = False
    resolved_efforts: Mapping[Tier, str | None] | None = None
    prompt_bundle_sha256: str | None = None

    @property
    def records(self) -> TurnRecords:
        """The attribution as a fresh :class:`agents.turn_driver.TurnRecords`."""
        return TurnRecords(invocations=list(self.invocations), tool_calls=list(self.tool_calls))


@runtime_checkable
class ExclusionRecordingProvider(Protocol):
    """A provider that tallies its own categorised exclusions."""

    def drain_exclusions(self) -> list[Exclusion]: ...


def drain_provider_exclusions(provider: ModelProvider) -> list[Exclusion]:
    """Return every exclusion ``provider`` recorded since the last drain, or an empty list.

    A live :class:`eval.live.ThrottleRetryingProvider` records one per throttled or retried
    attempt; the stub records none.
    """
    if isinstance(provider, ExclusionRecordingProvider):
        return provider.drain_exclusions()
    return []


class _CountingProvider(ModelProvider):
    """Delegates :meth:`stream` unchanged and counts the calls."""

    def __init__(self, wrapped: ModelProvider) -> None:
        self.wrapped = wrapped
        self.call_count = 0

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> Any:
        """Count the call, then delegate to the wrapped provider."""
        self.call_count += 1
        return self.wrapped.stream(invocation, messages, tools)


async def observe_turn(
    *,
    prompt: str,
    task_id: str,
    tenant: str,
    providers: ProviderResolver,
    matrix: CapabilityMatrix,
    tuning: TurnTuning,
    dispatch: ToolDispatch,
    max_rounds: int | None = None,
    dispatch_clock: Callable[[], float] | None = None,
    turn_clock: Callable[[], float] | None = None,
) -> ObservedTurn:
    """Drive one single-turn eval prompt and return what it did.

    Args:
        prompt: The golden task's prompt.
        task_id: The golden task's id; also the turn's ``session_id``.
        tenant: The eval tenant the turn runs in, carried into the outcome.
        providers: The per-family provider resolver; each provider is wrapped to count its calls.
        matrix: The capability matrix every tier is resolved against.
        tuning: The arm's per-tier tuning.
        dispatch: The identity-bound dispatch, wrapped by
            :func:`eval.fault_injection.with_injected_faults`, then in :class:`ObservingDispatch`.
        max_rounds: Backstop on supervisor rounds, as :func:`agents.turn_driver.drive_turn`.
        dispatch_clock: Monotonic clock timing each tool dispatch, or ``None``.
        turn_clock: Monotonic clock timing the whole turn, or ``None`` for no turn latency.

    Raises:
        crm.errors.ValidationFailed: ``tuning`` does not resolve against ``matrix``.
        eval.fault_injection.FaultInjectionRefused: ``task_id`` declares a fault and ``tenant`` is
            not an ``eval-<run_id>`` tenant.
    """
    from agents.prompt_bundle import prompt_bundle_sha256

    observing = ObservingDispatch(with_injected_faults(dispatch, task_id=task_id, tenant=tenant))
    counters: dict[Family, _CountingProvider] = {}

    def counted(family: Family) -> ModelProvider:
        if family not in counters:
            counters[family] = _CountingProvider(providers(family))
        return counters[family]

    records = TurnRecords()
    budget_exhausted = False
    resolved_efforts: dict[Tier, str | None] | None = None
    started = turn_clock() if turn_clock is not None else None
    async for event in drive_turn(
        prompt=prompt,
        session_id=task_id,
        providers=counted,
        matrix=matrix,
        tuning=tuning,
        budget=EVAL_BUDGET,
        dispatch=observing,
        records=records,
        page_context=None,
        history_messages=None,
        max_rounds=max_rounds,
        dispatch_clock=dispatch_clock,
    ):
        if event.kind == "error" and event.error_kind == "budget_exhausted":
            budget_exhausted = True
        if event.kind in ("usage", "completed") and event.resolved_tiers is not None:
            resolved_efforts = {entry["tier"]: entry["effort"] for entry in event.resolved_tiers}
    latency_ms = (
        (turn_clock() - started) * 1000.0
        if turn_clock is not None and started is not None
        else None
    )
    exclusions = [
        exclusion
        for counter in counters.values()
        for exclusion in drain_provider_exclusions(counter.wrapped)
    ]
    return ObservedTurn(
        outcome=observing.outcome(
            tenant=tenant, budget_exhausted=budget_exhausted, tool_calls=records.tool_calls
        ),
        tool_arguments=observing.tool_arguments,
        invocations=tuple(records.invocations),
        tool_calls=tuple(records.tool_calls),
        stream_calls=sum(counter.call_count for counter in counters.values()),
        exclusions=tuple(exclusions),
        turn_latency_ms=latency_ms,
        usage_lower_bound=records.calls_without_usage > 0,
        resolved_efforts=resolved_efforts,
        prompt_bundle_sha256=prompt_bundle_sha256(),
    )


#: The optional :func:`serialise_turn` key, present (``true``) only for a usage-lower-bound turn.
USAGE_LOWER_BOUND_KEY: Final[str] = "usage_lower_bound"

#: The optional :func:`serialise_turn` key carrying ``{tier: effort}``, present only when the turn
#: recorded :attr:`ObservedTurn.resolved_efforts`.
RESOLVED_TIERS_KEY: Final[str] = "resolved_tiers"

#: The optional :func:`serialise_turn` key carrying :attr:`ObservedTurn.prompt_bundle_sha256`,
#: present only when the turn recorded one.
PROMPT_BUNDLE_SHA256_KEY: Final[str] = "prompt_bundle_sha256"

_TIERS: Final[frozenset[str]] = frozenset(get_args(Tier))


def serialise_turn(turn: ObservedTurn) -> dict[str, Any]:
    """Render ``turn`` as the strict JSON wire object :func:`deserialise_turn` reads back.

    :data:`USAGE_LOWER_BOUND_KEY` is written only when ``turn.usage_lower_bound`` is true, and
    :data:`RESOLVED_TIERS_KEY` only when ``turn.resolved_efforts`` is set, and
    :data:`PROMPT_BUNDLE_SHA256_KEY` only when ``turn.prompt_bundle_sha256`` is set, so a turn with
    none of them carries none of these keys.
    """
    outcome = turn.outcome
    serialised: dict[str, Any] = {
        "schema": SERIALISED_TURN_SCHEMA,
        "outcome": {
            "tenant": outcome.tenant,
            "tool_calls": [
                {"tool_name": c.tool_name, "target_table": c.target_table, "target_id": c.target_id}
                for c in outcome.tool_calls
            ],
            "proposals": [{"proposal_id": p.proposal_id} for p in outcome.proposals],
            "denial_outcome": outcome.denial_outcome,
            "denial_missing_scope": outcome.denial_missing_scope,
            "budget_exhausted": outcome.budget_exhausted,
            "truncated": outcome.truncated,
            "truncation_disclosed": outcome.truncation_disclosed,
            "observed_outcomes": sorted(outcome.observed_outcomes),
        },
        "tool_arguments": [
            {"tool_name": a.tool_name, "arguments": dict(a.arguments)} for a in turn.tool_arguments
        ],
        "records": {
            "invocations": [
                {
                    "tier": i.tier,
                    "role": i.role,
                    "model_id": i.model_id,
                    "tokens": {
                        "input_tokens": i.tokens.input_tokens,
                        "output_tokens": i.tokens.output_tokens,
                    },
                    "cache": {
                        "read_tokens": i.cache.read_tokens,
                        "write_tokens": i.cache.write_tokens,
                    },
                    "latency_ms": i.latency_ms,
                }
                for i in turn.invocations
            ],
            "tool_calls": [
                {
                    "tool_name": t.tool_name,
                    "role": t.role,
                    "latency_ms": t.latency_ms,
                    "outcome_kind": t.outcome_kind,
                    "truncated": t.truncated,
                }
                for t in turn.tool_calls
            ],
        },
        "stream_calls": turn.stream_calls,
        "exclusions": [
            {"invocation_id": e.invocation_id, "category": e.category.value, "detail": e.detail}
            for e in turn.exclusions
        ],
        "turn_latency_ms": turn.turn_latency_ms,
    }
    if turn.usage_lower_bound:
        serialised[USAGE_LOWER_BOUND_KEY] = True
    if turn.resolved_efforts is not None:
        serialised[RESOLVED_TIERS_KEY] = dict(turn.resolved_efforts)
    if turn.prompt_bundle_sha256 is not None:
        serialised[PROMPT_BUNDLE_SHA256_KEY] = turn.prompt_bundle_sha256
    return serialised


class _Reader:
    """Strict accessors over one decoded JSON object; every miss raises ``ValueError``."""

    def __init__(self, value: object, where: str, keys: Iterable[str]) -> None:
        expected = frozenset(keys)
        if not isinstance(value, dict):
            raise ValueError(f"{where} must be an object")
        if frozenset(value) != expected:
            raise ValueError(f"{where} must carry exactly the keys {sorted(expected)}")
        self.value: dict[str, object] = value
        self.where = where

    def _raw(self, key: str) -> object:
        return self.value[key]

    def string(self, key: str) -> str:
        raw = self._raw(key)
        if not isinstance(raw, str):
            raise ValueError(f"{self.where}.{key} must be a string")
        return raw

    def optional_string(self, key: str) -> str | None:
        raw = self._raw(key)
        if raw is not None and not isinstance(raw, str):
            raise ValueError(f"{self.where}.{key} must be a string or null")
        return raw

    def boolean(self, key: str) -> bool:
        raw = self._raw(key)
        if not isinstance(raw, bool):
            raise ValueError(f"{self.where}.{key} must be a boolean")
        return raw

    def count(self, key: str) -> int:
        raw = self._raw(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError(f"{self.where}.{key} must be a non-negative integer")
        return raw

    def optional_number(self, key: str) -> float | None:
        raw = self._raw(key)
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            raise ValueError(f"{self.where}.{key} must be a number or null")
        return float(raw)

    def array(self, key: str) -> list[object]:
        raw = self._raw(key)
        if not isinstance(raw, list):
            raise ValueError(f"{self.where}.{key} must be an array")
        return raw


def _invocation(raw: object, where: str) -> InvocationAttribution:
    reader = _Reader(raw, where, ("tier", "role", "model_id", "tokens", "cache", "latency_ms"))
    tokens = _Reader(reader.value["tokens"], f"{where}.tokens", ("input_tokens", "output_tokens"))
    cache = _Reader(reader.value["cache"], f"{where}.cache", ("read_tokens", "write_tokens"))
    return InvocationAttribution(
        tier=cast(Tier, reader.string("tier")),
        role=reader.string("role"),
        model_id=reader.string("model_id"),
        tokens=TokenCounts(
            input_tokens=tokens.count("input_tokens"), output_tokens=tokens.count("output_tokens")
        ),
        cache=CacheCounts(
            read_tokens=cache.count("read_tokens"), write_tokens=cache.count("write_tokens")
        ),
        latency_ms=reader.optional_number("latency_ms"),
    )


def _tool_call_attribution(raw: object, where: str) -> ToolCallAttribution:
    reader = _Reader(raw, where, ("tool_name", "role", "latency_ms", "outcome_kind", "truncated"))
    return ToolCallAttribution(
        tool_name=reader.string("tool_name"),
        role=reader.string("role"),
        latency_ms=reader.optional_number("latency_ms"),
        outcome_kind=cast(OutcomeKind, reader.string("outcome_kind")),
        truncated=reader.boolean("truncated"),
    )


def _outcome(raw: object) -> TurnOutcome:
    reader = _Reader(
        raw,
        "outcome",
        (
            "tenant",
            "tool_calls",
            "proposals",
            "denial_outcome",
            "denial_missing_scope",
            "budget_exhausted",
            "truncated",
            "truncation_disclosed",
            "observed_outcomes",
        ),
    )
    tool_calls = []
    for index, item in enumerate(reader.array("tool_calls")):
        call = _Reader(
            item, f"outcome.tool_calls[{index}]", ("tool_name", "target_table", "target_id")
        )
        tool_calls.append(
            ToolCall(
                tool_name=call.string("tool_name"),
                target_table=call.optional_string("target_table"),
                target_id=call.optional_string("target_id"),
            )
        )
    proposals = [
        ObservedProposal(
            proposal_id=_Reader(item, f"outcome.proposals[{index}]", ("proposal_id",)).string(
                "proposal_id"
            )
        )
        for index, item in enumerate(reader.array("proposals"))
    ]
    labels = reader.array("observed_outcomes")
    if not all(isinstance(label, str) for label in labels):
        raise ValueError("outcome.observed_outcomes must be an array of strings")
    return TurnOutcome(
        tenant=reader.string("tenant"),
        tool_calls=tuple(tool_calls),
        proposals=tuple(proposals),
        denial_outcome=reader.optional_string("denial_outcome"),
        denial_missing_scope=reader.optional_string("denial_missing_scope"),
        budget_exhausted=reader.boolean("budget_exhausted"),
        truncated=reader.boolean("truncated"),
        truncation_disclosed=reader.boolean("truncation_disclosed"),
        observed_outcomes=frozenset(cast("list[str]", labels)),
    )


def _resolved_efforts(raw: object) -> dict[Tier, str | None]:
    where = f"turn.{RESOLVED_TIERS_KEY}"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object")
    efforts: dict[Tier, str | None] = {}
    for tier, effort in raw.items():
        if tier not in _TIERS:
            raise ValueError(f"{where} names an unknown tier {tier!r}")
        if effort is not None and not isinstance(effort, str):
            raise ValueError(f"{where}.{tier} must be a string or null")
        efforts[cast(Tier, tier)] = effort
    return efforts


def deserialise_turn(payload: Mapping[str, Any]) -> ObservedTurn:
    """Decode a :func:`serialise_turn` object back into an :class:`ObservedTurn`.

    :data:`USAGE_LOWER_BOUND_KEY` is optional and reads as ``false`` when absent;
    :data:`RESOLVED_TIERS_KEY` and :data:`PROMPT_BUNDLE_SHA256_KEY` are optional and read as
    ``None`` when absent.

    Raises:
        ValueError: ``payload`` is not exactly the :data:`SERIALISED_TURN_SCHEMA` shape: a key is
            missing or extra, a value has the wrong type, or an attribution fails its own
            validation.
    """
    body = dict(payload) if isinstance(payload, Mapping) else payload
    usage_lower_bound = False
    if isinstance(body, dict) and USAGE_LOWER_BOUND_KEY in body:
        flag = body.pop(USAGE_LOWER_BOUND_KEY)
        if not isinstance(flag, bool):
            raise ValueError(f"turn.{USAGE_LOWER_BOUND_KEY} must be a boolean")
        usage_lower_bound = flag
    resolved_efforts = None
    if isinstance(body, dict) and RESOLVED_TIERS_KEY in body:
        resolved_efforts = _resolved_efforts(body.pop(RESOLVED_TIERS_KEY))
    bundle_digest = None
    if isinstance(body, dict) and PROMPT_BUNDLE_SHA256_KEY in body:
        bundle_digest = body.pop(PROMPT_BUNDLE_SHA256_KEY)
        if not isinstance(bundle_digest, str):
            raise ValueError(f"turn.{PROMPT_BUNDLE_SHA256_KEY} must be a string")
    top = _Reader(
        body,
        "turn",
        (
            "schema",
            "outcome",
            "tool_arguments",
            "records",
            "stream_calls",
            "exclusions",
            "turn_latency_ms",
        ),
    )
    schema = top.value["schema"]
    if isinstance(schema, bool) or schema != SERIALISED_TURN_SCHEMA:
        raise ValueError(f"turn.schema must be {SERIALISED_TURN_SCHEMA}, got {schema!r}")
    arguments = []
    for index, item in enumerate(top.array("tool_arguments")):
        entry = _Reader(item, f"turn.tool_arguments[{index}]", ("tool_name", "arguments"))
        if not isinstance(entry.value["arguments"], dict):
            raise ValueError(f"turn.tool_arguments[{index}].arguments must be an object")
        arguments.append(
            ObservedToolArguments(
                tool_name=entry.string("tool_name"), arguments=dict(entry.value["arguments"])
            )
        )
    records = _Reader(top.value["records"], "turn.records", ("invocations", "tool_calls"))
    exclusions = []
    for index, item in enumerate(top.array("exclusions")):
        entry = _Reader(item, f"turn.exclusions[{index}]", ("invocation_id", "category", "detail"))
        exclusions.append(
            Exclusion(
                invocation_id=entry.string("invocation_id"),
                category=ExclusionCategory(entry.string("category")),
                detail=entry.string("detail"),
            )
        )
    return ObservedTurn(
        outcome=_outcome(top.value["outcome"]),
        tool_arguments=tuple(arguments),
        invocations=tuple(
            _invocation(item, f"turn.records.invocations[{index}]")
            for index, item in enumerate(records.array("invocations"))
        ),
        tool_calls=tuple(
            _tool_call_attribution(item, f"turn.records.tool_calls[{index}]")
            for index, item in enumerate(records.array("tool_calls"))
        ),
        stream_calls=top.count("stream_calls"),
        exclusions=tuple(exclusions),
        turn_latency_ms=top.optional_number("turn_latency_ms"),
        usage_lower_bound=usage_lower_bound,
        resolved_efforts=resolved_efforts,
        prompt_bundle_sha256=bundle_digest,
    )


__all__ = [
    "PROMPT_BUNDLE_SHA256_KEY",
    "RESOLVED_TIERS_KEY",
    "SERIALISED_TURN_SCHEMA",
    "TOOL_OUTPUT_TRUNCATED_KEY",
    "USAGE_LOWER_BOUND_KEY",
    "ExclusionRecordingProvider",
    "ObservedTurn",
    "ObservingDispatch",
    "deserialise_turn",
    "drain_provider_exclusions",
    "observe_turn",
    "serialise_turn",
    "tool_output_reports_truncation",
]

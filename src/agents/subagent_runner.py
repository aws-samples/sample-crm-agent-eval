# SPDX-License-Identifier: MIT
"""The subagent tier: one delegation runs a subagent-tier model loop.

Calling one of the supervisor's subagent tools (:func:`agents.supervisor.subagent_tool_schemas`)
runs this loop. Given a :class:`~agents.subagents.base.SubagentSpec` and the supervisor's
``intent``, it:

1. drives the caller-resolved subagent-tier invocation over exactly ``spec.tools``;
2. realises each leaf tool call through the injected tool-tier actor (:data:`ToolActor`), which
   owns identity and dispatch;
3. classifies each raw result with :func:`agents.supervisor.classify_tool_result`, discloses
   truncation with :func:`agents.supervisor.describe_truncation`, and surfaces a ``submit_proposal``
   with :func:`agents.supervisor.proposal_event_for`;
4. returns one four-way :class:`~agents.supervisor.SubagentOutcome`, the
   :class:`~agents.events.AgentEvent` stream, and the per-invocation / per-tool-call attribution.

The subagent's first message is the system prompt ``config/agent_prompts/subagent.yaml`` rendered
with the spec's ``role`` and ``purpose`` (:func:`subagent_system_message`); the intent follows as
the user message. A ``data`` outcome carries every successful leaf result in call order,
``{"results": [{"tool": name, "body": payload}, ...]}``, the list bounded once by
:func:`bound_results` and marked ``"truncated": true`` when cut. A
``tool_failure`` from a leaf tool is cleared when a later call to the same tool returns ``data`` or
``no_match``; a denial is never cleared.

Tool results pass through verbatim, bounded only by :func:`agents.events.truncate_tool_output`.
The runner reads no wall clock of its own. Given the turn's :class:`~agents.budget.TurnBudget`, it
checks the budget's wall clock between rounds and bounds every model wait by the time left
(:func:`agents.budget.until_deadline`, :func:`agents.budget.within_deadline`). When the clock runs
out, the delegation ends with the outcome, events and attribution gathered so far. A leaf tool
call already dispatched is always recorded. Given a ``request_prompt_limit``, each subagent round's
request is checked with :func:`agents.prompt_estimate.refuse_if_over_limit` before it is sent; a
refused round, or a refused tool-tier call (:class:`agents.budget.RequestRefusedError`), ends the
delegation the same way, with no attribution for the refused request.
"""

from __future__ import annotations

import contextlib
import functools
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final, Protocol

from agents.agent_prompts import load_agent_prompts
from agents.budget import (
    DeadlineReachedError,
    RequestRefusedError,
    TurnBudget,
    until_deadline,
    within_deadline,
)
from agents.events import (
    TOOL_RESULT_BYTE_CEILING,
    TRUNCATION_MARKER,
    AgentEvent,
    truncate_tool_output,
)
from agents.prompt_estimate import refuse_if_over_limit
from agents.registry import RegisteredTool
from agents.subagents.base import SUBAGENT_TIER, SubagentSpec
from agents.supervisor import (
    SubagentOutcome,
    classify_tool_result,
    describe_truncation,
    proposal_event_for,
)
from agents.tier_attribution import (
    CacheCounts,
    InvocationAttribution,
    TokenCounts,
    ToolCallAttribution,
)
from agents.tool_actor import TOOL_TIER
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation

#: The one tool whose successful result is surfaced as a structured ``proposal`` event.
_SUBMIT_PROPOSAL_TOOL_NAME: Final[str] = "submit_proposal"

#: The ``config/agent_prompts/`` file stem holding the subagent system prompt template.
SUBAGENT_PROMPT_NAME: Final[str] = "subagent"

#: The key a ``data`` outcome's payload lists every successful leaf result under.
RESULTS_KEY: Final[str] = "results"

#: Backstop on the number of subagent-tier provider rounds one delegation may drive. When reached,
#: the delegation ends with the outcome accumulated so far.
DEFAULT_MAX_SUBAGENT_ROUNDS: Final[int] = 8


class ToolActorResult(Protocol):
    """What this runner consumes from a tool-tier actor call.

    Satisfied structurally by :class:`agents.tool_actor.ToolActorResult` and by test fakes.

    Attributes:
        output: The action Lambda's raw response, verbatim, in the
            :meth:`crm.handlers._common.ok` / :meth:`crm.errors.CrmError.to_api_response` shape
            :func:`agents.supervisor.classify_tool_result` reads.
        invocation: The tool-tier model call's attribution (``tier == "tool"``).
        dispatch_latency_ms: Wall-clock of the Lambda dispatch in ms, or ``None`` on the stub path.
    """

    @property
    def output(self) -> dict[str, object]: ...

    @property
    def invocation(self) -> InvocationAttribution: ...

    @property
    def dispatch_latency_ms(self) -> float | None: ...


#: A tool-tier actor as this runner uses it: given a leaf tool and the subagent's intent for that
#: call, formulate schema-valid args, dispatch the real Lambda, and return the raw result plus the
#: tool-tier attribution. Pre-bound with identity/provider/tool-model/dispatch by the driver.
ToolActor = Callable[[RegisteredTool, str], ToolActorResult | Awaitable[ToolActorResult]]


@dataclass(frozen=True, slots=True)
class SubagentRunResult:
    """One subagent delegation's full result.

    Attributes:
        outcome: Exactly one four-way :class:`~agents.supervisor.SubagentOutcome`.
        events: The closed :class:`~agents.events.AgentEvent` stream, in emission order.
        invocations: Every model-call attribution this delegation produced — the subagent-tier
            call(s) and the tool-tier actor call(s) — for ``(tier, role, model_id)`` metering.
        tool_calls: One :class:`~agents.tier_attribution.ToolCallAttribution` per dispatched leaf
            tool call.
    """

    outcome: SubagentOutcome
    events: tuple[AgentEvent, ...] = field(default_factory=tuple)
    invocations: tuple[InvocationAttribution, ...] = field(default_factory=tuple)
    tool_calls: tuple[ToolCallAttribution, ...] = field(default_factory=tuple)


def _tool_schema(tool: RegisteredTool) -> dict[str, object]:
    """Render a leaf tool in the ``{name, description, input_schema}`` shape the provider reads."""
    return {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}


def _is_usage(event: ProviderEvent) -> bool:
    """Whether ``event`` is a usage report, complete or not; once one arrives the runner meters."""
    return event.type == "usage"


def _reports_both_counts(event: ProviderEvent) -> bool:
    """Whether usage ``event`` carries both token counts."""
    return event.input_tokens is not None and event.output_tokens is not None


@functools.cache
def _subagent_prompt_template() -> str:
    return load_agent_prompts()[SUBAGENT_PROMPT_NAME]


def subagent_system_message(spec: SubagentSpec) -> dict[str, object]:
    """Return the ``system`` message opening every delegation to ``spec``.

    Raises:
        KeyError: ``config/agent_prompts/subagent.yaml`` is absent.
    """
    content = _subagent_prompt_template().format(role=spec.role, purpose=spec.purpose)
    return {"role": "system", "content": content}


def _call_intent(tool_input: dict[str, object] | None, fallback: str) -> str:
    """Derive the intent handed to the tool-tier actor for one leaf call.

    The subagent's draft arguments, serialised with sorted keys; the delegation's own intent when
    the subagent emitted no arguments.
    """
    if tool_input:
        return json.dumps(tool_input, sort_keys=True)
    return fallback


def run_subagent(
    *,
    spec: SubagentSpec,
    intent: str,
    resolved: ResolvedInvocation,
    provider: ModelProvider,
    tool_actor: ToolActor,
    max_rounds: int = DEFAULT_MAX_SUBAGENT_ROUNDS,
    budget: TurnBudget | None = None,
    request_prompt_limit: int | None = None,
) -> SubagentRunResult:
    """Run one subagent delegation and return its outcome, events, and attribution.

    Args:
        spec: The subagent whose role and leaf tools this delegation runs over.
        intent: The supervisor's delegated task (the subagent tool schema's ``intent`` input).
        resolved: The subagent tier's validated invocation (model, effort), as
            :func:`providers.tier_tuning.resolve_tier` returns it.
        provider: The provider seam for that model's family (the stub or a live adapter).
        tool_actor: The pre-bound tool-tier actor each leaf call is realised through. This runner
            never dispatches a Lambda or builds an identity itself — the actor owns that boundary.
        max_rounds: Backstop on subagent-tier provider rounds (see
            :data:`DEFAULT_MAX_SUBAGENT_ROUNDS`).
        budget: The turn's budget, whose wall clock ends the delegation early; ``None`` sets no
            wall-clock bound.
        request_prompt_limit: The subagent tier's per-request estimated prompt-token limit; a
            round over it is refused and recorded on ``budget``. ``None`` checks nothing.

    Returns:
        A :class:`SubagentRunResult`.
    """
    import asyncio

    return asyncio.run(
        run_subagent_async(
            spec=spec,
            intent=intent,
            resolved=resolved,
            provider=provider,
            tool_actor=tool_actor,
            max_rounds=max_rounds,
            budget=budget,
            request_prompt_limit=request_prompt_limit,
        )
    )


async def run_subagent_async(
    *,
    spec: SubagentSpec,
    intent: str,
    resolved: ResolvedInvocation,
    provider: ModelProvider,
    tool_actor: ToolActor,
    max_rounds: int = DEFAULT_MAX_SUBAGENT_ROUNDS,
    budget: TurnBudget | None = None,
    request_prompt_limit: int | None = None,
) -> SubagentRunResult:
    """Await one subagent delegation in the caller's event loop and return its result.

    The awaitable core :func:`run_subagent` wraps, used by :func:`agents.turn_driver.drive_turn` so
    a cancellation can reach a delegation in flight. Arguments and return are identical to
    :func:`run_subagent`.
    """
    driver = _SubagentDriver(
        spec=spec,
        resolved=resolved,
        provider=provider,
        tool_actor=tool_actor,
        budget=budget,
        request_prompt_limit=request_prompt_limit,
    )
    with contextlib.suppress(DeadlineReachedError, RequestRefusedError):
        await driver._consume(intent=intent, max_rounds=max_rounds)
    return driver.result()


@dataclass(slots=True)
class _SubagentDriver:
    """Mutable per-delegation state: the provider loop, its events, and its attribution records."""

    spec: SubagentSpec
    resolved: ResolvedInvocation
    provider: ModelProvider
    tool_actor: ToolActor
    budget: TurnBudget | None = None
    request_prompt_limit: int | None = None
    events: list[AgentEvent] = field(default_factory=list)
    invocations: list[InvocationAttribution] = field(default_factory=list)
    tool_calls: list[ToolCallAttribution] = field(default_factory=list)
    _results: list[dict[str, object]] = field(default_factory=list)
    _matched_nothing: bool = False
    _denied: SubagentOutcome | None = None
    _open_failures: dict[str, str] = field(default_factory=dict)

    async def _consume(self, *, intent: str, max_rounds: int) -> None:
        messages: list[dict[str, object]] = [
            subagent_system_message(self.spec),
            {"role": "user", "content": intent},
        ]
        tool_schemas = [_tool_schema(tool) for tool in self.spec.tools]
        tools_by_name = {tool.name: tool for tool in self.spec.tools}
        round_number = 0
        while True:
            round_number += 1
            if round_number > max_rounds:
                return
            if self.budget is not None and self.budget.is_deadline_expired():
                return
            total_input = total_output = total_cache_read = total_cache_write = 0
            saw_usage = False
            saw_incomplete_usage = False
            saw_error = False
            round_latency_ms: float | None = None
            awaiting_tools = False
            round_text = ""
            round_calls: list[dict[str, object]] = []
            round_results: list[dict[str, object]] = []

            if self.request_prompt_limit is not None:
                refuse_if_over_limit(
                    messages,
                    tool_schemas,
                    limit=self.request_prompt_limit,
                    tier=SUBAGENT_TIER,
                    role=self.spec.role,
                    budget=self.budget,
                )
            stream = self.provider.stream(self.resolved, messages, tool_schemas)
            if self.budget is not None:
                stream = until_deadline(
                    stream,
                    self.budget,
                    tier=SUBAGENT_TIER,
                    role=self.spec.role,
                    is_metered=_is_usage,
                )
            try:
                async for event in stream:
                    if event.type == "text_delta":
                        round_text += event.text or ""
                        continue
                    if event.type == "usage":
                        total_input += event.input_tokens or 0
                        total_output += event.output_tokens or 0
                        total_cache_read += event.cache_read_tokens or 0
                        total_cache_write += event.cache_write_tokens or 0
                        if _reports_both_counts(event):
                            saw_usage = True
                        else:
                            saw_incomplete_usage = True
                        if event.latency_ms is not None:
                            round_latency_ms = event.latency_ms
                        continue
                    if event.type == "completed":
                        awaiting_tools = event.awaiting_tools
                        continue
                    if event.type == "error":
                        saw_error = True
                        continue
                    if event.type != "tool_call":
                        continue
                    tool_name = event.tool_name
                    if tool_name is None or tool_name not in tools_by_name:
                        continue
                    call_id = event.tool_id or f"call_{len(round_calls)}"
                    tool_input = dict(event.tool_input or {})
                    leaf_output = await self._dispatch_leaf(
                        tools_by_name[tool_name], tool_input, fallback_intent=intent
                    )
                    round_calls.append({"id": call_id, "name": tool_name, "input": tool_input})
                    round_results.append({"id": call_id, "output": json.dumps(leaf_output)})
            except (DeadlineReachedError, RequestRefusedError) as stopped:
                self._settle_round(
                    saw_usage=saw_usage,
                    saw_incomplete_usage=saw_incomplete_usage,
                    saw_error=saw_error,
                    tokens=TokenCounts(input_tokens=total_input, output_tokens=total_output),
                    cache=CacheCounts(read_tokens=total_cache_read, write_tokens=total_cache_write),
                    latency_ms=round_latency_ms,
                    unreported_cause=(
                        "deadline" if isinstance(stopped, DeadlineReachedError) else "turn_stopped"
                    ),
                )
                raise

            self._settle_round(
                saw_usage=saw_usage,
                saw_incomplete_usage=saw_incomplete_usage,
                saw_error=saw_error,
                tokens=TokenCounts(input_tokens=total_input, output_tokens=total_output),
                cache=CacheCounts(read_tokens=total_cache_read, write_tokens=total_cache_write),
                latency_ms=round_latency_ms,
                unreported_cause=None,
            )

            if awaiting_tools and round_calls:
                messages.append(
                    {"role": "assistant", "content": round_text, "tool_calls": round_calls}
                )
                messages.append({"role": "tool", "tool_results": round_results})
                continue
            return

    def _settle_round(
        self,
        *,
        saw_usage: bool,
        saw_incomplete_usage: bool,
        saw_error: bool,
        tokens: TokenCounts,
        cache: CacheCounts,
        latency_ms: float | None,
        unreported_cause: str | None,
    ) -> None:
        """Meter one subagent round exactly once: an invocation or a budget note, never both.

        A round is metered only when it reported both token counts in every usage event. A usage
        event missing either count makes the round unmetered (``partial_usage``), so the turn's
        usage reads as a lower bound instead of a complete figure that is too low. A round that
        ended on a provider ``error`` event without complete usage is noted ``provider_error``
        (that cause wins over ``partial_usage``). A round that reported no usage is noted with
        ``unreported_cause`` when given; ``None`` leaves it to :func:`agents.budget.until_deadline`.
        Notes need a budget.
        """
        if saw_usage and not saw_incomplete_usage:
            self.invocations.append(
                InvocationAttribution(
                    tier=SUBAGENT_TIER,
                    role=self.spec.role,
                    model_id=self.resolved.model.model_id,
                    tokens=tokens,
                    cache=cache,
                    latency_ms=latency_ms,
                )
            )
            return
        if saw_error:
            cause: str | None = "provider_error"
        elif saw_incomplete_usage:
            cause = "partial_usage"
        else:
            cause = unreported_cause
        if cause is not None and self.budget is not None:
            self.budget.note_call_without_usage(
                tier=SUBAGENT_TIER, role=self.spec.role, cause=cause
            )

    async def _dispatch_leaf(
        self, tool: RegisteredTool, tool_input: dict[str, object], *, fallback_intent: str
    ) -> dict[str, object]:
        """Realise one leaf tool call through the actor, classify it, and record events+attribution.

        The actor may be sync or async (:data:`ToolActor`). Returns the actor's raw ``output``
        verbatim for the next round's tool result; the emitted ``tool_result`` event carries the
        record-boundary-bounded copy.
        """
        call = self.tool_actor(tool, _call_intent(tool_input, fallback_intent))
        if isinstance(call, Awaitable):
            result = await (
                call
                if self.budget is None
                else within_deadline(call, self.budget, tier=TOOL_TIER, role=self.spec.role)
            )
        else:
            result = call
        raw_output = result.output
        self.invocations.append(result.invocation)

        display_output, truncated = _bounded_output(raw_output)
        notice = describe_truncation(raw_output, event_truncated=truncated)
        outcome = classify_tool_result(raw_output)
        self._record_outcome(tool.name, outcome)

        self.events.append(
            AgentEvent(kind="tool_call", tool_name=tool.name, tool_input=dict(tool_input))
        )
        self.events.append(
            AgentEvent(kind="tool_result", tool_output=display_output, truncated=truncated)
        )
        if tool.name == _SUBMIT_PROPOSAL_TOOL_NAME and "error" not in raw_output:
            self.events.append(proposal_event_for(raw_output))
        self.tool_calls.append(
            ToolCallAttribution(
                tool_name=tool.name,
                role=self.spec.role,
                latency_ms=result.dispatch_latency_ms,
                outcome_kind=outcome.kind,
                truncated=truncated or notice is not None,
            )
        )
        return raw_output

    def _record_outcome(self, tool_name: str, outcome: SubagentOutcome) -> None:
        """Fold one leaf outcome into the delegation's aggregate.

        The first denial is kept. A failure stays open until a later call to the same tool returns
        ``data`` or ``no_match``. A ``data`` result appends its full payload; :func:`bound_results`
        bounds the aggregate once.
        """
        if outcome.kind == "denied":
            if self._denied is None:
                self._denied = outcome
            return
        if outcome.kind == "tool_failure":
            self._open_failures.setdefault(
                tool_name, outcome.error_message or "A subagent tool call failed."
            )
            return
        self._open_failures.pop(tool_name, None)
        if outcome.kind == "no_match":
            self._matched_nothing = True
            return
        self._results.append({"tool": tool_name, "body": outcome.data or {}})

    def result(self) -> SubagentRunResult:
        """Assemble the single four-way outcome and the collected events/attribution."""
        return SubagentRunResult(
            outcome=self._aggregate_outcome(),
            events=tuple(self.events),
            invocations=tuple(self.invocations),
            tool_calls=tuple(self.tool_calls),
        )

    def _aggregate_outcome(self) -> SubagentOutcome:
        """Collapse every leaf outcome into one: denied, then an open failure, then data, else
        no_match.

        An open failure reports the first such tool's first message. A delegation that dispatched
        no leaf tool reports ``no_match``.
        """
        if self._denied is not None:
            return self._denied
        if self._open_failures:
            return SubagentOutcome(
                kind="tool_failure", error_message=next(iter(self._open_failures.values()))
            )
        if self._results:
            results, truncated = bound_results(self._results)
            data: dict[str, object] = {RESULTS_KEY: results}
            if truncated:
                data["truncated"] = True
            return SubagentOutcome(kind="data", data=data)
        return SubagentOutcome(kind="no_match")


def _repr_size(value: object) -> int:
    """The UTF-8 length of ``value``'s ``repr``, the measure :func:`truncate_tool_output` uses."""
    return len(repr(value).encode("utf-8"))


def _record_list_field(body: dict[str, object]) -> tuple[str, list[object]] | None:
    """Return the ``body`` list to trim at a record boundary: ``items``, else its largest list."""
    items = body.get("items")
    if isinstance(items, list):
        return "items", items
    lists = [(key, value) for key, value in body.items() if isinstance(value, list) and value]
    if not lists:
        return None
    return max(lists, key=lambda pair: _repr_size(pair[1]))


def _trim_entry(entry: dict[str, object], budget: int) -> dict[str, object] | None:
    """Return ``entry`` cut to ``budget`` bytes by trimming its body's record list.

    Returns ``None`` when the body holds no list, so it cannot be cut at a record boundary.
    """
    body = entry.get("body")
    if not isinstance(body, dict):
        return None
    record_list = _record_list_field(body)
    if record_list is None:
        return None
    field_name, records = record_list
    shell = {**entry, "body": {**body, field_name: []}}
    room = max(budget - _repr_size(shell) - _repr_size({TRUNCATION_MARKER: True}), 0)
    kept, _ = truncate_tool_output(
        [record for record in records if isinstance(record, dict)], byte_ceiling=room
    )
    if kept and kept[-1] == {TRUNCATION_MARKER: True}:
        kept.pop()
    trimmed = {**entry, "body": {**body, field_name: [*kept, {TRUNCATION_MARKER: True}]}}
    while kept and _repr_size(trimmed) > budget:
        kept.pop()
        trimmed = {**entry, "body": {**body, field_name: [*kept, {TRUNCATION_MARKER: True}]}}
    return trimmed


def bound_results(
    results: list[dict[str, object]], *, byte_ceiling: int = TOOL_RESULT_BYTE_CEILING
) -> tuple[list[dict[str, object]], bool]:
    """Bound a delegation's ``{"tool", "body"}`` entries, in order, to ``byte_ceiling`` bytes.

    Whole entries are kept while they fit, after room for the ``results``/``truncated`` envelope.
    The first entry that does not fit has its record list trimmed (:func:`_trim_entry`) to the room
    left; a first entry with no list is kept whole, marked ``"truncated": true``, and a later one
    with no list is dropped. Whatever follows is replaced by one
    :data:`agents.events.TRUNCATION_MARKER` entry. The first entry is never dropped.

    Returns:
        The bounded entries and whether anything was cut.
    """
    kept: list[dict[str, object]] = []
    used = _repr_size({RESULTS_KEY: [], "truncated": True})
    for index, entry in enumerate(results):
        size = _repr_size(entry)
        if used + size <= byte_ceiling:
            kept.append(entry)
            used += size
            continue
        trimmed = _trim_entry(entry, byte_ceiling - used)
        if trimmed is not None:
            kept.append(trimmed)
        elif index == 0:
            kept.append({**entry, "truncated": True})
        if trimmed is None or index + 1 < len(results):
            kept.append({TRUNCATION_MARKER: True})
        return kept, True
    return kept, False


def _bounded_output(raw_output: dict[str, object]) -> tuple[dict[str, object], bool]:
    """Apply the existing record-boundary byte ceiling to a result's ``items``, if any.

    Uses :func:`agents.events.truncate_tool_output`, as direct tool results do. A payload with no
    ``items`` list passes through unchanged.
    """
    body = raw_output.get("body")
    container = body if isinstance(body, dict) else raw_output
    items = container.get("items")
    if not isinstance(items, list):
        return raw_output, False
    bounded_items, truncated = truncate_tool_output(
        [item for item in items if isinstance(item, dict)]
    )
    if not truncated:
        return raw_output, False
    bounded_container = {**container, "items": bounded_items}
    if isinstance(body, dict):
        return {**raw_output, "body": bounded_container}, True
    return bounded_container, True


__all__ = [
    "DEFAULT_MAX_SUBAGENT_ROUNDS",
    "RESULTS_KEY",
    "SUBAGENT_PROMPT_NAME",
    "SubagentRunResult",
    "ToolActor",
    "ToolActorResult",
    "bound_results",
    "run_subagent",
    "run_subagent_async",
    "subagent_system_message",
]

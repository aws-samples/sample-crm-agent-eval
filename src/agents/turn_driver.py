# SPDX-License-Identifier: MIT
"""The one turn orchestrator shared by the runtime and the eval harness.

`drive_turn` resolves each tier's model and tuning from a :class:`~providers.tier_tuning.TurnTuning`,
takes each tier's provider from a :data:`ProviderResolver` keyed by that tier's family, and runs the
supervisor-tier provider loop over the supervisor's direct read tools plus the
subagent tools (`subagent_tool_schemas`). A subagent-tool call hands off to
:func:`agents.subagent_runner.run_subagent_async`, which runs the subagent tier over that role's
leaf tools and realises each leaf call through the tool tier
(:func:`agents.tool_actor.formulate_and_invoke`).
It yields the :class:`~agents.events.AgentEvent` stream, whose ``usage`` and ``completed`` events
carry the turn's priced cost lines and resolved tiers (:mod:`agents.turn_costing`), and appends one
:class:`~agents.tier_attribution.InvocationAttribution` per model invocation and one
:class:`~agents.tier_attribution.ToolCallAttribution` per dispatched tool call to the caller's
:class:`TurnRecords`.

The :class:`~agents.supervisor.TurnGuard` is consulted only between steps, so a dispatched tool call
completes; every terminating path calls :meth:`~agents.supervisor.TurnGuard.note_terminal`. The
turn's wall clock also bounds every model wait (:func:`agents.budget.until_deadline`) and is checked
between subagent rounds, so a turn that runs out of time ends with the guard's ``budget_exhausted``
event and the partial work, attribution and usage gathered so far. Tool calls go through the injected,
identity-bound ``dispatch`` callable (:data:`~agents.tool_actor.ToolDispatch`); this module builds
no identity and makes no authorization decision.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from agents.agent_prompts import load_agent_prompts
from agents.answer_style import answer_style_system_message
from agents.budget import TurnBudget, TurnBudgetSpec, until_deadline
from agents.cancellation import CancellationStore
from agents.events import AgentEvent, ResolvedTierInfo, budget_exhausted_event
from agents.registry import RegisteredTool, tool_registry_for
from agents.subagent_runner import SubagentRunResult, run_subagent_async
from agents.subagents import SUBAGENT_SPECS_BY_ROLE
from agents.subagents.base import SUBAGENT_TIER
from agents.supervisor import (
    DIRECT_TOOL_NAMES,
    SUPERVISOR_ROLE,
    SUPERVISOR_TIER,
    SubagentOutcome,
    TurnGuard,
    classify_tool_result,
    page_context_briefing,
    proposal_event_for,
    provider_event_to_agent_event,
    subagent_tool_schemas,
)
from agents.tier_attribution import (
    CacheCounts,
    InvocationAttribution,
    TokenCounts,
    ToolCallAttribution,
)
from agents.tool_actor import TOOL_TIER, ToolActorResult, ToolDispatch, formulate_and_invoke
from agents.turn_costing import cost_lines, is_cost_withheld, turn_cost
from crm.toolspec.page_context import PageContext
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, Tuning
from providers.capabilities import CapabilityMatrix, Family, Tier
from providers.prompt_caching import CACHE_SCOPE_KEY, VOLATILE
from providers.tier_tuning import TIERS, TierRequest, TurnTuning, resolve_turn

#: Returns the provider that serves one model family; called once per tier with that tier's family.
ProviderResolver = Callable[[Family], ModelProvider]

#: The ``config/agent_prompts/`` file stem holding the supervisor system prompt.
SUPERVISOR_PROMPT_NAME: Final[str] = "supervisor"


def supervisor_system_message() -> dict[str, object]:
    """Return the supervisor system prompt as a ``{role: "system", content}`` message.

    Raises:
        ValueError: ``config/agent_prompts/`` carries no supervisor prompt, or a prompt file there
            is malformed.
    """
    prompt = load_agent_prompts().get(SUPERVISOR_PROMPT_NAME)
    if prompt is None:
        raise ValueError(f"config/agent_prompts/ carries no {SUPERVISOR_PROMPT_NAME!r} prompt")
    return {"role": "system", "content": prompt}


@dataclass(slots=True)
class TurnRecords:
    """Out-of-band attribution accumulated as a turn runs, for `(tier, role, model_id)` metering.

    The runtime path ignores this; the harness reads it after the stream is exhausted.

    Attributes:
        invocations: One record per model invocation — the supervisor rounds, and every subagent-
            and tool-tier invocation a delegation produced.
        tool_calls: One record per dispatched tool call — the supervisor's direct calls and every
            subagent leaf call.
        calls_without_usage: Model calls of the turn that ended before reporting token usage
            (:attr:`agents.budget.TurnBudget.calls_without_usage`); when non-zero, ``invocations``
            under-count the turn's usage and cost.
    """

    invocations: list[InvocationAttribution] = field(default_factory=list)
    tool_calls: list[ToolCallAttribution] = field(default_factory=list)
    _budget: TurnBudget | None = field(default=None, repr=False, compare=False)

    @property
    def calls_without_usage(self) -> int:
        """Model calls of the turn that ended before reporting token usage; 0 before it starts."""
        return 0 if self._budget is None else self._budget.calls_without_usage


def _direct_tool_schemas(tools: tuple[RegisteredTool, ...]) -> list[dict[str, object]]:
    """Render the supervisor's direct tools in the `{name, description, input_schema}` shape."""
    return [
        {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
        for tool in tools
    ]


def _sum_usage(records: TurnRecords) -> tuple[int, int, int, int]:
    """Sum (input, output, cache-read, cache-write) tokens over every tier's invocation record."""
    return (
        sum(inv.tokens.input_tokens for inv in records.invocations),
        sum(inv.tokens.output_tokens for inv in records.invocations),
        sum(inv.cache.read_tokens for inv in records.invocations),
        sum(inv.cache.write_tokens for inv in records.invocations),
    )


def session_turn_tuning(tuning: Tuning) -> TurnTuning:
    """Map a single-family session :class:`~providers.base.Tuning` onto every tier.

    Every tier runs ``tuning.family``'s model for that tier; ``effort`` applies to the supervisor
    only, and the subagent and tool tiers run their ``config/models.yaml`` tier effort.
    """
    tiers = {tier: TierRequest(family=tuning.family) for tier in TIERS}
    tiers[SUPERVISOR_TIER] = TierRequest(family=tuning.family, effort=tuning.effort)
    return TurnTuning(source=tuning.source, tiers=tiers)


def _resolved_tiers(resolved: Mapping[Tier, ResolvedInvocation]) -> tuple[ResolvedTierInfo, ...]:
    """Describe the model and tuning each tier ran with, in tier order."""
    return tuple(
        {
            "tier": tier,
            "family": invocation.model.family,
            "model_id": invocation.model.model_id,
            "effort": invocation.effort,
        }
        for tier, invocation in resolved.items()
    )


def _bind_tool_actor(
    role: str,
    *,
    provider: ModelProvider,
    resolved: ResolvedInvocation,
    dispatch: ToolDispatch,
    dispatch_clock: Callable[[], float] | None = None,
) -> Callable[[RegisteredTool, str], Any]:
    """Pre-bind the tool-tier actor for one subagent role.

    ``dispatch_clock`` is a monotonic-seconds source used only to time each leaf's Lambda dispatch;
    ``None`` records no dispatch latency.
    """

    async def actor(tool: RegisteredTool, intent: str) -> ToolActorResult:
        return await formulate_and_invoke(
            tool,
            intent,
            role,
            provider=provider,
            resolved=resolved,
            dispatch=dispatch,
            clock=dispatch_clock,
        )

    return actor


def _outcome_to_tool_output(outcome: SubagentOutcome) -> dict[str, object]:
    """Render a subagent delegation's outcome as the action-shaped result the supervisor reads back.

    Round-trips through :func:`agents.supervisor.classify_tool_result` to the same kind, so the
    supervisor sees a delegation exactly as it would see a direct tool result.
    """
    if outcome.kind == "data":
        return {"statusCode": 200, "body": outcome.data or {}}
    if outcome.kind == "no_match":
        return {"statusCode": 200, "body": {"items": []}}
    if outcome.kind == "denied":
        return {
            "statusCode": 403,
            "error": {
                "code": "scope_denied",
                "missing_scope": outcome.missing_scope or "unknown",
                "message": outcome.denial_message or "The request was denied.",
            },
        }
    return {
        "statusCode": 502,
        "error": {"code": "tool_failure", "message": outcome.error_message or "A subagent failed."},
    }


def _proposal_or_none(tool_output: dict[str, object]) -> AgentEvent | None:
    """Return the structured `proposal` event `tool_output` carries, or ``None``."""
    body = tool_output.get("body", tool_output)
    payload = body if isinstance(body, dict) else {}
    if not isinstance(payload.get("proposal"), dict):
        return None
    return proposal_event_for(tool_output)


async def drive_turn(
    *,
    prompt: str,
    session_id: str,
    providers: ProviderResolver,
    matrix: CapabilityMatrix,
    tuning: TurnTuning,
    budget: TurnBudgetSpec,
    dispatch: ToolDispatch,
    records: TurnRecords,
    answer_style_message: dict[str, object] | None = None,
    supervisor_message: dict[str, object] | None = None,
    page_context: PageContext | None = None,
    history_messages: list[dict[str, object]] | None = None,
    max_rounds: int | None = None,
    cancellation: CancellationStore | None = None,
    clock: Callable[[], float] | None = None,
    dispatch_clock: Callable[[], float] | None = None,
) -> AsyncGenerator[AgentEvent]:
    """Drive one three-tier turn to completion, yielding its `AgentEvent` stream.

    Args:
        prompt: The user's chat message for this turn.
        session_id: The turn's session id — the cancellation marker's key.
        providers: Returns the provider for a family; called with each tier's resolved family.
        matrix: The capability matrix every tier's request is resolved and validated against.
        tuning: The per-tier request; each tier runs its own family, model and effort.
        budget: The turn's caps, normally :data:`agents.budget.CHAT_BUDGET` or
            :data:`agents.budget.EVAL_BUDGET`.
        dispatch: The identity-bound dispatch callable for direct and leaf tool calls.
        records: Caller-owned accumulator the driver appends attribution to (see :class:`TurnRecords`).
        answer_style_message: Test override for the answer-style system message, sent second.
            When ``None``, the driver loads it via
            :func:`agents.answer_style.answer_style_system_message`.
        supervisor_message: Test override for the supervisor system message, sent first. When
            ``None``, the driver loads it via :func:`supervisor_system_message`.
        page_context: The view the user was on, rendered as a system message marked
            :data:`providers.prompt_caching.VOLATILE`, or ``None``.
        history_messages: Prior conversation turns as ``{role, content}`` dicts, already read,
            scoped, and redacted by the runtime path. Spliced after the system context and before
            the current prompt. ``None`` (the eval path) means no history.
        max_rounds: Backstop on supervisor rounds; defaults to ``budget.max_tool_calls + 1``.
        cancellation: The cancel-marker store, or ``None`` for a turn with no cancel channel.
        clock: Monotonic-seconds source for the budget's wall-clock dimension (injectable for tests).
        dispatch_clock: Monotonic-seconds source used only to time each tool's Lambda dispatch, for
            both direct and subagent-leaf calls; ``None`` records no dispatch latency. Distinct from
            ``clock``.

    Yields:
        The closed :class:`~agents.events.AgentEvent` stream, ending in exactly one terminal
        ``completed``/``error`` event or a budget/cancellation terminal event.

    Raises:
        crm.errors.ValidationFailed: A tier's request does not resolve against ``matrix``; raised
            before any provider is called.
        ValueError: ``config/pricing.yaml`` has no boolean ``provisional``, or the supervisor
            prompt is missing or malformed (both before any provider is called), or the turn's cost
            total is unpriced or non-finite (at completion).
        KeyError: A model that ran has no price-table entry (at completion, when cost is not
            withheld).

    Being an async generator, it raises these on iteration, not on the call.
    """
    resolved = resolve_turn(matrix, tuning)
    resolved_supervisor = resolved[SUPERVISOR_TIER]
    supervisor_provider = providers(resolved_supervisor.model.family)
    cost_withheld = is_cost_withheld()

    direct_tools = tuple(
        t for t in tool_registry_for(SUPERVISOR_ROLE) if t.name in DIRECT_TOOL_NAMES
    )
    direct_by_name = {tool.name: tool for tool in direct_tools}
    subagent_names = frozenset(SUBAGENT_SPECS_BY_ROLE)
    tool_schemas: list[dict[str, object]] = [
        *_direct_tool_schemas(direct_tools),
        *subagent_tool_schemas(),
    ]

    messages: list[dict[str, object]] = [
        supervisor_message if supervisor_message is not None else supervisor_system_message(),
        answer_style_message if answer_style_message is not None else answer_style_system_message(),
    ]
    if page_context is not None:
        messages.append(
            {
                "role": "system",
                "content": page_context_briefing(page_context),
                CACHE_SCOPE_KEY: VOLATILE,
            }
        )
    if history_messages:
        messages.extend(history_messages)
    messages.append({"role": "user", "content": prompt})

    turn_budget = (
        TurnBudget.from_spec(budget, clock=clock)
        if clock is not None
        else TurnBudget.from_spec(budget)
    )
    records._budget = turn_budget
    guard = TurnGuard(session_id=session_id, budget=turn_budget, cancellation=cancellation)

    pre_flight = guard.terminal_event()
    if pre_flight is not None:
        yield pre_flight
        return

    effective_max_rounds = max_rounds if max_rounds is not None else budget.max_tool_calls + 1
    round_number = 0

    while True:
        round_number += 1
        if round_number > effective_max_rounds:
            guard.note_terminal()
            partial_work = (
                "; ".join(turn_budget.completed_actions)
                if turn_budget.completed_actions
                else "no tool calls completed"
            )
            yield budget_exhausted_event(
                partial_work=partial_work,
                error_message=(
                    f"Turn stopped after reaching the maximum of {effective_max_rounds} model "
                    "round(s) without producing a final answer."
                ),
            )
            return

        accumulated_text = ""
        round_tool_calls: list[dict[str, object]] = []
        round_tool_results: list[dict[str, object]] = []
        awaiting_tools = False
        completed_this_round = False
        round_usage_seen = False

        provider_events: AsyncIterator[ProviderEvent] = until_deadline(
            supervisor_provider.stream(resolved_supervisor, messages, tool_schemas),
            turn_budget,
            tier=SUPERVISOR_TIER,
            role=SUPERVISOR_ROLE,
            is_metered=lambda event: event.type == "usage",
        )
        # Groups this round's streamed text_delta frames; deterministic per session and round.
        stream_event_id = f"{session_id}:r{round_number}"
        async for provider_event in provider_events:
            if provider_event.type == "text_delta" and provider_event.text:
                accumulated_text += provider_event.text
                frame = provider_event_to_agent_event(
                    provider_event, stream_event_id=stream_event_id
                )
                if frame is not None:
                    yield frame
                continue
            if provider_event.type == "tool_result":
                continue
            if provider_event.type == "usage":
                round_usage_seen = True
                round_input = provider_event.input_tokens or 0
                round_output = provider_event.output_tokens or 0
                turn_budget.record_tokens(round_input + round_output)
                records.invocations.append(
                    InvocationAttribution(
                        tier=SUPERVISOR_TIER,
                        role=SUPERVISOR_ROLE,
                        model_id=resolved_supervisor.model.model_id,
                        tokens=TokenCounts(input_tokens=round_input, output_tokens=round_output),
                        cache=CacheCounts(
                            read_tokens=provider_event.cache_read_tokens or 0,
                            write_tokens=provider_event.cache_write_tokens or 0,
                        ),
                        latency_ms=provider_event.latency_ms,
                    )
                )
                continue
            if provider_event.type == "completed":
                awaiting_tools = provider_event.awaiting_tools
                if awaiting_tools and round_tool_calls:
                    completed_this_round = True
                    break
                final_text = (
                    provider_event.text if provider_event.text is not None else accumulated_text
                )
                if not final_text:
                    raise ValueError(
                        "completed provider event carries no text and none was accumulated"
                    )
                # The turn's usage figure sums every tier's attribution records.
                ui_in, ui_out, ui_cr, ui_cw = _sum_usage(records)
                lines = cost_lines(records.invocations, withheld=cost_withheld)
                accounting: dict[str, Any] = {
                    "input_tokens": ui_in,
                    "output_tokens": ui_out,
                    "cache_read_tokens": ui_cr,
                    "cache_write_tokens": ui_cw,
                    "cost_lines": lines,
                    "cost_usd": turn_cost(lines, withheld=cost_withheld),
                    "cost_withheld": cost_withheld,
                    "resolved_tiers": _resolved_tiers(resolved),
                }
                yield AgentEvent(kind="usage", **accounting)
                yield AgentEvent(kind="completed", text=final_text, **accounting)
                guard.note_terminal()
                return

            agent_event = provider_event_to_agent_event(
                provider_event, accumulated_text=accumulated_text
            )
            if agent_event is None:
                continue
            yield agent_event

            if agent_event.kind == "tool_call":
                name = agent_event.tool_name or ""
                call_id = provider_event.tool_id or f"call_{len(round_tool_calls)}"
                tool_input = dict(agent_event.tool_input or {})
                if name in subagent_names:
                    tool_output, nested_events, delegation = await _run_delegation(
                        role=name,
                        tool_input=tool_input,
                        fallback_intent=prompt,
                        providers=providers,
                        resolved=resolved,
                        dispatch=dispatch,
                        records=records,
                        budget=turn_budget,
                        dispatch_clock=dispatch_clock,
                    )
                    # The turn budget is charged with the delegation's tokens and leaf calls.
                    delegation_tokens = sum(
                        inv.tokens.input_tokens + inv.tokens.output_tokens
                        for inv in delegation.invocations
                    )
                    if delegation_tokens:
                        turn_budget.record_tokens(delegation_tokens)
                    for leaf in delegation.tool_calls:
                        turn_budget.record_tool_call(f"called {leaf.tool_name}")
                    guard.note_step(f"delegated to {name}")
                    for event in nested_events:
                        yield event
                    round_tool_calls.append({"id": call_id, "name": name, "input": tool_input})
                    round_tool_results.append({"id": call_id, "output": json.dumps(tool_output)})
                elif name in direct_by_name:
                    tool = direct_by_name[name]
                    dispatch_start = dispatch_clock() if dispatch_clock is not None else None
                    tool_output = dispatch(tool, tool_input)
                    dispatch_latency_ms = (
                        (dispatch_clock() - dispatch_start) * 1000.0
                        if dispatch_start is not None and dispatch_clock is not None
                        else None
                    )
                    turn_budget.record_tool_call(f"called {tool.name}")
                    guard.note_step(f"called {tool.name}")
                    yield AgentEvent(kind="tool_result", tool_output=tool_output)
                    proposal = _proposal_or_none(tool_output)
                    if proposal is not None:
                        yield proposal
                    records.tool_calls.append(
                        ToolCallAttribution(
                            tool_name=tool.name,
                            role=SUPERVISOR_ROLE,
                            latency_ms=dispatch_latency_ms,
                            outcome_kind=classify_tool_result(tool_output).kind,
                            truncated=False,
                        )
                    )
                    round_tool_calls.append({"id": call_id, "name": tool.name, "input": tool_input})
                    round_tool_results.append({"id": call_id, "output": json.dumps(tool_output)})

            if agent_event.kind == "error":
                guard.note_terminal()
                return

            terminal = guard.terminal_event()
            if terminal is not None:
                if not round_usage_seen:
                    turn_budget.note_call_without_usage(
                        tier=SUPERVISOR_TIER, role=SUPERVISOR_ROLE, cause="turn_stopped"
                    )
                    if terminal.error_kind == "budget_exhausted":
                        terminal = turn_budget.exhausted_event()
                yield terminal
                return

        if turn_budget.is_deadline_expired():
            terminal = guard.terminal_event()
            if terminal is not None:
                yield terminal
                return

        if not completed_this_round and not round_tool_calls:
            # The stream ended without completed/error and dispatched nothing.
            return

        messages.append(
            {"role": "assistant", "content": accumulated_text, "tool_calls": round_tool_calls}
        )
        messages.append({"role": "tool", "tool_results": round_tool_results})

        terminal = guard.terminal_event()
        if terminal is not None:
            yield terminal
            return


async def _run_delegation(
    *,
    role: str,
    tool_input: dict[str, object],
    fallback_intent: str,
    providers: ProviderResolver,
    resolved: Mapping[Tier, ResolvedInvocation],
    dispatch: ToolDispatch,
    records: TurnRecords,
    budget: TurnBudget,
    dispatch_clock: Callable[[], float] | None = None,
) -> tuple[dict[str, object], tuple[AgentEvent, ...], SubagentRunResult]:
    """Run one subagent delegation; merge its attribution; return outcome + events + the result.

    Awaits :func:`agents.subagent_runner.run_subagent_async` in this event loop, bounded by the
    turn's ``budget`` wall clock. Returns the outcome
    rendered as a tool result, the subagent's events, and the full
    :class:`~agents.subagent_runner.SubagentRunResult`; the per-tier attribution is merged into
    ``records``.
    """
    intent = tool_input.get("intent")
    resolved_intent = str(intent) if isinstance(intent, str) and intent.strip() else fallback_intent
    subagent = resolved[SUBAGENT_TIER]
    tool = resolved[TOOL_TIER]
    actor = _bind_tool_actor(
        role,
        provider=providers(tool.model.family),
        resolved=tool,
        dispatch=dispatch,
        dispatch_clock=dispatch_clock,
    )
    result = await run_subagent_async(
        spec=SUBAGENT_SPECS_BY_ROLE[role],
        intent=resolved_intent,
        resolved=subagent,
        provider=providers(subagent.model.family),
        tool_actor=actor,
        budget=budget,
    )
    records.invocations.extend(result.invocations)
    records.tool_calls.extend(result.tool_calls)
    return _outcome_to_tool_output(result.outcome), tuple(result.events), result


__all__ = [
    "SUPERVISOR_PROMPT_NAME",
    "ProviderResolver",
    "TurnRecords",
    "drive_turn",
    "session_turn_tuning",
    "supervisor_system_message",
]

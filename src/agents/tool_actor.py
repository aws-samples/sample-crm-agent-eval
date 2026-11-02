# SPDX-License-Identifier: MIT
"""The tool tier: an actor that formulates one tool call's arguments, never a summarizer.

:func:`formulate_and_invoke` runs one tool-tier model call that binds a natural-language ``intent``
to a tool's ``input_schema``, hands the arguments to the injected ``dispatch`` callable, and returns
the tool's raw response verbatim. The actor does not validate the arguments; the action Lambda's
handler does, and its ``validation_failed`` envelope returns verbatim. A model error or a missing
tool call becomes a ``tool_actor_error`` envelope without dispatching. The tool-tier attribution is
returned in every case. The actor holds no identity; the injected dispatch carries it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agents.registry import RegisteredTool
from agents.tier_attribution import CacheCounts, InvocationAttribution, TokenCounts
from providers.base import ModelProvider, ResolvedInvocation
from providers.capabilities import Tier

#: The tier every tool-tier invocation runs on.
TOOL_TIER: Tier = "tool"

#: The error ``code`` on an envelope the actor synthesises when the tool-tier model failed to
#: produce a usable call; classifies as ``tool_failure``.
_ACTOR_ERROR_CODE = "tool_actor_error"

#: A dispatch callable binding tool + validated arguments to the tool's action Lambda, with the
#: invoking identity already bound by the caller's factory. Structurally identical to
#: :data:`eval.harness.TurnDispatch`.
ToolDispatch = Callable[[RegisteredTool, dict[str, object]], dict[str, Any]]

#: A clock returning monotonic seconds, used only to time the Lambda dispatch. ``None`` leaves
#: dispatch latency unrecorded. Model-call latency comes from the provider's usage event.
Clock = Callable[[], float]

#: Folded into the single user message; directs the tool-tier model to emit a tool call only.
_ACTOR_DIRECTIVE = (
    "Call the provided tool once. Below is either draft arguments as JSON or a plain request. "
    "Use draft arguments as given wherever they fit the tool's schema, and fill in only what is "
    "missing. Reply with the tool call only, no text."
)


@dataclass(frozen=True, slots=True)
class ToolActorResult:
    """The outcome of one tool-tier leaf call: a classify-ready output plus its attribution.

    Attributes:
        output: A dict in the action-Lambda response/error shape — either the tool's verbatim
            response (never model-summarized) or a synthesised error envelope when the formulation
            failed. Always safe to pass to :func:`agents.supervisor.classify_tool_result`.
        invocation: Attribution for the tool-tier model call that formulated the arguments, always
            present (the model call happened regardless of whether its arguments were usable).
        dispatch_latency_ms: Wall-clock of the dispatch in milliseconds, or ``None`` when not
            measured or when no dispatch occurred (a failed formulation dispatches nothing).
    """

    output: dict[str, Any]
    invocation: InvocationAttribution
    dispatch_latency_ms: float | None = None


async def formulate_and_invoke(
    tool: RegisteredTool,
    intent: str,
    role: str,
    *,
    provider: ModelProvider,
    resolved: ResolvedInvocation,
    dispatch: ToolDispatch,
    clock: Clock | None = None,
) -> ToolActorResult:
    """Formulate one tool call's arguments on the tool tier and dispatch them verbatim.

    Args:
        tool: The leaf tool to call, from a subagent's ``SubagentSpec.tools``.
        intent: The natural-language description of what this tool call should accomplish.
        role: The agent role on whose behalf the tool tier acts (recorded in the attribution).
        provider: The provider seam. Called exactly once — to formulate arguments, never to
            summarize a result.
        resolved: The tool tier's validated invocation (model, effort), as
            :func:`providers.tier_tuning.resolve_tier` returns it; ``provider`` is its family's.
        dispatch: The identity-bound dispatch callable (see :data:`ToolDispatch`).
        clock: A monotonic-seconds clock used only to time the Lambda dispatch; when omitted,
            dispatch latency is left ``None``.

    Returns:
        A :class:`ToolActorResult`. ``output`` is the tool's verbatim response (including the
        handler's own ``validation_failed`` envelope when the formulation was schema-invalid); when
        the model emitted no tool call or errored, ``output`` is a ``tool_actor_error`` envelope
        and no dispatch occurs. Never raises for a model failure.
    """
    messages: list[dict[str, object]] = [
        {"role": "user", "content": f"{_ACTOR_DIRECTIVE}\n\n{intent}"}
    ]
    provider_tools: list[dict[str, object]] = [
        {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
    ]

    tool_input: dict[str, Any] | None = None
    input_tokens = output_tokens = cache_read = cache_write = 0
    saw_error = False
    # Model-call latency comes from the provider's usage event, which excludes retries.
    model_latency_ms: float | None = None

    async for event in provider.stream(resolved, messages, provider_tools):
        if event.type == "tool_call" and tool_input is None:
            tool_input = dict(event.tool_input or {})
        elif event.type == "usage":
            input_tokens += event.input_tokens or 0
            output_tokens += event.output_tokens or 0
            cache_read += event.cache_read_tokens or 0
            cache_write += event.cache_write_tokens or 0
            if event.latency_ms is not None:
                model_latency_ms = event.latency_ms
        elif event.type == "error":
            saw_error = True

    invocation = InvocationAttribution(
        tier=TOOL_TIER,
        role=role,
        model_id=resolved.model.model_id,
        tokens=TokenCounts(input_tokens=input_tokens, output_tokens=output_tokens),
        cache=CacheCounts(read_tokens=cache_read, write_tokens=cache_write),
        latency_ms=model_latency_ms,
    )

    output, dispatch_latency_ms = _resolve_output(
        tool, tool_input, saw_error=saw_error, dispatch=dispatch, clock=clock
    )
    return ToolActorResult(
        output=output, invocation=invocation, dispatch_latency_ms=dispatch_latency_ms
    )


def _resolve_output(
    tool: RegisteredTool,
    tool_input: dict[str, Any] | None,
    *,
    saw_error: bool,
    dispatch: ToolDispatch,
    clock: Clock | None,
) -> tuple[dict[str, Any], float | None]:
    """Dispatch the formulation verbatim, or synthesise a typed error envelope. Never raises."""
    if saw_error:
        return _actor_error(f"the tool-tier model reported an error formulating {tool.name}"), None
    if tool_input is None:
        return _actor_error(f"the tool-tier model produced no tool call for {tool.name}"), None

    dispatch_start = clock() if clock is not None else None
    output = dispatch(tool, tool_input)
    return output, _elapsed_ms(dispatch_start, clock)


def _actor_error(message: str) -> dict[str, Any]:
    """Build a taxonomy-shaped error envelope classify reads as ``tool_failure`` (not a denial)."""
    return {"statusCode": 502, "error": {"code": _ACTOR_ERROR_CODE, "message": message}}


def _elapsed_ms(start: float | None, clock: Clock | None) -> float | None:
    """Return milliseconds elapsed since ``start``, or ``None`` when latency is not being measured."""
    if start is None or clock is None:
        return None
    return (clock() - start) * 1000.0


__all__ = [
    "TOOL_TIER",
    "Clock",
    "ToolActorResult",
    "ToolDispatch",
    "formulate_and_invoke",
]

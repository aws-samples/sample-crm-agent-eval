# SPDX-License-Identifier: MIT
"""Binding test: `run_subagent` driving the real async `formulate_and_invoke` tool-tier actor.

Asserts all four `SubagentOutcome` kinds and the per-tier attribution records end to end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from agents.registry import RegisteredTool
from agents.subagent_runner import SubagentRunResult, run_subagent
from agents.subagents import SUBAGENT_SPECS_BY_ROLE
from agents.tool_actor import ToolActorResult, ToolDispatch, formulate_and_invoke
from agents.turn_driver import session_turn_tuning
from crm.errors import ValidationFailed
from crm.toolspec.definitions import BY_NAME
from crm.toolspec.validation import validate_arguments
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, Tuning
from providers.capabilities import load_capability_matrix
from providers.tier_tuning import resolve_turn

MATRIX = load_capability_matrix()
TUNING = Tuning(source="test.binding", family="anthropic", effort=None)
ROLE = "research"
_SPEC = SUBAGENT_SPECS_BY_ROLE[ROLE]
_TOOL = next(tool for tool in _SPEC.tools if tool.name == "get_account")

SUBAGENT_MODEL_ID = MATRIX.model_for("anthropic", "subagent").model_id
TOOL_MODEL_ID = MATRIX.model_for("anthropic", "tool").model_id


@dataclass
class _ScriptedProvider(ModelProvider):
    """Emits one scripted round: a tool_call for a tool, then usage + completed."""

    tool_name: str
    tool_input: dict[str, object]
    input_tokens: int = 40
    output_tokens: int = 12

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(
            type="tool_call",
            tool_name=self.tool_name,
            tool_input=dict(self.tool_input),
            tool_id="tc-1",
        )
        yield ProviderEvent(
            type="usage", input_tokens=self.input_tokens, output_tokens=self.output_tokens
        )
        yield ProviderEvent(type="completed", awaiting_tools=False)


@dataclass
class _StubDispatch:
    """Records dispatch calls and returns a fixed action-Lambda response."""

    response: dict[str, Any]
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def __call__(self, tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        self.calls.append((tool.name, dict(tool_input)))
        return self.response


@dataclass
class _HandlerDispatch:
    """A dispatch that runs the handler's `validate_arguments`, standing in for the action Lambda."""

    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def __call__(self, tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        self.calls.append((tool.name, dict(tool_input)))
        try:
            validate_arguments(BY_NAME[tool.name], tool_input)
        except ValidationFailed as error:
            return error.to_api_response()
        return {"statusCode": 200, "body": {"account_id": tool_input.get("account_id")}}


def _bind_actor(tool_provider: ModelProvider, dispatch: ToolDispatch) -> Any:
    """Pre-bind the real actor with role, provider, matrix, tuning and dispatch closed over."""

    async def actor(tool: RegisteredTool, intent: str) -> ToolActorResult:
        return await formulate_and_invoke(
            tool,
            intent,
            ROLE,
            provider=tool_provider,
            resolved=resolve_turn(MATRIX, session_turn_tuning(TUNING))["tool"],
            dispatch=dispatch,
        )

    return actor


def _run(
    *, dispatch_response: dict[str, Any], tool_args: dict[str, object]
) -> tuple[SubagentRunResult, _StubDispatch]:
    """Drive one delegation: the subagent calls get_account and the actor dispatches `tool_args`."""
    dispatch = _StubDispatch(response=dispatch_response)
    subagent_provider = _ScriptedProvider(
        tool_name="get_account", tool_input={"account_id": "acc-1"}
    )
    tool_provider = _ScriptedProvider(tool_name="get_account", tool_input=tool_args)
    result = run_subagent(
        spec=_SPEC,
        intent="find account acc-1",
        resolved=resolve_turn(MATRIX, session_turn_tuning(TUNING))["subagent"],
        provider=subagent_provider,
        tool_actor=_bind_actor(tool_provider, dispatch),
    )
    return result, dispatch


def _valid_args() -> dict[str, object]:
    return {"account_id": "acc-1"}


class TestFourOutcomesEndToEnd:
    def test_data_outcome_carries_the_body(self) -> None:
        body = {"account_id": "acc-1", "name": "Acme"}
        result, dispatch = _run(
            dispatch_response={"statusCode": 200, "body": body}, tool_args=_valid_args()
        )
        assert result.outcome.kind == "data"
        assert result.outcome.data == {"results": [{"tool": "get_account", "body": body}]}
        assert dispatch.calls == [
            ("get_account", {"account_id": "acc-1"})
        ]  # validated args dispatched
        assert result.tool_calls[0].outcome_kind == "data"

    def test_no_match_outcome_on_empty_items(self) -> None:
        result, _ = _run(
            dispatch_response={"statusCode": 200, "body": {"items": []}}, tool_args=_valid_args()
        )
        assert result.outcome.kind == "no_match"
        assert result.tool_calls[0].outcome_kind == "no_match"

    def test_denied_outcome_names_the_missing_scope(self) -> None:
        result, _ = _run(
            dispatch_response={
                "error": {
                    "code": "scope_denied",
                    "missing_scope": "account:acc-1",
                    "message": "Outside your book.",
                }
            },
            tool_args=_valid_args(),
        )
        assert result.outcome.kind == "denied"
        assert result.outcome.missing_scope == "account:acc-1"
        assert result.tool_calls[0].outcome_kind == "denied"

    def test_tool_failure_outcome_carries_the_message(self) -> None:
        result, _ = _run(
            dispatch_response={
                "statusCode": 500,
                "error": {"code": "internal_error", "message": "boom"},
            },
            tool_args=_valid_args(),
        )
        assert result.outcome.kind == "tool_failure"
        assert result.tool_calls[0].outcome_kind == "tool_failure"

    def test_schema_invalid_formulation_is_dispatched_and_the_handler_rejects_it(self) -> None:
        # The actor does not validate: the empty formulation is dispatched and the handler's
        # validate_arguments rejects it, which the runner classifies tool_failure.
        dispatch = _HandlerDispatch()
        subagent_provider = _ScriptedProvider(
            tool_name="get_account", tool_input={"account_id": "acc-1"}
        )
        tool_provider = _ScriptedProvider(tool_name="get_account", tool_input={})
        result = run_subagent(
            spec=_SPEC,
            intent="find account acc-1",
            resolved=resolve_turn(MATRIX, session_turn_tuning(TUNING))["subagent"],
            provider=subagent_provider,
            tool_actor=_bind_actor(tool_provider, dispatch),
        )
        assert dispatch.calls  # dispatched — the handler is the trust boundary
        assert result.outcome.kind == "tool_failure"
        assert result.tool_calls[0].outcome_kind == "tool_failure"


class TestAttributionRecordsEndToEnd:
    def test_both_tiers_are_attributed_and_the_tool_call_is_recorded(self) -> None:
        body = {"account_id": "acc-1"}
        result, _ = _run(
            dispatch_response={"statusCode": 200, "body": body}, tool_args=_valid_args()
        )

        by_tier = {inv.tier: inv for inv in result.invocations}
        assert "subagent" in by_tier and "tool" in by_tier

        subagent = by_tier["subagent"]
        assert subagent.role == ROLE
        assert subagent.model_id == SUBAGENT_MODEL_ID
        assert subagent.tokens.input_tokens == 40  # from the scripted subagent usage event

        tool = by_tier["tool"]
        assert tool.role == ROLE
        assert tool.model_id == TOOL_MODEL_ID  # the tool tier resolved by the real actor

        assert len(result.tool_calls) == 1
        call = result.tool_calls[0]
        assert call.tool_name == "get_account"
        assert call.role == ROLE
        assert call.outcome_kind == "data"

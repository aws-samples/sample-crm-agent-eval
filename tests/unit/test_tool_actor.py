# SPDX-License-Identifier: MIT
"""Unit tests for :mod:`agents.tool_actor`.

One model call formulates arguments, the injected dispatch runs them unvalidated, and the result
returns verbatim; no tool call or a model error yields a ``tool_actor_error`` envelope.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from agents import prompt_bundle, tool_actor
from agents.registry import RegisteredTool, full_registry
from agents.runtime_server import agent_identity_event
from agents.supervisor import classify_tool_result
from agents.tier_attribution import InvocationAttribution
from agents.tool_actor import TOOL_TIER, ToolActorResult, formulate_and_invoke
from agents.turn_driver import session_turn_tuning
from crm.envelope import (
    AGENT_IDENTITY_KEY,
    AGENT_PRINCIPAL_TYPE_KEY,
    CLAIM_REP_ID,
    CLAIM_TENANT,
    Identity,
    decode_identity,
)
from crm.errors import ValidationFailed
from crm.toolspec.definitions import BY_NAME
from crm.toolspec.validation import validate_arguments
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, Tuning
from providers.capabilities import load_capability_matrix
from providers.tier_tuning import resolve_turn

MATRIX = load_capability_matrix()
TUNING = Tuning(source="test.tool_actor")
ROLE = "research"


def _tool(name: str = "get_account") -> RegisteredTool:
    return full_registry()[name]


class FakeProvider(ModelProvider):
    """A provider whose stream is a fixed, caller-chosen event list, recording every call."""

    def __init__(self, events: list[ProviderEvent]) -> None:
        self._events = events
        self.calls: list[
            tuple[ResolvedInvocation, list[dict[str, object]], list[dict[str, object]]]
        ] = []

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.calls.append((invocation, messages, tools))
        for event in self._events:
            yield event


class RecordingDispatch:
    """A dispatch callable that records its calls and returns a fixed response."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response if response is not None else {"body": {"ok": True}}
        self.calls: list[tuple[RegisteredTool, dict[str, object]]] = []

    def __call__(self, tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        self.calls.append((tool, dict(tool_input)))
        return self.response


class HandlerDispatch:
    """A dispatch that runs `validate_arguments` as the action handler does.

    Returns the `validation_failed` envelope on a bad formulation and a success body otherwise.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[RegisteredTool, dict[str, object]]] = []

    def __call__(self, tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        self.calls.append((tool, dict(tool_input)))
        try:
            validate_arguments(BY_NAME[tool.name], tool_input)
        except ValidationFailed as error:
            return error.to_api_response()
        return {"statusCode": 200, "body": {"account_id": tool_input.get("account_id")}}


def _tool_call_stream(
    tool_input: dict[str, object],
    *,
    input_tokens: int = 50,
    output_tokens: int = 8,
    cache_read: int = 2,
    cache_write: int = 0,
) -> list[ProviderEvent]:
    return [
        ProviderEvent(type="tool_call", tool_name="get_account", tool_input=tool_input),
        ProviderEvent(
            type="usage",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        ),
        ProviderEvent(type="completed", input_tokens=input_tokens, output_tokens=output_tokens),
    ]


def _invoke(
    *,
    provider: ModelProvider,
    dispatch: Any,
    tool: RegisteredTool | None = None,
    intent: str = "look up the account",
    clock: Any = None,
) -> ToolActorResult:
    return asyncio.run(
        formulate_and_invoke(
            tool if tool is not None else _tool(),
            intent,
            ROLE,
            provider=provider,
            resolved=resolve_turn(MATRIX, session_turn_tuning(TUNING))["tool"],
            dispatch=dispatch,
            clock=clock,
        )
    )


class TestFormulateAndInvoke:
    def test_happy_path_returns_verbatim_output_and_attribution(self) -> None:
        provider = FakeProvider(_tool_call_stream({"account_id": "acc-1"}))
        response = {"body": {"account_id": "acc-1", "name": "Acme"}}
        dispatch = RecordingDispatch(response)

        result = _invoke(provider=provider, dispatch=dispatch)

        assert result.output is response  # verbatim, same object, not reconstructed
        assert dispatch.calls[0][1] == {"account_id": "acc-1"}  # the validated args, verbatim
        assert isinstance(result.invocation, InvocationAttribution)
        assert result.invocation.tier == TOOL_TIER
        assert result.invocation.role == ROLE
        assert (
            result.invocation.model_id == MATRIX.model_for(MATRIX.default_family, "tool").model_id
        )
        assert result.invocation.tokens.input_tokens == 50
        assert result.invocation.tokens.output_tokens == 8
        assert result.invocation.cache.read_tokens == 2
        assert result.invocation.latency_ms is None  # usage event carried no latency -> unmeasured
        assert result.dispatch_latency_ms is None
        assert classify_tool_result(result.output).kind == "data"

    def test_makes_exactly_one_model_call_and_never_resummarizes(self) -> None:
        provider = FakeProvider(_tool_call_stream({"account_id": "acc-1"}))
        dispatch = RecordingDispatch({"body": {"account_id": "acc-1"}})

        _invoke(provider=provider, dispatch=dispatch)

        # One provider call (to formulate args) and exactly one dispatch; the result is never fed
        # back to the model.
        assert len(provider.calls) == 1
        assert len(dispatch.calls) == 1
        _invocation, _messages, tools = provider.calls[0]
        assert tools == [
            {
                "name": "get_account",
                "description": _tool().description,
                "input_schema": _tool().input_schema,
            }
        ]

    def test_resolves_the_tool_tier_model(self) -> None:
        provider = FakeProvider(_tool_call_stream({"account_id": "acc-1"}))
        result = _invoke(provider=provider, dispatch=RecordingDispatch())
        expected = MATRIX.model_for(MATRIX.default_family, "tool")
        assert result.invocation.model_id == expected.model_id
        assert result.invocation.tier == "tool"

    def test_schema_invalid_formulation_is_dispatched_and_the_handler_rejects_it(self) -> None:
        # {} is missing the required account_id; the actor dispatches it and HandlerDispatch
        # returns a validation_failed envelope.
        provider = FakeProvider(_tool_call_stream({}))
        dispatch = HandlerDispatch()

        result = _invoke(provider=provider, dispatch=dispatch)

        assert len(dispatch.calls) == 1  # dispatched; the handler is the trust boundary
        assert result.output["error"]["code"] == "validation_failed"
        assert classify_tool_result(result.output).kind == "tool_failure"
        # The model call still happened, so its attribution is still recorded.
        assert result.invocation.tier == "tool"
        assert result.invocation.tokens.input_tokens == 50

    def test_no_tool_call_is_a_typed_failure(self) -> None:
        provider = FakeProvider(
            [
                ProviderEvent(type="text_delta", text="I will not call a tool"),
                ProviderEvent(type="usage", input_tokens=10, output_tokens=2),
                ProviderEvent(type="completed", input_tokens=10, output_tokens=2),
            ]
        )
        dispatch = RecordingDispatch()

        result = _invoke(provider=provider, dispatch=dispatch)

        assert dispatch.calls == []
        assert result.output["error"]["code"] == "tool_actor_error"
        assert classify_tool_result(result.output).kind == "tool_failure"

    def test_model_error_round_is_a_typed_failure(self) -> None:
        provider = FakeProvider(
            [ProviderEvent(type="error", error_message="upstream model failure")]
        )
        dispatch = RecordingDispatch()

        result = _invoke(provider=provider, dispatch=dispatch)

        assert dispatch.calls == []
        assert result.output["error"]["code"] == "tool_actor_error"
        assert classify_tool_result(result.output).kind == "tool_failure"

    def test_dispatch_latency_is_measured_only_when_a_clock_is_supplied(self) -> None:
        # The clock times only the dispatch; model-call latency comes from the usage event.
        provider = FakeProvider(_tool_call_stream({"account_id": "acc-1"}))
        clock = iter([0.0, 1.0]).__next__  # dispatch span 0->1s
        result = _invoke(provider=provider, dispatch=RecordingDispatch(), clock=clock)
        assert result.dispatch_latency_ms == 1000.0
        # The usage event carried no latency, so the model-call attribution stays unmeasured.
        assert result.invocation.latency_ms is None

    def test_model_call_latency_comes_from_the_usage_event_not_the_clock(self) -> None:
        # A latency on the usage event is copied onto the invocation attribution.
        events = _tool_call_stream({"account_id": "acc-1"})
        events[1] = replace(events[1], latency_ms=1234.5)  # the usage event
        provider = FakeProvider(events)
        result = _invoke(provider=provider, dispatch=RecordingDispatch(), clock=None)
        assert result.invocation.latency_ms == 1234.5
        assert result.dispatch_latency_ms is None  # no clock -> dispatch unmeasured


class TestSB3IdentityHandling:
    def test_production_dispatch_strips_identity_keys_and_appends_the_trusted_block(self) -> None:
        # The dispatch this actor is wired to in production (agent_identity_event ->
        # crm.envelope.identity_event) strips every identity channel from the model's arguments and
        # writes the trusted block last, so a spoofed identity can never win.
        identity = Identity(rep_id="rep-1", book=frozenset({"acc-1"}), tenant="demo")
        spoofed_args: dict[str, object] = {
            "account_id": "acc-1",
            AGENT_IDENTITY_KEY: {
                "claims": {CLAIM_REP_ID: "attacker", CLAIM_TENANT: "other"},
                "principal_type": "user",
            },
        }

        event = agent_identity_event(identity, spoofed_args)

        trusted = event[AGENT_IDENTITY_KEY]
        assert trusted["claims"][CLAIM_REP_ID] == "rep-1"  # the real identity, not "attacker"
        assert trusted["claims"][CLAIM_TENANT] == "demo"
        assert event["account_id"] == "acc-1"  # the non-identity arg survives as a sibling

    def test_a_service_identity_keeps_its_principal_type_across_the_agent_hop(self) -> None:
        identity = Identity(
            rep_id="svc-1", book=frozenset({"acc-1"}), principal_type="service", tenant="demo"
        )

        event = agent_identity_event(identity, {"account_id": "acc-1"})

        assert event[AGENT_IDENTITY_KEY][AGENT_PRINCIPAL_TYPE_KEY] == "service"
        decoded = decode_identity(event)
        assert decoded.principal_type == "service"
        assert decoded.source == "agent"

    def test_a_user_identity_round_trips_unchanged_across_the_agent_hop(self) -> None:
        identity = Identity(rep_id="rep-1", book=frozenset({"acc-1", "acc-2"}), tenant="demo")

        decoded = decode_identity(agent_identity_event(identity, {}))

        assert decoded.principal_type == "user"
        assert (decoded.rep_id, decoded.book, decoded.tenant) == ("rep-1", identity.book, "demo")


class TestSB4OutOfScopeDenied:
    def test_out_of_scope_account_id_returns_verbatim_and_classifies_denied(self) -> None:
        # An out-of-scope account_id yields a scope_denied envelope, returned verbatim.
        denied = {
            "error": {
                "code": "scope_denied",
                "missing_scope": "account:acc-not-mine",
                "message": "That account is outside your book.",
            }
        }
        provider = FakeProvider(_tool_call_stream({"account_id": "acc-not-mine"}))
        dispatch = RecordingDispatch(denied)

        result = _invoke(provider=provider, dispatch=dispatch)

        assert result.output is denied  # verbatim, never summarized
        outcome = classify_tool_result(result.output)
        assert outcome.kind == "denied"
        assert outcome.missing_scope == "account:acc-not-mine"


#: The tool-tier directive.
_DESIGN_DIRECTIVE = (
    "Call the provided tool once. Below is either draft arguments as JSON or a plain request. "
    "Use draft arguments as given wherever they fit the tool's schema, and fill in only what is "
    "missing. Reply with the tool call only, no text."
)

#: The tool-tier directive it replaced.
_PRE_AP5_DIRECTIVE = (
    "Formulate the arguments for a single call to the provided tool that accomplishes the "
    "request below, and respond with that tool call only — do not answer in prose."
)


class TestActorDirective:
    def test_the_directive_is_the_design_text(self) -> None:
        assert tool_actor._ACTOR_DIRECTIVE == _DESIGN_DIRECTIVE

    def test_the_tool_tier_receives_the_directive_then_the_input_as_one_user_message(self) -> None:
        draft = '{"account_id": "acc-1"}'
        provider = FakeProvider(_tool_call_stream({"account_id": "acc-1"}))
        _invoke(provider=provider, dispatch=RecordingDispatch(), intent=draft)
        _invocation, messages, _tools = provider.calls[0]
        assert messages == [{"role": "user", "content": f"{_DESIGN_DIRECTIVE}\n\n{draft}"}]

    def test_no_tool_choice_forcing_is_sent(self) -> None:
        provider = FakeProvider(_tool_call_stream({"account_id": "acc-1"}))
        _invoke(provider=provider, dispatch=RecordingDispatch())
        invocation, messages, tools = provider.calls[0]
        rendered = repr((invocation, messages, tools))
        assert "tool_choice" not in rendered

    def test_the_bundle_digest_moved_with_the_rewrite(self, monkeypatch: Any) -> None:
        current = prompt_bundle.prompt_bundle_sha256()
        monkeypatch.setattr(tool_actor, "_ACTOR_DIRECTIVE", _PRE_AP5_DIRECTIVE)
        assert prompt_bundle.prompt_bundle_sha256() != current

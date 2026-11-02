# SPDX-License-Identifier: MIT
"""A two-round OpenAI turn feeds tool results back with a matching `call_id`.

Runs the real `OpenAIAdapter` and `runtime_server._agent_events_for_turn` loop; only the HTTP
transport (`httpx2.MockTransport` replaying Responses SSE bodies) and action dispatch are doubled.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import pytest
from botocore.credentials import Credentials

import agents.runtime_server as runtime_server
from agents.events import AgentEvent
from agents.runtime_entrypoint import RuntimeInvocation, decode_invocation
from providers.capabilities import load_capability_matrix
from providers.openai_adapter import OpenAIAdapter
from providers.tier_tuning import WIRE_SCHEMA_VERSION

_CALL_ID = "call_abc123"

_APIGW_PAYLOAD: dict[str, Any] = {
    "requestContext": {
        "requestId": "req-openai-1",
        "authorizer": {"jwt": {"claims": {"custom:rep_id": "rep-1", "custom:book": "acc-1"}}},
    },
    "prompt": "Tell me about Acme.",
    "session_id": "sess-openai-multiround",
    "tuning": {
        "schema": WIRE_SCHEMA_VERSION,
        "tiers": {tier: {"family": "openai"} for tier in ("supervisor", "subagent", "tool")},
    },
}


def _sse(events: list[dict[str, Any]]) -> bytes:
    """Serialise Responses API events as the `text/event-stream` body Bedrock returns."""
    frames = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return (frames + "data: [DONE]\n\n").encode("utf-8")


def _completed(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "sequence_number": 9,
        "response": {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "output": [],
            "usage": usage,
        },
    }


class _TwoRoundResponses:
    """A transport handler: round one emits a tool call, round two the final answer.

    Records each request body so a test can assert what the adapter sent.
    """

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        if len(self.bodies) == 1:
            events = [
                {
                    "type": "response.output_item.done",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": _CALL_ID,
                        "name": "get_account",
                        "arguments": json.dumps({"account_id": "acc-1"}),
                        "status": "completed",
                    },
                },
                _completed({"input_tokens": 10, "output_tokens": 0}),
            ]
        else:
            events = [
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "Acme is in your book.",
                },
                _completed({"input_tokens": 0, "output_tokens": 7}),
            ]
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=_sse(events)
        )


class _StaticSession:
    def get_credentials(self) -> Credentials:
        return Credentials("test", "test")


def _openai_invocation() -> RuntimeInvocation:
    """Decode an OpenAI-family invocation the same way `/invocations` does."""
    return decode_invocation(_APIGW_PAYLOAD, load_capability_matrix())


def _run_turn(invocation: RuntimeInvocation, provider: OpenAIAdapter) -> list[AgentEvent]:
    async def _collect() -> list[AgentEvent]:
        agen = runtime_server._agent_events_for_turn(
            invocation, provider, matrix=load_capability_matrix()
        )
        events: list[AgentEvent] = []
        try:
            async for event in agen:
                events.append(event)
        finally:
            await agen.aclose()
        return events

    return asyncio.run(_collect())


def test_openai_turn_sends_a_second_round_carrying_the_tool_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round two's request body carries a `function_call` and matching `function_call_output`."""
    invocation = _openai_invocation()
    assert invocation.resolved_invocation.model.family == "openai"

    def _fake_dispatch(tool: Any, tool_input: dict[str, object]) -> dict[str, Any]:
        del tool, tool_input
        return {"statusCode": 200, "body": {"account": {"account_id": "acc-1", "name": "Acme"}}}

    # `production_dispatch(identity)` returns the turn's `(tool, tool_input) -> dict` dispatch.
    monkeypatch.setattr(runtime_server, "production_dispatch", lambda _identity: _fake_dispatch)

    for name in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_BEDROCK_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    responses = _TwoRoundResponses()
    provider = OpenAIAdapter(
        region_name="us-east-1", session=_StaticSession(), transport=httpx2.MockTransport(responses)
    )
    events = _run_turn(invocation, provider)

    assert len(responses.bodies) == 2, "the tool result was never fed back for a second round"

    round_two_input = responses.bodies[1]["input"]
    function_calls = [item for item in round_two_input if item.get("type") == "function_call"]
    function_outputs = [
        item for item in round_two_input if item.get("type") == "function_call_output"
    ]
    assert len(function_calls) == 1, round_two_input
    assert len(function_outputs) == 1, round_two_input
    assert function_calls[0]["call_id"] == _CALL_ID
    assert function_outputs[0]["call_id"] == _CALL_ID
    assert function_calls[0]["name"] == "get_account"

    kinds = [event.kind for event in events]
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    completed = events[-1]
    assert completed.kind == "completed"
    assert completed.text == "Acme is in your book."

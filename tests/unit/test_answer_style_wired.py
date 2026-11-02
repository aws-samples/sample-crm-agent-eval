# SPDX-License-Identifier: MIT
"""Tests that `drive_turn` self-loads the answer-style system message, right after the supervisor
system message.

The `answer_style_message` parameter is a test override that substitutes it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from agents.answer_style import answer_style_system_message
from agents.budget import EVAL_BUDGET
from agents.events import AgentEvent
from agents.registry import RegisteredTool
from agents.turn_driver import (
    TurnRecords,
    drive_turn,
    session_turn_tuning,
    supervisor_system_message,
)
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, Tuning
from providers.capabilities import load_capability_matrix

_MATRIX = load_capability_matrix()


def _noop_dispatch(_tool: RegisteredTool, _tool_input: dict[str, object]) -> dict[str, object]:
    """A dispatch that is never called by a text-only turn (no tool calls)."""
    return {"statusCode": 200, "body": {}}


class _RecordingProvider(ModelProvider):
    """Captures the `messages` list it is asked to stream, then answers with text (no tool call)."""

    def __init__(self) -> None:
        self.seen_messages: list[dict[str, object]] | None = None

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        if self.seen_messages is None:  # capture only the first round
            self.seen_messages = list(messages)
        yield ProviderEvent(type="text_delta", text="ok")
        yield ProviderEvent(type="usage", input_tokens=1, output_tokens=1)
        yield ProviderEvent(type="completed", text="ok", input_tokens=1, output_tokens=1)


def _drive(
    provider: ModelProvider, *, answer_style_message: dict[str, object] | None = None
) -> None:
    async def _run() -> None:
        async for _ in drive_turn(
            prompt="hello",
            session_id="s1",
            providers=lambda _family: provider,
            matrix=_MATRIX,
            tuning=session_turn_tuning(Tuning(source="test")),
            budget=EVAL_BUDGET,
            dispatch=_noop_dispatch,
            records=TurnRecords(),
            answer_style_message=answer_style_message,
        ):
            pass

    asyncio.run(_run())


def test_drive_turn_self_loads_the_answer_style_message_second_when_no_param_is_passed() -> None:
    """With no override, drive_turn sends the answer-style system message as messages[1], after
    the supervisor system message."""
    provider = _RecordingProvider()
    _drive(provider)

    assert provider.seen_messages is not None
    assert provider.seen_messages[0] == supervisor_system_message()
    assert provider.seen_messages[1] == answer_style_system_message()
    assert provider.seen_messages[1]["role"] == "system"


def test_the_answer_style_message_precedes_the_user_prompt() -> None:
    provider = _RecordingProvider()
    _drive(provider)

    assert provider.seen_messages is not None
    roles = [m["role"] for m in provider.seen_messages]
    # supervisor then answer-style system messages; the user prompt is last (no page-context/history)
    assert roles[:2] == ["system", "system"]
    assert provider.seen_messages[-1] == {"role": "user", "content": "hello"}


def test_the_param_is_a_test_override_that_substitutes_the_self_loaded_message() -> None:
    override: dict[str, object] = {"role": "system", "content": "OVERRIDE STYLE"}
    provider = _RecordingProvider()
    _drive(provider, answer_style_message=override)

    assert provider.seen_messages is not None
    assert provider.seen_messages[1] == override
    assert provider.seen_messages[1] != answer_style_system_message()


def test_the_self_loaded_message_is_the_shipped_answer_style_prompt() -> None:
    provider = _RecordingProvider()
    _drive(provider)

    assert provider.seen_messages is not None
    assert provider.seen_messages[1]["content"] == answer_style_system_message()["content"]


def test_a_text_only_turn_still_completes_with_the_answer_style_message_prepended() -> None:
    provider = _RecordingProvider()
    events: list[AgentEvent] = []

    async def _run() -> None:
        async for event in drive_turn(
            prompt="hello",
            session_id="s1",
            providers=lambda _family: provider,
            matrix=_MATRIX,
            tuning=session_turn_tuning(Tuning(source="test")),
            budget=EVAL_BUDGET,
            dispatch=_noop_dispatch,
            records=TurnRecords(),
        ):
            events.append(event)

    asyncio.run(_run())
    kinds = [e.kind for e in events]
    assert kinds[-1] == "completed"
    assert sum(1 for k in kinds if k == "completed") == 1

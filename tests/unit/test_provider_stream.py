# SPDX-License-Identifier: MIT
"""The real StubProvider drives to completion through drive_turn, in two rounds on the tool branch.

On the tool branch round 1 calls a tool with `awaiting_tools=True` and round 2 answers from the tool
result. Two runs of a prompt produce identical events.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from agents.budget import EVAL_BUDGET
from agents.events import AgentEvent
from agents.registry import RegisteredTool
from agents.turn_driver import TurnRecords, drive_turn, session_turn_tuning
from providers.base import (
    ModelProvider,
    ProviderEvent,
    ResolvedInvocation,
    Tuning,
    resolve_session_model,
    validate_tuning,
)
from providers.capabilities import load_capability_matrix
from providers.stub import StubProvider

MATRIX = load_capability_matrix()
TUNING = Tuning(source="test.provider_stream", family="anthropic", effort=None)

# Distinct prompts; the stub's digest sends some down the tool branch and some down the text branch.
PROMPTS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]


def _stub_dispatch(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
    return {"statusCode": 200, "body": {"items": [], "tool": tool.name}}


def _drive(prompt: str) -> list[AgentEvent]:
    records = TurnRecords()
    stub = StubProvider()

    async def _run() -> list[AgentEvent]:
        return [
            event
            async for event in drive_turn(
                prompt=prompt,
                session_id=f"sess-{prompt}",
                providers=lambda _family: stub,
                matrix=MATRIX,
                tuning=session_turn_tuning(TUNING),
                budget=EVAL_BUDGET,
                dispatch=_stub_dispatch,
                records=records,
            )
        ]

    return asyncio.run(_run())


def _fingerprint(events: list[AgentEvent]) -> list[tuple[Any, ...]]:
    """A comparable, order-preserving fingerprint of an event stream for determinism checks."""
    return [
        (
            event.kind,
            event.text,
            event.tool_name,
            None if event.tool_input is None else tuple(sorted(event.tool_input)),
            event.input_tokens,
            event.output_tokens,
        )
        for event in events
    ]


def test_every_prompt_completes_through_drive_turn_with_the_real_stub() -> None:
    for prompt in PROMPTS:
        events = _drive(prompt)
        terminal = [event for event in events if event.kind in ("completed", "error")]
        assert len(terminal) == 1, f"{prompt!r} did not end in exactly one terminal event"
        assert terminal[0].kind == "completed", (
            f"{prompt!r} ended in {terminal[0].kind} (error_kind={terminal[0].error_kind}) — "
            "the stub's tool branch should complete, not crash"
        )
        assert terminal[0].text is not None


def test_at_least_one_prompt_exercises_the_two_round_tool_branch() -> None:
    drove_a_tool_turn = False
    for prompt in PROMPTS:
        events = _drive(prompt)
        if any(event.kind == "tool_call" for event in events):
            drove_a_tool_turn = True
            # A tool turn dispatched the real tool (the stub emits no self tool_result).
            assert any(event.kind == "tool_result" for event in events)
            break
    assert drove_a_tool_turn, "no prompt took the stub tool branch across the sample"


def test_two_runs_of_each_prompt_produce_identical_events() -> None:
    for prompt in PROMPTS:
        assert _fingerprint(_drive(prompt)) == _fingerprint(_drive(prompt)), (
            f"{prompt!r} was not reproducible run to run"
        )


def _stream_seam(prompt: str, tools: list[dict[str, object]]) -> list[ProviderEvent]:
    """Call `StubProvider.stream` directly (below drive_turn) and collect its raw ProviderEvents."""
    provider = StubProvider()
    invocation = validate_tuning(resolve_session_model(MATRIX, "supervisor", TUNING), TUNING)
    messages: list[dict[str, object]] = [{"role": "user", "content": prompt}]

    async def _run() -> list[ProviderEvent]:
        return [event async for event in provider.stream(invocation, messages, tools)]

    return asyncio.run(_run())


class TestProviderSeamEmitsTextUnfolded:
    """The stub emits reply text as `text_delta` events and a text-free `completed`."""

    def test_text_branch_emits_text_delta_and_a_textless_completed(self) -> None:
        # A text-branch prompt (no tools available, so the tool branch cannot fire).
        events = _stream_seam("alpha", tools=[])
        kinds = [event.type for event in events]
        assert "text_delta" in kinds, "the stub must emit its reply as an unfolded text_delta"
        text_deltas = [e for e in events if e.type == "text_delta"]
        assert all(e.text for e in text_deltas), "each text_delta carries its incremental text"
        completed = [e for e in events if e.type == "completed"]
        assert len(completed) == 1
        assert completed[0].text is None
        assert completed[0].awaiting_tools is False

    def test_the_seam_is_deterministic_run_to_run(self) -> None:
        def _fp(events: list[ProviderEvent]) -> list[tuple[Any, ...]]:
            return [
                (e.type, e.text, e.awaiting_tools, e.input_tokens, e.output_tokens) for e in events
            ]

        assert _fp(_stream_seam("alpha", tools=[])) == _fp(_stream_seam("alpha", tools=[]))


@dataclass
class _AnswerProvider(ModelProvider):
    """Answers with text streamed as one `text_delta` per chunk, then a usage + completed round.

    Frame granularity (how many chunks the same answer is split into) is the only thing that varies
    between instances — used to prove the streaming frames don't change eval artifacts.
    """

    chunks: list[str] = field(default_factory=list)

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        return self._emit()

    async def _emit(self) -> AsyncIterator[ProviderEvent]:
        for chunk in self.chunks:
            yield ProviderEvent(type="text_delta", text=chunk)
        yield ProviderEvent(type="usage", input_tokens=10, output_tokens=5)
        yield ProviderEvent(type="completed", input_tokens=10, output_tokens=5)


def _drive_provider(provider: ModelProvider) -> tuple[list[AgentEvent], TurnRecords]:
    records = TurnRecords()

    async def _run() -> list[AgentEvent]:
        return [
            event
            async for event in drive_turn(
                prompt="hi",
                session_id="sess-frames",
                providers=lambda _family: provider,
                matrix=MATRIX,
                tuning=session_turn_tuning(TUNING),
                budget=EVAL_BUDGET,
                dispatch=_stub_dispatch,
                records=records,
            )
        ]

    return asyncio.run(_run()), records


def test_drive_turn_emits_text_delta_frames_and_a_completed_carrying_the_full_text() -> None:
    # One partial text_delta frame per provider delta under one event_id; completed carries the
    # full text.
    events, _records = _drive_provider(_AnswerProvider(["Hel", "lo ", "world"]))
    frames = [event for event in events if event.kind == "text_delta"]
    assert [frame.text for frame in frames] == ["Hel", "lo ", "world"]
    assert all(frame.partial is True for frame in frames)
    assert len({frame.event_id for frame in frames}) == 1, "one answer groups under one event_id"
    assert frames[0].event_id is not None
    completed = [event for event in events if event.kind == "completed"]
    assert len(completed) == 1
    assert completed[0].text == "Hello world"


def test_harness_ignores_text_delta_frames_so_eval_artifacts_are_byte_identical() -> None:
    # The same answer streamed at different frame granularities yields identical outcomes and
    # records.
    from crm.store.store import Store
    from eval.harness import _run_provider_turn

    # _run_provider_turn returns (TurnOutcome, TurnRecords, provider_call_count); only the first two
    # are compared.
    def _run(chunks: list[str]) -> tuple[object, TurnRecords]:
        outcome, records, _call_count = _run_provider_turn(
            _AnswerProvider(chunks),
            matrix=MATRIX,
            tuning=TUNING,
            prompt="hi",
            store=Store(client=object()),
            tenant="demo",
            identity_payload={},
            task_id="t",
            dispatch=_stub_dispatch,
        )
        return outcome, records

    outcome_multi, records_multi = _run(["Hel", "lo ", "world"])
    outcome_single, records_single = _run(["Hello world"])
    assert outcome_multi == outcome_single
    assert records_multi.invocations == records_single.invocations
    assert records_multi.tool_calls == records_single.tool_calls

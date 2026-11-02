# SPDX-License-Identifier: MIT
"""Driver parity: the runtime and eval adapters drive an identical model-call sequence.

The runtime path (`runtime_server._agent_events_for_turn`) and the eval path
(`eval.harness._run_provider_turn`) must each reproduce the sequence the turn driver produces.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from agents.budget import EVAL_BUDGET
from agents.registry import RegisteredTool
from agents.turn_driver import TurnRecords, drive_turn, session_turn_tuning
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, Tuning
from providers.capabilities import load_capability_matrix

MATRIX = load_capability_matrix()
TUNING = Tuning(source="test.driver_parity", family="anthropic", effort=None)
SUPERVISOR_MODEL = MATRIX.model_for("anthropic", "supervisor").model_id
SUBAGENT_MODEL = MATRIX.model_for("anthropic", "subagent").model_id
TOOL_MODEL = MATRIX.model_for("anthropic", "tool").model_id

#: The canonical model-call sequence for the scripted turn below: the supervisor delegates to the
#: research subagent (round 1), the subagent calls its get_account leaf (subagent tier), the tool
#: actor formulates the args (tool tier), then the supervisor answers (round 2).
CANONICAL_SEQUENCE = [SUPERVISOR_MODEL, SUBAGENT_MODEL, TOOL_MODEL, SUPERVISOR_MODEL]


@dataclass
class _RecordingProvider(ModelProvider):
    """A scripted provider that records the resolved model id of every `stream` call, in order."""

    rounds: list[list[ProviderEvent]]
    model_seq: list[str] = field(default_factory=list)

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.model_seq.append(invocation.model.model_id)
        assert self.rounds, "provider.stream called more times than the test scripted"
        return self._emit(self.rounds.pop(0))

    async def _emit(self, events: list[ProviderEvent]) -> AsyncIterator[ProviderEvent]:
        for event in events:
            yield event


def _stub_dispatch(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
    return {"statusCode": 200, "body": {"account_id": "acc-1", "name": "Acme"}}


def _canonical_rounds() -> list[list[ProviderEvent]]:
    return [
        [  # supervisor round 1: delegate to research
            ProviderEvent(
                type="tool_call", tool_name="research", tool_input={"intent": "acme"}, tool_id="t1"
            ),
            ProviderEvent(type="usage", input_tokens=100, output_tokens=20),
            ProviderEvent(type="completed", awaiting_tools=True),
        ],
        [  # subagent round: research calls get_account
            ProviderEvent(
                type="tool_call",
                tool_name="get_account",
                tool_input={"account_id": "acc-1"},
                tool_id="s1",
            ),
            ProviderEvent(type="usage", input_tokens=40, output_tokens=10),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
        [  # tool round: the actor formulates get_account's args
            ProviderEvent(
                type="tool_call", tool_name="get_account", tool_input={"account_id": "acc-1"}
            ),
            ProviderEvent(type="usage", input_tokens=8, output_tokens=4),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
        [  # supervisor round 2: final answer
            ProviderEvent(type="text_delta", text="Here is Acme."),
            ProviderEvent(type="usage", input_tokens=50, output_tokens=15),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
    ]


def _drive_sequence() -> tuple[list[str], TurnRecords]:
    """Drive the scripted turn through `drive_turn` and return its model-call sequence + records."""
    provider = _RecordingProvider(_canonical_rounds())
    records = TurnRecords()

    async def _run() -> None:
        async for _event in drive_turn(
            prompt="find acme",
            session_id="sess-1",
            providers=lambda _family: provider,
            matrix=MATRIX,
            tuning=session_turn_tuning(TUNING),
            budget=EVAL_BUDGET,
            dispatch=_stub_dispatch,
            records=records,
        ):
            pass

    asyncio.run(_run())
    return provider.model_seq, records


def _drive_via_runtime(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Drive the scripted turn through the runtime adapter and return its model-call sequence.

    `invoke_action` is patched to return the same fixed payload as `_stub_dispatch`.
    """
    from agents import runtime_server, tool_dispatch
    from agents.runtime_entrypoint import RuntimeInvocation
    from crm.envelope import Identity
    from providers.tier_tuning import resolve_turn

    monkeypatch.setattr(
        tool_dispatch,
        "invoke_action",
        lambda _fn, _event: {"statusCode": 200, "body": {"account_id": "acc-1", "name": "Acme"}},
    )

    identity = Identity(
        rep_id="rep-1",
        book=frozenset({"acc-1"}),
        full_visibility=False,
        principal_type="user",
        session_id="sess-1",
        source="apigw",
        tenant="demo",
    )
    turn_tuning = session_turn_tuning(TUNING)
    invocation = RuntimeInvocation(
        identity=identity,
        session_id="sess-1",
        prompt="find acme",
        tuning=turn_tuning,
        resolved_tiers=resolve_turn(MATRIX, turn_tuning),
    )
    provider = _RecordingProvider(_canonical_rounds())

    async def _run() -> None:
        async for _event in runtime_server._agent_events_for_turn(
            invocation, provider, matrix=MATRIX
        ):
            pass

    asyncio.run(_run())
    return provider.model_seq


def _drive_via_harness() -> list[str]:
    """Drive the scripted turn through the eval harness adapter and return its model sequence.

    `dispatch=_stub_dispatch` replaces the in-process dispatch; the sentinel `Store` is unused.
    """
    from crm.store.store import Store
    from eval.harness import _run_provider_turn

    provider = _RecordingProvider(_canonical_rounds())
    _run_provider_turn(
        provider,
        matrix=MATRIX,
        tuning=TUNING,
        prompt="find acme",
        store=Store(client=object()),
        tenant="demo",
        identity_payload={},
        task_id="sess-1",
        dispatch=_stub_dispatch,
    )
    return provider.model_seq


class TestDriverParityReference:
    """The reference sequence both adapters must reproduce, asserted against the driver directly."""

    def test_scripted_turn_drives_the_three_tier_sequence(self) -> None:
        model_seq, records = _drive_sequence()
        assert model_seq == CANONICAL_SEQUENCE
        # A tool-tier record is appended before its supervisor round's usage, so order is not fixed.
        assert Counter((inv.tier, inv.model_id) for inv in records.invocations) == Counter(
            [
                ("supervisor", SUPERVISOR_MODEL),
                ("supervisor", SUPERVISOR_MODEL),
                ("subagent", SUBAGENT_MODEL),
                ("tool", TOOL_MODEL),
            ]
        )
        assert [(c.tool_name, c.role) for c in records.tool_calls] == [("get_account", "research")]

    def test_the_sequence_is_reproducible(self) -> None:
        first, _ = _drive_sequence()
        second, _ = _drive_sequence()
        assert first == second == CANONICAL_SEQUENCE


class TestAdapterParity:
    """Each adapter drives the same sequence as the driver."""

    def test_runtime_adapter_drives_the_same_sequence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert _drive_via_runtime(monkeypatch) == CANONICAL_SEQUENCE

    def test_harness_adapter_drives_the_same_sequence(self) -> None:
        assert _drive_via_harness() == CANONICAL_SEQUENCE

# SPDX-License-Identifier: MIT
"""A mixed-family turn on the real :class:`providers.stub.StubProvider`.

No model call and no credential: the supervisor runs its own family's model, every
``usage``/``completed`` frame carries cost lines and the three resolved tiers, the wire frames are
strict JSON, and the stream is deterministic run to run. Subordinate-tier routing is covered by
``tests/unit/test_turn_driver_tiers.py``; the stub never delegates.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agents.budget import CHAT_BUDGET
from agents.events import AgentEvent
from agents.registry import RegisteredTool
from agents.runtime_entrypoint import format_sse_line
from agents.turn_costing import is_cost_withheld
from agents.turn_driver import TurnRecords, drive_turn
from providers.base import ModelProvider
from providers.capabilities import Family, load_capability_matrix
from providers.stub import StubProvider
from providers.tier_tuning import TierRequest, TurnTuning

MATRIX = load_capability_matrix()

OPENAI_SUBAGENT = MATRIX.model_for("openai", "subagent").model_id
ANTHROPIC_TOOL = MATRIX.model_for("anthropic", "tool").model_id

MIXED = TurnTuning(
    source="test.stub.mixed",
    tiers={
        "supervisor": TierRequest(family="openai", effort="low"),
        "subagent": TierRequest(family="openai", model=OPENAI_SUBAGENT),
        "tool": TierRequest(family="anthropic", model=ANTHROPIC_TOOL, effort="high"),
    },
)


def _dispatch(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
    del tool_input
    return {"statusCode": 200, "body": {"items": [], "tool": tool.name}}


def _turn(prompt: str) -> tuple[list[AgentEvent], TurnRecords, list[Family]]:
    stub = StubProvider()
    asked: list[Family] = []

    def providers(family: Family) -> ModelProvider:
        asked.append(family)
        return stub

    records = TurnRecords()

    async def _run() -> list[AgentEvent]:
        return [
            event
            async for event in drive_turn(
                prompt=prompt,
                session_id="sess-mixed-stub",
                providers=providers,
                matrix=MATRIX,
                tuning=MIXED,
                budget=CHAT_BUDGET,
                dispatch=_dispatch,
                records=records,
                page_context=None,
                history_messages=None,
            )
        ]

    return asyncio.run(_run()), records, asked


def test_the_supervisor_runs_its_own_family_and_every_tier_is_reported() -> None:
    """The supervisor runs the OpenAI model; ``resolved_tiers`` reports all three tiers as tuned."""
    events, records, asked = _turn("which accounts slipped?")
    supervisor_model = MATRIX.model_for("openai", "supervisor").model_id
    assert asked[0] == "openai"
    assert {inv.model_id for inv in records.invocations if inv.tier == "supervisor"} == {
        supervisor_model
    }
    completed = [e for e in events if e.kind == "completed"]
    assert len(completed) == 1
    resolved = completed[0].resolved_tiers or ()
    assert [(t["tier"], t["family"], t["model_id"]) for t in resolved] == [
        ("supervisor", "openai", supervisor_model),
        ("subagent", "openai", OPENAI_SUBAGENT),
        ("tool", "anthropic", ANTHROPIC_TOOL),
    ]
    assert [t["effort"] for t in resolved] == [
        "low",
        MATRIX.tiers["openai"]["subagent"].tuning["effort"],
        "high",
    ]
    assert all("temperature" not in t for t in resolved)
    lines = completed[0].cost_lines or ()
    assert {(line["tier"], line["model_id"]) for line in lines} >= {
        ("supervisor", supervisor_model)
    }
    assert completed[0].cost_withheld is is_cost_withheld()
    if not is_cost_withheld():
        assert completed[0].cost_usd is not None and completed[0].cost_usd > 0


def test_accounting_frames_are_strict_json_and_deterministic() -> None:
    """Two identical turns render byte-identical SSE frames, each parseable as strict JSON."""
    first = [format_sse_line(e) for e in _turn("which accounts slipped?")[0]]
    second = [format_sse_line(e) for e in _turn("which accounts slipped?")[0]]
    assert first == second
    assert any('"cost_lines": [' in frame for frame in first)
    for frame in first:
        json.loads(frame.removeprefix("data: "), parse_constant=_reject_constant)


def _reject_constant(token: str) -> object:
    raise ValueError(f"non-standard JSON constant {token}")

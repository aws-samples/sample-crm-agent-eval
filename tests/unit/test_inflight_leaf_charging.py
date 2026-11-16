# SPDX-License-Identifier: MIT
"""Leaf tool calls are charged to the turn budget as they are dispatched.

:meth:`agents.budget.TurnBudget.charge_tool_call` charges a call or refuses it at the cap; the
subagent runner charges each leaf before dispatching it, so a delegation cannot run past the turn's
tool-call cap, and the turn driver does not charge the same leaves again afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from agents.budget import RequestRefusedError, TurnBudget, TurnBudgetSpec
from agents.events import AgentEvent
from agents.registry import RegisteredTool
from agents.subagents import SUBAGENT_SPECS_BY_ROLE
from providers.base import ProviderEvent
from tests.unit.test_budget_exhaustion_tier import _answer_round, _drive, _Round

_LEAF_TOOL = SUBAGENT_SPECS_BY_ROLE["research"].tools[0].name


@dataclass
class _CountingDispatch:
    """Counts every dispatched tool call and returns one small account record."""

    names: list[str] = field(default_factory=list)

    def __call__(self, tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        del tool_input
        self.names.append(tool.name)
        return {"statusCode": 200, "body": {"account_id": "acc-1", "name": "Acme"}}


def _supervisor_delegates() -> _Round:
    return _Round(
        [
            ProviderEvent(
                type="tool_call", tool_name="research", tool_input={"intent": "acme"}, tool_id="d1"
            ),
            ProviderEvent(type="usage", input_tokens=10, output_tokens=2),
            ProviderEvent(type="completed", awaiting_tools=True),
        ]
    )


def _subagent_calls(count: int) -> _Round:
    calls = [
        ProviderEvent(
            type="tool_call",
            tool_name=_LEAF_TOOL,
            tool_input={"account_id": f"acc-{n}"},
            tool_id=f"s{n}",
        )
        for n in range(count)
    ]
    return _Round(
        [
            *calls,
            ProviderEvent(type="usage", input_tokens=10, output_tokens=2),
            ProviderEvent(type="completed", awaiting_tools=False),
        ]
    )


def _tool_tier_round() -> _Round:
    return _Round(
        [
            ProviderEvent(type="tool_call", tool_name=_LEAF_TOOL, tool_input={"account_id": "x"}),
            ProviderEvent(type="usage", input_tokens=2, output_tokens=1),
            ProviderEvent(type="completed", awaiting_tools=False),
        ]
    )


def _delegation_of(leaves: int, *, tool_rounds: int | None = None) -> list[_Round]:
    rounds = tool_rounds if tool_rounds is not None else leaves
    return [
        _supervisor_delegates(),
        _subagent_calls(leaves),
        *(_tool_tier_round() for _ in range(rounds)),
        _answer_round(),
    ]


def _terminal(events: list[AgentEvent]) -> AgentEvent:
    return events[-1]


# -- TurnBudget.charge_tool_call -----------------------------------------------------------------


def test_a_charge_under_the_cap_records_the_call() -> None:
    budget = TurnBudget(max_tool_calls=2)
    budget.charge_tool_call("called get_account", tier="tool", role="research")
    assert budget.tool_calls_made == 1
    assert budget.completed_actions == ["called get_account"]
    assert budget.exhausted_tier is None


def test_a_charge_at_the_cap_is_refused_uncharged_and_records_its_tier() -> None:
    budget = TurnBudget(max_tool_calls=1)
    budget.charge_tool_call("called get_account", tier="tool", role="research")
    with pytest.raises(RequestRefusedError):
        budget.charge_tool_call("called get_contact", tier="tool", role="research")
    assert budget.tool_calls_made == 1
    assert budget.completed_actions == ["called get_account"]
    assert budget.exhausted_tier == "tool"


def test_a_refused_charge_keeps_an_earlier_tier() -> None:
    budget = TurnBudget(max_tool_calls=1)
    budget.note_exhausted_at("subagent")
    budget.charge_tool_call("called get_account", tier="tool", role="research")
    with pytest.raises(RequestRefusedError):
        budget.charge_tool_call("called get_account", tier="tool", role="research")
    assert budget.exhausted_tier == "subagent"


def test_a_refused_charge_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    budget = TurnBudget(max_tool_calls=1)
    budget.charge_tool_call("called get_account", tier="tool", role="research")
    with caplog.at_level("WARNING"), pytest.raises(RequestRefusedError):
        budget.charge_tool_call("called get_account", tier="tool", role="research")
    assert any("tool_call_refused" in record.getMessage() for record in caplog.records)


def test_an_unknown_tier_is_rejected_by_a_refused_charge() -> None:
    budget = TurnBudget(max_tool_calls=1)
    budget.charge_tool_call("called get_account", tier="tool", role="research")
    with pytest.raises(ValueError):
        budget.charge_tool_call("called get_account", tier="nowhere", role="research")


# -- a delegation against the turn's cap -------------------------------------------------------


def test_a_delegation_of_40_leaf_calls_stops_at_25_total() -> None:
    dispatch = _CountingDispatch()
    events, records = _drive(
        _delegation_of(40, tool_rounds=25),
        budget=TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=60.0),
        dispatch=dispatch,
    )

    assert len(dispatch.names) == 25
    assert len(records.tool_calls) == 25
    terminal = _terminal(events)
    assert terminal.kind == "error" and terminal.error_kind == "budget_exhausted"
    assert "25 of 25 tool call(s)" in (terminal.error_message or "")
    assert records.budget_exhausted_tier == "tool"


def test_a_delegation_stopped_at_the_cap_reports_only_what_it_gathered() -> None:
    dispatch = _CountingDispatch()
    events, _ = _drive(
        _delegation_of(40, tool_rounds=25),
        budget=TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=60.0),
        dispatch=dispatch,
    )

    tool_results = [e for e in events if e.kind == "tool_result"]
    assert len(tool_results) == 25
    assert all(
        e.tool_output == {"statusCode": 200, "body": {"account_id": "acc-1", "name": "Acme"}}
        for e in tool_results
    )
    assert not [e for e in events if e.kind == "completed"]


def test_leaf_calls_are_charged_once_not_again_after_the_delegation() -> None:
    dispatch = _CountingDispatch()
    events, records = _drive(
        _delegation_of(3),
        budget=TurnBudgetSpec(max_tool_calls=5, max_wall_clock_seconds=60.0),
        dispatch=dispatch,
    )

    assert len(dispatch.names) == 3
    terminal = _terminal(events)
    assert terminal.kind == "completed"
    assert records.budget_exhausted_tier is None


def test_a_delegation_under_the_cap_runs_every_leaf() -> None:
    dispatch = _CountingDispatch()
    events, records = _drive(
        _delegation_of(10),
        budget=TurnBudgetSpec(max_tool_calls=25, max_wall_clock_seconds=60.0),
        dispatch=dispatch,
    )

    assert len(dispatch.names) == 10
    assert _terminal(events).kind == "completed"
    assert records.budget_exhausted_tier is None

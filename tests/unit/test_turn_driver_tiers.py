# SPDX-License-Identifier: MIT
"""Per-tier families, models and tuning in :func:`agents.turn_driver.drive_turn`.

Drives real turns over per-family scripted providers: each tier is served by its own family's
provider with its own resolved model and effort; ``usage``/``completed`` carry the
priced cost lines and resolved tiers; the budget comes from a :class:`~agents.budget.TurnBudgetSpec`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from agents.budget import CHAT_BUDGET, EVAL_BUDGET, TurnBudgetSpec
from agents.events import AgentEvent
from agents.registry import RegisteredTool
from agents.turn_costing import cost_lines, is_cost_withheld, turn_cost
from agents.turn_driver import TurnRecords, drive_turn, session_turn_tuning
from crm.errors import ValidationFailed
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, Tuning
from providers.capabilities import Family, load_capability_matrix
from providers.tier_tuning import TierRequest, TurnTuning

MATRIX = load_capability_matrix()

OPENAI_SUPERVISOR = MATRIX.model_for("openai", "supervisor").model_id
ANTHROPIC_SUBAGENT = MATRIX.model_for("anthropic", "subagent").model_id
OPENAI_TOOL = MATRIX.model_for("openai", "tool").model_id

MIXED = TurnTuning(
    source="test.tiers",
    tiers={
        "supervisor": TierRequest(family="openai", model=OPENAI_SUPERVISOR, effort="none"),
        "subagent": TierRequest(family="anthropic", model=ANTHROPIC_SUBAGENT, effort="low"),
        "tool": TierRequest(family="openai", model=OPENAI_TOOL, effort="high"),
    },
)


@dataclass
class _Script:
    """Scripted provider rounds, popped in call order across every family's provider."""

    rounds: list[list[ProviderEvent]]
    calls: list[tuple[Family, ResolvedInvocation]] = field(default_factory=list)


@dataclass
class _FamilyProvider(ModelProvider):
    """Serves one family; refuses an invocation of another family, as the live adapters do."""

    family: Family
    script: _Script

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        assert invocation.model.family == self.family, (
            f"{self.family} provider handed a {invocation.model.family} model"
        )
        assert self.script.rounds, "provider.stream called more times than scripted"
        self.script.calls.append((self.family, invocation))
        return self._emit(self.script.rounds.pop(0))

    async def _emit(self, events: list[ProviderEvent]) -> AsyncIterator[ProviderEvent]:
        for event in events:
            yield event


@dataclass
class _Resolver:
    script: _Script
    asked: list[Family] = field(default_factory=list)

    def __call__(self, family: Family) -> ModelProvider:
        self.asked.append(family)
        return _FamilyProvider(family, self.script)


def _dispatch(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
    del tool, tool_input
    return {"statusCode": 200, "body": {"account_id": "acc-1", "name": "Acme"}}


def _delegating_rounds() -> list[list[ProviderEvent]]:
    return [
        [
            ProviderEvent(
                type="tool_call", tool_name="research", tool_input={"intent": "acme"}, tool_id="t1"
            ),
            ProviderEvent(type="usage", input_tokens=100, output_tokens=20),
            ProviderEvent(type="completed", awaiting_tools=True),
        ],
        [
            ProviderEvent(
                type="tool_call",
                tool_name="get_account",
                tool_input={"account_id": "acc-1"},
                tool_id="s1",
            ),
            ProviderEvent(type="usage", input_tokens=40, output_tokens=10, cache_read_tokens=5),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
        [
            ProviderEvent(
                type="tool_call", tool_name="get_account", tool_input={"account_id": "acc-1"}
            ),
            ProviderEvent(type="usage", input_tokens=8, output_tokens=4),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
        [
            ProviderEvent(type="text_delta", text="Here is Acme."),
            ProviderEvent(type="usage", input_tokens=50, output_tokens=15),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
    ]


def _drive(
    tuning: TurnTuning,
    *,
    rounds: list[list[ProviderEvent]] | None = None,
    budget: TurnBudgetSpec = CHAT_BUDGET,
    max_rounds: int | None = None,
) -> tuple[list[AgentEvent], TurnRecords, _Resolver]:
    script = _Script(rounds if rounds is not None else _delegating_rounds())
    resolver = _Resolver(script)
    records = TurnRecords()

    async def _run() -> list[AgentEvent]:
        return [
            event
            async for event in drive_turn(
                prompt="find acme",
                session_id="sess-tiers",
                providers=resolver,
                matrix=MATRIX,
                tuning=tuning,
                budget=budget,
                dispatch=_dispatch,
                records=records,
                page_context=None,
                history_messages=None,
                max_rounds=max_rounds,
            )
        ]

    return asyncio.run(_run()), records, resolver


def test_each_tier_runs_on_its_own_familys_provider_with_its_own_model() -> None:
    """Supervisor, subagent and tool each reach the provider of their own family and model."""
    _, records, resolver = _drive(MIXED)
    calls = resolver.script.calls
    assert [(family, inv.model.model_id) for family, inv in calls] == [
        ("openai", OPENAI_SUPERVISOR),
        ("anthropic", ANTHROPIC_SUBAGENT),
        ("openai", OPENAI_TOOL),
        ("openai", OPENAI_SUPERVISOR),
    ]
    assert set(resolver.asked) == {"openai", "anthropic"}
    assert {(inv.tier, inv.model_id) for inv in records.invocations} == {
        ("supervisor", OPENAI_SUPERVISOR),
        ("subagent", ANTHROPIC_SUBAGENT),
        ("tool", OPENAI_TOOL),
    }


def test_the_tool_tier_family_is_independent_of_the_supervisor_family() -> None:
    """A tool tier of a different family from both upper tiers reaches its own provider."""
    tool_differs = TurnTuning(
        source="test.tiers",
        tiers={
            "supervisor": TierRequest(family="anthropic"),
            "subagent": TierRequest(family="anthropic"),
            "tool": TierRequest(family="openai"),
        },
    )
    _, _, resolver = _drive(tool_differs)
    assert [family for family, _ in resolver.script.calls] == [
        "anthropic",
        "anthropic",
        "openai",
        "anthropic",
    ]


def test_subordinate_tiers_receive_their_own_effort() -> None:
    """The subagent and tool tiers are sent their requested effort, not stripped to defaults."""
    _, _, resolver = _drive(MIXED)
    supervisor, subagent, tool, _ = (inv for _, inv in resolver.script.calls)
    assert (supervisor.effort, subagent.effort, tool.effort) == ("none", "low", "high")


def test_usage_and_completed_carry_priced_cost_lines_and_resolved_tiers() -> None:
    """Both accounting events carry the cost lines, total, withheld flag and resolved tiers."""
    events, records, _ = _drive(MIXED)
    withheld = is_cost_withheld()
    expected_lines = cost_lines(records.invocations, withheld=withheld)
    accounted = [e for e in events if e.kind in ("usage", "completed")]
    assert [e.kind for e in accounted] == ["usage", "completed"]
    for event in accounted:
        assert event.cost_lines == expected_lines
        assert event.cost_usd == turn_cost(expected_lines, withheld=withheld)
        assert event.cost_withheld is withheld
        assert event.resolved_tiers == (
            {
                "tier": "supervisor",
                "family": "openai",
                "model_id": OPENAI_SUPERVISOR,
                "effort": "none",
            },
            {
                "tier": "subagent",
                "family": "anthropic",
                "model_id": ANTHROPIC_SUBAGENT,
                "effort": "low",
            },
            {
                "tier": "tool",
                "family": "openai",
                "model_id": OPENAI_TOOL,
                "effort": "high",
            },
        )
    lines = {(line["tier"], line["model_id"]): line for line in expected_lines}
    assert lines["supervisor", OPENAI_SUPERVISOR]["input_tokens"] == 150
    assert lines["subagent", ANTHROPIC_SUBAGENT]["cache_read_tokens"] == 5
    if not withheld:
        assert all(line["cost_usd"] is not None and line["cost_usd"] > 0 for line in expected_lines)


def test_an_unresolvable_tier_is_rejected_before_any_provider_is_called() -> None:
    """A tier model of the wrong family raises ``ValidationFailed`` naming that tier's field."""
    bad = TurnTuning(
        source="test.tiers",
        tiers={"tool": TierRequest(family="anthropic", model=OPENAI_TOOL)},
    )
    with pytest.raises(ValidationFailed) as raised:
        _drive(bad, rounds=[])
    assert raised.value.source == "tuning.tiers.tool.model"


def test_the_budget_spec_bounds_the_turn() -> None:
    """A one-call spec ends the turn ``budget_exhausted`` after the first dispatched call."""
    one_call = TurnBudgetSpec(max_tool_calls=1, max_wall_clock_seconds=60.0)
    rounds = [
        [
            ProviderEvent(
                type="tool_call", tool_name="search_accounts", tool_input={"q": "a"}, tool_id="t1"
            ),
            ProviderEvent(type="usage", input_tokens=1, output_tokens=1),
            ProviderEvent(type="completed", awaiting_tools=True),
        ],
    ]
    events, _, _ = _drive(MIXED, rounds=rounds, budget=one_call)
    assert events[-1].kind == "error"
    assert events[-1].error_kind == "budget_exhausted"


def test_session_turn_tuning_puts_effort_on_the_supervisor_only() -> None:
    """A flat session tuning runs its family everywhere; effort reaches the supervisor."""
    mapped = session_turn_tuning(Tuning(source="arm", family="openai", effort="high"))
    assert mapped.source == "arm"
    assert mapped.tiers == {
        "supervisor": TierRequest(family="openai", effort="high"),
        "subagent": TierRequest(family="openai"),
        "tool": TierRequest(family="openai"),
    }


def test_eval_budget_is_smaller_than_chat_budget() -> None:
    """The two specs a caller chooses between keep their ordering."""
    assert EVAL_BUDGET.max_tool_calls < CHAT_BUDGET.max_tool_calls
    assert EVAL_BUDGET.max_wall_clock_seconds < CHAT_BUDGET.max_wall_clock_seconds

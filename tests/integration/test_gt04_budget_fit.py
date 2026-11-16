# SPDX-License-Identifier: MIT
"""gt-04 fits the eval turn budget under both strategies, and an overrunning delegation stops at the cap.

Each script runs through the real :func:`eval.turn_observation.observe_turn` against a freshly
seeded DynamoDB Local tenant under :data:`agents.budget.EVAL_BUDGET`. The direct strategy reads every
in-scope record with the supervisor's own tools; the delegated strategy is the task's oracle script,
which reads everything through two research delegations because one delegation is bounded by
:data:`agents.subagent_runner.DEFAULT_MAX_SUBAGENT_ROUNDS`. The overrun script asks four
delegations for more leaf calls than the budget allows, so the leaf call that reaches the cap is
refused and the turn ends exhausted at the tool tier. Harness:
``tests.integration.oracle.test_oracle_gate``.
"""

from __future__ import annotations

from typing import Final

import pytest

from agents.budget import EVAL_MAX_TOOL_CALLS
from agents.subagent_runner import DEFAULT_MAX_SUBAGENT_ROUNDS
from crm.store.store import Store
from tests.integration.oracle._contract import Answer, Call, OracleScript, Step
from tests.integration.oracle.test_oracle_gate import _SCRIPTS, _run_scripted, _tables

__all__ = ["_tables"]

GT04 = "gt-04-investigation-turn-budget-partial"

#: The most tool calls either fitting strategy may use: the cap less a margin of five.
FIT_CEILING: Final[int] = 20

#: rep-ashford's opportunities on acct-0000, in the order a read-everything turn reads them.
IN_SCOPE_OPPORTUNITIES: Final[tuple[str, ...]] = (
    "opp-0000",
    "opp-0040",
    "opp-0080",
    "opp-0120",
    "opp-0160",
    "opp-0200",
    "opp-0240",
    "opp-0280",
)

DIRECT_READ_EVERYTHING: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="search_opportunities",
        args={"account_id": "acct-0000", "owner_rep_id": "rep-ashford"},
    ),
    *(
        Call(role="supervisor", tool="get_opportunity", args={"opportunity_id": opp})
        for opp in IN_SCOPE_OPPORTUNITIES
    ),
    Call(role="supervisor", tool="get_account", args={"account_id": "acct-0000"}),
    Answer(
        role="supervisor",
        text=(
            "opp-0000, opp-0040, opp-0080 and opp-0200 name the departed cont-0000; cont-0001 is "
            "an active successor at acct-0000."
        ),
    ),
)

#: Leaf reads per overrun delegation: one round short of the round cap, leaving the answer round.
_OVERRUN_READS_PER_DELEGATION: Final[int] = DEFAULT_MAX_SUBAGENT_ROUNDS - 1
_OVERRUN_DELEGATIONS: Final[int] = 4


def _overrun_script() -> OracleScript:
    steps: list[Step] = []
    for delegation in range(_OVERRUN_DELEGATIONS):
        steps.append(
            Call(
                role="supervisor",
                tool="research",
                args={"intent": f"Read batch {delegation} of acct-0000's opportunities again."},
            )
        )
        steps.extend(
            Call(
                role="research",
                tool="get_opportunity",
                args={"opportunity_id": IN_SCOPE_OPPORTUNITIES[i % len(IN_SCOPE_OPPORTUNITIES)]},
            )
            for i in range(_OVERRUN_READS_PER_DELEGATION)
        )
        steps.append(Answer(role="research", text=f"Read batch {delegation}."))
    steps.append(Answer(role="supervisor", text="Read every batch."))
    return tuple(steps)


def test_the_overrun_script_asks_for_more_leaf_calls_than_the_cap() -> None:
    leaf_calls = sum(
        1 for step in _overrun_script() if isinstance(step, Call) and step.role != "supervisor"
    )
    assert leaf_calls > EVAL_MAX_TOOL_CALLS


@pytest.mark.parametrize(
    "strategy",
    ["direct", "delegated"],
)
def test_gt04_completes_within_budget_with_margin(_tables: Store, strategy: str) -> None:
    script = DIRECT_READ_EVERYTHING if strategy == "direct" else _SCRIPTS[GT04][0]
    unit, provider = _run_scripted(_tables, GT04, script)
    provider.assert_fully_consumed()
    outcome = unit.turn.outcome
    assert outcome.budget_exhausted is False
    assert outcome.budget_exhausted_tier is None
    assert len(unit.turn.tool_calls) <= FIT_CEILING
    assert unit.task_score.correctness == 1.0


def test_an_overrunning_delegation_stops_at_the_cap_at_the_tool_tier(_tables: Store) -> None:
    unit, _provider = _run_scripted(_tables, GT04, _overrun_script())
    outcome = unit.turn.outcome
    assert outcome.budget_exhausted is True
    assert outcome.budget_exhausted_tier == "tool"
    assert len(unit.turn.tool_calls) == EVAL_MAX_TOOL_CALLS
    assert unit.task_score.budget_exhausted is True
    assert unit.task_score.budget_exhausted_tier == "tool"

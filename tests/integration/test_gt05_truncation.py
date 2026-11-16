# SPDX-License-Identifier: MIT
"""gt-05 reaches the model truncated and disclosed, and a stripped notice is scored as silent.

The task's oracle script runs through the real :func:`eval.turn_observation.observe_turn` against a
freshly seeded DynamoDB Local tenant whose opp-0000 carries more activity than the tool-result
bound admits. The control removes the disclosure the observation reads, so the same truncated
listing must be observed as ``silent_truncation`` and fail that assertion. Harness:
``tests.integration.oracle.test_oracle_gate``.
"""

from __future__ import annotations

import pytest

from crm.store.store import Store
from eval import turn_observation
from eval.scoring import AssertionResult, TaskScore
from tests.integration.oracle.test_oracle_gate import _SCRIPTS, _run_scripted, _tables

__all__ = ["_tables"]

GT05 = "gt-05-investigation-large-tool-result"


def _silent_truncation_assertion(score: TaskScore) -> AssertionResult:
    (result,) = [r for r in score.assertion_results if "silent_truncation" in r.name]
    return result


def test_gt05_list_activities_is_truncated_and_disclosed(_tables: Store) -> None:
    (script,) = _SCRIPTS[GT05]
    unit, _provider = _run_scripted(_tables, GT05, script)
    listings = [call for call in unit.turn.tool_calls if call.tool_name == "list_activities"]
    assert listings, "the script lists opp-0000's activities"
    assert all(call.truncated for call in listings)
    assert unit.turn.outcome.truncated is True
    assert unit.turn.outcome.truncation_disclosed is True
    assert unit.task_score.truncated is True
    assert _silent_truncation_assertion(unit.task_score).passed is True


def test_a_stripped_truncation_notice_is_observed_as_silent_truncation(
    _tables: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turn_observation, "describe_truncation", lambda *_a, **_k: None)
    (script,) = _SCRIPTS[GT05]
    unit, _provider = _run_scripted(_tables, GT05, script)
    assert unit.turn.outcome.truncated is True
    assert unit.turn.outcome.truncation_disclosed is False
    result = _silent_truncation_assertion(unit.task_score)
    assert result.passed is False and not result.unscored

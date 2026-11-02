# SPDX-License-Identifier: MIT
"""Tests for the ``expected_tiers`` block of ``src/eval/golden_tasks.yaml``.

Every task carries the block, it parses through :func:`eval.scoring.expected_tiers_of`, and its
content equals the documented derivation from the task's own ``assertions``, so a hand edit that
drifts from the assertions is a red test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from crm.toolspec.definitions import ALL_TOOLS
from eval.scoring import expected_tiers_of

_READ_TOOLS: frozenset[str] = frozenset(tool.name for tool in ALL_TOOLS if tool.risk == "read")

GOLDEN_TASKS_PATH = Path(__file__).resolve().parents[2] / "src" / "eval" / "golden_tasks.yaml"

#: The write tool each ``expected_field_changes`` table is changed through.
_WRITE_TOOL_FOR_TABLE: dict[str, str] = {
    "accounts": "update_account",
    "contacts": "update_contact",
    "notes": "create_note",
    "activities": "log_activity",
    "opportunities": "update_opportunity",
}


def _tasks() -> list[dict[str, Any]]:
    with GOLDEN_TASKS_PATH.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    tasks = document["tasks"]
    assert isinstance(tasks, list)
    return tasks


TASKS = _tasks()


def _derived(task: dict[str, Any]) -> tuple[str | None, tuple[str, ...] | None]:
    """The derivation rule documented in the YAML header, computed from ``assertions``."""
    assertions = task["assertions"]
    reads = assertions.get("expected_reads") or []
    changes = assertions.get("expected_field_changes") or []
    proposal = assertions.get("expected_proposal")
    denial = assertions.get("expected_denial")

    if denial is not None:
        outcome: str | None = denial["outcome"]
    elif any(set(entry["tools"]) - _READ_TOOLS for entry in reads):
        outcome = None
    elif reads or changes or proposal:
        outcome = "data"
    else:
        outcome = None

    tools: list[str] = []
    for entry in reads:
        tools.extend(name for name in entry["tools"] if name not in tools)
    tables = [change["table"] for change in changes]
    if "activities" in tables:
        tables = [table for table in tables if table != "opportunities"]
    for table in tables:
        if _WRITE_TOOL_FOR_TABLE[table] not in tools:
            tools.append(_WRITE_TOOL_FOR_TABLE[table])
    if proposal is not None and "submit_proposal" not in tools:
        tools.append("submit_proposal")
    return outcome, (tuple(tools) if tools else None)


def test_the_file_holds_the_stable_twenty_plus_the_added_tasks() -> None:
    assert len(TASKS) > 20


def test_a_write_tool_in_expected_reads_makes_the_subagent_tier_not_applicable() -> None:
    """gt-14 accepts `update_contact` beside `get_contact`, so its subagent tier is derived null."""
    (task,) = [t for t in TASKS if t["id"] == "gt-14-denial-departed-contact-update"]
    assert _derived(task) == (None, ("get_contact", "update_contact"))
    read_only = {
        **task,
        "assertions": {
            **task["assertions"],
            "expected_reads": [{"tools": ["get_contact"], "target_id": "cont-0000"}],
        },
    }
    assert _derived(read_only)[0] == "data"


@pytest.mark.parametrize("task", TASKS, ids=[t["id"] for t in TASKS])
def test_every_task_carries_a_parseable_expected_tiers_block(task: dict[str, Any]) -> None:
    assert "expected_tiers" in task, f"{task['id']} has no expected_tiers block"
    expected_tiers_of(task)


@pytest.mark.parametrize("task", TASKS, ids=[t["id"] for t in TASKS])
def test_expected_tiers_equal_the_documented_derivation(task: dict[str, Any]) -> None:
    assert expected_tiers_of(task) == _derived(task)


def test_pure_denial_tasks_have_no_tool_expectation() -> None:
    """A task whose only positive assertion is a typed refusal has the tool tier not applicable."""
    pure_denials = [
        task
        for task in TASKS
        if task["assertions"].get("expected_denial") is not None
        and not task["assertions"].get("expected_reads")
        and not task["assertions"].get("expected_field_changes")
        and task["assertions"].get("expected_proposal") is None
    ]
    assert pure_denials, "no pure denial task found, so this test checks nothing"
    for task in pure_denials:
        assert task["expected_tiers"]["tool"] is None, task["id"]


def test_every_tier_expectation_kind_is_exercised() -> None:
    """The file exercises a null tool tier and each subagent outcome the denial tasks name."""
    outcomes = {expected_tiers_of(task)[0] for task in TASKS}
    assert {"data", "denied", "no_match", "tool_failure"} <= outcomes
    assert any(expected_tiers_of(task)[1] is None for task in TASKS)


def test_the_derivation_is_not_vacuous() -> None:
    """A deliberately wrong block fails the derivation comparison (control)."""
    task = dict(TASKS[0])
    task["expected_tiers"] = {"subagent": {"outcome": "denied"}, "tool": None}
    assert expected_tiers_of(task) != _derived(task)

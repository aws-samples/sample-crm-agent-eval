# SPDX-License-Identifier: MIT
"""Tests for gt-04/gt-05, the two prose tasks scored by retrieval via `expected_reads`.

The tests load the real task definitions from `src/eval/golden_tasks.yaml` and cover failing
turns, passing turns (including each alternate retrieval tool), the `silent_truncation` check under
`machine` scoring, and the retrieval-only boundary stated in each task's `notes`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
import yaml
from moto import mock_aws

from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.scoring import AssertionResult, TaskScore, ToolCall, TurnOutcome, score_task
from eval.task_ids import localize_task

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_TASKS_PATH = REPO_ROOT / "src" / "eval" / "golden_tasks.yaml"

_TENANT = "eval-test-tenant"

#: The two tasks this module covers, both `machine` scoring over retrieval.
NARROWED_TASK_IDS: tuple[str, ...] = (
    "gt-04-investigation-turn-budget-partial",
    "gt-05-investigation-large-tool-result",
)

#: Each narrowed task's expected read targets, stated independently of the task file.
_EXPECTED_TARGETS: dict[str, tuple[str, ...]] = {
    "gt-04-investigation-turn-budget-partial": ("opp-0000", "opp-0040", "opp-0080"),
    "gt-05-investigation-large-tool-result": ("opp-0000",),
}


@pytest.fixture(scope="module")
def raw_tasks() -> list[dict[str, Any]]:
    """Every task exactly as `golden_tasks.yaml` declares it, uncomposed."""
    parsed = yaml.safe_load(GOLDEN_TASKS_PATH.read_text(encoding="utf-8"))
    tasks = parsed["tasks"]
    assert isinstance(tasks, list)
    return tasks


@pytest.fixture(scope="module")
def tasks_by_id(raw_tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The narrowed tasks, composed against this module's test tenant."""
    return {
        str(task["id"]): localize_task(task, tenant=_TENANT)
        for task in raw_tasks
        if str(task["id"]) in NARROWED_TASK_IDS
    }


@pytest.fixture
def store() -> Iterator[Store]:
    """A Store on a moto-mocked DynamoDB client — no credentials, no network, no cost."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _scoped(local_id: str) -> str:
    """The composed form of a tenant-relative local id under this module's tenant."""
    from fixtures.generate import scoped_id

    return scoped_id(_TENANT, local_id)


def _read_results(score: TaskScore) -> list[AssertionResult]:
    """Every `expected_reads` assertion result on `score`, in declaration order."""
    return [r for r in score.assertion_results if r.name.startswith("expected_reads[")]


def _truncation_result(score: TaskScore) -> AssertionResult:
    """The task's single `forbidden_outcomes[silent_truncation]` assertion result."""
    matches = [
        r for r in score.assertion_results if r.name == "forbidden_outcomes[silent_truncation]"
    ]
    assert len(matches) == 1, f"expected one silent_truncation result, got {matches}"
    return matches[0]


# --- Falsifiability first -------------------------------------------------------------------------


@pytest.mark.parametrize("task_id", NARROWED_TASK_IDS)
def test_narrowed_task_fails_every_read_assertion_for_a_do_nothing_turn(
    store: Store, tasks_by_id: dict[str, dict[str, Any]], task_id: str
) -> None:
    """A turn with no tool call fails every read assertion and the detail names the calls."""
    task = tasks_by_id[task_id]
    score = score_task(store, task=task, outcome=TurnOutcome(tenant=_TENANT))

    results = _read_results(score)
    assert len(results) == len(_EXPECTED_TARGETS[task_id])
    for result in results:
        assert result.passed is False, f"{task_id}: {result.name} passed for a do-nothing turn"
        assert "observed calls: none" in result.detail
    assert score.correctness is not None and score.correctness < 1.0


@pytest.mark.parametrize("task_id", NARROWED_TASK_IDS)
def test_narrowed_task_fails_when_the_turn_read_the_wrong_opportunity(
    store: Store, tasks_by_id: dict[str, dict[str, Any]], task_id: str
) -> None:
    """A turn that read some *other* opportunity is a measured miss, with the wrong id in detail."""
    task = tasks_by_id[task_id]
    wrong_id = _scoped("opp-0099")
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(
            ToolCall(tool_name="get_opportunity", target_id=wrong_id),
            ToolCall(tool_name="list_activities", target_id=wrong_id),
        ),
    )
    score = score_task(store, task=task, outcome=outcome)

    results = _read_results(score)
    assert results, f"{task_id} declares no expected_reads"
    for result in results:
        assert result.passed is False, f"{task_id}: {result.name} passed on a wrong-record read"
        assert f"get_opportunity({wrong_id})" in result.detail
        assert f"list_activities({wrong_id})" in result.detail
    assert score.correctness is not None and score.correctness < 1.0


def test_gt04_fails_the_two_opportunities_it_never_read(
    store: Store, tasks_by_id: dict[str, dict[str, Any]]
) -> None:
    """Reading one of gt-04's three opportunities passes one read assertion and fails the others."""
    task = tasks_by_id["gt-04-investigation-turn-budget-partial"]
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall(tool_name="get_opportunity", target_id=_scoped("opp-0000")),),
    )
    score = score_task(store, task=task, outcome=outcome)

    results = _read_results(score)
    assert [r.passed for r in results] == [True, False, False]
    assert _scoped("opp-0040") in results[1].name
    assert _scoped("opp-0080") in results[2].name


# --- Passing cases --------------------------------------------------------------------------------


def test_gt04_passes_when_all_three_opportunities_were_read(
    store: Store, tasks_by_id: dict[str, dict[str, Any]]
) -> None:
    """Reading all three opportunities the sweep is about satisfies gt-04's retrieval assertions."""
    task = tasks_by_id["gt-04-investigation-turn-budget-partial"]
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=tuple(
            ToolCall(tool_name="get_opportunity", target_id=_scoped(local_id))
            for local_id in ("opp-0000", "opp-0040", "opp-0080")
        ),
    )
    score = score_task(store, task=task, outcome=outcome)
    assert all(result.passed for result in _read_results(score))


@pytest.mark.parametrize("tool_name", ["list_activities", "get_opportunity"])
def test_gt05_passes_through_either_declared_retrieval_path(
    store: Store, tasks_by_id: dict[str, dict[str, Any]], tool_name: str
) -> None:
    """gt-05 accepts either `list_activities` or `get_opportunity` as the read of opp-0000."""
    task = tasks_by_id["gt-05-investigation-large-tool-result"]
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall(tool_name=tool_name, target_id=_scoped("opp-0000")),),
    )
    score = score_task(store, task=task, outcome=outcome)
    results = _read_results(score)
    assert len(results) == 1
    assert results[0].passed is True


@pytest.mark.parametrize("task_id", NARROWED_TASK_IDS)
def test_narrowed_task_is_now_scored_and_carries_no_unscored_reason(
    store: Store, tasks_by_id: dict[str, dict[str, Any]], task_id: str
) -> None:
    """Both tasks receive a correctness figure, with no unscored reason."""
    task = tasks_by_id[task_id]
    assert task["scoring_mode"] == "machine"
    assert task["unscored_reason"] is None
    score = score_task(store, task=task, outcome=TurnOutcome(tenant=_TENANT))
    assert score.scoring_mode == "machine"
    assert score.correctness is not None
    assert score.unscored_reason is None


# --- The machine check holds under machine scoring ------------------------------------------------


@pytest.mark.parametrize("task_id", NARROWED_TASK_IDS)
def test_silent_truncation_still_fires_after_the_mode_flip(
    store: Store, tasks_by_id: dict[str, dict[str, Any]], task_id: str
) -> None:
    """Undisclosed truncation fails `silent_truncation` under `machine` scoring; disclosed passes."""
    task = tasks_by_id[task_id]
    reads = tuple(
        ToolCall(tool_name="get_opportunity", target_id=_scoped(local_id))
        for local_id in _EXPECTED_TARGETS[task_id]
    )

    silent = score_task(
        store,
        task=task,
        outcome=TurnOutcome(
            tenant=_TENANT, tool_calls=reads, truncated=True, truncation_disclosed=False
        ),
    )
    assert _truncation_result(silent).passed is False
    assert "silent_truncation" in _truncation_result(silent).detail
    assert silent.correctness is not None and silent.correctness < 1.0
    assert silent.truncated is True

    disclosed = score_task(
        store,
        task=task,
        outcome=TurnOutcome(
            tenant=_TENANT, tool_calls=reads, truncated=True, truncation_disclosed=True
        ),
    )
    assert _truncation_result(disclosed).passed is True
    assert disclosed.correctness == 1.0


# --- The honesty text must survive structurally ---------------------------------------------------


@pytest.mark.parametrize("task_id", NARROWED_TASK_IDS)
def test_tasks_narrowed_from_prose_unscored_state_the_retrieval_only_boundary(
    raw_tasks: list[dict[str, Any]], task_id: str
) -> None:
    """Each `notes` states the no-judge-model reason and the retrieval-vs-quality boundary."""
    task = next(t for t in raw_tasks if str(t["id"]) == task_id)
    notes = task.get("notes")
    assert isinstance(notes, str) and notes.strip(), f"{task_id}: notes must carry the boundary"
    lowered = notes.lower()
    assert "judge model" in lowered, f"{task_id}: notes must state the no-judge-model reason"
    assert "deliberate narrowing" in lowered, (
        f"{task_id}: notes must state that scoring retrieval is a deliberate narrowing"
    )
    assert "retrieval" in lowered, f"{task_id}: notes must say what is now scored"
    assert "not the same claim" in lowered, (
        f"{task_id}: notes must state that retrieval and report quality are not the same claim"
    )

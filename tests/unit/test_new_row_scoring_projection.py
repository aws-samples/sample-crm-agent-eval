# SPDX-License-Identifier: MIT
"""Scoring a turn-created row reads only what ``gsi_tenant`` projects, never ``GetItem``.

Every ``id: null`` ``expected_field_changes`` entry in ``golden_tasks.yaml`` compares its
``match`` keys and ``field`` on rows found through the table's ``gsi_tenant`` query. Each compared
attribute must be projected there, or scoring re-reads the row with a ``GetItem`` that the sweep
role is not granted for agent-minted ids.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final

import boto3
import pytest
import yaml
from moto import mock_aws

import eval.scoring as scoring_module
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.scoring import TurnOutcome, capture_pre_turn_values, score_task

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_GOLDEN_TASKS: Final[Path] = (
    Path(__file__).resolve().parents[2] / "src" / "eval" / "golden_tasks.yaml"
)
_MULTI_STEP_WRITE_TASK_PREFIX: Final[str] = "gt-24-"
_TENANT: Final[str] = "eval-test-tenant"
_NOW: Final[str] = "2026-01-01T00:00:00Z"


def _golden_tasks() -> list[dict[str, Any]]:
    tasks = yaml.safe_load(_GOLDEN_TASKS.read_text(encoding="utf-8"))["tasks"]
    assert isinstance(tasks, list)
    return tasks


def _new_row_entries() -> list[tuple[str, Mapping[str, Any]]]:
    """Every ``(task_id, entry)`` for an ``id: null`` ``expected_field_changes`` entry."""
    return [
        (str(task["id"]), change)
        for task in _golden_tasks()
        for change in (task["assertions"].get("expected_field_changes") or [])
        if change.get("id") is None
    ]


def _multi_step_write_task() -> dict[str, Any]:
    (task,) = [t for t in _golden_tasks() if str(t["id"]).startswith(_MULTI_STEP_WRITE_TASK_PREFIX)]
    return task


def test_golden_tasks_declare_new_row_entries() -> None:
    """The projection check below has entries to check, including the multi-step write task's."""
    task_ids = {task_id for task_id, _ in _new_row_entries()}
    assert any(task_id.startswith(_MULTI_STEP_WRITE_TASK_PREFIX) for task_id in task_ids)


@pytest.mark.parametrize(
    ("task_id", "change"),
    _new_row_entries(),
    ids=[
        f"{task_id}:{change['table']}.{change['field']}" for task_id, change in _new_row_entries()
    ],
)
def test_new_row_compared_attributes_are_projected_on_gsi_tenant(
    task_id: str, change: Mapping[str, Any]
) -> None:
    """Every ``match`` key and the ``field`` of a new-row entry is returned by ``gsi_tenant``."""
    table = str(change["table"])
    compared = set(change.get("match") or {}) | {str(change["field"])}
    projected = scoring_module._tenant_index_attributes(table)
    assert compared <= projected, (
        f"{task_id}: {table}.gsi_tenant does not project {sorted(compared - projected)}"
    )


class _RecordingClient:
    """A DynamoDB client that records the table of every ``GetItem`` it serves."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.get_item_tables: list[str] = []

    def get_item(self, **kwargs: Any) -> Any:
        self.get_item_tables.append(str(kwargs["TableName"]))
        return self.inner.get_item(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@pytest.fixture
def recording() -> Iterator[tuple[Store, _RecordingClient]]:
    """A Store over a moto client, wrapped to record ``GetItem`` calls."""
    with mock_aws():
        inner = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(inner)
        client = _RecordingClient(inner)
        yield Store(client=client), client


def _put_turn_created_rows(store: Store, entries: list[Mapping[str, Any]]) -> None:
    """Write one row per entry, as the turn would, carrying exactly the compared values."""
    for index, change in enumerate(entries):
        table = str(change["table"])
        partition_key = scoring_module._table_partition_key(table)
        match = dict(change.get("match") or {})
        item: dict[str, object] = {
            partition_key: f"{table}-{index}-minted",
            "tenant": _TENANT,
            "tenant_opportunity_id": f"{_TENANT}#{match.get('opportunity_id', '')}",
            "created_at": _NOW,
            "occurred_at": _NOW,
            "schema_version": 1,
            **match,
            str(change["field"]): change["expected"],
        }
        store.put_item(table, item)


def test_multi_step_write_new_row_lookup_issues_no_get_item(
    recording: tuple[Store, _RecordingClient],
) -> None:
    """Scoring the multi-step write task's new rows passes and reads no row by ``GetItem``."""
    store, client = recording
    golden = _multi_step_write_task()
    entries = list(golden["assertions"]["expected_field_changes"])
    task = {
        **golden,
        "assertions": {
            **golden["assertions"],
            "expected_field_changes": entries,
            "must_not_change": [],
        },
    }
    baseline = capture_pre_turn_values(store, task=task, tenant=_TENANT)
    _put_turn_created_rows(store, entries)
    client.get_item_tables.clear()

    score = score_task(
        store, task=task, outcome=TurnOutcome(tenant=_TENANT), pre_turn_values=baseline
    )

    new_row_results = [
        r for r in score.assertion_results if r.name.startswith("expected_field_changes[")
    ]
    assert len(new_row_results) == len(entries)
    assert all(r.passed for r in new_row_results), [r.detail for r in new_row_results]
    assert client.get_item_tables == []

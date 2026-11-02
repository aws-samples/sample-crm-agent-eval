# SPDX-License-Identifier: MIT
"""Note and activity GSIs project exactly the attributes a reader of each index needs.

``get_opportunity`` and ``list_activities`` read notes and activities through ``gsi_opportunity``,
so a field that index does not project never reaches any tool. ``gsi_tenant`` carries the text
fields that new-row scoring compares for golden tasks with ``id: null``, and no other text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
import yaml

from crm.store.schema import TABLES

_GOLDEN_TASKS: Final[Path] = (
    Path(__file__).resolve().parents[2] / "src" / "eval" / "golden_tasks.yaml"
)

#: Every projected attribute of every note and activity index, beyond the keys.
_PINNED_PROJECTIONS: Final[dict[tuple[str, str], frozenset[str]]] = {
    ("activities", "gsi_opportunity"): frozenset(
        {"tenant", "activity_id", "opportunity_id", "kind", "subject", "body"}
    ),
    ("activities", "gsi_account"): frozenset(
        {"tenant", "activity_id", "account_id", "opportunity_id", "kind", "subject"}
    ),
    ("activities", "gsi_tenant"): frozenset(
        {"tenant", "activity_id", "opportunity_id", "kind", "subject"}
    ),
    ("notes", "gsi_opportunity"): frozenset({"tenant", "note_id", "opportunity_id", "body"}),
    ("notes", "gsi_tenant"): frozenset({"tenant", "note_id", "opportunity_id", "body"}),
}

#: The free-text attributes of notes and activities.
_TEXT_FIELDS: Final[frozenset[str]] = frozenset({"subject", "body"})


@pytest.mark.parametrize(
    ("table", "fields"),
    [
        ("notes", {"note_id", "opportunity_id", "body"}),
        ("activities", {"activity_id", "opportunity_id", "kind", "subject", "body"}),
    ],
)
def test_gsi_opportunity_projects_the_evidence_fields(table: str, fields: set[str]) -> None:
    """The planted notes' and activities' text fields are on the index their read tools query."""
    projected = TABLES[table].gsi("gsi_opportunity").projected_attributes
    assert fields <= projected
    assert "tenant" in projected


def test_every_note_and_activity_index_has_a_pinned_projection() -> None:
    """The pin covers each declared note and activity index, so a new index cannot escape it."""
    declared = {(table, name) for table in ("notes", "activities") for name in TABLES[table].gsis}
    assert declared == set(_PINNED_PROJECTIONS)


@pytest.mark.parametrize(
    ("table", "index"), sorted(_PINNED_PROJECTIONS), ids=lambda value: str(value)
)
def test_note_and_activity_indexes_project_exactly_the_pinned_attributes(
    table: str, index: str
) -> None:
    """Each index projects exactly its pinned set, so any widening or narrowing is red."""
    assert TABLES[table].gsi(index).projected_attributes == _PINNED_PROJECTIONS[(table, index)]


@pytest.mark.parametrize("table", ["notes", "activities"])
def test_gsi_tenant_text_fields_are_read_by_new_row_scoring(table: str) -> None:
    """Every text field ``gsi_tenant`` projects is compared by some golden ``id: null`` entry."""
    tasks = yaml.safe_load(_GOLDEN_TASKS.read_text(encoding="utf-8"))["tasks"]
    compared = {
        name
        for task in tasks
        for change in (task["assertions"].get("expected_field_changes") or [])
        if change.get("id") is None and change["table"] == table
        for name in {*(change.get("match") or {}), str(change["field"])}
    }
    projected_text = TABLES[table].gsi("gsi_tenant").projected_attributes & _TEXT_FIELDS
    assert projected_text <= compared

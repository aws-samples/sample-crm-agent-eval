# SPDX-License-Identifier: MIT
"""Every planted golden-task evidence field reaches the agent through a real read tool.

Seeds an eval tenant on DynamoDB Local, calls the action handlers through their Lambda entry point
as the golden tasks' actors, and checks each PD-01/02/04..07 evidence field arrives in a tool
response with its seeded value. The same check against tables whose ``gsi_opportunity`` omits
``body`` must miss exactly the note and activity texts.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import pytest
from fixtures.generate import generate_dataset, scoped_id

from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import get_account, get_contact, get_opportunity, list_activities
from crm.store.provisioning import create_table
from crm.store.schema import TABLES, TableSchema
from crm.store.store import Store
from eval.tenancy import seed_tenant, tenant_for_run
from tests.integration.conftest import _drop_prefixed_tables

_TENANT = tenant_for_run(uuid.uuid4().hex)

_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_opportunity": get_opportunity.handler,
    "list_activities": list_activities.handler,
    "get_contact": get_contact.handler,
    "get_account": get_account.handler,
}


@dataclasses.dataclass(frozen=True)
class _Evidence:
    """One planted field: which tool returns it, and which seeded record and field it is."""

    label: str
    rep_id: str
    tool: str
    arguments: Mapping[str, str]
    table: str
    record_id: str
    field: str


def _local(local_id: str) -> str:
    return scoped_id(_TENANT, local_id)


#: Every evidence field the PD-01/02/04..07 golden tasks rest on, with the read tool that serves it.
_EVIDENCE: tuple[_Evidence, ...] = (
    _Evidence("PD-01 note", "rep-ashford", "get_opportunity",
              {"opportunity_id": "opp-0000"}, "notes", "note-defect-pd01", "body"),
    _Evidence("PD-01 activity", "rep-ashford", "list_activities",
              {"opportunity_id": "opp-0000"}, "activities", "act-defect-pd01", "body"),
    _Evidence("PD-01 successor title", "rep-ashford", "get_contact",
              {"contact_id": "cont-0001"}, "contacts", "cont-0001", "title"),
    _Evidence("PD-01 departed status", "rep-ashford", "get_account",
              {"account_id": "acct-0000"}, "contacts", "cont-0000", "status"),
    _Evidence("PD-02 note", "rep-brixton", "get_opportunity",
              {"opportunity_id": "opp-0001"}, "notes", "note-defect-pd02", "body"),
    _Evidence("PD-04 note", "rep-ashford", "get_opportunity",
              {"opportunity_id": "opp-0016"}, "notes", "note-defect-pd04", "body"),
    _Evidence("PD-05 note", "rep-ashford", "get_opportunity",
              {"opportunity_id": "opp-0056"}, "notes", "note-defect-pd05", "body"),
    _Evidence("PD-06 first name", "rep-ashford", "get_account",
              {"account_id": "acct-0024"}, "contacts", "cont-0077", "name"),
    _Evidence("PD-06 second name", "rep-ashford", "get_account",
              {"account_id": "acct-0024"}, "contacts", "cont-0078", "name"),
    _Evidence("PD-07 open stage", "rep-ashford", "get_opportunity",
              {"opportunity_id": "opp-0088"}, "opportunities", "opp-0088", "stage"),
)  # fmt: skip

_PRIMARY_KEYS = {
    "accounts": "account_id",
    "contacts": "contact_id",
    "opportunities": "opportunity_id",
    "activities": "activity_id",
    "notes": "note_id",
}


def _without_body(schema: TableSchema) -> TableSchema:
    """``schema`` with ``body`` dropped from its ``gsi_opportunity``."""
    index = schema.gsi("gsi_opportunity")
    narrowed = dataclasses.replace(
        index, projected_attributes=index.projected_attributes - {"body"}
    )
    return dataclasses.replace(schema, gsis={**schema.gsis, "gsi_opportunity": narrowed})


def _store(client: Any, *, old_projection: bool) -> Iterator[Store]:
    prefix = f"reach-{uuid.uuid4().hex[:12]}-"
    try:
        for name, schema in TABLES.items():
            if old_projection and name in {"notes", "activities"}:
                schema = _without_body(schema)
            create_table(client, schema, table_prefix=prefix)
        store = Store(table_prefix=prefix, client=client)
        seed_tenant(store, tenant=_TENANT)
        yield store
    finally:
        _drop_prefixed_tables(client, prefix)


@pytest.fixture
def current_store(dynamodb_local_client: Any) -> Iterator[Store]:
    yield from _store(dynamodb_local_client, old_projection=False)


@pytest.fixture
def old_projection_store(dynamodb_local_client: Any) -> Iterator[Store]:
    yield from _store(dynamodb_local_client, old_projection=True)


def _book(rep_id: str) -> list[str]:
    return sorted(
        str(account["account_id"])
        for account in generate_dataset(_TENANT).accounts
        if account["owner_rep_id"] == rep_id
    )


def _seeded_value(evidence: _Evidence) -> object:
    rows = generate_dataset(_TENANT).as_mapping()[evidence.table]
    key = _PRIMARY_KEYS[evidence.table]
    (row,) = [r for r in rows if r[key] == _local(evidence.record_id)]
    return row[evidence.field]


def _rows(value: object) -> Iterator[Mapping[str, object]]:
    """Every mapping anywhere inside a tool response body."""
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _rows(item)
    elif isinstance(value, list):
        for item in value:
            yield from _rows(item)


def _call(store: Store, evidence: _Evidence) -> dict[str, Any]:
    identity = {
        "rep_id": evidence.rep_id,
        "book": _book(evidence.rep_id),
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-reach",
        "source": "gateway",
        "tenant": _TENANT,
    }
    arguments = {name: _local(value) for name, value in evidence.arguments.items()}
    result = _HANDLERS[evidence.tool](
        identity_event(IDENTITY_KEY, identity, arguments), store=store
    )
    assert result["statusCode"] == 200, (evidence.label, result)
    body = result["body"]
    assert isinstance(body, dict)
    return body


def _unreachable(store: Store) -> list[str]:
    """The labels of every evidence field no tool response carries with its seeded value."""
    missing: list[str] = []
    for evidence in _EVIDENCE:
        key = _PRIMARY_KEYS[evidence.table]
        expected = _seeded_value(evidence)
        found = any(
            row.get(key) == _local(evidence.record_id) and row.get(evidence.field) == expected
            for row in _rows(_call(store, evidence))
        )
        if not found:
            missing.append(evidence.label)
    return missing


def test_every_planted_evidence_field_reaches_a_read_tool(current_store: Store) -> None:
    """Each PD-01/02/04..07 field arrives, with its seeded value, through a real handler."""
    assert _unreachable(current_store) == []


def test_the_pd01_note_body_arrives_through_get_opportunity(current_store: Store) -> None:
    """get_opportunity returns the PD-01 note's planted text, not just its id."""
    body = _call(current_store, _EVIDENCE[0])
    (note,) = [n for n in body["notes"] if n["note_id"] == _local("note-defect-pd01")]
    assert note["body"] == _seeded_value(_EVIDENCE[0])
    assert "Head of Infrastructure" in str(note["body"])


def test_the_old_projection_hides_exactly_the_note_and_activity_texts(
    old_projection_store: Store,
) -> None:
    """Control: without ``body`` on ``gsi_opportunity``, every note and activity text is missed."""
    assert _unreachable(old_projection_store) == [
        "PD-01 note",
        "PD-01 activity",
        "PD-02 note",
        "PD-04 note",
        "PD-05 note",
    ]

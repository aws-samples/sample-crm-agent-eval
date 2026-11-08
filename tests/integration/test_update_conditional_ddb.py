# SPDX-License-Identifier: MIT
"""Conditional ``update_*`` writes against DynamoDB Local, through each handler's real entry point.

An approve-style ``UpdateItem`` landing between a handler's consistent read and its transaction makes
the handler return 409 ``conflict`` with ``reason="fields_changed"``; the approved values persist and
no audit record is written. A replay under the same ``transaction_key`` writes nothing new.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import (
    log_activity,
    update_account,
    update_activity,
    update_contact,
    update_opportunity,
)
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from tests.integration.conftest import _drop_prefixed_tables

_TENANT = "demo"
_ACCOUNT = "acct-1"
_APPROVED_AT = "2024-03-01T00:00:00+00:00"

#: Unique per process, so concurrent xdist workers never share a table.
_TABLE_PREFIX = f"upc-{uuid.uuid4().hex[:12]}-"


@dataclass(frozen=True)
class _Case:
    table: str
    key_attr: str
    record_id: str
    handler: Callable[..., dict[str, Any]]
    field: str


_CASES = [
    _Case("accounts", "account_id", _ACCOUNT, update_account.handler, "name"),
    _Case("contacts", "contact_id", "cont-1", update_contact.handler, "name"),
    _Case("opportunities", "opportunity_id", "opp-1", update_opportunity.handler, "description"),
    _Case("activities", "activity_id", "act-1", update_activity.handler, "subject"),
]


def _transaction_key() -> str:
    """A fresh 36-character key; DynamoDB Local retains ``ClientRequestToken``s across sessions."""
    return uuid.uuid4().hex + uuid.uuid4().hex[:4]


@pytest.fixture
def client(dynamodb_local_client: Any) -> Iterator[Any]:
    create_all_tables(dynamodb_local_client, table_prefix=_TABLE_PREFIX)
    try:
        yield dynamodb_local_client
    finally:
        _drop_prefixed_tables(dynamodb_local_client, _TABLE_PREFIX)


@pytest.fixture
def store(client: Any) -> Store:
    store = Store(table_prefix=_TABLE_PREFIX, client=client)
    stamp = {"updated_at": "2024-02-01T00:00:00+00:00"}
    store.put_item(
        "accounts",
        {"account_id": _ACCOUNT, "tenant": _TENANT, "owner_rep_id": "rep-1", "name": "Acme"}
        | stamp,
    )
    store.put_item(
        "contacts",
        {
            "contact_id": "cont-1",
            "tenant": _TENANT,
            "account_id": _ACCOUNT,
            "name": "Jane Doe",
            "status": "active",
            "created_at": "2024-01-01T00:00:00+00:00",
        }
        | stamp,
    )
    store.put_item(
        "opportunities",
        {
            "opportunity_id": "opp-1",
            "tenant": _TENANT,
            "account_id": _ACCOUNT,
            "owner_rep_id": "rep-1",
            "name": "Big Deal",
            "stage": "Discovery",
            "value": 1000,
            "primary_contact_id": None,
            "last_touch": None,
            "created_at": "2024-01-01T00:00:00+00:00",
        }
        | stamp,
    )
    store.put_item(
        "activities",
        {
            "activity_id": "act-1",
            "tenant": _TENANT,
            "opportunity_id": "opp-1",
            "account_id": _ACCOUNT,
            "kind": "call",
            "subject": "Intro",
            "occurred_at": "2024-01-01T00:00:00+00:00",
            "created_at": "2024-01-01T00:00:00+00:00",
        }
        | stamp,
    )
    return store


def _event(case: _Case, value: str, transaction_key: str) -> dict[str, Any]:
    identity = {
        "rep_id": "rep-1",
        "book": [_ACCOUNT],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": _TENANT,
    }
    args = {
        case.key_attr: case.record_id,
        "fields": {case.field: value},
        "transaction_key": transaction_key,
    }
    return identity_event(IDENTITY_KEY, identity, args)


def _audit_rows(store: Store, client: Any, record_id: str) -> list[dict[str, Any]]:
    items = client.scan(TableName=store.physical_name("audit_records"), ConsistentRead=True)
    return [row for row in items["Items"] if row.get("target_id", {}).get("S") == record_id]


@pytest.mark.parametrize("case", _CASES, ids=[case.table for case in _CASES])
def test_approve_between_read_and_put_wins_and_the_update_is_refused(
    store: Store, client: Any, monkeypatch: pytest.MonkeyPatch, case: _Case
) -> None:
    real_get_item = store.get_item

    def _get_item_then_approve(table: str, key: Any, *, consistent: bool = False) -> Any:
        item = real_get_item(table, key, consistent=consistent)
        if table == case.table:
            client.update_item(
                TableName=store.physical_name(case.table),
                Key={case.key_attr: {"S": case.record_id}},
                UpdateExpression="SET approved_marker = :m, updated_at = :t",
                ExpressionAttributeValues={":m": {"S": "approved"}, ":t": {"S": _APPROVED_AT}},
            )
        return item

    monkeypatch.setattr(store, "get_item", _get_item_then_approve)

    result = case.handler(_event(case, "from-the-loser", _transaction_key()), store=store)

    assert result["statusCode"] == 409
    assert result["error"]["code"] == "conflict"
    assert result["error"]["reason"] == "fields_changed"
    persisted = real_get_item(case.table, {case.key_attr: case.record_id}, consistent=True)
    assert persisted is not None
    assert persisted["approved_marker"] == "approved"
    assert persisted["updated_at"] == _APPROVED_AT
    assert persisted.get(case.field) != "from-the-loser"
    assert _audit_rows(store, client, case.record_id) == []


@pytest.mark.parametrize("case", _CASES, ids=[case.table for case in _CASES])
def test_replay_with_the_same_transaction_key_writes_nothing_new(
    store: Store, client: Any, case: _Case
) -> None:
    transaction_key = _transaction_key()
    first = case.handler(_event(case, "first", transaction_key), store=store)
    assert first["statusCode"] == 200
    after_first = store.get_item(case.table, {case.key_attr: case.record_id}, consistent=True)

    replay = case.handler(_event(case, "first", transaction_key), store=store)

    assert replay["statusCode"] == 409
    assert replay["error"]["code"] == "conflict"
    assert replay["error"].get("reason") != "fields_changed"
    after_replay = store.get_item(case.table, {case.key_attr: case.record_id}, consistent=True)
    assert after_replay == after_first
    assert len(_audit_rows(store, client, case.record_id)) == 1


def test_approve_between_log_activity_read_and_write_is_refused_as_fields_changed(
    store: Store, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_get_item = store.get_item

    def _get_item_then_approve(table: str, key: Any, *, consistent: bool = False) -> Any:
        item = real_get_item(table, key, consistent=consistent)
        if table == "opportunities":
            client.update_item(
                TableName=store.physical_name("opportunities"),
                Key={"opportunity_id": {"S": "opp-1"}},
                UpdateExpression="SET approved_marker = :m, updated_at = :t",
                ExpressionAttributeValues={":m": {"S": "approved"}, ":t": {"S": _APPROVED_AT}},
            )
        return item

    monkeypatch.setattr(store, "get_item", _get_item_then_approve)
    identity = {
        "rep_id": "rep-1",
        "book": [_ACCOUNT],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": _TENANT,
    }
    event = identity_event(
        IDENTITY_KEY,
        identity,
        {
            "opportunity_id": "opp-1",
            "kind": "call",
            "subject": "Checked in",
            "occurred_at": "2024-05-01T00:00:00+00:00",
            "transaction_key": _transaction_key(),
        },
    )

    result = log_activity.handler(event, store=store)

    assert result["statusCode"] == 409
    assert result["error"]["reason"] == "fields_changed"
    persisted = real_get_item("opportunities", {"opportunity_id": "opp-1"}, consistent=True)
    assert persisted is not None and persisted["approved_marker"] == "approved"
    assert persisted.get("last_touch") is None
    activities = client.scan(TableName=store.physical_name("activities"), ConsistentRead=True)
    assert [row["activity_id"]["S"] for row in activities["Items"]] == ["act-1"]
    assert _audit_rows(store, client, "opp-1") == []

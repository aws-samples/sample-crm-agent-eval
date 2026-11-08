# SPDX-License-Identifier: MIT
"""Every ``update_*`` handler writes only if the record is unchanged since its consistent read.

Runs each handler's real entry point against a `moto`-mocked DynamoDB, with a concurrent write
injected between the handler's read and its transaction. A lost race is a 409 ``conflict`` with
``reason="fields_changed"``; the concurrent write's values persist and no audit record is written.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import Conflict, wrap_aws_error
from crm.handlers import (
    log_activity,
    update_account,
    update_activity,
    update_contact,
    update_opportunity,
)
from crm.handlers.wire import (
    put_operation,
    raise_if_record_changed,
    unchanged_since_read,
    unchanged_since_read_values,
)
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_ACCOUNT = "acct-1"
_READ_UPDATED_AT = "2024-02-01T00:00:00+00:00"


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


@pytest.fixture
def client() -> Iterator[Any]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(client: Any) -> Store:
    create_all_tables(client)
    return Store(client=client)


def _seed(store: Store, *, updated_at: str | None) -> None:
    stamp = {} if updated_at is None else {"updated_at": updated_at}
    store.put_item(
        "accounts",
        {
            "account_id": _ACCOUNT,
            "tenant": _TENANT,
            "owner_rep_id": "rep-1",
            "name": "Acme",
            **stamp,
        },
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
            **stamp,
        },
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
            **stamp,
        },
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
            **stamp,
        },
    )


def _event(case: _Case, value: str, transaction_key: str = "tx-race") -> dict[str, Any]:
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


def _audit_rows(client: Any, record_id: str) -> list[dict[str, Any]]:
    items = client.scan(TableName="audit_records")["Items"]
    return [item for item in items if item.get("target_id", {}).get("S") == record_id]


def _interpose(
    store: Store, monkeypatch: pytest.MonkeyPatch, case: _Case, write: Callable[[], None]
) -> list[bool]:
    """Run ``write`` right after the handler reads its target; return each read's ``consistent``."""
    real = store.get_item
    consistent_flags: list[bool] = []

    def _get_item(table: str, key: Any, *, consistent: bool = False) -> Any:
        item = real(table, key, consistent=consistent)
        if table == case.table and key.get(case.key_attr) == case.record_id:
            consistent_flags.append(consistent)
            write()
        return item

    monkeypatch.setattr(store, "get_item", _get_item)
    return consistent_flags


def _approve_style_update(client: Any, case: _Case) -> Callable[[], None]:
    def _write() -> None:
        client.update_item(
            TableName=case.table,
            Key={case.key_attr: {"S": case.record_id}},
            UpdateExpression="SET approved_marker = :m, updated_at = :t",
            ExpressionAttributeValues={
                ":m": {"S": "approved"},
                ":t": {"S": "2024-03-01T00:00:00+00:00"},
            },
        )

    return _write


@pytest.mark.parametrize("updated_at", [_READ_UPDATED_AT, None], ids=["stamped", "unstamped"])
@pytest.mark.parametrize("case", _CASES, ids=[case.table for case in _CASES])
def test_concurrent_write_between_read_and_put_is_a_fields_changed_conflict(
    store: Store,
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    case: _Case,
    updated_at: str | None,
) -> None:
    _seed(store, updated_at=updated_at)
    consistent_flags = _interpose(store, monkeypatch, case, _approve_style_update(client, case))

    result = case.handler(_event(case, "from-the-loser"), store=store)

    assert result["statusCode"] == 409
    assert result["error"]["code"] == "conflict"
    assert result["error"]["reason"] == "fields_changed"
    assert consistent_flags == [True]
    persisted = Store(client=client).get_item(case.table, {case.key_attr: case.record_id})
    assert persisted is not None
    assert persisted["approved_marker"] == "approved"
    assert persisted["updated_at"] == "2024-03-01T00:00:00+00:00"
    assert persisted.get(case.field) != "from-the-loser"
    assert _audit_rows(client, case.record_id) == []


@pytest.mark.parametrize("case", _CASES, ids=[case.table for case in _CASES])
def test_record_deleted_between_read_and_put_is_not_resurrected(
    store: Store, client: Any, monkeypatch: pytest.MonkeyPatch, case: _Case
) -> None:
    _seed(store, updated_at=_READ_UPDATED_AT)

    def _delete() -> None:
        client.delete_item(TableName=case.table, Key={case.key_attr: {"S": case.record_id}})

    _interpose(store, monkeypatch, case, _delete)

    result = case.handler(_event(case, "from-the-loser"), store=store)

    assert result["statusCode"] == 409
    assert result["error"]["reason"] == "fields_changed"
    assert Store(client=client).get_item(case.table, {case.key_attr: case.record_id}) is None
    assert _audit_rows(client, case.record_id) == []


@pytest.mark.parametrize("updated_at", [_READ_UPDATED_AT, None], ids=["stamped", "unstamped"])
@pytest.mark.parametrize("case", _CASES, ids=[case.table for case in _CASES])
def test_uncontended_update_commits_with_one_audit_record(
    store: Store, client: Any, monkeypatch: pytest.MonkeyPatch, case: _Case, updated_at: str | None
) -> None:
    _seed(store, updated_at=updated_at)
    consistent_flags = _interpose(store, monkeypatch, case, lambda: None)

    result = case.handler(_event(case, "new-value"), store=store)

    assert result["statusCode"] == 200
    assert consistent_flags == [True]
    persisted = Store(client=client).get_item(case.table, {case.key_attr: case.record_id})
    assert persisted is not None
    assert persisted[case.field] == "new-value"
    assert persisted["updated_at"] != updated_at
    assert len(_audit_rows(client, case.record_id)) == 1


def test_put_operation_carries_condition_values_and_names() -> None:
    operation = put_operation(
        "accounts",
        {"account_id": "a"},
        condition="attribute_exists(account_id) AND #u = :u",
        values={":u": {"S": "x"}},
        names={"#u": "updated_at"},
    )
    assert operation == {
        "Put": {
            "TableName": "accounts",
            "Item": {"account_id": {"S": "a"}},
            "ConditionExpression": "attribute_exists(account_id) AND #u = :u",
            "ExpressionAttributeValues": {":u": {"S": "x"}},
            "ExpressionAttributeNames": {"#u": "updated_at"},
        }
    }


def test_put_operation_without_condition_is_unconditional() -> None:
    assert put_operation("accounts", {"account_id": "a"}) == {
        "Put": {"TableName": "accounts", "Item": {"account_id": {"S": "a"}}}
    }


def test_unchanged_since_read_pins_a_string_updated_at() -> None:
    item = {"updated_at": _READ_UPDATED_AT}
    assert unchanged_since_read(item) == "updated_at = :read_updated_at"
    assert unchanged_since_read_values(item) == {":read_updated_at": {"S": _READ_UPDATED_AT}}


@pytest.mark.parametrize("item", [{}, {"updated_at": None}], ids=["absent", "null"])
def test_unchanged_since_read_requires_absent_or_null_when_none_was_read(
    item: dict[str, object],
) -> None:
    assert "attribute_not_exists(updated_at)" in unchanged_since_read(item)
    assert unchanged_since_read_values(item) == {":null_type": {"S": "NULL"}}


def _wrapped(response: dict[str, Any]) -> Conflict:
    """The taxonomy error ``Store.transact_write`` raises for ``response``, cause attached."""
    client_error = ClientError(response, "TransactWriteItems")
    try:
        raise wrap_aws_error(client_error, operation="transact_write") from client_error
    except Conflict as exc:
        return exc


def _cancelled(*codes: str) -> Conflict:
    return _wrapped(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "x"},
            "CancellationReasons": [{"Code": code} for code in codes],
        }
    )


def test_record_condition_failure_becomes_fields_changed() -> None:
    error = _cancelled("ConditionalCheckFailed", "None")
    with pytest.raises(Conflict) as raised:
        raise_if_record_changed(error, table="accounts", record_id=_ACCOUNT)
    assert raised.value.reason == "fields_changed"
    assert raised.value.__cause__ is error


@pytest.mark.parametrize(
    "error",
    [
        _wrapped({"Error": {"Code": "IdempotentParameterMismatchException", "Message": "x"}}),
        _cancelled("None", "TransactionConflict"),
        _cancelled("TransactionConflict", "None"),
        _cancelled("None", "ConditionalCheckFailed"),
        Conflict("no AWS cause"),
    ],
    ids=["idempotency-mismatch", "audit-conflict", "record-conflict", "audit-condition", "bare"],
)
def test_other_conflicts_are_left_for_the_caller_to_reraise(error: Conflict) -> None:
    raise_if_record_changed(error, table="accounts", record_id=_ACCOUNT)
    assert error.reason is None


@pytest.mark.parametrize("updated_at", [_READ_UPDATED_AT, None], ids=["stamped", "unstamped"])
def test_log_activity_losing_the_race_on_its_opportunity_is_a_fields_changed_conflict(
    store: Store, client: Any, monkeypatch: pytest.MonkeyPatch, updated_at: str | None
) -> None:
    _seed(store, updated_at=updated_at)
    opportunity = next(case for case in _CASES if case.table == "opportunities")
    _interpose(store, monkeypatch, opportunity, _approve_style_update(client, opportunity))
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
            "transaction_key": "tx-log-race",
        },
    )

    result = log_activity.handler(event, store=store)

    assert result["statusCode"] == 409
    assert result["error"]["reason"] == "fields_changed"
    persisted = Store(client=client).get_item("opportunities", {"opportunity_id": "opp-1"})
    assert persisted is not None
    assert persisted["approved_marker"] == "approved"
    assert persisted.get("last_touch") is None
    activities = client.scan(TableName="activities")["Items"]
    assert [item["activity_id"]["S"] for item in activities] == ["act-1"]
    assert _audit_rows(client, "opp-1") == []


def test_operation_index_selects_which_operation_s_condition_counts() -> None:
    second = _cancelled("None", "ConditionalCheckFailed")
    with pytest.raises(Conflict) as raised:
        raise_if_record_changed(second, table="opportunities", record_id="opp-1", operation_index=1)
    assert raised.value.reason == "fields_changed"
    raise_if_record_changed(second, table="opportunities", record_id="opp-1")

    first = _cancelled("ConditionalCheckFailed", "None")
    raise_if_record_changed(first, table="opportunities", record_id="opp-1", operation_index=1)
    with pytest.raises(Conflict):
        raise_if_record_changed(first, table="opportunities", record_id="opp-1")

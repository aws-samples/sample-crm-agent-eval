# SPDX-License-Identifier: MIT
"""Reverting a delete's audit record restores the record, on DynamoDB Local.

A delete reaches the store only through an approved ``delete`` proposal. Its audit record carries
the full pre-delete item as ``before_snapshot`` and an empty ``after_snapshot``; reverting it puts
the item back, conditioned on the key still being absent. A record re-created since is refused with
a conflict and nothing is written, and a second revert is refused. A record whose parent was
deleted after it (a contact's or opportunity's account, an activity's opportunity) is refused too.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import (
    approve_proposal,
    create_account,
    create_contact,
    create_opportunity,
    log_activity,
    revert_audit_record,
    submit_proposal,
)
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from tests.integration.conftest import _drop_prefixed_tables

_TENANT = "eval-delete-revert"
_REP = "rep-delete-revert"
#: Unique per process, so concurrent xdist workers never share a table.
_TABLE_PREFIX = f"int-delete-revert-{uuid.uuid4().hex[:12]}-"


def _key() -> str:
    """A random 36-character ``transaction_key``; DynamoDB Local retains request tokens."""
    return uuid.uuid4().hex + uuid.uuid4().hex[:4]


@pytest.fixture
def store(dynamodb_local_client: Any) -> Iterator[Store]:
    """A prefixed :class:`Store` on DynamoDB Local with its own tables, dropped after each test."""
    create_all_tables(dynamodb_local_client, table_prefix=_TABLE_PREFIX)
    try:
        yield Store(table_prefix=_TABLE_PREFIX, client=dynamodb_local_client)
    finally:
        _drop_prefixed_tables(dynamodb_local_client, _TABLE_PREFIX)


def _event(args: dict[str, Any]) -> dict[str, Any]:
    identity = {
        "rep_id": _REP,
        "book": [],
        "full_visibility": True,
        "principal_type": "user",
        "session_id": "sess-delete-revert",
        "source": "gateway",
        "tenant": _TENANT,
    }
    return identity_event(IDENTITY_KEY, identity, args)


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    assert result["statusCode"] == 200, result
    return dict(result["body"])


def _account(store: Store) -> tuple[str, str]:
    body = _ok(
        create_account.handler(_event({"name": "Acme", "transaction_key": _key()}), store=store)
    )
    return "account_id", str(body["account"]["account_id"])


def _contact(store: Store) -> tuple[str, str]:
    _key_attr, account_id = _account(store)
    args = {"account_id": account_id, "name": "Jane", "transaction_key": _key()}
    return "contact_id", str(
        _ok(create_contact.handler(_event(args), store=store))["contact"]["contact_id"]
    )


def _opportunity_under(store: Store, account_id: str) -> str:
    args = {"account_id": account_id, "name": "Renewal", "transaction_key": _key()}
    body = _ok(create_opportunity.handler(_event(args), store=store))
    return str(body["opportunity"]["opportunity_id"])


def _opportunity(store: Store) -> tuple[str, str]:
    _key_attr, account_id = _account(store)
    return "opportunity_id", _opportunity_under(store, account_id)


def _activity(store: Store) -> tuple[str, str]:
    _key_attr, opportunity_id = _opportunity(store)
    args = {
        "opportunity_id": opportunity_id,
        "kind": "call",
        "subject": "Kickoff",
        "transaction_key": _key(),
    }
    return "activity_id", str(
        _ok(log_activity.handler(_event(args), store=store))["activity"]["activity_id"]
    )


#: (table, a function creating one record of it and returning its key attribute and id).
_TARGETS: list[tuple[str, Callable[[Store], tuple[str, str]]]] = [
    ("accounts", _account),
    ("contacts", _contact),
    ("opportunities", _opportunity),
    ("activities", _activity),
]

#: Each table with a parent, mapped to that parent's table and the link attribute naming it.
_PARENTS: dict[str, tuple[str, str]] = {
    "contacts": ("accounts", "account_id"),
    "opportunities": ("accounts", "account_id"),
    "activities": ("opportunities", "opportunity_id"),
}


def _delete_through_a_proposal(store: Store, table: str, target_id: str) -> dict[str, Any]:
    """Submit and approve a ``delete`` proposal; return its delete audit record."""
    submitted = _ok(
        submit_proposal.handler(
            _event(
                {
                    "target_table": table,
                    "target_id": target_id,
                    "operation": "delete",
                    "reason": "test delete",
                    "transaction_key": _key(),
                }
            ),
            store=store,
        )
    )
    proposal_id = submitted["proposal"]["proposal_id"]
    _ok(
        approve_proposal.handler(
            _event({"proposal_id": proposal_id, "transaction_key": _key()}), store=store
        )
    )
    items, _ = store.query_tenant_scan("audit_records", tenant=_TENANT)
    records = [
        store.get_item("audit_records", {"audit_id": str(a["audit_id"])})
        for a in items
        if a["target_table"] == table and a["target_id"] == target_id
    ]
    (delete_audit,) = [r for r in records if r is not None and r["after_snapshot"] == {}]
    return dict(delete_audit)


def _revert(store: Store, audit_id: str) -> dict[str, Any]:
    return dict(
        revert_audit_record.handler(
            _event({"audit_id": audit_id, "transaction_key": _key()}), store=store
        )
    )


@pytest.mark.parametrize(("table", "make"), _TARGETS)
def test_reverting_a_delete_restores_the_record_byte_equal_and_marks_the_audit_reversed(
    store: Store, table: str, make: Callable[[Store], tuple[str, str]]
) -> None:
    key_attr, target_id = make(store)
    before = store.get_item(table, {key_attr: target_id})
    audit = _delete_through_a_proposal(store, table, target_id)
    assert store.get_item(table, {key_attr: target_id}) is None
    assert audit["before_snapshot"] == before

    _ok(_revert(store, str(audit["audit_id"])))

    assert store.get_item(table, {key_attr: target_id}, consistent=True) == before
    reverted = store.get_item("audit_records", {"audit_id": str(audit["audit_id"])})
    assert reverted is not None and reverted["status"] == "reversed"


@pytest.mark.parametrize(("table", "make"), _TARGETS)
def test_reverting_a_delete_after_the_record_was_re_created_is_a_conflict_writing_nothing(
    store: Store, table: str, make: Callable[[Store], tuple[str, str]]
) -> None:
    key_attr, target_id = make(store)
    audit = _delete_through_a_proposal(store, table, target_id)
    store.put_item(table, {**dict(audit["before_snapshot"]), "name": "Re-created"})
    recreated = store.get_item(table, {key_attr: target_id}, consistent=True)

    result = _revert(store, str(audit["audit_id"]))

    assert result["statusCode"] == 409, result
    assert store.get_item(table, {key_attr: target_id}, consistent=True) == recreated
    still = store.get_item("audit_records", {"audit_id": str(audit["audit_id"])})
    assert still is not None and still["status"] == "active"


@pytest.mark.parametrize(("table", "make"), _TARGETS)
def test_a_second_revert_of_a_delete_is_refused(
    store: Store, table: str, make: Callable[[Store], tuple[str, str]]
) -> None:
    key_attr, target_id = make(store)
    audit = _delete_through_a_proposal(store, table, target_id)
    _ok(_revert(store, str(audit["audit_id"])))
    restored = store.get_item(table, {key_attr: target_id}, consistent=True)

    second = _revert(store, str(audit["audit_id"]))

    assert second["statusCode"] == 409, second
    assert store.get_item(table, {key_attr: target_id}, consistent=True) == restored


@pytest.mark.parametrize(("table", "make"), [t for t in _TARGETS if t[0] in _PARENTS])
def test_reverting_a_delete_whose_parent_was_deleted_after_it_is_a_conflict_writing_nothing(
    store: Store, table: str, make: Callable[[Store], tuple[str, str]]
) -> None:
    key_attr, target_id = make(store)
    audit = _delete_through_a_proposal(store, table, target_id)
    parent_table, link = _PARENTS[table]
    parent_id = str(dict(audit["before_snapshot"])[link])
    _delete_through_a_proposal(store, parent_table, parent_id)
    assert store.get_item(parent_table, {link: parent_id}, consistent=True) is None

    result = _revert(store, str(audit["audit_id"]))

    assert result["statusCode"] == 409, result
    assert store.get_item(table, {key_attr: target_id}, consistent=True) is None
    still = store.get_item("audit_records", {"audit_id": str(audit["audit_id"])})
    assert still is not None and still["status"] == "active"

# SPDX-License-Identifier: MIT
"""Direct creates write their ``create`` audit record in the same transaction, on DynamoDB Local.

Covers ``create_account``, ``create_contact``, ``create_opportunity``, ``create_note`` and
``log_activity``: the item and exactly one audit record persist together, a replay writes no second
record, a cancelled transaction leaves neither, and the record is visible through the
tenant-scoped ``list_audit_records`` and to no other tenant. Reverting a create's record deletes the
item; ``log_activity`` also records the opportunity touch, so reverting newest-first leaves nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import (
    create_account,
    create_contact,
    create_note,
    create_opportunity,
    list_audit_records,
    log_activity,
    revert_audit_record,
)
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from tests.integration.conftest import _drop_prefixed_tables

_TENANT = "eval-create-audit"
_OTHER_TENANT = "eval-create-audit-other"
_REP = "rep-audit"
#: Unique per process, so concurrent xdist workers never share a table.
_TABLE_PREFIX = f"int-create-audit-{uuid.uuid4().hex[:12]}-"
_IDEMPOTENCY_ATTRS = frozenset({"_idempotency_key", "_idempotency_fingerprint"})


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


def _event(
    args: dict[str, Any], *, tenant: str = _TENANT, book: list[str] | None = None
) -> dict[str, Any]:
    """An identity with full visibility, or, given ``book``, one scoped to exactly that book."""
    identity = {
        "rep_id": _REP,
        "book": book if book is not None else [],
        "full_visibility": book is None,
        "principal_type": "user",
        "session_id": "sess-audit",
        "source": "gateway",
        "tenant": tenant,
    }
    return identity_event(IDENTITY_KEY, identity, args)


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    assert result["statusCode"] == 200, result
    return dict(result["body"])


def _audits(store: Store, tenant: str = _TENANT) -> list[dict[str, Any]]:
    items, _ = store.query_tenant_scan("audit_records", tenant=tenant)
    return [dict(item) for item in items]


def _account(store: Store) -> dict[str, Any]:
    body = _ok(
        create_account.handler(_event({"name": "Acme", "transaction_key": _key()}), store=store)
    )
    return dict(body["account"])


def _opportunity(store: Store) -> dict[str, Any]:
    account = _account(store)
    args = {"account_id": account["account_id"], "name": "Deal", "transaction_key": _key()}
    return dict(_ok(create_opportunity.handler(_event(args), store=store))["opportunity"])


#: (table, key attribute, response key, a function returning the create's arguments).
_CREATES: list[tuple[str, str, str, Any, Callable[[Store], dict[str, Any]]]] = [
    ("accounts", "account_id", "account", create_account, lambda _s: {"name": "Acme"}),
    (
        "contacts",
        "contact_id",
        "contact",
        create_contact,
        lambda s: {"account_id": _account(s)["account_id"], "name": "Jane"},
    ),
    (
        "opportunities",
        "opportunity_id",
        "opportunity",
        create_opportunity,
        lambda s: {"account_id": _account(s)["account_id"], "name": "Deal"},
    ),
    (
        "notes",
        "note_id",
        "note",
        create_note,
        lambda s: {"opportunity_id": _opportunity(s)["opportunity_id"], "body": "hello"},
    ),
    (
        "activities",
        "activity_id",
        "activity",
        log_activity,
        lambda s: {
            "opportunity_id": _opportunity(s)["opportunity_id"],
            "kind": "call",
            "subject": "Intro",
        },
    ),
]


def _audits_for(store: Store, table: str, target_id: str) -> list[dict[str, Any]]:
    """The full audit records for one target, read by key after the tenant index finds them."""
    found = [
        a for a in _audits(store) if a["target_table"] == table and a["target_id"] == target_id
    ]
    return [dict(store.get_item("audit_records", {"audit_id": a["audit_id"]}) or {}) for a in found]


@pytest.mark.parametrize(("table", "key_attr", "field", "module", "args_for"), _CREATES)
def test_a_create_and_its_audit_record_persist_together(
    store: Store,
    table: str,
    key_attr: str,
    field: str,
    module: Any,
    args_for: Callable[[Store], dict[str, Any]],
) -> None:
    created = _ok(
        module.handler(_event({**args_for(store), "transaction_key": _key()}), store=store)
    )[field]
    persisted = store.get_item(table, {key_attr: created[key_attr]})
    assert persisted is not None

    (audit,) = _audits_for(store, table, created[key_attr])
    assert audit["actor_rep_id"] == _REP
    assert audit["tenant"] == _TENANT
    assert audit["action"] == "create"
    assert audit["before_snapshot"] == {}
    expected_after = {k: v for k, v in persisted.items() if k not in _IDEMPOTENCY_ATTRS}
    if table == "activities":
        expected_after = {k: v for k, v in created.items() if k not in _IDEMPOTENCY_ATTRS}
    assert audit["after_snapshot"] == expected_after


@pytest.mark.parametrize(("table", "key_attr", "field", "module", "args_for"), _CREATES[:4])
def test_a_replayed_create_writes_no_second_audit_record(
    store: Store,
    table: str,
    key_attr: str,
    field: str,
    module: Any,
    args_for: Callable[[Store], dict[str, Any]],
) -> None:
    event = _event({**args_for(store), "transaction_key": _key()})
    first = _ok(module.handler(event, store=store))[field]
    second = _ok(module.handler(event, store=store))[field]
    assert first[key_attr] == second[key_attr]
    assert len(_audits_for(store, table, first[key_attr])) == 1


class _LosesTheRace(Store):
    """A store whose first read of ``table`` misses, as if a concurrent writer committed after it."""

    def __init__(self, base: Store, table: str) -> None:
        super().__init__(table_prefix=_TABLE_PREFIX, client=base._client)  # noqa: SLF001
        self._table = table
        self._missed = False

    def get_item(self, table: str, key: Any, *, consistent: bool = False) -> Any:
        if table == self._table and not self._missed:
            self._missed = True
            return None
        return super().get_item(table, key, consistent=consistent)


def test_a_cancelled_create_transaction_leaves_neither_the_item_nor_an_audit(
    store: Store,
) -> None:
    """The conditional item ``Put`` fails on the real engine, cancelling the audit ``Put`` with it."""
    key = _key()
    original = _ok(
        create_account.handler(_event({"name": "Acme", "transaction_key": key}), store=store)
    )["account"]
    racer = _LosesTheRace(store, "accounts")
    result = create_account.handler(
        _event({"name": "A different name", "transaction_key": key}), store=racer
    )

    assert result["statusCode"] == 409, result
    unchanged = store.get_item("accounts", {"account_id": original["account_id"]})
    assert unchanged is not None and unchanged["name"] == "Acme"
    assert len(_audits_for(store, "accounts", original["account_id"])) == 1


def test_a_create_refused_before_its_transaction_writes_no_audit(store: Store) -> None:
    result = create_contact.handler(
        _event({"account_id": "acct-absent", "name": "Jane", "transaction_key": _key()}),
        store=store,
    )
    assert result["statusCode"] == 404, result
    assert _audits(store) == []


def test_the_create_audit_is_visible_in_tenant_scoped_audit_search_and_to_no_other_tenant(
    store: Store,
) -> None:
    account = _account(store)

    listed = _ok(list_audit_records.handler(_event({}), store=store))["audit_records"]
    assert [r["target_id"] for r in listed] == [account["account_id"]]
    targeted = _ok(
        list_audit_records.handler(_event({"target_id": account["account_id"]}), store=store)
    )["audit_records"]
    assert len(targeted) == 1

    other = _ok(list_audit_records.handler(_event({}, tenant=_OTHER_TENANT), store=store))
    assert other["audit_records"] == []


def test_a_create_that_loses_the_race_to_an_identical_create_returns_its_item(
    store: Store,
) -> None:
    """A concurrent identical create cancels the loser's transaction; the loser returns the winner's
    item and no second audit record exists."""
    event = _event({"name": "Acme", "transaction_key": _key()})
    winner = _ok(create_account.handler(event, store=store))["account"]
    loser = _ok(create_account.handler(event, store=_LosesTheRace(store, "accounts")))["account"]

    assert {k: v for k, v in loser.items() if k not in _IDEMPOTENCY_ATTRS} == winner
    assert len(_audits_for(store, "accounts", winner["account_id"])) == 1


def _revert(store: Store, audit_id: str) -> dict[str, Any]:
    return dict(
        revert_audit_record.handler(
            _event({"audit_id": audit_id, "transaction_key": _key()}), store=store
        )
    )


@pytest.mark.parametrize(("table", "key_attr", "field", "module", "args_for"), _CREATES[:4])
def test_reverting_a_create_audit_deletes_the_created_item(
    store: Store,
    table: str,
    key_attr: str,
    field: str,
    module: Any,
    args_for: Callable[[Store], dict[str, Any]],
) -> None:
    created = _ok(
        module.handler(_event({**args_for(store), "transaction_key": _key()}), store=store)
    )[field]
    (audit,) = _audits_for(store, table, created[key_attr])

    _ok(_revert(store, audit["audit_id"]))

    assert store.get_item(table, {key_attr: created[key_attr]}) is None
    (reverted,) = _audits_for(store, table, created[key_attr])
    assert reverted["status"] == "reversed"


def test_log_activity_audits_the_opportunity_touch_so_reverting_newest_first_undoes_everything(
    store: Store,
) -> None:
    """Journey teardown order: revert every audit record newest-first, ending with the creates."""
    account = _account(store)
    opp_args = {"account_id": account["account_id"], "name": "Deal", "transaction_key": _key()}
    opportunity = _ok(create_opportunity.handler(_event(opp_args), store=store))["opportunity"]
    before_touch = store.get_item(
        "opportunities", {"opportunity_id": opportunity["opportunity_id"]}
    )
    args = {
        "opportunity_id": opportunity["opportunity_id"],
        "kind": "call",
        "subject": "Intro",
        "occurred_at": "2026-10-01T09:00:00+00:00",
        "transaction_key": _key(),
    }
    activity = _ok(log_activity.handler(_event(args), store=store))["activity"]

    (touch,) = [
        a
        for a in _audits_for(store, "opportunities", opportunity["opportunity_id"])
        if a["action"] == "update"
    ]
    assert touch["before_snapshot"] == before_touch
    assert touch["after_snapshot"]["last_touch"] == "2026-10-01T09:00:00+00:00"

    (activity_audit,) = _audits_for(store, "activities", activity["activity_id"])
    _ok(_revert(store, activity_audit["audit_id"]))
    _ok(_revert(store, touch["audit_id"]))
    assert store.get_item("activities", {"activity_id": activity["activity_id"]}) is None
    assert (
        store.get_item("opportunities", {"opportunity_id": opportunity["opportunity_id"]})
        == before_touch
    )

    (opp_create,) = [
        a
        for a in _audits_for(store, "opportunities", opportunity["opportunity_id"])
        if a["action"] == "create"
    ]
    _ok(_revert(store, opp_create["audit_id"]))
    (account_create,) = _audits_for(store, "accounts", account["account_id"])
    _ok(_revert(store, account_create["audit_id"]))
    assert (
        store.get_item("opportunities", {"opportunity_id": opportunity["opportunity_id"]}) is None
    )
    assert store.get_item("accounts", {"account_id": account["account_id"]}) is None


class _StaleOpportunity(Store):
    """A store whose opportunity read returns an older ``updated_at`` than the table holds."""

    def __init__(self, base: Store) -> None:
        super().__init__(table_prefix=_TABLE_PREFIX, client=base._client)  # noqa: SLF001

    def get_item(self, table: str, key: Any, *, consistent: bool = False) -> Any:
        item = super().get_item(table, key, consistent=consistent)
        if table == "opportunities" and item is not None:
            return {**item, "updated_at": "2000-01-01T00:00:00+00:00"}
        return item


def test_log_activity_refuses_when_the_opportunity_changed_since_it_was_read(
    store: Store,
) -> None:
    """A stale read would make the update record's before-snapshot wrong, so nothing is written."""
    opportunity = _opportunity(store)
    args = {
        "opportunity_id": opportunity["opportunity_id"],
        "kind": "call",
        "subject": "Intro",
        "transaction_key": _key(),
    }
    result = log_activity.handler(_event(args), store=_StaleOpportunity(store))

    assert result["statusCode"] == 409, result
    assert [a for a in _audits(store) if a["target_table"] == "activities"] == []
    assert [
        a
        for a in _audits_for(store, "opportunities", opportunity["opportunity_id"])
        if a["action"] == "update"
    ] == []


# -- revert fails closed without scope, note scope, child check ---------------------------


def _note(store: Store) -> tuple[dict[str, Any], dict[str, Any]]:
    opportunity = _opportunity(store)
    args = {
        "opportunity_id": opportunity["opportunity_id"],
        "body": "hi",
        "transaction_key": _key(),
    }
    return opportunity, dict(_ok(create_note.handler(_event(args), store=store))["note"])


def test_a_note_create_audit_carries_its_opportunitys_account_scope(store: Store) -> None:
    opportunity, note = _note(store)
    (audit,) = _audits_for(store, "notes", note["note_id"])
    assert audit["account_id"] == opportunity["account_id"]

    in_book = _event({}, book=[opportunity["account_id"]])
    listed = _ok(list_audit_records.handler(in_book, store=store))["audit_records"]
    assert note["note_id"] in {r["target_id"] for r in listed}


def test_an_out_of_book_note_revert_is_denied_and_the_note_survives(store: Store) -> None:
    """The reviewer's measured scenario: a rep whose book excludes the account cannot revert."""
    _opportunity_row, note = _note(store)
    (audit,) = _audits_for(store, "notes", note["note_id"])

    result = revert_audit_record.handler(
        _event({"audit_id": audit["audit_id"], "transaction_key": _key()}, book=["acct-elsewhere"]),
        store=store,
    )

    assert result["statusCode"] == 403, result
    assert result["error"]["code"] == "scope_denied"
    assert store.get_item("notes", {"note_id": note["note_id"]}) is not None


def test_an_in_book_note_revert_deletes_the_note(store: Store) -> None:
    opportunity, note = _note(store)
    (audit,) = _audits_for(store, "notes", note["note_id"])
    result = revert_audit_record.handler(
        _event(
            {"audit_id": audit["audit_id"], "transaction_key": _key()},
            book=[opportunity["account_id"]],
        ),
        store=store,
    )
    _ok(result)
    assert store.get_item("notes", {"note_id": note["note_id"]}) is None


def test_a_revert_of_an_audit_record_with_no_account_scope_fails_closed(store: Store) -> None:
    """Even a full-visibility caller is refused when no scope is resolvable; nothing is deleted."""
    _opportunity_row, note = _note(store)
    (audit,) = _audits_for(store, "notes", note["note_id"])
    unscoped = {k: v for k, v in audit.items() if k != "account_id"}
    store.put_item("audit_records", unscoped)

    result = _revert(store, audit["audit_id"])

    assert result["statusCode"] == 403, result
    assert result["error"]["code"] == "scope_denied"
    assert store.get_item("notes", {"note_id": note["note_id"]}) is not None
    record = store.get_item("audit_records", {"audit_id": audit["audit_id"]})
    assert record is not None and record["status"] == "active"


def test_an_account_create_revert_with_a_child_opportunity_is_a_conflict(store: Store) -> None:
    """The reviewer's measured scenario: the account would be deleted under a live opportunity."""
    opportunity = _opportunity(store)
    (account_create,) = _audits_for(store, "accounts", opportunity["account_id"])

    result = _revert(store, account_create["audit_id"])

    assert result["statusCode"] == 409, result
    assert "opportunities" in result["error"]["message"]
    assert store.get_item("accounts", {"account_id": opportunity["account_id"]}) is not None
    (still,) = _audits_for(store, "accounts", opportunity["account_id"])
    assert still["status"] == "active"


def test_an_account_create_revert_with_a_child_contact_is_a_conflict(store: Store) -> None:
    account = _account(store)
    args = {"account_id": account["account_id"], "name": "Jane", "transaction_key": _key()}
    _ok(create_contact.handler(_event(args), store=store))
    (account_create,) = _audits_for(store, "accounts", account["account_id"])

    result = _revert(store, account_create["audit_id"])

    assert result["statusCode"] == 409, result
    assert "contacts" in result["error"]["message"]


@pytest.mark.parametrize("child", ["notes", "activities"])
def test_an_opportunity_create_revert_with_a_child_is_a_conflict(store: Store, child: str) -> None:
    opportunity = _opportunity(store)
    if child == "notes":
        args = {
            "opportunity_id": opportunity["opportunity_id"],
            "body": "hi",
            "transaction_key": _key(),
        }
        _ok(create_note.handler(_event(args), store=store))
    else:
        args = {
            "opportunity_id": opportunity["opportunity_id"],
            "kind": "call",
            "subject": "Intro",
            "transaction_key": _key(),
        }
        _ok(log_activity.handler(_event(args), store=store))
    (opp_create,) = [
        a
        for a in _audits_for(store, "opportunities", opportunity["opportunity_id"])
        if a["action"] == "create"
    ]

    result = _revert(store, opp_create["audit_id"])

    assert result["statusCode"] == 409, result
    assert child in result["error"]["message"]
    assert store.get_item("opportunities", {"opportunity_id": opportunity["opportunity_id"]})


def _contact(store: Store, account_id: str) -> dict[str, Any]:
    args = {"account_id": account_id, "name": "Jane", "transaction_key": _key()}
    return dict(_ok(create_contact.handler(_event(args), store=store))["contact"])


def test_a_contact_create_revert_while_an_opportunity_names_it_primary_is_a_conflict(
    store: Store,
) -> None:
    """A measured case: the contact would vanish under its opportunity."""
    account = _account(store)
    contact = _contact(store, account["account_id"])
    opp_args = {
        "account_id": account["account_id"],
        "name": "Deal",
        "primary_contact_id": contact["contact_id"],
        "transaction_key": _key(),
    }
    _ok(create_opportunity.handler(_event(opp_args), store=store))
    (contact_create,) = _audits_for(store, "contacts", contact["contact_id"])

    result = _revert(store, contact_create["audit_id"])

    assert result["statusCode"] == 409, result
    assert "opportunities" in result["error"]["message"]
    assert store.get_item("contacts", {"contact_id": contact["contact_id"]}) is not None


def test_a_contact_no_opportunity_names_still_reverts_beside_other_opportunities(
    store: Store,
) -> None:
    """An account opportunity naming a different contact (or none) does not block the revert."""
    account = _account(store)
    contact = _contact(store, account["account_id"])
    other = _contact(store, account["account_id"])
    for primary in (other["contact_id"], None):
        args: dict[str, Any] = {
            "account_id": account["account_id"],
            "name": "Deal",
            "transaction_key": _key(),
        }
        if primary is not None:
            args["primary_contact_id"] = primary
        _ok(create_opportunity.handler(_event(args), store=store))
    (contact_create,) = _audits_for(store, "contacts", contact["contact_id"])

    _ok(_revert(store, contact_create["audit_id"]))

    assert store.get_item("contacts", {"contact_id": contact["contact_id"]}) is None


def test_a_note_revert_that_would_put_a_before_snapshot_is_refused(store: Store) -> None:
    """Note reverts are delete-only: a record carrying a before-snapshot is refused, note unchanged."""
    _opportunity_row, note = _note(store)
    (audit,) = _audits_for(store, "notes", note["note_id"])
    store.put_item(
        "audit_records",
        {
            **audit,
            "before_snapshot": {**audit["after_snapshot"], "body": "earlier"},
            "action": "update",
        },
    )

    result = _revert(store, audit["audit_id"])

    assert result["statusCode"] == 409, result
    assert "deleting a created note" in result["error"]["message"]
    persisted = store.get_item("notes", {"note_id": note["note_id"]})
    assert persisted is not None and persisted["body"] == "hi"

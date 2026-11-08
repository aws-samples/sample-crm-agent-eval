# SPDX-License-Identifier: MIT
"""Caller-bound cursors and scope-filtered search metadata, on DynamoDB Local.

Paging every cursor-issuing operation to exhaustion returns each in-scope row exactly once and no
out-of-book row; ``examined_count`` never counts out-of-book rows, and below the examination
budget they never cause ``truncated``; and a cursor replayed by another rep is refused as a
``cursor`` validation failure.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from crm import search
from crm.authz import Identity
from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import list_activities, list_audit_records, list_proposals
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from tests.integration.conftest import _drop_prefixed_tables

_TENANT = "eval-cursor-scope"
_REP = "rep-cursor"
#: Unique per process, so concurrent xdist workers never share a table.
_TABLE_PREFIX = f"int-cursor-scope-{uuid.uuid4().hex[:12]}-"
_IN_BOOK = tuple(f"acct-in-{n}" for n in range(6))
_OUT_OF_BOOK = tuple(f"acct-out-{n}" for n in range(7))
_ACTIVITY_OPPORTUNITY = "opp-acct-in-0"
_ACTIVITIES = 7
_PAGE = 3


@pytest.fixture
def store(dynamodb_local_client: Any) -> Iterator[Store]:
    """A prefixed, seeded :class:`Store` on DynamoDB Local, dropped after each test."""
    create_all_tables(dynamodb_local_client, table_prefix=_TABLE_PREFIX)
    try:
        seeded = Store(table_prefix=_TABLE_PREFIX, client=dynamodb_local_client)
        _seed(seeded)
        yield seeded
    finally:
        _drop_prefixed_tables(dynamodb_local_client, _TABLE_PREFIX)


@pytest.fixture
def small_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink every page and the search budget so a dozen rows span several cursors."""
    monkeypatch.setattr(search, "EXAMINATION_BUDGET", _PAGE)
    monkeypatch.setattr(search, "_PAGE_SIZE", _PAGE)
    for module in (list_activities, list_audit_records, list_proposals):
        monkeypatch.setattr(module, "_DEFAULT_LIMIT", _PAGE)


def _seed(store: Store) -> None:
    for n, account_id in enumerate(sorted(_IN_BOOK + _OUT_OF_BOOK)):
        created_at = f"2026-11-01T00:00:{n:02d}+00:00"
        opportunity_id = f"opp-{account_id}"
        store.put_item(
            "accounts",
            {
                "account_id": account_id,
                "tenant": _TENANT,
                "name": f"Account {account_id}",
                "owner_rep_id": _REP,
                "created_at": created_at,
            },
        )
        store.put_item(
            "opportunities",
            {
                "opportunity_id": opportunity_id,
                "tenant": _TENANT,
                "account_id": account_id,
                "owner_rep_id": _REP,
                "stage": "Discovery",
                "close_date": "2026-12-01",
                "value": 100,
                "name": "Renewal",
                "created_at": created_at,
            },
        )
        store.put_item(
            "audit_records",
            {
                "audit_id": f"aud-{account_id}",
                "tenant": _TENANT,
                "account_id": account_id,
                "target_table": "opportunities",
                "target_id": opportunity_id,
                "actor_rep_id": _REP,
                "status": "active",
                "created_at": created_at,
            },
        )
        store.put_item(
            "proposals",
            {
                "proposal_id": f"prop-{account_id}",
                "tenant": _TENANT,
                "status": "pending",
                "actor_rep_id": _REP,
                "target_table": "opportunities",
                "target_id": opportunity_id,
                "created_at": created_at,
                "before_snapshot": {"tenant": _TENANT, "account_id": account_id},
            },
        )
    for n in range(_ACTIVITIES):
        store.put_item(
            "activities",
            {
                "activity_id": f"act-{n:02d}",
                "tenant": _TENANT,
                "opportunity_id": _ACTIVITY_OPPORTUNITY,
                "account_id": "acct-in-0",
                "kind": "call",
                "subject": f"call {n}",
                "occurred_at": f"2026-11-02T00:00:{n:02d}+00:00",
                "created_at": f"2026-11-02T00:00:{n:02d}+00:00",
            },
        )


def _identity(*, rep_id: str = _REP) -> Identity:
    return Identity(rep_id=rep_id, book=frozenset(_IN_BOOK), tenant=_TENANT)


def _identity_payload(*, rep_id: str = _REP) -> dict[str, Any]:
    return {
        "rep_id": rep_id,
        "book": list(_IN_BOOK),
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-cursor-scope",
        "source": "gateway",
        "tenant": _TENANT,
    }


def _call(
    handler: Callable[..., dict[str, Any]],
    store: Store,
    arguments: dict[str, Any],
    *,
    rep_id: str = _REP,
) -> dict[str, Any]:
    event = identity_event(IDENTITY_KEY, _identity_payload(rep_id=rep_id), arguments)
    return handler(event, store=store)


def _page_all(
    handler: Callable[..., dict[str, Any]],
    store: Store,
    arguments: dict[str, Any],
    rows_key: str,
    row_id: Callable[[dict[str, Any]], str],
) -> tuple[list[str], int]:
    """Follow ``next_cursor`` to exhaustion; return every row id seen and the page count."""
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        args = dict(arguments) if cursor is None else {**arguments, "cursor": cursor}
        result = _call(handler, store, args)
        assert result["statusCode"] == 200, result
        pages += 1
        seen.extend(row_id(row) for row in result["body"][rows_key])
        cursor = result["body"]["next_cursor"]
        if cursor is None:
            return seen, pages
        assert pages < 50, "paging did not terminate"


def test_search_opportunities_pages_every_in_scope_row_once_and_counts_only_in_scope(
    store: Store, small_pages: None
) -> None:
    seen: list[str] = []
    examined = 0
    cursor: str | None = None
    pages = 0
    while True:
        result = search.search_opportunities(store, _identity(), cursor=cursor)
        pages += 1
        seen.extend(str(item["account_id"]) for item in result.items)
        examined += result.examined_count
        cursor = result.next_cursor
        if cursor is None:
            break
        assert pages < 50, "paging did not terminate"

    assert sorted(seen) == sorted(_IN_BOOK)
    assert examined == len(_IN_BOOK)
    assert pages > 1


def test_search_accounts_pages_every_in_scope_row_once(store: Store, small_pages: None) -> None:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(50):
        result = search.search_accounts(store, _identity(), cursor=cursor)
        seen.extend(str(item["account_id"]) for item in result.items)
        cursor = result.next_cursor
        if cursor is None:
            break

    assert cursor is None
    assert sorted(seen) == sorted(_IN_BOOK)


@pytest.mark.parametrize(
    ("handler", "arguments", "rows_key", "row_id", "expected"),
    [
        (
            list_audit_records.handler,
            {},
            "audit_records",
            lambda row: str(row["account_id"]),
            sorted(_IN_BOOK),
        ),
        (
            list_proposals.handler,
            {},
            "proposals",
            lambda row: str(row["before_snapshot"]["account_id"]),
            sorted(_IN_BOOK),
        ),
        (
            list_activities.handler,
            {"opportunity_id": _ACTIVITY_OPPORTUNITY},
            "activities",
            lambda row: str(row["activity_id"]),
            [f"act-{n:02d}" for n in range(_ACTIVITIES)],
        ),
    ],
    ids=["list_audit_records", "list_proposals", "list_activities"],
)
def test_each_listing_pages_every_in_scope_row_exactly_once(
    store: Store,
    small_pages: None,
    handler: Callable[..., dict[str, Any]],
    arguments: dict[str, Any],
    rows_key: str,
    row_id: Callable[[dict[str, Any]], str],
    expected: list[str],
) -> None:
    seen, pages = _page_all(handler, store, arguments, rows_key, row_id)

    assert sorted(seen) == expected
    assert len(seen) == len(set(seen))
    assert pages > 1


def test_out_of_book_rows_below_the_budget_never_move_examined_count_or_truncated(
    store: Store,
) -> None:
    outsider = Identity(rep_id=_REP, book=frozenset({"acct-elsewhere"}), tenant=_TENANT)

    for result in (
        search.search_opportunities(store, outsider, limit=1),
        search.search_accounts(store, outsider, limit=1),
    ):
        assert result.items == []
        assert result.examined_count == 0
        assert result.truncated is False
        assert result.next_cursor is None


@pytest.mark.parametrize(
    ("handler", "arguments"),
    [
        (list_audit_records.handler, {}),
        (list_proposals.handler, {}),
        (list_activities.handler, {"opportunity_id": _ACTIVITY_OPPORTUNITY}),
    ],
    ids=["list_audit_records", "list_proposals", "list_activities"],
)
def test_a_listing_cursor_replayed_by_another_rep_is_a_cursor_validation_failure(
    store: Store,
    small_pages: None,
    handler: Callable[..., dict[str, Any]],
    arguments: dict[str, Any],
) -> None:
    first = _call(handler, store, arguments)
    cursor = first["body"]["next_cursor"]
    assert cursor is not None

    replayed = _call(handler, store, {**arguments, "cursor": cursor}, rep_id="rep-other")

    assert replayed["statusCode"] == 400, replayed
    assert replayed["error"]["code"] == "validation_failed"
    assert replayed["error"]["source"] == "cursor"

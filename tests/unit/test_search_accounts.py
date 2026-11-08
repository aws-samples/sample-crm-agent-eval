# SPDX-License-Identifier: MIT
"""Tests for crm.search.search_accounts: scope-constrained account search and the account-index
projection-vs-query contract.

Runs against moto through the real Store and create_all_tables, so GSI projections are enforced.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from crm.authz import Identity
from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import ValidationFailed
from crm.handlers.search_accounts import handler as search_accounts_handler
from crm.search import (
    _ACCOUNT_GSI_OWNER,
    _ACCOUNT_GSI_TENANT,
    EXAMINATION_BUDGET,
    _account_index_for,
    search_accounts,
)
from crm.store.keys import name_lower, tenant_prefixed
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_OWNER = "rep-1"


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    """A moto-mocked DynamoDB client — no credentials, no network, no cost."""
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


@pytest.fixture
def identity() -> Identity:
    return Identity(rep_id=_OWNER, full_visibility=True, tenant=_TENANT)


def _seed_account(
    store: Store,
    *,
    account_id: str,
    tenant: str = _TENANT,
    owner_rep_id: str = _OWNER,
    name: str = "Cascadia Group",
) -> None:
    store.put_item(
        "accounts",
        {
            "account_id": account_id,
            "tenant": tenant,
            "owner_rep_id": owner_rep_id,
            "name": name,
            "name_lower": name_lower(name),
            "created_at": "2026-01-01T00:00:00Z",
            "tenant_owner_rep_id": tenant_prefixed(tenant, owner_rep_id),
        },
    )


def _seed_many(store: Store, count: int, **kwargs: object) -> None:
    for i in range(count):
        _seed_account(store, account_id=f"acct-{i:05d}", **kwargs)  # type: ignore[arg-type]


# -- index selection -----------------------------------------------------------------------------


def test_account_index_selection_routes_owner_to_gsi_owner_and_otherwise_gsi_tenant() -> None:
    assert _account_index_for(owner_rep_id="rep-9") == _ACCOUNT_GSI_OWNER
    assert _account_index_for(owner_rep_id=None) == _ACCOUNT_GSI_TENANT


# -- basic contract ------------------------------------------------------------------------------


def test_finds_a_matching_account_by_free_text(store: Store, identity: Identity) -> None:
    _seed_account(store, account_id="acct-1", name="Cascadia Group")
    _seed_account(store, account_id="acct-2", name="Globex Holdings")

    result = search_accounts(store, identity, query="cascadia")

    assert [item["account_id"] for item in result.items] == ["acct-1"]
    assert result.truncated is False
    assert result.next_cursor is None


def test_query_matching_requires_every_token(store: Store, identity: Identity) -> None:
    _seed_account(store, account_id="acct-1", name="Cascadia Group Holdings")
    _seed_account(store, account_id="acct-2", name="Cascadia Other")

    result = search_accounts(store, identity, query="cascadia group")

    assert [item["account_id"] for item in result.items] == ["acct-1"]


def test_owner_filter_narrows_the_result(store: Store, identity: Identity) -> None:
    _seed_account(store, account_id="acct-1", owner_rep_id="rep-1")
    _seed_account(store, account_id="acct-2", owner_rep_id="rep-2")

    result = search_accounts(store, identity, owner_rep_id="rep-2")

    assert [item["account_id"] for item in result.items] == ["acct-2"]


def test_owner_filtered_free_text_matches_via_the_projected_name(
    store: Store, identity: Identity
) -> None:
    """An owner-filtered free-text search matches on ``name``, which ``gsi_owner`` projects
    (``name_lower`` is not projected on that index)."""
    _seed_account(store, account_id="acct-1", owner_rep_id="rep-7", name="Zephyr Holdings")

    result = search_accounts(store, identity, owner_rep_id="rep-7", query="zephyr")

    assert [item["account_id"] for item in result.items] == ["acct-1"]


def test_no_match_returns_an_empty_untruncated_result(store: Store, identity: Identity) -> None:
    _seed_account(store, account_id="acct-1", name="Cascadia Group")

    result = search_accounts(store, identity, query="nonexistent")

    assert result.items == []
    assert result.truncated is False
    assert result.examined_count == 1
    assert result.next_cursor is None


# -- scope enforcement shares crm.authz's predicate ----------------------------------------------


def test_out_of_book_accounts_are_dropped_from_results(store: Store) -> None:
    """Accounts outside the caller's book are dropped from the results."""
    _seed_account(store, account_id="acct-in-book")
    _seed_account(store, account_id="acct-out-of-book")
    scoped_identity = Identity(rep_id="rep-1", book=frozenset({"acct-in-book"}), tenant=_TENANT)

    result = search_accounts(store, scoped_identity)

    assert [item["account_id"] for item in result.items] == ["acct-in-book"]


def test_cross_tenant_accounts_never_appear(store: Store, identity: Identity) -> None:
    _seed_account(store, account_id="acct-1", tenant=_TENANT)
    _seed_account(store, account_id="acct-2", tenant="eval-other-run")

    result = search_accounts(store, identity)

    assert [item["account_id"] for item in result.items] == ["acct-1"]


# -- the examination-budget ceiling (shared with search_opportunities) ---------------------------


def test_examined_count_and_truncated_are_present_on_every_result(
    store: Store, identity: Identity
) -> None:
    _seed_account(store, account_id="acct-1")

    result = search_accounts(store, identity)

    assert result.truncated is False
    assert result.examined_count == 1
    assert result.next_cursor is None


def test_a_result_set_exceeding_the_budget_cannot_come_back_looking_complete(
    store: Store, identity: Identity
) -> None:
    total = EXAMINATION_BUDGET + 50
    _seed_many(store, total, name="Cascadia Group")

    result = search_accounts(store, identity, query="cascadia")

    assert result.truncated is True
    assert result.examined_count == EXAMINATION_BUDGET
    assert len(result.items) < total
    assert result.next_cursor is not None


def test_next_cursor_resumes_examination_past_the_first_truncated_call(
    store: Store, identity: Identity
) -> None:
    total = EXAMINATION_BUDGET + 10
    _seed_many(store, total, name="Cascadia Group")

    first = search_accounts(store, identity, query="cascadia")
    assert first.truncated is True
    assert first.next_cursor is not None

    second = search_accounts(store, identity, query="cascadia", cursor=first.next_cursor)

    assert second.truncated is False
    assert second.examined_count == 10
    first_ids = {item["account_id"] for item in first.items}
    second_ids = {item["account_id"] for item in second.items}
    assert first_ids.isdisjoint(second_ids)


def test_limit_reached_before_index_exhausted_still_marks_truncated(
    store: Store, identity: Identity
) -> None:
    _seed_many(store, 20, name="Cascadia Group")

    result = search_accounts(store, identity, query="cascadia", limit=5)

    assert len(result.items) == 5
    assert result.truncated is True


# -- handler wire shape --------------------------------------------------------------------------


def test_handler_returns_the_off_proxy_result_shape(store: Store) -> None:
    """The thin adapter returns ``{statusCode, body:{items, truncated, examined_count, next_cursor}}``."""
    _seed_account(store, account_id="acct-1", name="Cascadia Group")
    payload = {
        "rep_id": _OWNER,
        "book": [],
        "full_visibility": True,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": _TENANT,
    }
    event = identity_event(IDENTITY_KEY, payload, {"query": "cascadia"})

    response = search_accounts_handler(event, store=store)

    assert response["statusCode"] == 200
    body = response["body"]
    assert set(body) == {"items", "truncated", "examined_count", "next_cursor"}
    assert [item["account_id"] for item in body["items"]] == ["acct-1"]
    assert body["truncated"] is False


def test_an_empty_owner_filter_is_a_validation_error_not_a_crash(store: Store) -> None:
    """An empty owner_rep_id is refused before any query, naming the field."""
    identity = Identity(rep_id="rep-1", full_visibility=True, tenant="demo")
    with pytest.raises(ValidationFailed) as raised:
        search_accounts(store, identity, owner_rep_id="")
    assert raised.value.source == "owner_rep_id"


def test_out_of_book_accounts_below_the_budget_are_neither_counted_nor_cause_truncation(
    store: Store,
) -> None:
    for index in range(5):
        _seed_account(store, account_id=f"acct-out-{index}")
    scoped = Identity(rep_id=_OWNER, book=frozenset({"acct-in-book"}), tenant=_TENANT)

    result = search_accounts(store, scoped, limit=1)

    assert result.items == []
    assert result.examined_count == 0
    assert result.truncated is False
    assert result.next_cursor is None


def test_a_search_accounts_cursor_replayed_by_another_rep_is_refused(
    store: Store, identity: Identity
) -> None:
    _seed_many(store, EXAMINATION_BUDGET + 10)
    first = search_accounts(store, identity, query="cascadia")
    assert first.next_cursor is not None
    other = Identity(rep_id="rep-2", full_visibility=True, tenant=_TENANT)

    with pytest.raises(ValidationFailed) as refused:
        search_accounts(store, other, query="cascadia", cursor=first.next_cursor)

    assert refused.value.source == "cursor"

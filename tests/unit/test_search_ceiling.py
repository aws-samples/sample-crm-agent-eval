# SPDX-License-Identifier: MIT
"""Tests for crm.search: scope-constrained GSI search and the examination-budget ceiling.

Runs against moto so budget crossing exercises the real `query_gsi` pagination.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from crm.authz import Identity
from crm.errors import ScopeDenied, ValidationFailed
from crm.search import EXAMINATION_BUDGET, decode_cursor, encode_cursor, search_opportunities
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_ACCOUNT = "acct-1"
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


def _seed_opportunity(
    store: Store,
    *,
    opportunity_id: str,
    tenant: str = _TENANT,
    account_id: str = _ACCOUNT,
    owner_rep_id: str = _OWNER,
    stage: str = "Discovery",
    name: str = "Acme Renewal",
) -> None:
    from crm.store.keys import stage_close_date, tenant_prefixed

    store.put_item(
        "opportunities",
        {
            "opportunity_id": opportunity_id,
            "tenant": tenant,
            "account_id": account_id,
            "owner_rep_id": owner_rep_id,
            "stage": stage,
            "close_date": "2026-01-01",
            "value": 1000,
            "name": name,
            "name_lower": name.lower(),
            "created_at": "2026-01-01T00:00:00Z",
            "tenant_account_id": tenant_prefixed(tenant, account_id),
            "tenant_owner_rep_id": tenant_prefixed(tenant, owner_rep_id),
            "tenant_stage": tenant_prefixed(tenant, stage),
            "stage_close_date": stage_close_date(stage, "2026-01-01"),
        },
    )


def _seed_many(store: Store, count: int, **kwargs: object) -> None:
    for i in range(count):
        _seed_opportunity(store, opportunity_id=f"opp-{i:05d}", **kwargs)  # type: ignore[arg-type]


# -- basic contract ------------------------------------------------------------------------------


def test_finds_a_matching_opportunity_by_free_text(store: Store, identity: Identity) -> None:
    _seed_opportunity(store, opportunity_id="opp-1", name="Acme Renewal")
    _seed_opportunity(store, opportunity_id="opp-2", name="Globex Expansion")

    result = search_opportunities(store, identity, query="acme")

    assert [item["opportunity_id"] for item in result.items] == ["opp-1"]
    assert result.truncated is False
    assert result.next_cursor is None


def test_query_matching_requires_every_token(store: Store, identity: Identity) -> None:
    _seed_opportunity(store, opportunity_id="opp-1", name="Acme Renewal Deal")
    _seed_opportunity(store, opportunity_id="opp-2", name="Acme Other")

    result = search_opportunities(store, identity, query="acme renewal")

    assert [item["opportunity_id"] for item in result.items] == ["opp-1"]


def test_stage_filter_narrows_the_index_and_the_result(store: Store, identity: Identity) -> None:
    _seed_opportunity(store, opportunity_id="opp-1", stage="Discovery")
    _seed_opportunity(store, opportunity_id="opp-2", stage="Negotiation")

    result = search_opportunities(store, identity, stage="Negotiation")

    assert [item["opportunity_id"] for item in result.items] == ["opp-2"]


def test_owner_filter_narrows_the_index_and_the_result(store: Store, identity: Identity) -> None:
    _seed_opportunity(store, opportunity_id="opp-1", owner_rep_id="rep-1")
    _seed_opportunity(store, opportunity_id="opp-2", owner_rep_id="rep-2")

    result = search_opportunities(store, identity, owner_rep_id="rep-2")

    assert [item["opportunity_id"] for item in result.items] == ["opp-2"]


def test_no_match_returns_an_empty_untruncated_result(store: Store, identity: Identity) -> None:
    _seed_opportunity(store, opportunity_id="opp-1", name="Acme Renewal")

    result = search_opportunities(store, identity, query="nonexistent")

    assert result.items == []
    assert result.truncated is False
    assert result.examined_count == 1
    assert result.next_cursor is None


# -- scope enforcement shares crm.authz's predicate ----------------------------------------------


def test_out_of_book_opportunities_are_dropped_from_results(store: Store) -> None:
    _seed_opportunity(store, opportunity_id="opp-1", account_id="acct-in-book")
    _seed_opportunity(store, opportunity_id="opp-2", account_id="acct-out-of-book")
    scoped_identity = Identity(rep_id="rep-1", book=frozenset({"acct-in-book"}), tenant=_TENANT)

    result = search_opportunities(store, scoped_identity)

    assert [item["opportunity_id"] for item in result.items] == ["opp-1"]


def test_cross_tenant_opportunities_never_appear(store: Store, identity: Identity) -> None:
    _seed_opportunity(store, opportunity_id="opp-1", tenant=_TENANT)
    _seed_opportunity(store, opportunity_id="opp-2", tenant="eval-other-run")

    result = search_opportunities(store, identity)

    assert [item["opportunity_id"] for item in result.items] == ["opp-1"]


# -- the examination-budget ceiling: truncation is never silent ---------------------------------


def test_examined_count_and_truncated_are_present_on_every_result(
    store: Store, identity: Identity
) -> None:
    """A caller cannot receive a result missing the signal needed to detect truncation."""
    _seed_opportunity(store, opportunity_id="opp-1")

    result = search_opportunities(store, identity)

    assert hasattr(result, "truncated")
    assert hasattr(result, "examined_count")
    assert hasattr(result, "next_cursor")
    assert result.truncated is False
    assert result.examined_count == 1


def test_a_result_set_exceeding_the_budget_cannot_come_back_looking_complete(
    store: Store, identity: Identity
) -> None:
    """More matching items than the budget yields a result marked truncated with a cursor."""
    total = EXAMINATION_BUDGET + 50
    _seed_many(store, total, name="Acme Renewal")

    result = search_opportunities(store, identity, query="acme")

    assert result.truncated is True
    assert result.examined_count == EXAMINATION_BUDGET
    assert len(result.items) <= EXAMINATION_BUDGET
    assert len(result.items) < total
    assert result.next_cursor is not None


def test_truncation_ceiling_examines_at_most_the_budget_even_far_over(
    store: Store, identity: Identity
) -> None:
    total = EXAMINATION_BUDGET + 500
    _seed_many(store, total)

    result = search_opportunities(store, identity)

    assert result.examined_count == EXAMINATION_BUDGET
    assert result.truncated is True


def test_next_cursor_resumes_examination_past_the_first_truncated_call(
    store: Store, identity: Identity
) -> None:
    total = EXAMINATION_BUDGET + 10
    _seed_many(store, total, name="Acme Renewal")

    first = search_opportunities(store, identity, query="acme")
    assert first.truncated is True
    assert first.next_cursor is not None

    second = search_opportunities(store, identity, query="acme", cursor=first.next_cursor)

    assert second.truncated is False
    assert second.examined_count == 10
    first_ids = {item["opportunity_id"] for item in first.items}
    second_ids = {item["opportunity_id"] for item in second.items}
    assert first_ids.isdisjoint(second_ids)


def test_resuming_with_a_cursor_and_no_further_matches_still_reports_a_full_examined_count(
    store: Store, identity: Identity
) -> None:
    """examined_count counts every item read even when none of them match."""
    total = EXAMINATION_BUDGET + 10
    _seed_many(store, total, name="Globex Expansion")
    first = search_opportunities(store, identity, query="acme")

    assert first.truncated is True
    assert first.examined_count == EXAMINATION_BUDGET
    assert first.items == []
    assert first.next_cursor is not None


def test_limit_reached_before_budget_still_marks_truncated_when_index_not_exhausted(
    store: Store, identity: Identity
) -> None:
    """More matches than ``limit`` marks the result truncated, even when the index is exhausted."""
    _seed_many(store, 20, name="Acme Renewal")

    result = search_opportunities(store, identity, query="acme", limit=5)

    assert len(result.items) == 5
    assert result.truncated is True


def test_limit_truncation_with_no_cursor_is_still_never_silent(
    store: Store, identity: Identity
) -> None:
    """A limit-truncated page from an exhausted index has ``next_cursor`` None and ``truncated``
    True."""
    _seed_many(store, 20, name="Acme Renewal")

    result = search_opportunities(store, identity, query="acme", limit=5)

    assert result.truncated is True
    assert result.next_cursor is None
    assert len(result.items) == 5


def test_limit_not_reached_and_index_exhausted_is_not_truncated(
    store: Store, identity: Identity
) -> None:
    _seed_many(store, 3, name="Acme Renewal")

    result = search_opportunities(store, identity, query="acme", limit=5)

    assert len(result.items) == 3
    assert result.truncated is False
    assert result.next_cursor is None


# -- cursor encoding ------------------------------------------------------------------------------


def test_encode_and_decode_cursor_round_trip() -> None:
    page_key: dict[str, object] = {
        "opportunity_id": {"S": "opp-1"},
        "tenant_stage": {"S": "demo#Discovery"},
    }

    token = encode_cursor(page_key)
    decoded = decode_cursor(token)

    assert decoded == page_key


def test_decode_cursor_rejects_a_malformed_token() -> None:
    with pytest.raises(ValidationFailed) as exc_info:
        decode_cursor("not-a-valid-cursor!!!")

    assert exc_info.value.source == "cursor"


def test_decode_cursor_rejects_a_token_that_decodes_to_a_non_mapping() -> None:
    import base64

    token = base64.urlsafe_b64encode(b'"just a string"').decode("ascii")

    with pytest.raises(ValidationFailed):
        decode_cursor(token)


def test_search_rejects_a_malformed_cursor_argument(store: Store, identity: Identity) -> None:
    with pytest.raises(ValidationFailed):
        search_opportunities(store, identity, cursor="garbage")


# -- the account filter and the returned fields -----------------------------


def _recording_index(store: Store, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the index name of every `query_gsi` call `store` receives."""
    used: list[str] = []
    real = store.query_gsi

    def record(table: str, index_name: str, **kwargs: object) -> object:
        used.append(index_name)
        return real(table, index_name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "query_gsi", record)
    return used


def test_account_filter_returns_only_that_accounts_opportunities_by_query(
    store: Store, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_opportunity(store, opportunity_id="opp-1", account_id="acct-a")
    _seed_opportunity(store, opportunity_id="opp-2", account_id="acct-b")
    _seed_opportunity(store, opportunity_id="opp-3", account_id="acct-a", stage="Negotiation")
    used = _recording_index(store, monkeypatch)

    result = search_opportunities(store, identity, account_id="acct-a")

    assert sorted(str(item["opportunity_id"]) for item in result.items) == ["opp-1", "opp-3"]
    assert set(used) == {"gsi_account"}
    assert result.examined_count == 2, "the account partition alone was read, not the tenant"


def test_account_filter_combines_with_stage_and_owner(store: Store, identity: Identity) -> None:
    _seed_opportunity(store, opportunity_id="opp-1", account_id="acct-a", stage="Discovery")
    _seed_opportunity(store, opportunity_id="opp-2", account_id="acct-a", stage="Negotiation")
    _seed_opportunity(store, opportunity_id="opp-3", account_id="acct-a", owner_rep_id="rep-2")

    staged = search_opportunities(store, identity, account_id="acct-a", stage="Negotiation")
    owned = search_opportunities(store, identity, account_id="acct-a", owner_rep_id="rep-2")

    assert [item["opportunity_id"] for item in staged.items] == ["opp-2"]
    assert [item["opportunity_id"] for item in owned.items] == ["opp-3"]


def test_an_account_outside_the_callers_book_is_denied_naming_the_account(store: Store) -> None:
    _seed_opportunity(store, opportunity_id="opp-1", account_id="acct-in-book")
    _seed_opportunity(store, opportunity_id="opp-2", account_id="acct-out-of-book")
    scoped = Identity(rep_id="rep-1", book=frozenset({"acct-in-book"}), tenant=_TENANT)

    with pytest.raises(ScopeDenied) as denied:
        search_opportunities(store, scoped, account_id="acct-out-of-book")

    assert denied.value.missing_scope == "account:acct-out-of-book"


def test_an_account_filter_never_crosses_a_tenant(store: Store, identity: Identity) -> None:
    _seed_opportunity(store, opportunity_id="opp-1", account_id="acct-a", tenant="eval-other-run")

    result = search_opportunities(store, identity, account_id="acct-a")

    assert result.items == [] and result.examined_count == 0


@pytest.mark.parametrize(
    "filters",
    [{}, {"stage": "Discovery"}, {"owner_rep_id": _OWNER}, {"account_id": _ACCOUNT}],
    ids=["tenant", "stage", "owner", "account"],
)
def test_every_search_path_returns_close_date_value_and_stage(
    store: Store, identity: Identity, filters: dict[str, str]
) -> None:
    _seed_opportunity(store, opportunity_id="opp-1")

    result = search_opportunities(store, identity, **filters)  # type: ignore[arg-type]

    (item,) = result.items
    assert item["close_date"] == "2026-01-01"
    assert item["stage"] == "Discovery"
    assert int(str(item["value"])) == 1000


def test_the_handler_forwards_account_id(store: Store, identity: Identity) -> None:
    from crm.handlers.search_opportunities import _search_opportunities

    _seed_opportunity(store, opportunity_id="opp-1", account_id="acct-a")
    _seed_opportunity(store, opportunity_id="opp-2", account_id="acct-b")

    result = _search_opportunities(store, identity, {"account_id": "acct-b"})

    assert [item["opportunity_id"] for item in result["items"]] == ["opp-2"]


@pytest.mark.parametrize("field", ["account_id", "owner_rep_id"])
def test_an_empty_filter_is_a_validation_error_not_a_crash(
    store: Store, identity: Identity, field: str
) -> None:
    """An empty account or owner filter is refused before any query, naming the field."""
    with pytest.raises(ValidationFailed) as raised:
        search_opportunities(store, identity, **{field: ""})  # type: ignore[arg-type]
    assert raised.value.source == field

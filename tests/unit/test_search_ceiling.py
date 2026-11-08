# SPDX-License-Identifier: MIT
"""Tests for crm.search: scope-constrained GSI search and the examination-budget ceiling.

Runs against moto so budget crossing exercises the real `query_gsi` pagination.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from crm.authz import Identity
from crm.cursor import bound_operation, decode_cursor, encode_cursor
from crm.errors import ScopeDenied, ValidationFailed
from crm.search import EXAMINATION_BUDGET, search_opportunities
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


def _seed_many_named(store: Store, count: int, *, prefix: str, name: str) -> None:
    for i in range(count):
        _seed_opportunity(store, opportunity_id=f"{prefix}-{i:05d}", name=name)


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


def test_a_limit_cut_on_an_exhausted_index_still_issues_a_cursor_to_the_withheld_matches(
    store: Store, identity: Identity
) -> None:
    """A limit-truncated page from an exhausted index is ``truncated`` and carries a cursor."""
    _seed_many(store, 20, name="Acme Renewal")

    result = search_opportunities(store, identity, query="acme", limit=5)

    assert result.truncated is True
    assert result.next_cursor is not None
    assert len(result.items) == 5


@pytest.mark.parametrize("limit", [1, 3, 7, 199, 201])
def test_limited_paging_returns_every_match_exactly_once(
    store: Store, identity: Identity, limit: int
) -> None:
    _seed_many(store, 230, name="Acme Renewal")
    _seed_many_named(store, 40, prefix="other", name="Globex Expansion")

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(300):
        result = search_opportunities(store, identity, query="acme", limit=limit, cursor=cursor)
        assert len(result.items) <= limit
        seen.extend(str(item["opportunity_id"]) for item in result.items)
        cursor = result.next_cursor
        if cursor is None:
            break

    assert cursor is None
    assert len(seen) == len(set(seen))
    assert sorted(seen) == [f"opp-{i:05d}" for i in range(230)]


def test_limited_paging_with_out_of_book_rows_returns_every_in_scope_match_once(
    store: Store,
) -> None:
    for index in range(12):
        account = _IN_BOOK if index % 3 == 0 else _OUT_OF_BOOK
        _seed_opportunity(store, opportunity_id=f"opp-mix-{index:02d}", account_id=account)
    scoped = Identity(rep_id=_OWNER, book=frozenset({_IN_BOOK}), tenant=_TENANT)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(50):
        result = search_opportunities(store, scoped, limit=1, cursor=cursor)
        seen.extend(str(item["opportunity_id"]) for item in result.items)
        cursor = result.next_cursor
        if cursor is None:
            break

    assert sorted(seen) == [f"opp-mix-{index:02d}" for index in range(0, 12, 3)]


def test_limit_not_reached_and_index_exhausted_is_not_truncated(
    store: Store, identity: Identity
) -> None:
    _seed_many(store, 3, name="Acme Renewal")

    result = search_opportunities(store, identity, query="acme", limit=5)

    assert len(result.items) == 3
    assert result.truncated is False
    assert result.next_cursor is None


# -- cursor binding -------------------------------------------------------------------------------


def test_search_rejects_a_malformed_cursor_argument(store: Store, identity: Identity) -> None:
    with pytest.raises(ValidationFailed) as refused:
        search_opportunities(store, identity, cursor="garbage")

    assert refused.value.source == "cursor"


def test_a_next_cursor_does_not_carry_the_page_key_in_the_clear(
    store: Store, identity: Identity
) -> None:
    _seed_many(store, EXAMINATION_BUDGET + 10, name="Acme Renewal")

    first = search_opportunities(store, identity, query="acme")

    assert first.next_cursor is not None
    decoded = decode_cursor(
        first.next_cursor,
        identity=identity,
        operation=bound_operation("search_opportunities", "gsi_tenant", _TENANT),
    )
    assert decoded is not None
    raw = base64.urlsafe_b64decode(first.next_cursor.encode("ascii"))
    for attribute in decoded.values():
        for value in attribute.values():
            assert str(value).encode("utf-8") not in raw


@pytest.mark.parametrize(
    "replayer",
    [
        Identity(rep_id="rep-2", full_visibility=True, tenant=_TENANT),
        Identity(rep_id=_OWNER, full_visibility=True, tenant="eval-other"),
    ],
    ids=["other-rep", "other-tenant"],
)
def test_a_cursor_replayed_by_another_caller_is_a_cursor_validation_failure(
    store: Store, identity: Identity, replayer: Identity
) -> None:
    _seed_many(store, EXAMINATION_BUDGET + 10, name="Acme Renewal")
    first = search_opportunities(store, identity, query="acme")
    assert first.next_cursor is not None

    with pytest.raises(ValidationFailed) as refused:
        search_opportunities(store, replayer, query="acme", cursor=first.next_cursor)

    assert refused.value.source == "cursor"


def test_a_cursor_from_another_operation_is_a_cursor_validation_failure(
    store: Store, identity: Identity
) -> None:
    foreign = encode_cursor(
        {"opportunity_id": {"S": "opp-00001"}, "tenant": {"S": _TENANT}},
        identity=identity,
        operation="search_accounts",
    )

    with pytest.raises(ValidationFailed) as refused:
        search_opportunities(store, identity, cursor=foreign)

    assert refused.value.source == "cursor"


@pytest.mark.parametrize(
    "replay_filters",
    [{}, {"stage": "Negotiation"}, {"owner_rep_id": "rep-2"}, {"account_id": "acct-2"}],
    ids=["no-filter", "other-stage", "owner-index", "account-index"],
)
def test_a_cursor_replayed_with_different_index_selecting_filters_is_refused(
    store: Store, identity: Identity, replay_filters: dict[str, str]
) -> None:
    _seed_many(store, EXAMINATION_BUDGET + 10, name="Acme Renewal")
    first = search_opportunities(store, identity, query="acme", stage="Discovery")
    assert first.next_cursor is not None

    with pytest.raises(ValidationFailed) as refused:
        search_opportunities(
            store,
            identity,
            query="acme",
            cursor=first.next_cursor,
            stage=replay_filters.get("stage"),
            owner_rep_id=replay_filters.get("owner_rep_id"),
            account_id=replay_filters.get("account_id"),
        )

    assert refused.value.source == "cursor"


def test_a_cursor_replayed_with_the_same_filters_and_a_different_query_resumes(
    store: Store, identity: Identity
) -> None:
    _seed_many(store, EXAMINATION_BUDGET + 10, name="Acme Renewal")
    first = search_opportunities(store, identity, query="acme", stage="Discovery")
    assert first.next_cursor is not None

    second = search_opportunities(
        store, identity, query="renewal", stage="Discovery", cursor=first.next_cursor
    )

    assert second.examined_count == 10


# -- scope-filtered metadata ----------------------------------------------------------------------

_IN_BOOK = "acct-in-book"
_OUT_OF_BOOK = "acct-out-of-book"


def test_out_of_book_rows_below_the_budget_are_neither_counted_nor_cause_truncation(
    store: Store,
) -> None:
    for index in range(5):
        _seed_opportunity(store, opportunity_id=f"opp-out-{index}", account_id=_OUT_OF_BOOK)
    scoped = Identity(rep_id=_OWNER, book=frozenset({_IN_BOOK}), tenant=_TENANT)

    result = search_opportunities(store, scoped, limit=1)

    assert result.items == []
    assert result.examined_count == 0
    assert result.truncated is False
    assert result.next_cursor is None


def test_examined_count_counts_only_in_scope_rows(store: Store) -> None:
    for index in range(5):
        _seed_opportunity(store, opportunity_id=f"opp-out-{index}", account_id=_OUT_OF_BOOK)
    for index in range(2):
        _seed_opportunity(store, opportunity_id=f"opp-in-{index}", account_id=_IN_BOOK)
    scoped = Identity(rep_id=_OWNER, book=frozenset({_IN_BOOK}), tenant=_TENANT)

    result = search_opportunities(store, scoped, limit=1)

    assert [item["account_id"] for item in result.items] == [_IN_BOOK]
    assert result.examined_count == 2
    assert result.truncated is True


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


# -- store: client-side SDK failures map into the taxonomy ----------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        lambda store, key: store.query_gsi(
            "opportunities", "gsi_tenant", partition_value=_TENANT, exclusive_start_key=key
        ),
        lambda store, key: store.query_tenant_scan(
            "opportunities", tenant=_TENANT, exclusive_start_key=key
        ),
    ],
    ids=["query_gsi", "query_tenant_scan"],
)
def test_a_parameter_the_sdk_rejects_is_an_internal_error_not_a_raw_sdk_error_or_a_400(
    store: Store, query: object
) -> None:
    from crm.errors import CrmError

    with pytest.raises(CrmError) as failed:
        query(store, {"x": "y"})  # type: ignore[operator]

    assert type(failed.value) is CrmError
    assert failed.value.http_status == 500


@pytest.mark.parametrize("failure", ["unreachable", "read-timeout"])
def test_an_unreachable_or_timed_out_endpoint_is_a_retryable_service_busy(
    store: Store, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from botocore.exceptions import EndpointConnectionError, ReadTimeoutError

    from crm.errors import ServiceBusy

    def unreachable(**_: object) -> object:
        if failure == "unreachable":
            raise EndpointConnectionError(endpoint_url="https://dynamodb.invalid")
        raise ReadTimeoutError(endpoint_url="https://dynamodb.invalid")

    monkeypatch.setattr(store._client, "query", unreachable)

    with pytest.raises(ServiceBusy) as failed:
        store.query_tenant_scan("opportunities", tenant=_TENANT)

    assert failed.value.next_step == "retry_later"
    assert "dynamodb.invalid" not in failed.value.message

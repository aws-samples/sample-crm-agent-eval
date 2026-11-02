# SPDX-License-Identifier: MIT
"""Listing scope: empty-book callers and explicit out-of-book account filters are denied before
any store call, while book and full-visibility callers still get data.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.authz import Identity
from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import ScopeDenied, ValidationFailed
from crm.handlers import pipeline_aggregate, search_accounts, search_opportunities
from crm.search import ACCOUNT_ID_MAX_LENGTH
from crm.search import search_accounts as search_accounts_fn
from crm.search import search_opportunities as search_opportunities_fn
from crm.store.keys import name_lower, stage_close_date, tenant_prefixed
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_IN_BOOK = "acct-in-book"
_OUT_OF_BOOK = "acct-out-of-book"
_NONEXISTENT = "acct-does-not-exist"

_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "search_opportunities": search_opportunities.handler,
    "search_accounts": search_accounts.handler,
    "pipeline_aggregate": pipeline_aggregate.handler,
}


class _SpyStore:
    """A store stand-in that records every attribute access and refuses to serve any of them."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.calls.append(name)
        raise AssertionError(f"store.{name} was reached")


def _identity_payload(*, book: list[str], full_visibility: bool = False) -> dict[str, Any]:
    return {
        "rep_id": _REP,
        "book": book,
        "full_visibility": full_visibility,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": _TENANT,
    }


def _event(
    args: dict[str, Any], *, book: list[str], full_visibility: bool = False
) -> dict[str, Any]:
    return identity_event(
        IDENTITY_KEY, _identity_payload(book=book, full_visibility=full_visibility), args
    )


def _assert_denied(result: dict[str, Any], missing_scope: str) -> None:
    assert result["statusCode"] == 403, result
    error = result["error"]
    assert error["code"] == "scope_denied"
    assert error["missing_scope"] == missing_scope
    assert "detail" not in error


@pytest.fixture
def store() -> Iterator[Store]:
    """A moto-backed store seeded with one in-book and one out-of-book account and opportunity."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        seeded = Store(client=client)
        for index, account_id in enumerate((_IN_BOOK, _OUT_OF_BOOK)):
            _seed(seeded, account_id=account_id, opportunity_id=f"opp-{index}")
        yield seeded


def _seed(store: Store, *, account_id: str, opportunity_id: str) -> None:
    store.put_item(
        "accounts",
        {
            "account_id": account_id,
            "tenant": _TENANT,
            "name": f"Account {account_id}",
            "name_lower": name_lower(f"Account {account_id}"),
            "owner_rep_id": _REP,
            "tenant_owner_rep_id": tenant_prefixed(_TENANT, _REP),
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
            "close_date": "2026-01-01",
            "value": 1000,
            "name": "Renewal",
            "name_lower": "renewal",
            "created_at": "2026-01-01T00:00:00Z",
            "tenant_account_id": tenant_prefixed(_TENANT, account_id),
            "tenant_owner_rep_id": tenant_prefixed(_TENANT, _REP),
            "tenant_stage": tenant_prefixed(_TENANT, "Discovery"),
            "stage_close_date": stage_close_date("Discovery", "2026-01-01"),
        },
    )


# -- empty book -----------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(_HANDLERS))
def test_an_empty_book_caller_is_denied_account_star_and_the_store_is_never_called(
    tool: str,
) -> None:
    spy = _SpyStore()

    result = _HANDLERS[tool](_event({}, book=[]), store=spy)

    _assert_denied(result, "account:*")
    assert spy.calls == []


@pytest.mark.parametrize(
    "call",
    [
        lambda store, identity: search_opportunities_fn(store, identity),
        lambda store, identity: search_accounts_fn(store, identity),
    ],
    ids=["search_opportunities", "search_accounts"],
)
def test_the_search_functions_deny_an_empty_book_before_any_store_call(
    call: Callable[[Any, Identity], object],
) -> None:
    spy = _SpyStore()

    with pytest.raises(ScopeDenied) as denied:
        call(spy, Identity(rep_id=_REP, tenant=_TENANT))

    assert denied.value.missing_scope == "account:*"
    assert spy.calls == []


@pytest.mark.parametrize("tool", sorted(_HANDLERS))
def test_a_book_caller_gets_its_own_data(tool: str, store: Store) -> None:
    result = _HANDLERS[tool](_event({}, book=[_IN_BOOK]), store=store)

    assert result["statusCode"] == 200, result
    if tool == "pipeline_aggregate":
        assert result["body"]["value_rollup"] == 1000
    else:
        assert [item["account_id"] for item in result["body"]["items"]] == [_IN_BOOK]


@pytest.mark.parametrize("tool", sorted(_HANDLERS))
def test_a_full_visibility_caller_with_an_empty_book_gets_every_record(
    tool: str, store: Store
) -> None:
    result = _HANDLERS[tool](_event({}, book=[], full_visibility=True), store=store)

    assert result["statusCode"] == 200, result
    if tool == "pipeline_aggregate":
        assert result["body"]["value_rollup"] == 2000
    else:
        assert sorted(item["account_id"] for item in result["body"]["items"]) == [
            _IN_BOOK,
            _OUT_OF_BOOK,
        ]


# -- explicit account filter ----------------------------------------------------------------------


@pytest.mark.parametrize("account_id", [_OUT_OF_BOOK, _NONEXISTENT], ids=["exists", "nonexistent"])
def test_an_explicit_out_of_book_account_filter_is_denied_before_any_store_call(
    account_id: str,
) -> None:
    spy = _SpyStore()

    result = search_opportunities.handler(
        _event({"account_id": account_id}, book=[_IN_BOOK]), store=spy
    )

    _assert_denied(result, f"account:{account_id}")
    assert spy.calls == []


def test_an_existing_and_a_nonexistent_out_of_book_account_get_the_same_denial() -> None:
    results = [
        search_opportunities.handler(
            _event({"account_id": account_id}, book=[_IN_BOOK]), store=_SpyStore()
        )
        for account_id in (_OUT_OF_BOOK, _NONEXISTENT)
    ]

    for result in results:
        result["error"].pop("missing_scope")
    assert results[0] == results[1]


def test_an_explicit_in_book_account_filter_returns_that_accounts_data(store: Store) -> None:
    result = search_opportunities.handler(
        _event({"account_id": _IN_BOOK}, book=[_IN_BOOK]), store=store
    )

    assert result["statusCode"] == 200, result
    assert [item["opportunity_id"] for item in result["body"]["items"]] == ["opp-0"]


def test_a_full_visibility_caller_may_filter_on_any_account(store: Store) -> None:
    result = search_opportunities.handler(
        _event({"account_id": _OUT_OF_BOOK}, book=[], full_visibility=True), store=store
    )

    assert result["statusCode"] == 200, result
    assert [item["opportunity_id"] for item in result["body"]["items"]] == ["opp-1"]


# -- explicit account filter: malformed values are refused, never echoed --------------------------


_PLANTED = "Z" * 100_000


@pytest.mark.parametrize(
    "account_id",
    [_PLANTED, "x" * (ACCOUNT_ID_MAX_LENGTH + 1), "acct-1<script>", "acct 1", "-acct-1", "acct:1"],
    ids=["100k", "over-max", "markup", "space", "leading-dash", "colon"],
)
def test_a_malformed_account_filter_is_a_validation_failure_that_echoes_nothing(
    account_id: str,
) -> None:
    spy = _SpyStore()

    result = search_opportunities.handler(
        _event({"account_id": account_id}, book=[_IN_BOOK]), store=spy
    )

    assert result["statusCode"] == 400, result
    assert result["error"]["code"] == "validation_failed"
    assert result["error"]["source"] == "account_id"
    assert "detail" not in result["error"]
    assert account_id not in json.dumps(result)
    assert spy.calls == []


def test_a_planted_account_filter_never_reaches_the_mcp_error_text() -> None:
    with pytest.raises(ValidationFailed) as refused:
        search_opportunities_fn(
            _SpyStore(),  # type: ignore[arg-type]
            Identity(rep_id=_REP, book=frozenset({_IN_BOOK}), tenant=_TENANT),
            account_id=_PLANTED,
        )

    rendered = json.dumps([refused.value.to_api_response(), refused.value.to_mcp_error()])
    assert "ZZZZ" not in rendered
    assert len(str(refused.value.detail)) < 200


def test_a_well_formed_out_of_book_id_at_the_length_cap_is_still_named_in_the_denial() -> None:
    account_id = "a" * ACCOUNT_ID_MAX_LENGTH

    result = search_opportunities.handler(
        _event({"account_id": account_id}, book=[_IN_BOOK]), store=_SpyStore()
    )

    _assert_denied(result, f"account:{account_id}")

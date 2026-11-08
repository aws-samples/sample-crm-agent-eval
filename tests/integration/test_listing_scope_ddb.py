# SPDX-License-Identifier: MIT
"""Listing scope through the real handlers, on all three identity channels, against DynamoDB Local.

A seeded eval tenant backs every case: an empty-book caller is denied ``account:*`` by
``search_opportunities``, ``search_accounts`` and ``pipeline_aggregate``; an explicit out-of-book
``account_id`` is denied ``account:<id>`` whether or not the account exists; book and
full-visibility callers get data.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from fixtures.generate import generate_dataset

from crm.domain.stages import CLOSED_STAGES
from crm.envelope import (
    AGENT_IDENTITY_KEY,
    CLAIM_BOOK,
    CLAIM_FULL_VISIBILITY,
    CLAIM_REP_ID,
    CLAIM_TENANT,
    IDENTITY_KEY,
    identity_event,
)
from crm.handlers import pipeline_aggregate, search_accounts, search_opportunities
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.tenancy import allocate_run_id, seed_tenant, tenant_for_run
from tests.conftest import TEST_API_CLIENT_ID
from tests.integration.conftest import _ENDPOINT_URL, _LOCAL_ENGINE_CRED, _drop_prefixed_tables

_REP = "rep-listing-scope"
_CHANNELS = ("apigw", "gateway", "agent")
_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "search_opportunities": search_opportunities.handler,
    "search_accounts": search_accounts.handler,
    "pipeline_aggregate": pipeline_aggregate.handler,
}


class _Seeded:
    """The seeded store plus the corpus facts the assertions are derived from."""

    def __init__(self, store: Store, tenant: str) -> None:
        corpus = generate_dataset(tenant)
        self.store = store
        self.tenant = tenant
        self.opportunities = corpus.opportunities
        account_ids = sorted(str(account["account_id"]) for account in corpus.accounts)
        with_opportunities = {str(opp["account_id"]) for opp in corpus.opportunities}
        self.in_book = next(a for a in account_ids if a in with_opportunities)
        self.out_of_book = next(a for a in account_ids if a != self.in_book)
        self.nonexistent = f"{tenant}-acct-does-not-exist"
        self.account_ids = account_ids


@pytest.fixture(scope="module")
def seeded(_dynamodb_local_endpoint: None) -> Iterator[_Seeded]:
    """One seeded eval tenant on DynamoDB Local, shared by this module and dropped afterwards."""
    client = boto3.client(
        "dynamodb",
        endpoint_url=_ENDPOINT_URL,
        region_name="us-east-1",
        aws_access_key_id=_LOCAL_ENGINE_CRED,
        aws_secret_access_key=_LOCAL_ENGINE_CRED,
    )
    prefix = f"ls{uuid.uuid4().hex[:12]}_"
    create_all_tables(client, table_prefix=prefix)
    try:
        store = Store(table_prefix=prefix, client=client)
        tenant = tenant_for_run(allocate_run_id())
        seed_tenant(store, tenant=tenant)
        yield _Seeded(store, tenant)
    finally:
        _drop_prefixed_tables(client, prefix)


def _event(
    channel: str,
    args: dict[str, Any],
    *,
    tenant: str,
    book: list[str],
    full_visibility: bool = False,
) -> dict[str, Any]:
    claims = {
        CLAIM_REP_ID: _REP,
        CLAIM_BOOK: ",".join(book),
        CLAIM_FULL_VISIBILITY: "true" if full_visibility else "false",
        CLAIM_TENANT: tenant,
    }
    if channel == "apigw":
        return {
            "version": "2.0",
            "routeKey": "POST /tools/listing",
            "rawPath": "/tools/listing",
            "requestContext": {
                "requestId": "req-1",
                "authorizer": {
                    "jwt": {"claims": {**claims, "token_use": "id", "aud": TEST_API_CLIENT_ID}}
                },
            },
            "body": json.dumps(args),
            "isBase64Encoded": False,
        }
    if channel == "agent":
        return identity_event(AGENT_IDENTITY_KEY, {"claims": claims, "session_id": "s-1"}, args)
    payload = {
        "rep_id": _REP,
        "book": book,
        "full_visibility": full_visibility,
        "principal_type": "user",
        "session_id": "s-1",
        "source": "gateway",
        "tenant": tenant,
    }
    return identity_event(IDENTITY_KEY, payload, args)


def _invoke(
    seeded: _Seeded,
    tool: str,
    channel: str,
    args: dict[str, Any],
    *,
    book: list[str],
    full_visibility: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Invoke the real handler and return ``(status, payload)`` in the flat shape.

    A proxy success body is the bare result, so it is re-wrapped under ``body``.
    """
    event = _event(channel, args, tenant=seeded.tenant, book=book, full_visibility=full_visibility)
    result = _HANDLERS[tool](event, store=seeded.store)
    if channel == "apigw":
        assert isinstance(result["body"], str), "a proxy response body must be a JSON string"
        assert result["headers"], result
        status, decoded = int(result["statusCode"]), json.loads(result["body"])
        return status, decoded if status != 200 else {"body": decoded}
    return int(result["statusCode"]), result


def _assert_denied(status: int, payload: dict[str, Any], missing_scope: str) -> None:
    assert status == 403, payload
    error = payload["error"]
    assert error["code"] == "scope_denied"
    assert error["missing_scope"] == missing_scope
    assert "detail" not in error


@pytest.mark.parametrize("channel", _CHANNELS)
@pytest.mark.parametrize("tool", sorted(_HANDLERS))
def test_an_empty_book_caller_is_denied_account_star(
    seeded: _Seeded, tool: str, channel: str
) -> None:
    status, payload = _invoke(seeded, tool, channel, {}, book=[])

    _assert_denied(status, payload, "account:*")


@pytest.mark.parametrize("channel", _CHANNELS)
@pytest.mark.parametrize("tool", sorted(_HANDLERS))
def test_a_book_caller_gets_only_its_books_data(seeded: _Seeded, tool: str, channel: str) -> None:
    status, payload = _invoke(seeded, tool, channel, {}, book=[seeded.in_book])

    assert status == 200, payload
    body = payload["body"]
    if tool == "pipeline_aggregate":
        open_in_book = [
            opp
            for opp in seeded.opportunities
            if opp["account_id"] == seeded.in_book and opp["stage"] not in CLOSED_STAGES
        ]
        assert sum(body["stage_funnel"].values()) == len(open_in_book)
    else:
        assert body["items"], "the in-book account has records in the seeded tenant"
        assert {item["account_id"] for item in body["items"]} == {seeded.in_book}


@pytest.mark.parametrize("channel", _CHANNELS)
def test_a_full_visibility_caller_with_an_empty_book_sees_every_account(
    seeded: _Seeded, channel: str
) -> None:
    status, payload = _invoke(seeded, "search_accounts", channel, {}, book=[], full_visibility=True)

    assert status == 200, payload
    assert sorted(item["account_id"] for item in payload["body"]["items"]) == seeded.account_ids


@pytest.mark.parametrize("channel", _CHANNELS)
def test_a_full_visibility_caller_gets_a_pipeline_aggregate(seeded: _Seeded, channel: str) -> None:
    status, payload = _invoke(
        seeded, "pipeline_aggregate", channel, {}, book=[], full_visibility=True
    )

    assert status == 200, payload
    open_count = sum(1 for opp in seeded.opportunities if opp["stage"] not in CLOSED_STAGES)
    assert sum(payload["body"]["stage_funnel"].values()) == open_count


@pytest.mark.parametrize("channel", _CHANNELS)
@pytest.mark.parametrize("target", ["out_of_book", "nonexistent"])
def test_an_explicit_out_of_book_account_filter_is_denied_naming_it(
    seeded: _Seeded, channel: str, target: str
) -> None:
    account_id = getattr(seeded, target)

    status, payload = _invoke(
        seeded, "search_opportunities", channel, {"account_id": account_id}, book=[seeded.in_book]
    )

    _assert_denied(status, payload, f"account:{account_id}")


@pytest.mark.parametrize("channel", _CHANNELS)
def test_an_explicit_in_book_account_filter_returns_its_opportunities(
    seeded: _Seeded, channel: str
) -> None:
    status, payload = _invoke(
        seeded,
        "search_opportunities",
        channel,
        {"account_id": seeded.in_book},
        book=[seeded.in_book],
    )

    assert status == 200, payload
    expected = sorted(
        str(opp["opportunity_id"])
        for opp in seeded.opportunities
        if opp["account_id"] == seeded.in_book
    )
    assert sorted(item["opportunity_id"] for item in payload["body"]["items"]) == expected

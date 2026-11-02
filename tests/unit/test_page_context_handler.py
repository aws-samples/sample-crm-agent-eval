# SPDX-License-Identifier: MIT
"""Tests for the ``get_page_context`` action Lambda.

Exercised against a `moto`-mocked DynamoDB through the handler's own Lambda entry point, so identity
decoding, toolspec argument validation, and error translation are in the path. Properties covered:

* **Denial, not emptiness.** An out-of-book descriptor yields ``scope_denied`` (403), never a
  successful response with nothing in it.
* **Scope comes from the existing predicate.** The set of records this tool confirms is asserted
  identical to the set ``get_account``/``get_opportunity``/``get_contact`` confirm, so it cannot have
  acquired a second, laxer notion of scope.
* **No rows in, no rows out.** A row collection attached to the descriptor is refused, and the
  success payload is asserted against a closed key allowlist so the tool cannot become a
  row-delivery channel.
* **No query execution.** A ``list``/``board`` descriptor produces no row count and reads nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import get_account, get_contact, get_opportunity, get_page_context
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.toolspec.definitions import BY_NAME

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_OTHER_TENANT = "canary"
_REP = "rep-1"
_ACCOUNT = "acct-1"
_OUT_OF_BOOK_ACCOUNT = "acct-2"


@pytest.fixture
def dynamodb_client() -> Iterator[Any]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: Any) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _identity(
    *, rep_id: str = _REP, book: list[str] | None = None, tenant: str = _TENANT
) -> dict[str, Any]:
    return {
        "rep_id": rep_id,
        "book": book if book is not None else [_ACCOUNT],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": tenant,
    }


def _event(page_context_argument: object, **identity_overrides: Any) -> dict[str, Any]:
    return identity_event(
        IDENTITY_KEY, _identity(**identity_overrides), {"page_context": page_context_argument}
    )


def _invoke(store: Store, page_context_argument: object, **overrides: Any) -> dict[str, Any]:
    response = get_page_context.handler(_event(page_context_argument, **overrides), store=store)
    assert isinstance(response, dict)
    return response


def _seed_account(store: Store, *, account_id: str = _ACCOUNT, tenant: str = _TENANT) -> None:
    store.put_item(
        "accounts",
        {
            "account_id": account_id,
            "tenant": tenant,
            "tenant_owner_rep_id": f"{tenant}#{_REP}",
            "owner_rep_id": _REP,
            "name": "Acme",
            "name_lower": "acme",
        },
    )


def _seed_opportunity(
    store: Store,
    *,
    opportunity_id: str = "opp-1",
    account_id: str = _ACCOUNT,
    tenant: str = _TENANT,
) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": opportunity_id,
            "tenant": tenant,
            "tenant_account_id": f"{tenant}#{account_id}",
            "tenant_owner_rep_id": f"{tenant}#{_REP}",
            "tenant_stage": f"{tenant}#Discovery",
            "account_id": account_id,
            "owner_rep_id": _REP,
            "name": "Big Deal",
            "name_lower": "big deal",
            "stage": "Discovery",
            "value": 1000,
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )


def _seed_contact(store: Store, *, contact_id: str = "cont-1", account_id: str = _ACCOUNT) -> None:
    store.put_item(
        "contacts",
        {
            "contact_id": contact_id,
            "tenant": _TENANT,
            "tenant_account_id": f"{_TENANT}#{account_id}",
            "account_id": account_id,
            "name": "Jane Doe",
            "name_lower": "jane doe",
            "status": "active",
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )


def _entity_descriptor(
    *, entity_type: str = "opportunities", entity_id: str = "opp-1"
) -> dict[str, Any]:
    return {
        "kind": "entity",
        "route": f"/{entity_type}/{entity_id}",
        "entity_type": entity_type,
        "entity_id": entity_id,
    }


# --------------------------------------------------------------------------------------
# Denial, not emptiness
# --------------------------------------------------------------------------------------


def test_an_out_of_book_entity_descriptor_is_denied_not_returned_empty(store: Store) -> None:
    """403 `scope_denied`, naming the missing scope — never a 200 with nothing in it."""
    _seed_account(store, account_id=_OUT_OF_BOOK_ACCOUNT)
    _seed_opportunity(store, account_id=_OUT_OF_BOOK_ACCOUNT)

    response = _invoke(store, _entity_descriptor())

    assert response["statusCode"] == 403
    assert response["error"]["code"] == "scope_denied"
    assert response["error"]["missing_scope"] == f"account:{_OUT_OF_BOOK_ACCOUNT}"
    assert "summarisable" not in response


def test_a_denial_carries_no_record_content(store: Store) -> None:
    """The refusal must not leak the very record it refused."""
    _seed_account(store, account_id=_OUT_OF_BOOK_ACCOUNT)
    _seed_opportunity(store, account_id=_OUT_OF_BOOK_ACCOUNT)

    response = _invoke(store, _entity_descriptor())

    assert "Big Deal" not in repr(response)
    assert "1000" not in repr(response)


def test_an_absent_record_is_not_found_rather_than_denied(store: Store) -> None:
    """Absence and denial stay distinct members of the taxonomy."""
    response = _invoke(store, _entity_descriptor(entity_id="opp-missing"))

    assert response["statusCode"] == 404
    assert response["error"]["code"] == "not_found"


def test_a_cross_tenant_record_is_not_found_not_denied(store: Store) -> None:
    """A record in another tenant is reported as `not_found`, not `scope_denied`."""
    _seed_account(store, account_id=_ACCOUNT, tenant=_OTHER_TENANT)
    _seed_opportunity(store, account_id=_ACCOUNT, tenant=_OTHER_TENANT)

    response = _invoke(store, _entity_descriptor())

    assert response["statusCode"] == 404
    assert response["error"]["code"] == "not_found"


def test_a_cross_tenant_record_is_indistinguishable_from_an_absent_one(store: Store) -> None:
    """A foreign-tenant record and a nonexistent one produce the same 404 response."""
    _seed_account(store, account_id=_ACCOUNT, tenant=_OTHER_TENANT)
    _seed_opportunity(
        store, opportunity_id="opp-foreign", account_id=_ACCOUNT, tenant=_OTHER_TENANT
    )

    foreign = _invoke(store, _entity_descriptor(entity_id="opp-foreign"))
    absent = _invoke(store, _entity_descriptor(entity_id="opp-nowhere"))

    assert foreign["statusCode"] == absent["statusCode"] == 404
    assert foreign["error"]["code"] == absent["error"]["code"]
    # The identifier the caller itself supplied may appear; nothing about the other tenant may.
    assert _OTHER_TENANT not in repr(foreign)


def test_an_unauthenticated_call_is_refused(store: Store) -> None:
    """With no identity channel present the handler returns 401 `authentication_failed`."""
    response = get_page_context.handler({"page_context": _entity_descriptor()}, store=store)

    assert response["statusCode"] == 401
    assert response["error"]["code"] == "authentication_failed"


# --------------------------------------------------------------------------------------
# Scope is the existing predicate's decision, not a second one
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entity_type", "entity_id"),
    [
        pytest.param("accounts", _ACCOUNT, id="accounts"),
        pytest.param("opportunities", "opp-1", id="opportunities"),
        pytest.param("contacts", "cont-1", id="contacts"),
    ],
)
def test_scope_decision_matches_the_existing_read_tools_exactly(
    store: Store, entity_type: str, entity_id: str
) -> None:
    """An out-of-book record is denied here exactly as the matching `get_*` tool denies it."""
    _seed_account(store, account_id=_OUT_OF_BOOK_ACCOUNT)
    _seed_opportunity(store, account_id=_OUT_OF_BOOK_ACCOUNT)
    _seed_contact(store, account_id=_OUT_OF_BOOK_ACCOUNT)

    existing_handler, argument = {
        "accounts": (get_account.handler, {"account_id": _OUT_OF_BOOK_ACCOUNT}),
        "opportunities": (get_opportunity.handler, {"opportunity_id": "opp-1"}),
        "contacts": (get_contact.handler, {"contact_id": "cont-1"}),
    }[entity_type]
    if entity_type == "accounts":
        entity_id = _OUT_OF_BOOK_ACCOUNT

    existing = existing_handler(identity_event(IDENTITY_KEY, _identity(), argument), store=store)
    ours = _invoke(store, _entity_descriptor(entity_type=entity_type, entity_id=entity_id))

    assert ours["statusCode"] == existing["statusCode"] == 403
    assert ours["error"]["code"] == existing["error"]["code"]


def test_an_in_book_entity_resolves_to_its_scope_identifiers(store: Store) -> None:
    """The granted path: confirmed in scope, and the account it is scoped by is named."""
    _seed_account(store)
    _seed_opportunity(store)

    response = _invoke(store, _entity_descriptor())

    assert response["statusCode"] == 200
    assert response["body"]["summarisable"] is True
    assert response["body"]["entity_type"] == "opportunities"
    assert response["body"]["entity_id"] == "opp-1"
    assert response["body"]["account_id"] == _ACCOUNT


def test_full_visibility_resolves_an_entity_outside_any_book(store: Store) -> None:
    """`full_visibility` is honoured because the decision is `may_access`'s, not this handler's."""
    _seed_account(store, account_id=_OUT_OF_BOOK_ACCOUNT)
    _seed_opportunity(store, account_id=_OUT_OF_BOOK_ACCOUNT)

    identity = _identity(book=[])
    identity["full_visibility"] = True
    event = identity_event(IDENTITY_KEY, identity, {"page_context": _entity_descriptor()})

    response = get_page_context.handler(event, store=store)

    assert response["statusCode"] == 200
    assert response["body"]["account_id"] == _OUT_OF_BOOK_ACCOUNT


def test_the_response_reports_the_callers_own_scope(store: Store) -> None:
    """The response body names the caller's rep, scope and tenant."""
    _seed_account(store)
    _seed_opportunity(store)

    response = _invoke(store, _entity_descriptor())

    assert response["body"]["scope"] == f"rep={_REP} scope=1-accounts tenant={_TENANT}"


# --------------------------------------------------------------------------------------
# No rows in, no rows out
# --------------------------------------------------------------------------------------

#: Every key the success payload may carry, across all variants (a closed allowlist).
_PERMITTED_BODY_KEYS = {
    "kind",
    "route",
    "scope",
    "summarisable",
    "reason",
    "entity_type",
    "entity_id",
    "account_id",
    "query",
}


@pytest.mark.parametrize(
    "descriptor",
    [
        pytest.param(
            {
                "kind": "entity",
                "route": "/opportunities/opp-1",
                "entity_type": "opportunities",
                "entity_id": "opp-1",
            },
            id="entity",
        ),
        pytest.param(
            {
                "kind": "list",
                "route": "/opportunities",
                "entity_type": "opportunities",
                "query": {"filters": {"stage": "Discovery"}},
            },
            id="list",
        ),
        pytest.param({"kind": "board", "route": "/pipeline", "query": {"filters": {}}}, id="board"),
        pytest.param(
            {"kind": "opaque", "route": "/pipeline", "reason": "client-only sort"}, id="opaque"
        ),
    ],
)
def test_no_variant_returns_a_row(store: Store, descriptor: dict[str, Any]) -> None:
    """Whatever the descriptor, the payload holds no record and no collection of records."""
    _seed_account(store)
    _seed_opportunity(store)

    body = _invoke(store, descriptor)["body"]

    assert set(body) <= _PERMITTED_BODY_KEYS, sorted(set(body) - _PERMITTED_BODY_KEYS)
    assert "name" not in body
    assert "value" not in body
    for key, value in body.items():
        assert not isinstance(value, list), f"{key} is a collection"


def test_a_row_collection_attached_to_the_descriptor_is_refused(store: Store) -> None:
    """A `rows` collection attached to the descriptor is a 400 `validation_failed`."""
    response = _invoke(
        store,
        {
            "kind": "list",
            "route": "/opportunities",
            "entity_type": "opportunities",
            "query": {"filters": {}},
            "rows": [{"opportunity_id": "opp-1", "name": "Big Deal", "value": 1000}],
        },
    )

    assert response["statusCode"] == 400
    assert response["error"]["code"] == "validation_failed"


def test_a_row_collection_smuggled_as_a_filter_value_is_refused(store: Store) -> None:
    """The other shape the same mistake takes: a record hidden inside `filters`."""
    response = _invoke(
        store,
        {
            "kind": "list",
            "route": "/opportunities",
            "entity_type": "opportunities",
            "query": {"filters": {"stage": [{"opportunity_id": "opp-1"}]}},
        },
    )

    assert response["statusCode"] == 400
    assert response["error"]["code"] == "validation_failed"


def test_the_tool_schema_denies_every_identity_channel_key() -> None:
    """The tool schema is closed and declares only `page_context`, so no identity key validates."""
    schema = BY_NAME["get_page_context"].input_schema

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"page_context"}


def test_an_identity_key_in_the_arguments_cannot_reach_the_handler(store: Store) -> None:
    """A smuggled `_agent_identity` is stripped; the call succeeds under the real identity."""
    _seed_account(store)
    _seed_opportunity(store)
    event = _event(_entity_descriptor())
    event["_agent_identity"] = {
        "rep_id": "rep-attacker",
        "book": [_OUT_OF_BOOK_ACCOUNT],
        "full_visibility": True,
        "tenant": _TENANT,
    }

    response = get_page_context.handler(event, store=store)

    assert response["statusCode"] == 200
    assert response["body"]["scope"] == f"rep={_REP} scope=1-accounts tenant={_TENANT}"


# --------------------------------------------------------------------------------------
# It does not execute the described query
# --------------------------------------------------------------------------------------


class _ExplodingStore:
    """A store that fails on any access, proving a code path touched no data at all."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the handler must not reach the store, but called {name!r}")


@pytest.mark.parametrize(
    "descriptor",
    [
        pytest.param(
            {
                "kind": "list",
                "route": "/opportunities",
                "entity_type": "opportunities",
                "query": {"filters": {"stage": "Discovery"}},
            },
            id="list",
        ),
        pytest.param({"kind": "board", "route": "/pipeline", "query": {"filters": {}}}, id="board"),
        pytest.param(
            {"kind": "opaque", "route": "/pipeline", "reason": "client-only sort"}, id="opaque"
        ),
    ],
)
def test_a_non_entity_descriptor_reads_nothing(descriptor: dict[str, Any]) -> None:
    """A `list`/`board`/`opaque` descriptor succeeds without touching the store."""
    response = get_page_context.handler(_event(descriptor), store=_ExplodingStore())

    assert response["statusCode"] == 200


def test_no_row_count_is_reported_for_a_list_view(store: Store) -> None:
    """A list descriptor's response carries no count or total."""
    body = _invoke(
        store,
        {
            "kind": "list",
            "route": "/opportunities",
            "entity_type": "opportunities",
            "query": {"filters": {}},
        },
    )["body"]

    assert not any("count" in key for key in body)
    assert not any("total" in key for key in body)


def test_a_list_descriptor_echoes_the_query_it_was_given(store: Store) -> None:
    """The agent needs the filters back in order to re-run them through its own tools."""
    body = _invoke(
        store,
        {
            "kind": "list",
            "route": "/opportunities",
            "entity_type": "opportunities",
            "query": {
                "filters": {"stage": "Discovery", "include_closed": False},
                "sort": {"field": "close_date", "direction": "desc"},
                "page": {"cursor": "c-1", "limit": 25},
            },
        },
    )["body"]

    assert body["query"]["filters"] == {"stage": "Discovery", "include_closed": False}
    assert body["query"]["sort"] == {"field": "close_date", "direction": "desc"}
    assert body["query"]["page"] == {"cursor": "c-1", "limit": 25}


# --------------------------------------------------------------------------------------
# The opaque variant
# --------------------------------------------------------------------------------------


def test_an_opaque_descriptor_reports_that_the_view_cannot_be_summarised() -> None:
    """An opaque descriptor is reported not summarisable, with its reason."""
    body = get_page_context.handler(
        _event({"kind": "opaque", "route": "/pipeline", "reason": "client-only sort by health"}),
        store=_ExplodingStore(),
    )["body"]

    assert body["summarisable"] is False
    assert body["reason"] == "client-only sort by health"


def test_an_opaque_descriptor_offers_no_query_to_approximate_with() -> None:
    """An opaque descriptor's response carries no `query` and no `entity_id`."""
    body = get_page_context.handler(
        _event({"kind": "opaque", "route": "/pipeline", "reason": "client-only sort"}),
        store=_ExplodingStore(),
    )["body"]

    assert "query" not in body
    assert "entity_id" not in body


# --------------------------------------------------------------------------------------
# Argument validation reaches this handler like every other
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({}, id="page_context-absent"),
        pytest.param({"page_context": {"kind": "nope", "route": "/x"}}, id="unrecognised-kind"),
        pytest.param({"page_context": "entity"}, id="not-an-object"),
        pytest.param({"page_context": {"kind": "opaque", "route": "/x"}}, id="opaque-no-reason"),
        pytest.param(
            {"page_context": _entity_descriptor(), "undeclared": 1}, id="undeclared-argument"
        ),
    ],
)
def test_malformed_arguments_are_rejected_as_validation_failures(
    store: Store, arguments: dict[str, Any]
) -> None:
    """Every rejection is a taxonomy member; no `KeyError` or `ValueError` escapes the boundary."""
    response = get_page_context.handler(
        identity_event(IDENTITY_KEY, _identity(), arguments), store=store
    )

    assert response["statusCode"] == 400
    assert response["error"]["code"] == "validation_failed"


@pytest.mark.parametrize("entity_type", ["reps", "audit_records", "proposals"])
def test_a_table_outside_the_entity_vocabulary_is_a_validation_failure(
    store: Store, entity_type: str
) -> None:
    """A descriptor naming a non-entity table is a 400 `validation_failed`."""
    response = _invoke(store, _entity_descriptor(entity_type=entity_type, entity_id="rep-1"))

    assert response["statusCode"] == 400
    assert response["error"]["code"] == "validation_failed"


def test_the_tool_is_registered_as_a_read_and_commits_nothing() -> None:
    """`get_page_context` is registered with risk `read` and `mutates=False`."""
    tool = BY_NAME["get_page_context"]

    assert tool.risk == "read"
    assert tool.mutates is False
    assert tool.handler == "crm.handlers.get_page_context.handler"


def test_no_agent_gains_a_write_tool_from_this_addition() -> None:
    """Only the supervisor holds it, and it is a read, so no agent gains a write tool."""
    tool = BY_NAME["get_page_context"]

    assert tool.agents == frozenset({"supervisor"})

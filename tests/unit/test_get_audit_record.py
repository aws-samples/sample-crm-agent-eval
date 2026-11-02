# SPDX-License-Identifier: MIT
"""Tests for the `get_audit_record` tool against a `moto`-mocked DynamoDB.

The tool's authorization is the list's record-scope predicate, so a record the list withholds is
denied here. The returned record carries `action`, derived from the snapshots unless stored
explicitly. `revert` refuses a `proposal_rebased` record.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.audit import (
    PROPOSAL_REBASED,
    AuditAction,
    audit_action,
    build_audit_record,
    get_audit_record,
    revert,
)
from crm.authz import Identity
from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import Conflict, CrmError, NotFound, ScopeDenied
from crm.handlers import get_audit_record as get_audit_record_handler
from crm.handlers import list_audit_records as list_audit_records_handler
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_IN_BOOK = "acct-1"
_OUT_OF_BOOK = "acct-2"


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


@pytest.fixture
def identity() -> Identity:
    return Identity(rep_id=_REP, book=frozenset({_IN_BOOK}), tenant=_TENANT)


def _event(args: dict[str, Any], *, book: list[str] | None = None) -> dict[str, Any]:
    return identity_event(
        IDENTITY_KEY,
        {
            "rep_id": _REP,
            "book": book if book is not None else [_IN_BOOK],
            "full_visibility": False,
            "principal_type": "user",
            "session_id": "sess-1",
            "source": "gateway",
            "tenant": _TENANT,
        },
        args,
    )


def _seed(
    store: Store,
    audit_id: str,
    *,
    account_id: str | None = _IN_BOOK,
    before: dict[str, object] | None = None,
    after: dict[str, object] | None = None,
    tenant: str = _TENANT,
    action: AuditAction | None = None,
) -> None:
    scope: dict[str, object] = {} if account_id is None else {"account_id": account_id}
    record = build_audit_record(
        identity=Identity(rep_id=_REP, book=frozenset(), tenant=tenant),
        target_table="opportunities",
        target_id=f"opp-{audit_id}",
        before_snapshot={**scope, "stage": "Discovery"} if before is None else before,
        after_snapshot={**scope, "stage": "Negotiation"} if after is None else after,
        audit_id=audit_id,
        action=action,
    )
    store.put_item("audit_records", record.to_item())


# -- authorization: the list's predicate ------------------------------------------------------


def test_an_in_book_record_is_returned_in_full(store: Store, identity: Identity) -> None:
    _seed(store, "aud-in")
    item = get_audit_record(identity, store, "aud-in")
    assert item["audit_id"] == "aud-in"
    assert item["before_snapshot"] == {"account_id": _IN_BOOK, "stage": "Discovery"}
    assert item["after_snapshot"] == {"account_id": _IN_BOOK, "stage": "Negotiation"}
    assert item["actor_rep_id"] == _REP


def test_an_out_of_book_record_is_denied(store: Store, identity: Identity) -> None:
    _seed(store, "aud-out", account_id=_OUT_OF_BOOK)
    with pytest.raises(ScopeDenied) as raised:
        get_audit_record(identity, store, "aud-out")
    assert raised.value.missing_scope == f"account:{_OUT_OF_BOOK}"


def test_a_record_with_no_account_scope_is_denied(store: Store, identity: Identity) -> None:
    _seed(store, "aud-unscoped", account_id=None)
    with pytest.raises(ScopeDenied):
        get_audit_record(identity, store, "aud-unscoped")


def test_a_record_in_another_tenant_is_denied(store: Store, identity: Identity) -> None:
    _seed(store, "aud-tenant", tenant="eval-run-1")
    with pytest.raises(ScopeDenied) as raised:
        get_audit_record(identity, store, "aud-tenant")
    assert raised.value.missing_scope == "tenant"


def test_an_unknown_record_is_not_found(store: Store, identity: Identity) -> None:
    with pytest.raises(NotFound):
        get_audit_record(identity, store, "aud-missing")


def test_get_succeeds_for_exactly_the_records_the_list_returns(store: Store) -> None:
    """The get decision equals the list decision for every record, visible or not."""
    _seed(store, "aud-in")
    _seed(store, "aud-out", account_id=_OUT_OF_BOOK)
    _seed(store, "aud-unscoped", account_id=None)
    _seed(store, "aud-tenant", tenant="eval-run-1")

    listed = list_audit_records_handler.handler(_event({}), store=store)
    assert listed["statusCode"] == 200
    visible_in_list = {record["audit_id"] for record in listed["body"]["audit_records"]}

    gettable = {
        audit_id
        for audit_id in ("aud-in", "aud-out", "aud-unscoped", "aud-tenant")
        if get_audit_record_handler.handler(_event({"audit_id": audit_id}), store=store)[
            "statusCode"
        ]
        == 200
    }
    assert visible_in_list == gettable == {"aud-in"}


# -- action derivation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ({}, {"account_id": _IN_BOOK, "stage": "Discovery"}, "create"),
        ({"account_id": _IN_BOOK, "stage": "Discovery"}, {}, "delete"),
        (
            {"account_id": _IN_BOOK, "stage": "Discovery"},
            {"account_id": _IN_BOOK, "stage": "Negotiation"},
            "update",
        ),
    ],
)
def test_action_is_derived_from_the_snapshots(
    store: Store,
    identity: Identity,
    before: dict[str, object],
    after: dict[str, object],
    expected: str,
) -> None:
    _seed(store, "aud-derived", before=before, after=after)
    assert get_audit_record(identity, store, "aud-derived")["action"] == expected


def test_a_stored_action_wins_over_derivation(store: Store, identity: Identity) -> None:
    _seed(store, "aud-rebased", action=PROPOSAL_REBASED)
    assert get_audit_record(identity, store, "aud-rebased")["action"] == "proposal_rebased"


def test_an_unrecognised_stored_action_is_refused_as_an_internal_error() -> None:
    item: dict[str, object] = {"action": "bogus", "before_snapshot": {}, "after_snapshot": {"a": 1}}
    with pytest.raises(CrmError) as excinfo:
        audit_action(item)
    assert type(excinfo.value) is CrmError
    assert excinfo.value.code == "internal_error"


def test_a_record_without_an_explicit_action_stores_none(identity: Identity) -> None:
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={"stage": "Discovery"},
        after_snapshot={"stage": "Negotiation"},
    )
    assert "action" not in record.to_item()


# -- revert refuses a rebase record -----------------------------------------------------------


def test_revert_refuses_a_proposal_rebased_record_and_changes_nothing(
    store: Store, identity: Identity
) -> None:
    target = {
        "opportunity_id": "opp-aud-rebased",
        "tenant": _TENANT,
        "account_id": _IN_BOOK,
        "stage": "Negotiation",
    }
    store.put_item("opportunities", target)
    _seed(
        store,
        "aud-rebased",
        before={**target, "stage": "Discovery"},
        after=dict(target),
        action=PROPOSAL_REBASED,
    )

    with pytest.raises(Conflict):
        revert(identity, store, audit_id="aud-rebased", transaction_key="tx-revert-rebase")

    stored_target = store.get_item("opportunities", {"opportunity_id": "opp-aud-rebased"})
    assert stored_target is not None and stored_target["stage"] == "Negotiation"
    stored_audit = store.get_item("audit_records", {"audit_id": "aud-rebased"})
    assert stored_audit is not None and stored_audit["status"] == "active"


# -- handler contract -------------------------------------------------------------------------


def test_the_handler_returns_the_record_under_audit_record(store: Store) -> None:
    _seed(store, "aud-in")
    result = get_audit_record_handler.handler(_event({"audit_id": "aud-in"}), store=store)
    assert result["statusCode"] == 200
    assert set(result["body"]) == {"audit_record"}
    assert result["body"]["audit_record"]["action"] == "update"


def test_the_handler_denies_an_out_of_book_record_with_403(store: Store) -> None:
    _seed(store, "aud-out", account_id=_OUT_OF_BOOK)
    result = get_audit_record_handler.handler(_event({"audit_id": "aud-out"}), store=store)
    assert result["statusCode"] == 403
    assert result["error"]["code"] == "scope_denied"


def test_the_handler_rejects_a_missing_audit_id(store: Store) -> None:
    result = get_audit_record_handler.handler(_event({}), store=store)
    assert result["statusCode"] == 400
    assert result["error"]["code"] == "validation_failed"


def test_the_rest_proxy_response_is_proxy_shaped_with_a_json_string_body(store: Store) -> None:
    """Driven with the recorded REST v1 envelope, retargeted at this route."""
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "events"
    event: dict[str, Any] = json.loads(
        (fixture / "apigw_v1_proxy_post_get_account.json").read_text(encoding="utf-8")
    )
    claims = event["requestContext"]["authorizer"]["claims"]
    tenant = claims["custom:tenant"]
    in_book = claims["custom:book"].split(",")[0]
    _seed(store, "aud-proxy", tenant=tenant, account_id=in_book)
    event.update(
        resource="/get_audit_record",
        path="/get_audit_record",
        body=json.dumps({"audit_id": "aud-proxy"}),
    )

    result = get_audit_record_handler.handler(event, store=store)

    assert result["statusCode"] == 200
    assert result["headers"]["content-type"] == "application/json"
    assert isinstance(result["body"], str)
    assert json.loads(result["body"])["audit_record"]["audit_id"] == "aud-proxy"

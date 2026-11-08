# SPDX-License-Identifier: MIT
"""Proposal integrity re-checked at approve and rebase, against a `moto`-mocked DynamoDB.

Covers the shared primary-contact reference check (create, submit, approve), the delete cascade
rules re-run at approve, and the stage-transition check at approve and rebase. Every refusal leaves
the target record and the proposal unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.authz import Identity
from crm.domain.references import require_primary_contact_in_account
from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import ValidationFailed
from crm.handlers import (
    approve_proposal,
    create_opportunity,
    rebase_proposal,
    reject_proposal,
    submit_proposal,
)
from crm.store.keys import derive
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"
_OTHER_ACCOUNT = "acct-2"
_OPPORTUNITY = "opp-1"
_IDENTITY = Identity(rep_id=_REP, tenant=_TENANT, book=frozenset({_ACCOUNT}), source="gateway")


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _key() -> str:
    return uuid.uuid4().hex[:32]


def _event(args: dict[str, Any]) -> dict[str, Any]:
    return identity_event(
        IDENTITY_KEY,
        {
            "rep_id": _REP,
            "book": [_ACCOUNT, _OTHER_ACCOUNT],
            "full_visibility": False,
            "principal_type": "user",
            "session_id": "sess-1",
            "source": "gateway",
            "tenant": _TENANT,
        },
        args,
    )


def _seed_account(store: Store, account_id: str = _ACCOUNT) -> None:
    store.put_item(
        "accounts",
        derive(
            "accounts",
            {"account_id": account_id, "tenant": _TENANT, "owner_rep_id": _REP, "name": "Acme"},
        ),
    )


def _seed_contact(
    store: Store, contact_id: str = "cont-1", account_id: str = _ACCOUNT, tenant: str = _TENANT
) -> None:
    store.put_item(
        "contacts",
        derive(
            "contacts",
            {
                "contact_id": contact_id,
                "tenant": tenant,
                "account_id": account_id,
                "name": "Jane Doe",
                "status": "active",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        ),
    )


def _seed_opportunity(store: Store, **overrides: object) -> None:
    item: dict[str, object] = {
        "opportunity_id": _OPPORTUNITY,
        "tenant": _TENANT,
        "account_id": _ACCOUNT,
        "owner_rep_id": _REP,
        "name": "Acme Renewal",
        "stage": "Discovery",
        "value": 1000,
        "primary_contact_id": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    item.update(overrides)
    store.put_item("opportunities", derive("opportunities", item))


def _opportunity(store: Store) -> dict[str, object]:
    item = store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})
    assert item is not None
    return item


def _proposal(store: Store, proposal_id: str) -> dict[str, object]:
    item = store.get_item("proposals", {"proposal_id": proposal_id})
    assert item is not None
    return item


def _submit(
    store: Store,
    *,
    target_table: str = "opportunities",
    target_id: str = _OPPORTUNITY,
    operation: str = "update",
    payload: dict[str, object] | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "target_table": target_table,
        "target_id": target_id,
        "operation": operation,
        "reason": "integrity",
        "transaction_key": _key(),
    }
    if payload is not None:
        args["after_payload"] = payload
    result: dict[str, Any] = submit_proposal.handler(_event(args), store=store)
    return result


def _submitted_id(result: dict[str, Any]) -> str:
    assert result["statusCode"] == 200, result
    return str(result["body"]["proposal"]["proposal_id"])


def _approve(store: Store, proposal_id: str) -> dict[str, Any]:
    result: dict[str, Any] = approve_proposal.handler(
        _event({"proposal_id": proposal_id, "transaction_key": _key()}), store=store
    )
    return result


def _rebase(store: Store, proposal_id: str) -> dict[str, Any]:
    result: dict[str, Any] = rebase_proposal.handler(
        _event({"proposal_id": proposal_id, "transaction_key": _key()}), store=store
    )
    return result


def _assert_refused(result: dict[str, Any], *, status: int, source: str) -> None:
    assert result["statusCode"] == status, result
    assert result["error"]["source"] == source, result


def _row_count(store: Store, table: str) -> int:
    return len(store._client.scan(TableName=store.physical_name(table))["Items"])


def _audit_count(store: Store) -> int:
    return _row_count(store, "audit_records")


# --- require_primary_contact_in_account -------------------------------------------------------


@pytest.mark.parametrize("contact_id", [None, ""])
def test_absent_or_cleared_contact_is_accepted_without_a_contact(
    store: Store, contact_id: object
) -> None:
    require_primary_contact_in_account(store, _IDENTITY, _ACCOUNT, contact_id)


def test_contact_under_the_same_account_is_accepted(store: Store) -> None:
    _seed_contact(store)
    require_primary_contact_in_account(store, _IDENTITY, _ACCOUNT, "cont-1")


@pytest.mark.parametrize(
    ("contact_account", "contact_tenant", "contact_id"),
    [
        (_OTHER_ACCOUNT, _TENANT, "cont-1"),
        (_ACCOUNT, "other-tenant", "cont-1"),
        (_ACCOUNT, _TENANT, "cont-missing"),
    ],
    ids=["cross_account", "cross_tenant", "dangling"],
)
def test_contact_outside_the_account_is_refused_with_the_given_source(
    store: Store, contact_account: str, contact_tenant: str, contact_id: str
) -> None:
    _seed_contact(store, account_id=contact_account, tenant=contact_tenant)
    with pytest.raises(ValidationFailed) as raised:
        require_primary_contact_in_account(
            store, _IDENTITY, _ACCOUNT, contact_id, source="after_payload.primary_contact_id"
        )
    assert raised.value.source == "after_payload.primary_contact_id"


# --- create_opportunity -----------------------------------------------------------------------


def _create(store: Store, primary_contact_id: object) -> dict[str, Any]:
    result: dict[str, Any] = create_opportunity.handler(
        _event(
            {
                "account_id": _ACCOUNT,
                "name": "New Deal",
                "primary_contact_id": primary_contact_id,
                "transaction_key": _key(),
            }
        ),
        store=store,
    )
    return result


def test_create_refuses_a_cross_account_primary_contact(store: Store) -> None:
    _seed_account(store)
    _seed_contact(store, account_id=_OTHER_ACCOUNT)
    _assert_refused(_create(store, "cont-1"), status=400, source="primary_contact_id")


def test_create_accepts_a_same_account_primary_contact(store: Store) -> None:
    _seed_account(store)
    _seed_contact(store)
    result = _create(store, "cont-1")
    assert result["statusCode"] == 200, result
    assert result["body"]["opportunity"]["primary_contact_id"] == "cont-1"


def test_create_treats_an_empty_primary_contact_as_absent(store: Store) -> None:
    _seed_account(store)
    result = _create(store, "")
    assert result["statusCode"] == 200, result


# --- submit_proposal: primary contact ---------------------------------------------------------


@pytest.mark.parametrize("contact_id", ["cont-other", "cont-missing"], ids=["cross", "dangling"])
def test_submit_refuses_a_primary_contact_outside_the_account(
    store: Store, contact_id: str
) -> None:
    _seed_opportunity(store)
    _seed_contact(store, contact_id="cont-other", account_id=_OTHER_ACCOUNT)

    result = _submit(store, payload={"primary_contact_id": contact_id})

    _assert_refused(result, status=400, source="after_payload.primary_contact_id")
    assert _row_count(store, "proposals") == 0


@pytest.mark.parametrize("contact_id", ["cont-1", ""], ids=["same_account", "cleared"])
def test_submit_accepts_a_same_account_or_cleared_primary_contact(
    store: Store, contact_id: str
) -> None:
    _seed_opportunity(store, primary_contact_id="cont-1")
    _seed_contact(store)
    _submitted_id(_submit(store, payload={"primary_contact_id": contact_id}))


# --- approve_proposal: primary contact --------------------------------------------------------


def test_approve_refuses_a_contact_moved_to_another_account_after_submit(store: Store) -> None:
    _seed_opportunity(store)
    _seed_contact(store)
    proposal_id = _submitted_id(_submit(store, payload={"primary_contact_id": "cont-1"}))
    _seed_contact(store, account_id=_OTHER_ACCOUNT)

    result = _approve(store, proposal_id)

    _assert_refused(result, status=400, source="after_payload.primary_contact_id")
    assert _opportunity(store)["primary_contact_id"] is None
    assert _proposal(store, proposal_id)["status"] == "pending"


def test_approve_refuses_a_contact_deleted_after_submit(store: Store) -> None:
    _seed_opportunity(store)
    _seed_contact(store)
    proposal_id = _submitted_id(_submit(store, payload={"primary_contact_id": "cont-1"}))
    store.delete_item("contacts", {"contact_id": "cont-1"})

    result = _approve(store, proposal_id)

    _assert_refused(result, status=400, source="after_payload.primary_contact_id")
    assert _opportunity(store)["primary_contact_id"] is None


def test_approve_applies_a_same_account_primary_contact(store: Store) -> None:
    _seed_opportunity(store)
    _seed_contact(store)
    proposal_id = _submitted_id(_submit(store, payload={"primary_contact_id": "cont-1"}))

    assert _approve(store, proposal_id)["statusCode"] == 200
    assert _opportunity(store)["primary_contact_id"] == "cont-1"


# --- approve_proposal: delete cascades --------------------------------------------------------


def test_approve_refuses_an_account_delete_once_an_open_opportunity_exists(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store, stage="Closed Won")
    proposal_id = _submitted_id(
        _submit(store, target_table="accounts", target_id=_ACCOUNT, operation="delete")
    )
    _seed_opportunity(store, opportunity_id="opp-open", stage="Negotiation")

    result = _approve(store, proposal_id)

    _assert_refused(result, status=400, source="account_id")
    assert store.get_item("accounts", {"account_id": _ACCOUNT}) is not None
    assert _proposal(store, proposal_id)["status"] == "pending"


def test_approve_refuses_a_contact_delete_once_the_contact_is_referenced(store: Store) -> None:
    _seed_contact(store)
    _seed_opportunity(store)
    proposal_id = _submitted_id(
        _submit(store, target_table="contacts", target_id="cont-1", operation="delete")
    )
    _seed_opportunity(store, primary_contact_id="cont-1")

    result = _approve(store, proposal_id)

    _assert_refused(result, status=400, source="contact_id")
    assert store.get_item("contacts", {"contact_id": "cont-1"}) is not None


def test_approve_applies_an_account_delete_with_only_closed_opportunities(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store, stage="Closed Lost")
    proposal_id = _submitted_id(
        _submit(store, target_table="accounts", target_id=_ACCOUNT, operation="delete")
    )

    assert _approve(store, proposal_id)["statusCode"] == 200
    assert store.get_item("accounts", {"account_id": _ACCOUNT}) is None


def test_a_decided_proposal_reports_already_decided_not_a_cascade_refusal(store: Store) -> None:
    _seed_account(store)
    proposal_id = _submitted_id(
        _submit(store, target_table="accounts", target_id=_ACCOUNT, operation="delete")
    )
    rejected = reject_proposal.handler(
        _event({"proposal_id": proposal_id, "transaction_key": _key()}), store=store
    )
    assert rejected["statusCode"] == 200, rejected
    _seed_opportunity(store, stage="Negotiation")

    result = _approve(store, proposal_id)

    assert result["statusCode"] == 409, result
    assert result["error"]["reason"] == "already_decided"


# --- stage transition at approve and rebase ---------------------------------------------------


def _plant_snapshot(store: Store, proposal_id: str) -> None:
    """Make the proposal's before-snapshot match the record, as an unchecked rebase would."""
    store.put_item(
        "proposals", {**_proposal(store, proposal_id), "before_snapshot": _opportunity(store)}
    )


@pytest.mark.parametrize(
    ("closed_stage", "requested"),
    [("Closed Won", "Qualification"), ("Closed Won", "Closed Lost")],
    ids=["reopen", "closed_lateral"],
)
def test_approve_refuses_a_refused_move_from_the_current_stage(
    store: Store, closed_stage: str, requested: str
) -> None:
    _seed_opportunity(store, stage="Negotiation")
    proposal_id = _submitted_id(_submit(store, payload={"stage": requested}))
    _seed_opportunity(store, stage=closed_stage)
    _plant_snapshot(store, proposal_id)
    audits_before = _audit_count(store)

    result = _approve(store, proposal_id)

    _assert_refused(result, status=400, source="after_payload.stage")
    assert _opportunity(store)["stage"] == closed_stage
    assert _proposal(store, proposal_id)["status"] == "pending"
    assert _audit_count(store) == audits_before


@pytest.mark.parametrize(
    ("closed_stage", "requested"),
    [("Closed Won", "Qualification"), ("Closed Won", "Closed Lost")],
    ids=["reopen", "closed_lateral"],
)
def test_rebase_refuses_a_refused_move_before_updating_the_snapshot(
    store: Store, closed_stage: str, requested: str
) -> None:
    _seed_opportunity(store, stage="Negotiation")
    proposal_id = _submitted_id(_submit(store, payload={"stage": requested}))
    snapshot_before = _proposal(store, proposal_id)["before_snapshot"]
    _seed_opportunity(store, stage=closed_stage)
    audits_before = _audit_count(store)

    result = _rebase(store, proposal_id)

    _assert_refused(result, status=400, source="after_payload.stage")
    assert _proposal(store, proposal_id)["before_snapshot"] == snapshot_before
    assert _audit_count(store) == audits_before


def test_a_regression_after_rebase_is_still_approved(store: Store) -> None:
    """A racing advance, a fields_changed refusal, a rebase, then the approved regression."""
    _seed_opportunity(store, stage="Discovery")
    proposal_id = _submitted_id(_submit(store, payload={"stage": "Qualification"}))
    _seed_opportunity(store, stage="Negotiation")

    refused = _approve(store, proposal_id)
    assert refused["statusCode"] == 409, refused
    assert refused["error"]["reason"] == "fields_changed"

    rebased = _rebase(store, proposal_id)
    assert rebased["statusCode"] == 200, rebased

    approved = _approve(store, proposal_id)
    assert approved["statusCode"] == 200, approved
    assert _opportunity(store)["stage"] == "Qualification"

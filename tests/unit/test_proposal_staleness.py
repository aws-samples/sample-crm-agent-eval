# SPDX-License-Identifier: MIT
"""Approve refuses a proposal left pending past ``PROPOSAL_MAX_PENDING_AGE``; reject still works.

Runs the library functions and the real handlers against a ``moto``-mocked DynamoDB.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import (
    PROPOSAL_STALE_MESSAGE,
    TAXONOMY,
    Conflict,
    ProposalStale,
)
from crm.handlers import approve_proposal as approve_handler
from crm.handlers import reject_proposal as reject_handler
from crm.handlers import submit_proposal as submit_handler
from crm.handlers._common import idempotent_id
from crm.proposals import (
    PROPOSAL_MAX_PENDING_AGE,
    approve_proposal,
    reject_proposal,
    submit_proposal,
)
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"
_OPPORTUNITY = "opp-1"
_SUBMITTED = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _seed(store: Store) -> None:
    store.put_item(
        "accounts",
        {
            "account_id": _ACCOUNT,
            "tenant": _TENANT,
            "tenant_owner_rep_id": f"{_TENANT}#{_REP}",
            "owner_rep_id": _REP,
            "name": "Acme",
            "name_lower": "acme",
        },
    )
    store.put_item(
        "opportunities",
        {
            "opportunity_id": _OPPORTUNITY,
            "tenant": _TENANT,
            "tenant_account_id": f"{_TENANT}#{_ACCOUNT}",
            "tenant_owner_rep_id": f"{_TENANT}#{_REP}",
            "tenant_stage": f"{_TENANT}#Discovery",
            "account_id": _ACCOUNT,
            "owner_rep_id": _REP,
            "name": "Big Deal",
            "name_lower": "big deal",
            "stage": "Discovery",
            "value": 1000,
            "close_date": "2026-06-30",
            "primary_contact_id": None,
            "last_touch": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )


def _submit(store: Store, *, proposal_id: str = "prop-1", now: datetime = _SUBMITTED) -> str:
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id=proposal_id,
        target_table="opportunities",
        target_id=_OPPORTUNITY,
        operation="update",
        actor_rep_id=_REP,
        reason="raise the value",
        transaction_key=f"txn-submit-{proposal_id}",
        after_payload={"value": 2000},
        now=_iso(now),
    )
    return proposal_id


def _approve(store: Store, proposal_id: str, now: datetime, *, key: str = "txn-approve") -> Any:
    return approve_proposal(
        store,
        tenant=_TENANT,
        proposal_id=proposal_id,
        actor_rep_id=_REP,
        transaction_key=key,
        now=_iso(now),
    )


def _proposal(store: Store, proposal_id: str) -> dict[str, Any]:
    item = store.get_item("proposals", {"proposal_id": proposal_id})
    assert item is not None
    return item


def _value(store: Store) -> object:
    item = store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})
    assert item is not None
    return item["value"]


def _set_created_at(store: Store, proposal_id: str, created_at: object) -> None:
    item = _proposal(store, proposal_id)
    if created_at is None:
        item.pop("created_at", None)
    else:
        item["created_at"] = created_at
    store.put_item("proposals", item)


def test_the_approval_window_is_seven_days() -> None:
    assert timedelta(days=7) == PROPOSAL_MAX_PENDING_AGE


def test_a_proposal_exactly_at_the_window_edge_still_approves(store: Store) -> None:
    _seed(store)
    proposal_id = _submit(store)

    result = _approve(store, proposal_id, _SUBMITTED + PROPOSAL_MAX_PENDING_AGE)

    assert result["status"] == "approved"
    assert _value(store) == 2000


def test_a_proposal_one_second_past_the_window_is_refused_and_nothing_is_written(
    store: Store,
) -> None:
    _seed(store)
    proposal_id = _submit(store)
    before = _proposal(store, proposal_id)

    with pytest.raises(ProposalStale) as excinfo:
        _approve(store, proposal_id, _SUBMITTED + PROPOSAL_MAX_PENDING_AGE + timedelta(seconds=1))

    assert excinfo.value.code == "proposal_stale"
    assert _proposal(store, proposal_id) == before
    assert _value(store) == 1000


def test_a_stale_proposal_can_still_be_rejected(store: Store) -> None:
    _seed(store)
    proposal_id = _submit(store)
    later = _SUBMITTED + PROPOSAL_MAX_PENDING_AGE * 4

    with pytest.raises(ProposalStale):
        _approve(store, proposal_id, later)
    reject_proposal(
        store,
        tenant=_TENANT,
        proposal_id=proposal_id,
        actor_rep_id=_REP,
        transaction_key="txn-reject",
        now=_iso(later),
    )

    assert _proposal(store, proposal_id)["status"] == "rejected"
    assert _value(store) == 1000


@pytest.mark.parametrize("created_at", [None, "not-a-timestamp", 1700000000])
def test_an_absent_or_unreadable_created_at_is_refused_as_stale(
    store: Store, created_at: object
) -> None:
    _seed(store)
    proposal_id = _submit(store)
    _set_created_at(store, proposal_id, created_at)

    with pytest.raises(ProposalStale):
        _approve(store, proposal_id, _SUBMITTED)

    assert _proposal(store, proposal_id)["status"] == "pending"


def test_a_naive_created_at_is_read_as_utc(store: Store) -> None:
    _seed(store)
    proposal_id = _submit(store)
    _set_created_at(store, proposal_id, "2026-03-01T12:00:00")

    result = _approve(store, proposal_id, _SUBMITTED + PROPOSAL_MAX_PENDING_AGE)

    assert result["status"] == "approved"


def test_an_unreadable_now_is_a_caller_error_not_a_stale_refusal(store: Store) -> None:
    _seed(store)
    proposal_id = _submit(store)

    with pytest.raises(ValueError, match="ISO-8601"):
        approve_proposal(
            store,
            tenant=_TENANT,
            proposal_id=proposal_id,
            actor_rep_id=_REP,
            transaction_key="txn-approve",
            now="yesterday",
        )


def test_an_already_decided_old_proposal_is_still_a_conflict_not_stale(store: Store) -> None:
    _seed(store)
    proposal_id = _submit(store)
    _approve(store, proposal_id, _SUBMITTED, key="txn-first")

    with pytest.raises(Conflict) as excinfo:
        _approve(store, proposal_id, _SUBMITTED + PROPOSAL_MAX_PENDING_AGE * 2, key="txn-second")

    assert excinfo.value.reason == "already_decided"


def test_a_retried_committed_approval_replays_even_past_the_window(store: Store) -> None:
    _seed(store)
    proposal_id = _submit(store)
    _approve(store, proposal_id, _SUBMITTED, key="txn-approve")

    replay = _approve(store, proposal_id, _SUBMITTED + PROPOSAL_MAX_PENDING_AGE * 2)

    assert replay["status"] == "approved"


def test_proposal_stale_is_a_taxonomy_member_rendered_without_detail() -> None:
    assert ProposalStale in TAXONOMY
    error = ProposalStale(detail="SECRET_INTERNAL")

    payload = error.to_api_response()

    assert payload == {
        "statusCode": 409,
        "error": {
            "code": "proposal_stale",
            "message": PROPOSAL_STALE_MESSAGE,
            "next_step": "tell_user",
        },
    }
    assert "SECRET_INTERNAL" not in repr(error.to_mcp_error())


def _gateway_event(arguments: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "rep_id": _REP,
        "book": [_ACCOUNT],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": _TENANT,
    }
    return identity_event(IDENTITY_KEY, payload, arguments)


def _submit_through_handler(store: Store) -> str:
    result = submit_handler.handler(
        _gateway_event(
            {
                "target_table": "opportunities",
                "target_id": _OPPORTUNITY,
                "operation": "update",
                "after_payload": {"value": 2000},
                "reason": "raise the value",
                "transaction_key": "txn-submit-handler",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 200, result
    return str(result["body"]["proposal"]["proposal_id"])


def test_the_approve_handler_returns_proposal_stale_for_a_backdated_proposal(
    store: Store,
) -> None:
    _seed(store)
    proposal_id = _submit_through_handler(store)
    backdated = datetime.now(UTC) - PROPOSAL_MAX_PENDING_AGE - timedelta(hours=1)
    _set_created_at(store, proposal_id, _iso(backdated))

    result = approve_handler.handler(
        _gateway_event({"proposal_id": proposal_id, "transaction_key": "txn-approve-handler"}),
        store=store,
    )

    assert result["statusCode"] == 409, result
    assert result["error"] == {
        "code": "proposal_stale",
        "message": PROPOSAL_STALE_MESSAGE,
        "next_step": "tell_user",
    }
    assert _proposal(store, proposal_id)["status"] == "pending"
    assert _value(store) == 1000
    audit_id = idempotent_id("audit", "txn-approve-handler")
    assert store.get_item("audit_records", {"audit_id": audit_id}) is None

    rejected = reject_handler.handler(
        _gateway_event({"proposal_id": proposal_id, "transaction_key": "txn-reject-handler"}),
        store=store,
    )
    assert rejected["statusCode"] == 200, rejected
    assert _proposal(store, proposal_id)["status"] == "rejected"


def test_the_approve_handler_still_approves_a_fresh_proposal(store: Store) -> None:
    _seed(store)
    proposal_id = _submit_through_handler(store)

    result = approve_handler.handler(
        _gateway_event({"proposal_id": proposal_id, "transaction_key": "txn-approve-fresh"}),
        store=store,
    )

    assert result["statusCode"] == 200, result
    assert _value(store) == 2000

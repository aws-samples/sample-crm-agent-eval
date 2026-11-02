# SPDX-License-Identifier: MIT
"""Tests for the cascade rules in `crm.handlers.cascade_rules` and their handler call sites.

* Deleting an account with an open opportunity is refused, at the function level and through
  `submit_proposal`.
* Deleting a contact still referenced as an opportunity's primary contact is refused, while
  marking that contact `departed` is unaffected.
* `log_activity` is permitted on an in-book opportunity and denied on an out-of-book one.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import ValidationFailed
from crm.handlers import log_activity, submit_proposal
from crm.handlers.cascade_rules import (
    reject_account_delete_with_open_opportunities,
    reject_contact_delete_if_referenced,
)
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _event(args: dict[str, object], *, book: list[str] | None = None) -> dict[str, object]:
    return identity_event(
        IDENTITY_KEY,
        {
            "rep_id": _REP,
            "book": book if book is not None else [_ACCOUNT],
            "full_visibility": False,
            "principal_type": "user",
            "session_id": "sess-1",
            "source": "gateway",
            "tenant": _TENANT,
        },
        args,
    )


def _seed_account(store: Store, *, account_id: str = _ACCOUNT) -> None:
    store.put_item(
        "accounts",
        {
            "account_id": account_id,
            "tenant": _TENANT,
            "tenant_owner_rep_id": f"{_TENANT}#{_REP}",
            "owner_rep_id": _REP,
            "name": "Acme",
            "name_lower": "acme",
        },
    )


def _seed_contact(
    store: Store, *, contact_id: str = "cont-1", account_id: str = _ACCOUNT, status: str = "active"
) -> None:
    store.put_item(
        "contacts",
        {
            "contact_id": contact_id,
            "tenant": _TENANT,
            "tenant_account_id": f"{_TENANT}#{account_id}",
            "account_id": account_id,
            "name": "Jane Doe",
            "name_lower": "jane doe",
            "status": status,
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )


def _seed_opportunity(
    store: Store,
    *,
    opportunity_id: str = "opp-1",
    account_id: str = _ACCOUNT,
    stage: str = "Discovery",
    primary_contact_id: str | None = None,
) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": opportunity_id,
            "tenant": _TENANT,
            "tenant_account_id": f"{_TENANT}#{account_id}",
            "tenant_owner_rep_id": f"{_TENANT}#{_REP}",
            "tenant_stage": f"{_TENANT}#{stage}",
            "account_id": account_id,
            "owner_rep_id": _REP,
            "name": "Big Deal",
            "name_lower": "big deal",
            "stage": stage,
            "value": 1000,
            "primary_contact_id": primary_contact_id,
            "last_touch": None,
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )


# --------------------------------------------------------------------------------------
# Account delete with open opportunities
# --------------------------------------------------------------------------------------


def test_reject_account_delete_with_open_opportunity(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store, stage="Discovery")
    with pytest.raises(ValidationFailed):
        reject_account_delete_with_open_opportunities(store, tenant=_TENANT, account_id=_ACCOUNT)


def test_account_delete_permitted_when_only_closed_opportunities(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store, stage="Closed Won")
    reject_account_delete_with_open_opportunities(store, tenant=_TENANT, account_id=_ACCOUNT)


def test_account_delete_permitted_with_no_opportunities(store: Store) -> None:
    _seed_account(store)
    reject_account_delete_with_open_opportunities(store, tenant=_TENANT, account_id=_ACCOUNT)


def test_submit_proposal_refuses_account_delete_with_open_opportunity(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store, stage="Negotiation")
    result = submit_proposal.handler(
        _event(
            {
                "target_table": "accounts",
                "target_id": _ACCOUNT,
                "operation": "delete",
                "reason": "cleanup",
                "transaction_key": "tx-1",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 400
    # The account still exists.
    account = store.get_item("accounts", {"account_id": _ACCOUNT})
    assert account is not None


def test_submit_proposal_permits_account_delete_with_only_closed_opportunities(
    store: Store,
) -> None:
    _seed_account(store)
    _seed_opportunity(store, stage="Closed Lost")
    result = submit_proposal.handler(
        _event(
            {
                "target_table": "accounts",
                "target_id": _ACCOUNT,
                "operation": "delete",
                "reason": "cleanup",
                "transaction_key": "tx-1",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 200


# --------------------------------------------------------------------------------------
# Referenced primary contact
# --------------------------------------------------------------------------------------


def test_reject_contact_delete_if_referenced_as_primary_contact(store: Store) -> None:
    _seed_account(store)
    _seed_contact(store, contact_id="cont-1")
    _seed_opportunity(store, primary_contact_id="cont-1")
    with pytest.raises(ValidationFailed):
        reject_contact_delete_if_referenced(store, tenant=_TENANT, contact_id="cont-1")


def test_contact_delete_permitted_when_not_referenced(store: Store) -> None:
    _seed_account(store)
    _seed_contact(store, contact_id="cont-1")
    _seed_opportunity(store, primary_contact_id=None)
    reject_contact_delete_if_referenced(store, tenant=_TENANT, contact_id="cont-1")


def test_submit_proposal_refuses_contact_delete_when_referenced(store: Store) -> None:
    _seed_account(store)
    _seed_contact(store, contact_id="cont-1")
    _seed_opportunity(store, primary_contact_id="cont-1")
    result = submit_proposal.handler(
        _event(
            {
                "target_table": "contacts",
                "target_id": "cont-1",
                "operation": "delete",
                "reason": "cleanup",
                "transaction_key": "tx-1",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 400
    contact = store.get_item("contacts", {"contact_id": "cont-1"})
    assert contact is not None


def test_marking_referenced_contact_departed_is_unaffected_by_the_delete_rule(store: Store) -> None:
    """Marking a referenced contact departed succeeds and leaves `primary_contact_id` intact."""
    from crm.handlers import update_contact

    _seed_account(store)
    _seed_contact(store, contact_id="cont-1", status="active")
    _seed_opportunity(store, primary_contact_id="cont-1")

    result = update_contact.handler(
        {
            "_identity": {
                "rep_id": _REP,
                "book": [_ACCOUNT],
                "full_visibility": False,
                "principal_type": "user",
                "session_id": "s",
                "source": "gateway",
                "tenant": _TENANT,
            },
            "contact_id": "cont-1",
            "fields": {"status": "departed"},
            "transaction_key": "tx-1",
        },
        store=store,
    )
    assert result["statusCode"] == 200
    assert result["body"]["contact"]["status"] == "departed"

    opportunity = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert opportunity is not None
    assert opportunity["primary_contact_id"] == "cont-1"


# --------------------------------------------------------------------------------------
# Logging an activity against an in-book or out-of-book opportunity
# --------------------------------------------------------------------------------------


def test_log_activity_permitted_when_opportunity_is_in_book(store: Store) -> None:
    """`log_activity` succeeds on an opportunity in the caller's book."""
    _seed_account(store)
    _seed_opportunity(store)
    result = log_activity.handler(
        _event(
            {
                "opportunity_id": "opp-1",
                "kind": "call",
                "subject": "Checked in",
                "transaction_key": "tx-1",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 200


def test_log_activity_denied_identically_to_a_read_when_out_of_book(store: Store) -> None:
    """`log_activity` on an out-of-book opportunity returns 403 and writes nothing."""
    _seed_account(store)
    _seed_opportunity(store, opportunity_id="opp-1")
    result = log_activity.handler(
        _event(
            {
                "opportunity_id": "opp-1",
                "kind": "call",
                "subject": "Checked in",
                "transaction_key": "tx-1",
            },
            book=[],
        ),
        store=store,
    )
    assert result["statusCode"] == 403
    assert result["error"]["code"] == "scope_denied"
    opportunity = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert opportunity is not None
    assert opportunity["last_touch"] is None
    activities, _ = store.query_gsi(
        "activities", "gsi_opportunity", partition_value=f"{_TENANT}#opp-1"
    )
    assert activities == []


def test_log_activity_not_found_for_missing_opportunity(store: Store) -> None:
    result = log_activity.handler(
        _event(
            {
                "opportunity_id": "does-not-exist",
                "kind": "call",
                "subject": "x",
                "transaction_key": "tx-1",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 404

# SPDX-License-Identifier: MIT
"""Tests for what a proposal's ``after_payload`` may and may not carry.

1. The permitted set per table is exactly
   ``UPDATE_ALLOWLISTS[table] | high_risk_fields_for(table)``.
2. A key-redirecting, re-tenanting, or ownership-reassigning payload is refused **at submit** on all
   three identity planes, with the victim row unchanged and no proposal stored.
3. The refusal is re-derived **at approve** from a stored proposal row, and the ``Put``-key guard
   refuses key redirection even with the allowlist widened.
4. A ``delete`` proposal's ``after_payload`` is validated and then not stored.

Legitimate changes, including an opportunity stage change with recomputed derived index
attributes, still commit.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, cast

import boto3
import pytest
from moto import mock_aws

from crm.domain import allowlists
from crm.domain.allowlists import (
    UNKNOWN_FIELD_SOURCE,
    proposal_payload_allowlist,
    validate_proposal_payload,
)
from crm.envelope import (
    AGENT_IDENTITY_KEY,
    CLAIM_BOOK,
    CLAIM_FULL_VISIBILITY,
    CLAIM_REP_ID,
    CLAIM_TENANT,
    IDENTITY_KEY,
    REQUEST_CONTEXT_KEY,
)
from crm.errors import ValidationFailed
from crm.handlers import approve_proposal, submit_proposal
from crm.proposals import submit_proposal as submit_proposal_domain
from crm.store.keys import DERIVED_ATTRIBUTES, derive
from crm.store.provisioning import create_all_tables
from crm.store.schema import table_schema
from crm.store.store import Store
from crm.toolspec.definitions import TABLES, allowlist_for, high_risk_fields_for

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

#: Neither tenant is `demo`. Everything here is in-memory `moto`; no AWS call is made.
_ATTACKER_TENANT = "eval-smoke-payload-attacker"
_VICTIM_TENANT = "eval-smoke-payload-victim"
_ATTACKER_REP = "rep-attacker"
_VICTIM_REP = "rep-victim"
_ATTACKER_ACCOUNT = "acc-attacker"
_VICTIM_ACCOUNT = "acc-victim"
_OPPORTUNITY = "opp-attacker"
_ACTIVITY = "act-attacker"

_PLANES = [IDENTITY_KEY, AGENT_IDENTITY_KEY, REQUEST_CONTEXT_KEY]
_PLANE_IDS = ["gateway_mcp_plane", "agent_plane", "ui_api_plane"]

#: The planes that may approve; the agent plane's refusal is `test_proposal_decision_channel.py`'s.
_DECIDING_PLANES = [IDENTITY_KEY, REQUEST_CONTEXT_KEY]
_DECIDING_PLANE_IDS = ["gateway_mcp_plane", "ui_api_plane"]


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _identity_block(channel: str) -> dict[str, Any]:
    """Build the attacker's identity block in the shape ``channel`` requires.

    All three channels resolve to the same `Identity`: `rep-attacker`, book `{acc-attacker}`, no
    full visibility, attacker tenant.
    """
    if channel == IDENTITY_KEY:
        return {
            "rep_id": _ATTACKER_REP,
            "book": [_ATTACKER_ACCOUNT],
            "full_visibility": False,
            "principal_type": "user",
            "session_id": "sess-payload",
            "source": "gateway",
            "tenant": _ATTACKER_TENANT,
        }
    claims = {
        CLAIM_REP_ID: _ATTACKER_REP,
        CLAIM_BOOK: _ATTACKER_ACCOUNT,
        CLAIM_FULL_VISIBILITY: "false",
        CLAIM_TENANT: _ATTACKER_TENANT,
    }
    if channel == AGENT_IDENTITY_KEY:
        return {"claims": claims}
    return {"authorizer": {"jwt": {"claims": claims}}}


def _event(channel: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Build a flat action-Lambda event carrying the attacker's identity on ``channel``."""
    event: dict[str, Any] = dict(arguments)
    event[channel] = _identity_block(channel)
    return event


def _seed_account(
    store: Store, *, account_id: str, tenant: str, owner: str, name: str
) -> dict[str, object]:
    item = derive(
        "accounts",
        {
            "account_id": account_id,
            "tenant": tenant,
            "owner_rep_id": owner,
            "name": name,
            "industry": "Software",
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
        },
    )
    store.put_item("accounts", item)
    return item


def _seed_opportunity(store: Store) -> dict[str, object]:
    item = derive(
        "opportunities",
        {
            "opportunity_id": _OPPORTUNITY,
            "tenant": _ATTACKER_TENANT,
            "account_id": _ATTACKER_ACCOUNT,
            "owner_rep_id": _ATTACKER_REP,
            "name": "Renewal",
            "stage": "Discovery",
            "value": 1000.0,
            "close_date": "2026-06-01",
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
        },
    )
    store.put_item("opportunities", item)
    return item


def _seed_activity(store: Store) -> dict[str, object]:
    item = derive(
        "activities",
        {
            "activity_id": _ACTIVITY,
            "tenant": _ATTACKER_TENANT,
            "account_id": _ATTACKER_ACCOUNT,
            "opportunity_id": _OPPORTUNITY,
            "owner_rep_id": _ATTACKER_REP,
            "kind": "call",
            "subject": "Intro call",
            "body": "Discussed renewal.",
            "occurred_at": "2024-01-01T00:00:00+00:00",
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
        },
    )
    store.put_item("activities", item)
    return item


@pytest.fixture
def seeded(store: Store) -> Store:
    _seed_account(
        store,
        account_id=_ATTACKER_ACCOUNT,
        tenant=_ATTACKER_TENANT,
        owner=_ATTACKER_REP,
        name="Attacker Co",
    )
    _seed_account(
        store,
        account_id=_VICTIM_ACCOUNT,
        tenant=_VICTIM_TENANT,
        owner=_VICTIM_REP,
        name="VICTIM CONFIDENTIAL",
    )
    _seed_opportunity(store)
    _seed_activity(store)
    return store


def _victim_row(store: Store) -> dict[str, object] | None:
    return store.get_item("accounts", {"account_id": _VICTIM_ACCOUNT})


def _submit(
    store: Store, channel: str, after_payload: dict[str, Any], *, transaction_key: str
) -> dict[str, Any]:
    arguments = {
        "target_table": "accounts",
        "target_id": _ATTACKER_ACCOUNT,
        "operation": "update",
        "after_payload": after_payload,
        "reason": "routine rename",
        "transaction_key": transaction_key,
    }
    return cast(
        dict[str, Any], submit_proposal.handler(_event(channel, arguments), None, store=store)
    )


# --------------------------------------------------------------------------------------
# Property 1 — the permitted set is the union, per table
# --------------------------------------------------------------------------------------


def test_permitted_set_is_exactly_the_union_of_the_update_allowlist_and_the_high_risk_fields() -> (
    None
):
    """The permitted set equals the allowlist union high-risk fields, read from the definitions."""
    for table in sorted(TABLES):
        assert proposal_payload_allowlist(table) == allowlist_for(table) | high_risk_fields_for(
            table
        )


def test_no_table_permits_its_partition_key_its_tenant_or_a_derived_or_system_attribute() -> None:
    """Key redirection, re-tenanting, and supplied derived attributes are excluded by the rule."""
    for table in sorted(TABLES):
        permitted = proposal_payload_allowlist(table)
        assert table_schema(table).partition_key not in permitted
        assert "tenant" not in permitted
        assert "created_at" not in permitted
        assert "updated_at" not in permitted
        for derived in DERIVED_ATTRIBUTES[table]:
            assert derived.name not in permitted


def test_accounts_permit_a_rename_but_never_ownership_reassignment() -> None:
    permitted = proposal_payload_allowlist("accounts")
    assert "name" in permitted
    assert "account_id" not in permitted
    assert "owner_rep_id" not in permitted


def test_opportunities_permit_every_declared_high_risk_field() -> None:
    """Every declared high-risk field, including `owner_rep_id`, is permitted on opportunities."""
    permitted = proposal_payload_allowlist("opportunities")
    assert {"stage", "value", "close_date", "owner_rep_id", "primary_contact_id"} <= permitted
    assert "opportunity_id" not in permitted
    assert "tenant" not in permitted
    assert "tenant_stage" not in permitted


def test_validate_proposal_payload_reports_a_fixed_source_not_the_offending_key() -> None:
    with pytest.raises(ValidationFailed) as excinfo:
        validate_proposal_payload("accounts", {"name": "ok", "account_id": _VICTIM_ACCOUNT})
    assert excinfo.value.source == f"after_payload.{UNKNOWN_FIELD_SOURCE}"
    assert "account_id" not in excinfo.value.message


def test_validate_proposal_payload_accepts_an_empty_or_absent_payload() -> None:
    validate_proposal_payload("accounts", None)
    validate_proposal_payload("accounts", {})


def test_validate_proposal_payload_rejects_an_unknown_table() -> None:
    with pytest.raises(ValueError):
        validate_proposal_payload("not_a_table", {"name": "x"})


# --------------------------------------------------------------------------------------
# Property 2 — the exploit and both variants are refused at submit, on all three planes
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("channel", _PLANES, ids=_PLANE_IDS)
def test_key_redirecting_after_payload_is_refused_at_submit_on_every_plane(
    seeded: Store, channel: str
) -> None:
    """The exploit itself: `after_payload` carrying another tenant's partition key."""
    before = _victim_row(seeded)
    response = _submit(
        seeded,
        channel,
        {"account_id": _VICTIM_ACCOUNT, "name": "OVERWRITTEN_BY_ATTACKER"},
        transaction_key=f"tx-redirect-{channel}",
    )

    assert response["statusCode"] == 400
    assert response["error"]["code"] == "validation_failed"
    assert _victim_row(seeded) == before
    stored, _cursor = seeded.query_tenant_scan("proposals", tenant=_ATTACKER_TENANT)
    assert stored == []


@pytest.mark.parametrize("channel", _PLANES, ids=_PLANE_IDS)
def test_re_tenanting_after_payload_is_refused_at_submit_on_every_plane(
    seeded: Store, channel: str
) -> None:
    """Variant 1: `after_payload={'tenant': X}` moved the caller's own record to another tenant."""
    response = _submit(
        seeded,
        channel,
        {"tenant": _VICTIM_TENANT},
        transaction_key=f"tx-tenant-{channel}",
    )

    assert response["statusCode"] == 400
    assert response["error"]["code"] == "validation_failed"
    attacker_row = seeded.get_item("accounts", {"account_id": _ATTACKER_ACCOUNT})
    assert attacker_row is not None
    assert attacker_row["tenant"] == _ATTACKER_TENANT


@pytest.mark.parametrize("channel", _PLANES, ids=_PLANE_IDS)
def test_ownership_reassigning_after_payload_is_refused_at_submit_on_every_plane(
    seeded: Store, channel: str
) -> None:
    """Variant 2: `after_payload={'owner_rep_id': X}` on a table where it is not high-risk."""
    response = _submit(
        seeded,
        channel,
        {"owner_rep_id": _ATTACKER_REP, "name": "Taken Over"},
        transaction_key=f"tx-owner-{channel}",
    )

    assert response["statusCode"] == 400
    assert response["error"]["code"] == "validation_failed"


@pytest.mark.parametrize("channel", _PLANES, ids=_PLANE_IDS)
def test_supplied_derived_attribute_is_refused_at_submit_on_every_plane(
    seeded: Store, channel: str
) -> None:
    """A derived index attribute is computed, never supplied."""
    response = _submit(
        seeded,
        channel,
        {"name": "Renamed", "name_lower": "poisoned"},
        transaction_key=f"tx-derived-{channel}",
    )

    assert response["statusCode"] == 400
    assert response["error"]["code"] == "validation_failed"


# --------------------------------------------------------------------------------------
# Property 3 — the approve-time re-validation and the Put-key guard, each independently
# --------------------------------------------------------------------------------------


def _store_poisoned_proposal(store: Store, after_payload: dict[str, Any]) -> str:
    """Persist a pending proposal carrying ``after_payload`` **without** submit's validation."""
    proposal_id = "prop-poisoned"
    before = store.get_item("accounts", {"account_id": _ATTACKER_ACCOUNT})
    assert before is not None
    store.put_item(
        "proposals",
        derive(
            "proposals",
            {
                "proposal_id": proposal_id,
                "tenant": _ATTACKER_TENANT,
                "status": "pending",
                "target_table": "accounts",
                "target_id": _ATTACKER_ACCOUNT,
                "operation": "update",
                "before_snapshot": dict(before),
                "after_payload": dict(after_payload),
                "reason": "routine rename",
                "actor_rep_id": _ATTACKER_REP,
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            },
        ),
    )
    return proposal_id


def _approve(
    store: Store, channel: str, proposal_id: str, *, transaction_key: str
) -> dict[str, Any]:
    arguments = {"proposal_id": proposal_id, "transaction_key": transaction_key}
    return cast(
        dict[str, Any], approve_proposal.handler(_event(channel, arguments), None, store=store)
    )


@pytest.mark.parametrize("channel", _DECIDING_PLANES, ids=_DECIDING_PLANE_IDS)
def test_a_stored_key_redirecting_payload_is_refused_at_approve_on_every_plane(
    seeded: Store, channel: str
) -> None:
    """A stored key-redirecting payload never commits, whichever approve-time layer refuses it."""
    before = _victim_row(seeded)
    proposal_id = _store_poisoned_proposal(
        seeded, {"account_id": _VICTIM_ACCOUNT, "name": "OVERWRITTEN_BY_ATTACKER"}
    )

    response = _approve(seeded, channel, proposal_id, transaction_key=f"tx-approve-{channel}")

    assert response["statusCode"] == 400
    assert response["error"]["code"] == "validation_failed"
    assert _victim_row(seeded) == before
    stored = seeded.get_item("proposals", {"proposal_id": proposal_id})
    assert stored is not None
    assert stored["status"] == "pending"


@pytest.mark.parametrize("poisoned_field", ["created_at", "name_lower"])
@pytest.mark.parametrize("channel", _DECIDING_PLANES, ids=_DECIDING_PLANE_IDS)
def test_a_stored_non_key_forbidden_field_is_refused_by_the_approve_time_allowlist_alone(
    seeded: Store, channel: str, poisoned_field: str
) -> None:
    """The approve-time allowlist re-validation alone refuses a stored **non-key** forbidden field.

    The partition key and `tenant` match the proposal, so `_require_put_targets_declared_record`
    passes and only `validate_proposal_payload` in `approve_proposal` can refuse.
    """
    proposal_id = _store_poisoned_proposal(
        seeded, {"name": "Renamed By Proposal", poisoned_field: "POISON"}
    )
    before_target = seeded.get_item("accounts", {"account_id": _ATTACKER_ACCOUNT})

    response = _approve(
        seeded, channel, proposal_id, transaction_key=f"tx-nonkey-{poisoned_field}-{channel}"
    )

    assert response["statusCode"] == 400
    assert response["error"]["code"] == "validation_failed"
    assert response["error"]["source"] == f"after_payload.{UNKNOWN_FIELD_SOURCE}"
    assert poisoned_field not in json.dumps(response)
    assert seeded.get_item("accounts", {"account_id": _ATTACKER_ACCOUNT}) == before_target
    stored = seeded.get_item("proposals", {"proposal_id": proposal_id})
    assert stored is not None
    assert stored["status"] == "pending"


def test_the_put_key_guard_refuses_redirection_even_with_the_allowlist_widened(
    seeded: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the partition key allowlisted, the ``Put``-key guard still refuses the redirect."""
    widened = proposal_payload_allowlist("accounts") | {"account_id"}

    def _widened_allowlist(table: str) -> frozenset[str]:
        del table
        return widened

    def _no_validation(table: str, payload: object) -> None:
        del table, payload

    monkeypatch.setattr(allowlists, "proposal_payload_allowlist", _widened_allowlist, raising=True)
    import crm.proposals as proposals_module

    monkeypatch.setattr(proposals_module, "validate_proposal_payload", _no_validation, raising=True)

    before = _victim_row(seeded)
    proposal_id = _store_poisoned_proposal(
        seeded, {"account_id": _VICTIM_ACCOUNT, "name": "OVERWRITTEN_BY_ATTACKER"}
    )

    response = _approve(seeded, IDENTITY_KEY, proposal_id, transaction_key="tx-key-guard")

    assert response["statusCode"] == 400
    assert response["error"]["code"] == "validation_failed"
    assert _victim_row(seeded) == before


# --------------------------------------------------------------------------------------
# The legitimate changes the approval path exists to perform still commit
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("channel", _DECIDING_PLANES, ids=_DECIDING_PLANE_IDS)
def test_a_legitimate_account_rename_still_commits_on_every_plane(
    seeded: Store, channel: str
) -> None:
    submitted = _submit(
        seeded, channel, {"name": "Attacker Co Renamed"}, transaction_key=f"tx-rename-{channel}"
    )
    assert submitted["statusCode"] == 200
    proposal_id = str(submitted["body"]["proposal"]["proposal_id"])

    approved = _approve(seeded, channel, proposal_id, transaction_key=f"tx-rename-ok-{channel}")
    assert approved["statusCode"] == 200

    row = seeded.get_item("accounts", {"account_id": _ATTACKER_ACCOUNT})
    assert row is not None
    assert row["name"] == "Attacker Co Renamed"
    assert row["name_lower"] == "attacker co renamed"
    assert row["tenant"] == _ATTACKER_TENANT
    assert row["owner_rep_id"] == _ATTACKER_REP


@pytest.mark.parametrize("channel", _DECIDING_PLANES, ids=_DECIDING_PLANE_IDS)
def test_a_legitimate_stage_change_still_commits_with_recomputed_derived_attributes(
    seeded: Store, channel: str
) -> None:
    """An approved stage change commits with correctly recomputed derived index attributes."""
    submitted = submit_proposal.handler(
        _event(
            channel,
            {
                "target_table": "opportunities",
                "target_id": _OPPORTUNITY,
                "operation": "update",
                "after_payload": {"stage": "Qualification"},
                "reason": "buyer engaged",
                "transaction_key": f"tx-stage-{channel}",
            },
        ),
        None,
        store=seeded,
    )
    assert submitted["statusCode"] == 200
    proposal_id = str(submitted["body"]["proposal"]["proposal_id"])

    approved = _approve(seeded, channel, proposal_id, transaction_key=f"tx-stage-ok-{channel}")
    assert approved["statusCode"] == 200

    row = seeded.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})
    assert row is not None
    assert row["stage"] == "Qualification"
    assert row["tenant_stage"] == f"{_ATTACKER_TENANT}#Qualification"
    assert row["stage_close_date"] == "Qualification#2026-06-01"
    assert row["tenant"] == _ATTACKER_TENANT


@pytest.mark.parametrize("channel", _DECIDING_PLANES, ids=_DECIDING_PLANE_IDS)
def test_a_legitimate_owner_reassignment_on_an_opportunity_still_commits(
    seeded: Store, channel: str
) -> None:
    """An `owner_rep_id` change on an opportunity commits through the proposal path."""
    submitted = submit_proposal.handler(
        _event(
            channel,
            {
                "target_table": "opportunities",
                "target_id": _OPPORTUNITY,
                "operation": "update",
                "after_payload": {"owner_rep_id": "rep-new-owner"},
                "reason": "territory change",
                "transaction_key": f"tx-reassign-{channel}",
            },
        ),
        None,
        store=seeded,
    )
    assert submitted["statusCode"] == 200
    proposal_id = str(submitted["body"]["proposal"]["proposal_id"])

    approved = _approve(seeded, channel, proposal_id, transaction_key=f"tx-reassign-ok-{channel}")
    assert approved["statusCode"] == 200

    row = seeded.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})
    assert row is not None
    assert row["owner_rep_id"] == "rep-new-owner"
    assert row["tenant_owner_rep_id"] == f"{_ATTACKER_TENANT}#rep-new-owner"


def test_the_domain_submit_function_itself_refuses_a_bad_payload(seeded: Store) -> None:
    """`crm.proposals.submit_proposal` called directly refuses a forbidden payload."""
    with pytest.raises(ValidationFailed):
        submit_proposal_domain(
            seeded,
            tenant=_ATTACKER_TENANT,
            proposal_id="prop-direct",
            target_table="accounts",
            target_id=_ATTACKER_ACCOUNT,
            operation="update",
            actor_rep_id=_ATTACKER_REP,
            reason="routine rename",
            transaction_key="tx-direct",
            after_payload={"account_id": _VICTIM_ACCOUNT},
            now="2024-01-02T00:00:00+00:00",
        )
    assert seeded.get_item("proposals", {"proposal_id": "prop-direct"}) is None


# --------------------------------------------------------------------------------------
# A `delete` proposal's after_payload: validated, then not stored
# --------------------------------------------------------------------------------------


def _submit_delete(
    store: Store, channel: str, after_payload: dict[str, Any] | None, *, transaction_key: str
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "target_table": "activities",
        "target_id": _ACTIVITY,
        "operation": "delete",
        "reason": "duplicate record",
        "transaction_key": transaction_key,
    }
    if after_payload is not None:
        arguments["after_payload"] = after_payload
    return cast(
        dict[str, Any], submit_proposal.handler(_event(channel, arguments), None, store=store)
    )


@pytest.mark.parametrize("channel", _PLANES, ids=_PLANE_IDS)
def test_a_delete_proposal_with_a_forbidden_payload_is_refused_at_submit(
    seeded: Store, channel: str
) -> None:
    """A `delete` proposal carrying a forbidden field in its payload is refused at submit."""
    response = _submit_delete(
        seeded,
        channel,
        {"activity_id": "act-victim", "tenant": _VICTIM_TENANT},
        transaction_key=f"tx-del-bad-{channel}",
    )

    assert response["statusCode"] == 400
    assert response["error"]["code"] == "validation_failed"
    assert response["error"]["source"] == f"after_payload.{UNKNOWN_FIELD_SOURCE}"
    stored, _cursor = seeded.query_tenant_scan("proposals", tenant=_ATTACKER_TENANT)
    assert stored == []
    assert seeded.get_item("activities", {"activity_id": _ACTIVITY}) is not None


@pytest.mark.parametrize("channel", _PLANES, ids=_PLANE_IDS)
def test_a_delete_proposal_stores_no_after_payload_even_when_one_is_supplied(
    seeded: Store, channel: str
) -> None:
    """A `delete` proposal row stores no `after_payload`, even an allowlisted one."""
    response = _submit_delete(
        seeded, channel, {"subject": "irrelevant"}, transaction_key=f"tx-del-ok-{channel}"
    )

    assert response["statusCode"] == 200
    proposal_id = str(response["body"]["proposal"]["proposal_id"])
    row = seeded.get_item("proposals", {"proposal_id": proposal_id})
    assert row is not None
    assert row["after_payload"] is None
    assert response["body"]["proposal"]["after_payload"] is None


@pytest.mark.parametrize("channel", _DECIDING_PLANES, ids=_DECIDING_PLANE_IDS)
def test_a_delete_proposal_still_deletes_its_declared_record_on_approval(
    seeded: Store, channel: str
) -> None:
    """Approving a `delete` proposal still deletes its declared record."""
    submitted = _submit_delete(seeded, channel, None, transaction_key=f"tx-del-apply-{channel}")
    assert submitted["statusCode"] == 200
    proposal_id = str(submitted["body"]["proposal"]["proposal_id"])

    approved = _approve(seeded, channel, proposal_id, transaction_key=f"tx-del-appr-{channel}")

    assert approved["statusCode"] == 200
    assert seeded.get_item("activities", {"activity_id": _ACTIVITY}) is None


def test_the_domain_submit_function_refuses_a_forbidden_delete_payload(seeded: Store) -> None:
    """Enforcement is in `crm.proposals`, so a direct caller gets the same refusal."""
    with pytest.raises(ValidationFailed):
        submit_proposal_domain(
            seeded,
            tenant=_ATTACKER_TENANT,
            proposal_id="prop-delete-direct",
            target_table="activities",
            target_id=_ACTIVITY,
            operation="delete",
            actor_rep_id=_ATTACKER_REP,
            reason="duplicate record",
            transaction_key="tx-delete-direct",
            after_payload={"activity_id": "act-victim"},
            now="2024-01-02T00:00:00+00:00",
        )
    assert seeded.get_item("proposals", {"proposal_id": "prop-delete-direct"}) is None


# --------------------------------------------------------------------------------------
# Value types: a type-wrong or blank allowlisted field is refused, so no derived index key is lost
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("channel", _PLANES, ids=_PLANE_IDS)
def test_a_type_wrong_name_is_refused_at_submit_on_every_plane(seeded: Store, channel: str) -> None:
    before = seeded.get_item("accounts", {"account_id": _ATTACKER_ACCOUNT})

    response = _submit(seeded, channel, {"name": 12345}, transaction_key=f"tx-w13-{channel}")

    assert response["statusCode"] == 400
    assert response["error"]["code"] == "validation_failed"
    assert response["error"]["source"] == "name"
    assert seeded.get_item("accounts", {"account_id": _ATTACKER_ACCOUNT}) == before
    stored, _cursor = seeded.query_tenant_scan("proposals", tenant=_ATTACKER_TENANT)
    assert stored == []


@pytest.mark.parametrize(
    "payload,expected_source",
    [
        ({"name": 12345}, "name"),
        ({"name": ""}, "name"),
        ({"name": "   "}, "name"),
        ({"name": None}, "name"),
        ({"employee_count": "many"}, "employee_count"),
    ],
)
def test_a_type_wrong_or_blank_payload_is_refused_at_approve_and_the_row_keeps_its_index_key(
    seeded: Store, payload: dict[str, Any], expected_source: str
) -> None:
    """A stored type-wrong or blank payload is refused at approve and the row keeps `name_lower`.

    Each payload is stored *without* submit's validation, so only the approve-time re-validation
    can refuse it.
    """
    proposal_id = _store_poisoned_proposal(seeded, payload)
    before = seeded.get_item("accounts", {"account_id": _ATTACKER_ACCOUNT})
    assert before is not None and before["name_lower"] == "attacker co"

    response = _approve(
        seeded, IDENTITY_KEY, proposal_id, transaction_key=f"tx-w13-approve-{expected_source}"
    )

    assert response["statusCode"] == 400
    assert response["error"]["code"] == "validation_failed"
    assert response["error"]["source"] == expected_source
    after = seeded.get_item("accounts", {"account_id": _ATTACKER_ACCOUNT})
    assert after == before
    assert after is not None and after["name_lower"] == "attacker co"


def test_a_type_wrong_close_date_would_have_dropped_the_forecast_index_key_and_is_refused(
    seeded: Store,
) -> None:
    """`{"close_date": 5}` is refused, so the row keeps its `stage_close_date` index key."""
    with pytest.raises(ValidationFailed) as excinfo:
        validate_proposal_payload("opportunities", {"close_date": 5})
    assert excinfo.value.source == "close_date"

    row = seeded.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})
    assert row is not None
    assert row["stage_close_date"] == "Discovery#2026-06-01"


def test_the_divesting_payload_would_really_have_dropped_the_index_key(seeded: Store) -> None:
    """Unvalidated, the merge `_apply_operation` performs loses the derived index key."""
    before = seeded.get_item("accounts", {"account_id": _ATTACKER_ACCOUNT})
    assert before is not None

    divested = derive("accounts", {**before, "name": 12345})

    assert divested["name"] == 12345
    assert "name_lower" not in divested
    assert divested["account_id"] == _ATTACKER_ACCOUNT
    assert divested["tenant_owner_rep_id"] == before["tenant_owner_rep_id"]


def test_a_legitimate_rename_still_commits_and_keeps_its_index_key(seeded: Store) -> None:
    """A legitimate rename commits and keeps its derived index key."""
    submitted = _submit(
        seeded, IDENTITY_KEY, {"name": "Attacker Co GmbH"}, transaction_key="tx-w13-ok"
    )
    assert submitted["statusCode"] == 200
    proposal_id = str(submitted["body"]["proposal"]["proposal_id"])

    approved = _approve(
        seeded,
        IDENTITY_KEY,
        proposal_id,
        transaction_key="tx-w13-ok-appr",  # gitleaks:allow
    )

    assert approved["statusCode"] == 200
    row = seeded.get_item("accounts", {"account_id": _ATTACKER_ACCOUNT})
    assert row is not None
    assert row["name"] == "Attacker Co GmbH"
    assert row["name_lower"] == "attacker co gmbh"


def test_every_allowlisted_field_has_a_declared_value_type(seeded: Store) -> None:
    """Every field in each table's permitted set has a declared value type."""
    del seeded
    for table in sorted(TABLES):
        declared = set(allowlists.FIELD_VALUE_TYPES[table])
        assert declared == set(proposal_payload_allowlist(table)), table


def test_every_derived_source_inside_an_allowlist_is_protected_from_a_blank_value() -> None:
    """Every allowlisted source of a `DERIVED_ATTRIBUTES` entry is protected from a blank value."""
    for table in sorted(TABLES):
        sources = {
            source
            for spec in DERIVED_ATTRIBUTES.get(table, ())
            for source in spec.sources
            if source in proposal_payload_allowlist(table)
        }
        for field in sources:
            with pytest.raises(ValidationFailed) as excinfo:
                validate_proposal_payload(table, {field: ""})
            assert excinfo.value.source == field, (table, field)


def test_a_planted_unknown_proposal_key_appears_nowhere_in_the_error_payload() -> None:
    planted = "PLANTED-proposal-key"
    with pytest.raises(ValidationFailed) as excinfo:
        validate_proposal_payload("opportunities", {"name": "ok", planted: "PLANTED-value"})
    error = excinfo.value
    rendered = json.dumps(error.to_api_response()) + json.dumps(error.to_mcp_error())
    assert "PLANTED" not in rendered
    assert error.source == f"after_payload.{UNKNOWN_FIELD_SOURCE}"
    permitted = ", ".join(sorted(proposal_payload_allowlist("opportunities")))
    assert f"Permitted: {permitted}." in error.message

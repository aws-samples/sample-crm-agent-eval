# SPDX-License-Identifier: MIT
"""Tests for every action Lambda handler in crm.handlers.

Exercises each handler against a `moto`-mocked DynamoDB client. Covers: every read and write
operation has a working handler; `require_scope` runs before any read or write (an out-of-book
target is refused, nothing changes); activity logging updates the parent opportunity's `last_touch`
in one transaction; writes are idempotent under a caller transaction key; and every `update_*`
handler refuses a high-risk field before any store write.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import (
    approve_proposal,
    create_account,
    create_contact,
    create_note,
    create_opportunity,
    draft_message,
    get_account,
    get_contact,
    get_opportunity,
    list_activities,
    list_audit_records,
    list_proposals,
    log_activity,
    pipeline_aggregate,
    reject_proposal,
    revert_audit_record,
    search_opportunities,
    submit_proposal,
    update_account,
    update_activity,
    update_contact,
    update_opportunity,
)
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"
_OTHER_ACCOUNT = "acct-2"


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
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


def _event(args: dict[str, Any], **identity_overrides: Any) -> dict[str, Any]:
    return identity_event(IDENTITY_KEY, _identity(**identity_overrides), args)


def _seed_account(store: Store, *, account_id: str = _ACCOUNT, owner: str = _REP) -> None:
    store.put_item(
        "accounts",
        {
            "account_id": account_id,
            "tenant": _TENANT,
            "tenant_owner_rep_id": f"{_TENANT}#{owner}",
            "owner_rep_id": owner,
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
    owner: str = _REP,
    stage: str = "Discovery",
    primary_contact_id: str | None = None,
) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": opportunity_id,
            "tenant": _TENANT,
            "tenant_account_id": f"{_TENANT}#{account_id}",
            "tenant_owner_rep_id": f"{_TENANT}#{owner}",
            "tenant_stage": f"{_TENANT}#{stage}",
            "account_id": account_id,
            "owner_rep_id": owner,
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
# Read handlers
# --------------------------------------------------------------------------------------


def test_get_account_returns_account_and_contacts(store: Store) -> None:
    _seed_account(store)
    _seed_contact(store)
    result = get_account.handler(_event({"account_id": _ACCOUNT}), store=store)
    assert result["statusCode"] == 200
    assert result["body"]["account"]["account_id"] == _ACCOUNT
    assert len(result["body"]["contacts"]) == 1


def test_get_account_denies_out_of_book_account(store: Store) -> None:
    _seed_account(store, account_id=_OTHER_ACCOUNT)
    result = get_account.handler(_event({"account_id": _OTHER_ACCOUNT}), store=store)
    assert result["statusCode"] == 403
    assert result["error"]["code"] == "scope_denied"


def test_get_account_not_found(store: Store) -> None:
    result = get_account.handler(_event({"account_id": "nope"}), store=store)
    assert result["statusCode"] == 404


def test_get_contact(store: Store) -> None:
    _seed_account(store)
    _seed_contact(store, status="departed")
    result = get_contact.handler(_event({"contact_id": "cont-1"}), store=store)
    assert result["statusCode"] == 200
    assert result["body"]["contact"]["status"] == "departed"


def test_get_opportunity_with_related_records(store: Store) -> None:
    _seed_account(store)
    _seed_contact(store)
    _seed_opportunity(store)
    result = get_opportunity.handler(_event({"opportunity_id": "opp-1"}), store=store)
    assert result["statusCode"] == 200
    body = result["body"]
    assert body["opportunity"]["opportunity_id"] == "opp-1"
    assert body["contacts"][0]["contact_id"] == "cont-1"
    assert body["notes"] == []
    assert body["activities"] == []


def test_list_activities_most_recent_first(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store)
    for i in range(3):
        log_activity.handler(
            _event(
                {
                    "opportunity_id": "opp-1",
                    "kind": "note",
                    "subject": f"note-{i}",
                    "transaction_key": f"tx-log-{i}",
                    "occurred_at": f"2024-01-0{i + 1}T00:00:00+00:00",
                }
            ),
            store=store,
        )
    result = list_activities.handler(_event({"opportunity_id": "opp-1"}), store=store)
    assert result["statusCode"] == 200
    subjects = [a["subject"] for a in result["body"]["activities"]]
    assert subjects == ["note-2", "note-1", "note-0"]


def test_search_opportunities_scoped_to_book(store: Store) -> None:
    _seed_account(store)
    _seed_account(store, account_id=_OTHER_ACCOUNT)
    _seed_opportunity(store, opportunity_id="opp-1", account_id=_ACCOUNT)
    _seed_opportunity(store, opportunity_id="opp-2", account_id=_OTHER_ACCOUNT)
    result = search_opportunities.handler(_event({}), store=store)
    assert result["statusCode"] == 200
    ids = {item["opportunity_id"] for item in result["body"]["items"]}
    assert ids == {"opp-1"}
    assert result["body"]["truncated"] is False


def test_pipeline_aggregate_stage_funnel_and_rollup(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store, opportunity_id="opp-1", stage="Discovery")
    _seed_opportunity(store, opportunity_id="opp-2", stage="Negotiation")
    result = pipeline_aggregate.handler(_event({}), store=store)
    assert result["statusCode"] == 200
    funnel = result["body"]["stage_funnel"]
    assert funnel["Discovery"] == 1
    assert funnel["Negotiation"] == 1
    assert result["body"]["value_rollup"] == 2000


def test_list_proposals_scoped(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store)
    submit_proposal.handler(
        _event(
            {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
                "after_payload": {"stage": "Qualification"},
                "reason": "advance",
                "transaction_key": "tx-p1",
            }
        ),
        store=store,
    )
    result = list_proposals.handler(_event({}), store=store)
    assert result["statusCode"] == 200
    assert len(result["body"]["proposals"]) == 1


def test_list_audit_records_scoped(store: Store) -> None:
    _seed_account(store)
    update_account.handler(
        _event({"account_id": _ACCOUNT, "fields": {"name": "New"}, "transaction_key": "tx-u1"}),
        store=store,
    )
    result = list_audit_records.handler(_event({}), store=store)
    assert result["statusCode"] == 200
    assert len(result["body"]["audit_records"]) == 1


def test_list_audit_records_default_call_returns_revert_state(store: Store) -> None:
    """The default call (no ``target_id``) returns rows carrying ``target_id``, ``target_table``
    and ``status``."""
    _seed_account(store)
    update_account.handler(
        _event({"account_id": _ACCOUNT, "fields": {"name": "New"}, "transaction_key": "tx-u1"}),
        store=store,
    )
    result = list_audit_records.handler(_event({}), store=store)
    assert result["statusCode"] == 200
    records = result["body"]["audit_records"]
    assert len(records) == 1
    record = records[0]
    assert record["target_table"] == "accounts"
    assert record["target_id"] == _ACCOUNT
    assert record["status"] == "active"
    assert record["account_id"] == _ACCOUNT


def test_list_audit_records_by_target_id_returns_status(store: Store) -> None:
    """The ``target_id`` call path returns a correct ``status`` before and after a revert."""
    _seed_account(store)
    update_account.handler(
        _event({"account_id": _ACCOUNT, "fields": {"name": "New"}, "transaction_key": "tx-u1"}),
        store=store,
    )
    before_revert = list_audit_records.handler(_event({"target_id": _ACCOUNT}), store=store)
    assert before_revert["statusCode"] == 200
    active_record = before_revert["body"]["audit_records"][0]
    assert active_record["status"] == "active"
    assert active_record["target_table"] == "accounts"
    assert active_record["target_id"] == _ACCOUNT

    audit_id = active_record["audit_id"]
    revert_audit_record.handler(
        _event({"audit_id": audit_id, "transaction_key": "tx-revert-1"}), store=store
    )

    after_revert = list_audit_records.handler(_event({"target_id": _ACCOUNT}), store=store)
    reversed_record = after_revert["body"]["audit_records"][0]
    assert reversed_record["status"] == "reversed"


def test_list_audit_records_drops_a_record_with_no_determinable_account_scope(
    store: Store,
) -> None:
    """An audit record whose target carries no account scope key is dropped, not shown."""
    from crm.audit import build_audit_record
    from crm.authz import Identity

    _seed_account(store)
    unscoped_identity = Identity(rep_id=_REP, book=frozenset(), tenant=_TENANT)
    audit = build_audit_record(
        identity=unscoped_identity,
        target_table="notes",
        target_id="note-1",
        before_snapshot={},
        after_snapshot={"note_id": "note-1", "tenant": _TENANT, "opportunity_id": "opp-1"},
        audit_id="audit-note-1",
        created_at="2026-01-01T00:00:00+00:00",
    )
    assert audit.account_id is None  # the note snapshot carries no account_id/accountId
    store.put_item("audit_records", audit.to_item())

    result = list_audit_records.handler(_event({}), store=store)
    assert result["statusCode"] == 200
    assert result["body"]["audit_records"] == []


def test_draft_message(store: Store) -> None:
    _seed_account(store)
    _seed_contact(store)
    result = draft_message.handler(
        _event({"contact_id": "cont-1", "intent": "follow up", "tone": "formal"}), store=store
    )
    assert result["statusCode"] == 200
    assert "Jane Doe" in result["body"]["draft"]


# --------------------------------------------------------------------------------------
# Low-risk write handlers
# --------------------------------------------------------------------------------------


def test_create_account(store: Store) -> None:
    result = create_account.handler(
        _event({"name": "New Co", "transaction_key": "tx-1"}, book=[]), store=store
    )
    assert result["statusCode"] == 200
    assert result["body"]["account"]["name"] == "New Co"


def test_create_account_idempotent_retry_is_a_no_op(store: Store) -> None:
    event = _event({"name": "New Co", "transaction_key": "tx-1"}, book=[])
    first = create_account.handler(event, store=store)
    second = create_account.handler(event, store=store)
    assert first["body"]["account"]["account_id"] == second["body"]["account"]["account_id"]


def test_create_account_replay_with_different_payload_is_refused_with_conflict(
    store: Store,
) -> None:
    """Replaying a ``transaction_key`` with a different payload is refused with `Conflict`/409."""
    first = create_account.handler(
        _event({"name": "New Co", "transaction_key": "tx-mismatch"}, book=[]), store=store
    )
    assert first["statusCode"] == 200
    second = create_account.handler(
        _event({"name": "A Totally Different Co", "transaction_key": "tx-mismatch"}, book=[]),
        store=store,
    )
    assert second["statusCode"] == 409


def test_update_account_scope_denied(store: Store) -> None:
    _seed_account(store, account_id=_OTHER_ACCOUNT)
    result = update_account.handler(
        _event({"account_id": _OTHER_ACCOUNT, "fields": {"name": "X"}, "transaction_key": "tx-1"}),
        store=store,
    )
    assert result["statusCode"] == 403


def test_update_account_rejects_disallowed_field_before_any_write(store: Store) -> None:
    _seed_account(store)
    before = store.get_item("accounts", {"account_id": _ACCOUNT})
    result = update_account.handler(
        _event(
            {
                "account_id": _ACCOUNT,
                "fields": {"owner_rep_id": "someone-else"},
                "transaction_key": "tx-1",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 400
    assert result["error"]["source"] == "owner_rep_id"
    after = store.get_item("accounts", {"account_id": _ACCOUNT})
    assert after == before


def test_create_contact_under_account(store: Store) -> None:
    _seed_account(store)
    result = create_contact.handler(
        _event({"account_id": _ACCOUNT, "name": "Bob", "transaction_key": "tx-1"}), store=store
    )
    assert result["statusCode"] == 200
    assert result["body"]["contact"]["status"] == "active"


def test_update_contact_can_mark_departed(store: Store) -> None:
    _seed_account(store)
    _seed_contact(store)
    result = update_contact.handler(
        _event(
            {"contact_id": "cont-1", "fields": {"status": "departed"}, "transaction_key": "tx-1"}
        ),
        store=store,
    )
    assert result["statusCode"] == 200
    assert result["body"]["contact"]["status"] == "departed"


def test_create_opportunity(store: Store) -> None:
    _seed_account(store)
    result = create_opportunity.handler(
        _event({"account_id": _ACCOUNT, "name": "Deal", "transaction_key": "tx-1"}), store=store
    )
    assert result["statusCode"] == 200
    assert result["body"]["opportunity"]["stage"] == "Discovery"


def test_update_opportunity_rejects_high_risk_stage_field_before_any_write(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store)
    before = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    result = update_opportunity.handler(
        _event(
            {
                "opportunity_id": "opp-1",
                "fields": {"stage": "Closed Won"},
                "transaction_key": "tx-1",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 400
    assert result["error"]["source"] == "stage"
    after = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert after == before


def test_update_opportunity_allows_low_risk_field(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store)
    result = update_opportunity.handler(
        _event(
            {
                "opportunity_id": "opp-1",
                "fields": {"description": "updated"},
                "transaction_key": "tx-1",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 200
    assert result["body"]["opportunity"]["description"] == "updated"


def test_log_activity_updates_parent_last_touch_in_one_transaction(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store)
    result = log_activity.handler(
        _event(
            {
                "opportunity_id": "opp-1",
                "kind": "call",
                "subject": "Checked in",
                "transaction_key": "tx-1",
                "occurred_at": "2024-05-01T00:00:00+00:00",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 200
    opportunity = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert opportunity is not None
    assert opportunity["last_touch"] == "2024-05-01T00:00:00+00:00"
    activities, _ = store.query_gsi(
        "activities", "gsi_opportunity", partition_value=f"{_TENANT}#opp-1"
    )
    assert len(activities) == 1


def test_log_activity_denies_out_of_book_opportunity(store: Store) -> None:
    _seed_account(store, account_id=_OTHER_ACCOUNT)
    _seed_opportunity(store, opportunity_id="opp-x", account_id=_OTHER_ACCOUNT)
    result = log_activity.handler(
        _event(
            {
                "opportunity_id": "opp-x",
                "kind": "call",
                "subject": "x",
                "transaction_key": "tx-1",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 403
    opportunity = store.get_item("opportunities", {"opportunity_id": "opp-x"})
    assert opportunity is not None
    assert opportunity["last_touch"] is None


def test_update_activity_allowlist(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store)
    log_activity.handler(
        _event(
            {"opportunity_id": "opp-1", "kind": "call", "subject": "hi", "transaction_key": "tx-1"}
        ),
        store=store,
    )
    activities, _ = store.query_gsi(
        "activities", "gsi_opportunity", partition_value=f"{_TENANT}#opp-1"
    )
    activity_id = activities[0]["activity_id"]
    result = update_activity.handler(
        _event(
            {
                "activity_id": activity_id,
                "fields": {"subject": "updated"},
                "transaction_key": "tx-2",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 200
    assert result["body"]["activity"]["subject"] == "updated"


def test_create_note(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store)
    result = create_note.handler(
        _event({"opportunity_id": "opp-1", "body": "hello", "transaction_key": "tx-1"}), store=store
    )
    assert result["statusCode"] == 200
    assert result["body"]["note"]["body"] == "hello"


# --------------------------------------------------------------------------------------
# Proposal / approval / revert handlers
# --------------------------------------------------------------------------------------


def test_submit_proposal_for_stage_change_then_approve(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store, stage="Discovery")
    submitted = submit_proposal.handler(
        _event(
            {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
                "after_payload": {"stage": "Qualification"},
                "reason": "advancing",
                "transaction_key": "tx-submit",
            }
        ),
        store=store,
    )
    assert submitted["statusCode"] == 200
    proposal_id = submitted["body"]["proposal"]["proposal_id"]

    # Nothing mutates on submission.
    opportunity = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert opportunity is not None
    assert opportunity["stage"] == "Discovery"

    approved = approve_proposal.handler(
        _event({"proposal_id": proposal_id, "transaction_key": "tx-approve"}), store=store
    )
    assert approved["statusCode"] == 200
    opportunity = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert opportunity is not None
    assert opportunity["stage"] == "Qualification"

    audit_records, _ = store.query_gsi(
        "audit_records", "gsi_target", partition_value=f"{_TENANT}#opp-1"
    )
    assert len(audit_records) == 1


def test_submit_proposal_refuses_reopen_of_closed_stage(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store, stage="Closed Won")
    result = submit_proposal.handler(
        _event(
            {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
                "after_payload": {"stage": "Discovery"},
                "reason": "reopen",
                "transaction_key": "tx-1",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 400


def test_reject_proposal_is_terminal(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store)
    submitted = submit_proposal.handler(
        _event(
            {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
                "after_payload": {"stage": "Qualification"},
                "reason": "advancing",
                "transaction_key": "tx-1",
            }
        ),
        store=store,
    )
    proposal_id = submitted["body"]["proposal"]["proposal_id"]
    rejected = reject_proposal.handler(
        _event({"proposal_id": proposal_id, "transaction_key": "tx-2"}), store=store
    )
    assert rejected["statusCode"] == 200

    approve_result = approve_proposal.handler(
        _event({"proposal_id": proposal_id, "transaction_key": "tx-3"}), store=store
    )
    assert approve_result["statusCode"] == 409


def test_approve_proposal_stale_snapshot_refused(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store)
    submitted = submit_proposal.handler(
        _event(
            {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
                "after_payload": {"stage": "Qualification", "description": "proposed"},
                "reason": "advancing",
                "transaction_key": "tx-1",
            }
        ),
        store=store,
    )
    proposal_id = submitted["body"]["proposal"]["proposal_id"]

    # Mutate the target out from under the proposal.
    update_opportunity.handler(
        _event(
            {
                "opportunity_id": "opp-1",
                "fields": {"description": "changed"},
                "transaction_key": "tx-2",
            }
        ),
        store=store,
    )

    approved = approve_proposal.handler(
        _event({"proposal_id": proposal_id, "transaction_key": "tx-3"}), store=store
    )
    assert approved["statusCode"] == 409


def test_revert_audit_record_restores_before_snapshot(store: Store) -> None:
    _seed_account(store)
    update_account.handler(
        _event({"account_id": _ACCOUNT, "fields": {"name": "Renamed"}, "transaction_key": "tx-1"}),
        store=store,
    )
    audit_records, _ = store.query_gsi(
        "audit_records", "gsi_target", partition_value=f"{_TENANT}#{_ACCOUNT}"
    )
    audit_id = audit_records[0]["audit_id"]

    reverted = revert_audit_record.handler(
        _event({"audit_id": audit_id, "transaction_key": "tx-2"}), store=store
    )
    assert reverted["statusCode"] == 200
    account = store.get_item("accounts", {"account_id": _ACCOUNT})
    assert account is not None
    assert account["name"] == "Acme"


def test_revert_audit_record_twice_is_refused(store: Store) -> None:
    _seed_account(store)
    update_account.handler(
        _event({"account_id": _ACCOUNT, "fields": {"name": "Renamed"}, "transaction_key": "tx-1"}),
        store=store,
    )
    audit_records, _ = store.query_gsi(
        "audit_records", "gsi_target", partition_value=f"{_TENANT}#{_ACCOUNT}"
    )
    audit_id = audit_records[0]["audit_id"]
    revert_audit_record.handler(
        _event({"audit_id": audit_id, "transaction_key": "tx-2"}), store=store
    )
    second = revert_audit_record.handler(
        _event({"audit_id": audit_id, "transaction_key": "tx-3"}), store=store
    )
    assert second["statusCode"] == 409


def test_submit_proposal_delete_account_with_open_opportunities_refused(store: Store) -> None:
    _seed_account(store)
    _seed_opportunity(store, stage="Discovery")
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


def test_no_handler_missing_identity_channel_yields_401(store: Store) -> None:
    result = get_account.handler({"account_id": _ACCOUNT}, store=store)
    assert result["statusCode"] == 401

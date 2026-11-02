# SPDX-License-Identifier: MIT
"""Tests for crm.proposals: submit, approve, reject, stale-snapshot and concurrency protection.

Runs against a `moto`-mocked DynamoDB client. Concurrency is exercised through moto's
`ConditionExpression` evaluation on `TransactWriteItems`: two callers each submit a full
transaction, and exactly one is accepted.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.errors import Conflict, NotFound, ValidationFailed
from crm.proposals import (
    ApprovalContext,
    approve_proposal,
    reject_proposal,
    submit_proposal,
)
from crm.store.keys import derive
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_NOW = "2025-01-01T00:00:00Z"


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    """A moto-mocked DynamoDB client — no credentials, no network, no cost."""
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    """A Store backed by the mocked client, with every declared table created."""
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


@pytest.fixture
def prefixed_store(dynamodb_client: object) -> Store:
    """A Store using a non-empty ``table_prefix``, with every declared table created under it.

    A ``transact_write`` call naming a bare, unprefixed ``TableName`` raises ``NotFound`` here.
    """
    create_all_tables(dynamodb_client, table_prefix="envA-")
    return Store(table_prefix="envA-", client=dynamodb_client)


def _seed_opportunity(store: Store, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "opportunity_id": "opp-1",
        "tenant": _TENANT,
        "tenant_account_id": f"{_TENANT}#acct-1",
        "tenant_owner_stage": f"{_TENANT}#{_REP}",
        "tenant_stage": f"{_TENANT}#Discovery",
        "name_lower": "acme renewal",
        "account_id": "acct-1",
        "name": "Acme Renewal",
        "stage": "Discovery",
        "owner_rep_id": _REP,
        "value": 1000,
        "close_date": "2025-06-01",
        "primary_contact_id": None,
        "created_at": _NOW,
        "updated_at": _NOW,
        "created_by": _REP,
        "schema_version": 1,
    }
    item.update(overrides)
    # Derived index key attributes make this item equal to the row `Store.put_item` persists.
    item = derive("opportunities", item)
    store.put_item("opportunities", item)
    return item


# -- submit_proposal ----------------------------------------------------------------------------


def test_submit_proposal_persists_before_snapshot_and_after_payload_and_does_not_mutate_target(
    store: Store,
) -> None:
    """Submission records before/after/reason/status pending; nothing else changes."""
    original = _seed_opportunity(store)

    proposal = submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="update",
        actor_rep_id=_REP,
        reason="Deal is closing.",
        transaction_key="txn-1",
        after_payload={"stage": "Closed Won"},
        now=_NOW,
    )

    assert proposal["status"] == "pending"
    assert proposal["before_snapshot"] == original
    assert proposal["after_payload"] == {"stage": "Closed Won"}
    assert proposal["reason"] == "Deal is closing."

    unchanged = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert unchanged == original


def test_submit_proposal_rejects_unrecognised_target_table(store: Store) -> None:
    with pytest.raises(ValidationFailed):
        submit_proposal(
            store,
            tenant=_TENANT,
            proposal_id="prop-1",
            target_table="not_a_table",
            target_id="x",
            operation="delete",
            actor_rep_id=_REP,
            reason="cleanup",
            transaction_key="txn-1",
            now=_NOW,
        )


def test_submit_proposal_rejects_unrecognised_operation(store: Store) -> None:
    _seed_opportunity(store)
    with pytest.raises(ValidationFailed):
        submit_proposal(
            store,
            tenant=_TENANT,
            proposal_id="prop-1",
            target_table="opportunities",
            target_id="opp-1",
            operation="archive",
            actor_rep_id=_REP,
            reason="cleanup",
            transaction_key="txn-1",
            now=_NOW,
        )


def test_submit_proposal_rejects_empty_reason(store: Store) -> None:
    _seed_opportunity(store)
    with pytest.raises(ValidationFailed):
        submit_proposal(
            store,
            tenant=_TENANT,
            proposal_id="prop-1",
            target_table="opportunities",
            target_id="opp-1",
            operation="update",
            actor_rep_id=_REP,
            reason="",
            transaction_key="txn-1",
            after_payload={"stage": "Closed Won"},
            now=_NOW,
        )


def test_submit_proposal_rejects_update_with_no_after_payload(store: Store) -> None:
    _seed_opportunity(store)
    with pytest.raises(ValidationFailed):
        submit_proposal(
            store,
            tenant=_TENANT,
            proposal_id="prop-1",
            target_table="opportunities",
            target_id="opp-1",
            operation="update",
            actor_rep_id=_REP,
            reason="no payload",
            transaction_key="txn-1",
            now=_NOW,
        )


def test_submit_proposal_delete_does_not_require_after_payload(store: Store) -> None:
    _seed_opportunity(store)
    proposal = submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="delete",
        actor_rep_id=_REP,
        reason="dead deal",
        transaction_key="txn-1",
        now=_NOW,
    )
    assert proposal["operation"] == "delete"
    assert proposal["after_payload"] is None


def test_submit_proposal_raises_not_found_for_missing_target(store: Store) -> None:
    with pytest.raises(NotFound):
        submit_proposal(
            store,
            tenant=_TENANT,
            proposal_id="prop-1",
            target_table="opportunities",
            target_id="does-not-exist",
            operation="delete",
            actor_rep_id=_REP,
            reason="cleanup",
            transaction_key="txn-1",
            now=_NOW,
        )


def test_submit_proposal_raises_not_found_for_target_in_another_tenant(store: Store) -> None:
    _seed_opportunity(store, tenant="eval-run-1")
    with pytest.raises(NotFound):
        submit_proposal(
            store,
            tenant=_TENANT,
            proposal_id="prop-1",
            target_table="opportunities",
            target_id="opp-1",
            operation="delete",
            actor_rep_id=_REP,
            reason="cleanup",
            transaction_key="txn-1",
            now=_NOW,
        )


def test_submit_proposal_is_idempotent_under_retry(store: Store) -> None:
    """A retry with the same transaction key and identical payload is a no-op."""
    _seed_opportunity(store)
    kwargs: dict[str, Any] = {
        "store": store,
        "tenant": _TENANT,
        "proposal_id": "prop-1",
        "target_table": "opportunities",
        "target_id": "opp-1",
        "operation": "update",
        "actor_rep_id": _REP,
        "reason": "Deal is closing.",
        "transaction_key": "txn-1",
        "after_payload": {"stage": "Closed Won"},
        "now": _NOW,
    }
    first = submit_proposal(**kwargs)
    second = submit_proposal(**kwargs)
    assert first == second


# -- approve_proposal: happy path -----------------------------------------------------------------


def test_approve_proposal_applies_update_and_flips_status(store: Store) -> None:
    _seed_opportunity(store)
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="update",
        actor_rep_id=_REP,
        reason="Deal is closing.",
        transaction_key="txn-submit",
        after_payload={"stage": "Closed Won"},
        now=_NOW,
    )

    result = approve_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        actor_rep_id=_REP,
        transaction_key="txn-approve",
        now="2025-01-02T00:00:00Z",
    )

    assert result["status"] == "approved"
    updated = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert updated is not None
    assert updated["stage"] == "Closed Won"

    stored_proposal = store.get_item("proposals", {"proposal_id": "prop-1"})
    assert stored_proposal is not None
    assert stored_proposal["status"] == "approved"


def test_approve_proposal_applies_delete(store: Store) -> None:
    _seed_opportunity(store)
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="delete",
        actor_rep_id=_REP,
        reason="dead deal",
        transaction_key="txn-submit",
        now=_NOW,
    )

    approve_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        actor_rep_id=_REP,
        transaction_key="txn-approve",
        now="2025-01-02T00:00:00Z",
    )

    assert store.get_item("opportunities", {"opportunity_id": "opp-1"}) is None


def test_approve_proposal_invokes_audit_recorder_with_before_and_after(store: Store) -> None:
    """The approve+audit atomicity contract: the caller's callback joins the same transaction."""
    _seed_opportunity(store)
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="update",
        actor_rep_id=_REP,
        reason="Deal is closing.",
        transaction_key="txn-submit",
        after_payload={"stage": "Closed Won"},
        now=_NOW,
    )

    captured: list[ApprovalContext] = []

    def record_audit(context: ApprovalContext) -> dict[str, Any]:
        captured.append(context)
        return {
            "Put": {
                "TableName": "audit_records",
                "Item": {
                    "audit_id": {"S": "audit-1"},
                    "tenant": {"S": context.tenant},
                    "target_table": {"S": context.target_table},
                    "target_id": {"S": context.target_id},
                    "actor_rep_id": {"S": context.actor_rep_id},
                    "created_at": {"S": "2025-01-02T00:00:00Z"},
                },
            }
        }

    approve_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        actor_rep_id=_REP,
        transaction_key="txn-approve",
        now="2025-01-02T00:00:00Z",
        record_audit=record_audit,
    )

    assert len(captured) == 1
    context = captured[0]
    assert context.before_snapshot["stage"] == "Discovery"
    assert context.after_snapshot is not None
    assert context.after_snapshot["stage"] == "Closed Won"

    audit_row = store.get_item("audit_records", {"audit_id": "audit-1"})
    assert audit_row is not None
    assert audit_row["target_id"] == "opp-1"


# -- approve_proposal: stale snapshot -------------------------------------------------------------


def test_approve_proposal_refuses_stale_before_snapshot_with_conflict(store: Store) -> None:
    """The record changed after the proposal was raised; approval must refuse."""
    _seed_opportunity(store)
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="update",
        actor_rep_id=_REP,
        reason="Deal is closing.",
        transaction_key="txn-submit",
        after_payload={"stage": "Closed Won"},
        now=_NOW,
    )

    # The record changes out from under the proposal after submission.
    current = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert current is not None
    store.put_item(
        "opportunities", {**current, "stage": "Negotiation", "updated_at": "2025-01-01T12:00:00Z"}
    )

    with pytest.raises(Conflict) as excinfo:
        approve_proposal(
            store,
            tenant=_TENANT,
            proposal_id="prop-1",
            actor_rep_id=_REP,
            transaction_key="txn-approve",
            now="2025-01-02T00:00:00Z",
        )
    assert excinfo.value.reason == "fields_changed"

    # Nothing further mutated: the record still carries the out-of-band stage change, not the
    # proposal's, and the proposal is still pending.
    unchanged = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert unchanged is not None
    assert unchanged["stage"] == "Negotiation"
    stored_proposal = store.get_item("proposals", {"proposal_id": "prop-1"})
    assert stored_proposal is not None
    assert stored_proposal["status"] == "pending"


def test_approve_proposal_refuses_when_target_was_deleted_out_of_band(store: Store) -> None:
    """A deleted target can never match a stored before-snapshot; must refuse as Conflict."""
    _seed_opportunity(store)
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="update",
        actor_rep_id=_REP,
        reason="Deal is closing.",
        transaction_key="txn-submit",
        after_payload={"stage": "Closed Won"},
        now=_NOW,
    )

    store.delete_item("opportunities", {"opportunity_id": "opp-1"})

    with pytest.raises(Conflict):
        approve_proposal(
            store,
            tenant=_TENANT,
            proposal_id="prop-1",
            actor_rep_id=_REP,
            transaction_key="txn-approve",
            now="2025-01-02T00:00:00Z",
        )


# -- approve_proposal: not found -------------------------------------------------------------------


def test_approve_proposal_raises_not_found_for_missing_proposal(store: Store) -> None:
    with pytest.raises(NotFound):
        approve_proposal(
            store,
            tenant=_TENANT,
            proposal_id="does-not-exist",
            actor_rep_id=_REP,
            transaction_key="txn-approve",
            now=_NOW,
        )


def test_approve_proposal_raises_not_found_for_proposal_in_another_tenant(store: Store) -> None:
    _seed_opportunity(store)
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="delete",
        actor_rep_id=_REP,
        reason="cleanup",
        transaction_key="txn-submit",
        now=_NOW,
    )
    with pytest.raises(NotFound):
        approve_proposal(
            store,
            tenant="eval-run-1",
            proposal_id="prop-1",
            actor_rep_id=_REP,
            transaction_key="txn-approve",
            now=_NOW,
        )


# -- approve_proposal: concurrent double approval -------------------------------------------------


def test_concurrent_double_approval_yields_exactly_one_winner(store: Store) -> None:
    """Two callers approving the same proposal; exactly one succeeds.

    The second call's ConditionExpression on status == pending fails once the first has committed,
    so TransactWriteItems cancellation produces the single winner.
    """
    _seed_opportunity(store)
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="update",
        actor_rep_id=_REP,
        reason="Deal is closing.",
        transaction_key="txn-submit",
        after_payload={"stage": "Closed Won"},
        now=_NOW,
    )

    results: list[dict[str, object]] = []
    conflicts: list[Conflict] = []

    for caller in ("caller-a", "caller-b"):
        try:
            result = approve_proposal(
                store,
                tenant=_TENANT,
                proposal_id="prop-1",
                actor_rep_id=caller,
                transaction_key=f"txn-approve-{caller}",
                now="2025-01-02T00:00:00Z",
            )
            results.append(result)
        except Conflict as exc:
            conflicts.append(exc)

    assert len(results) == 1
    assert len(conflicts) == 1

    stored_proposal = store.get_item("proposals", {"proposal_id": "prop-1"})
    assert stored_proposal is not None
    assert stored_proposal["status"] == "approved"

    final = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert final is not None
    assert final["stage"] == "Closed Won"


def test_double_approval_second_call_after_first_commits_is_conflict(store: Store) -> None:
    """A simpler sequential double-approve: the second call must see status already approved."""
    _seed_opportunity(store)
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="delete",
        actor_rep_id=_REP,
        reason="dead deal",
        transaction_key="txn-submit",
        now=_NOW,
    )

    approve_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        actor_rep_id=_REP,
        transaction_key="txn-approve-1",
        now="2025-01-02T00:00:00Z",
    )

    with pytest.raises(Conflict):
        approve_proposal(
            store,
            tenant=_TENANT,
            proposal_id="prop-1",
            actor_rep_id=_REP,
            transaction_key="txn-approve-2",
            now="2025-01-03T00:00:00Z",
        )


# -- reject_proposal: happy path and terminality ---------------------------------------------------


def test_reject_proposal_flips_status_and_does_not_mutate_target(store: Store) -> None:
    original = _seed_opportunity(store)
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="update",
        actor_rep_id=_REP,
        reason="Deal is closing.",
        transaction_key="txn-submit",
        after_payload={"stage": "Closed Won"},
        now=_NOW,
    )

    result = reject_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        actor_rep_id="rep-2",
        transaction_key="txn-reject",
        now="2025-01-02T00:00:00Z",
        reason="Not enough evidence.",
    )

    assert result["status"] == "rejected"
    assert result["rejection_reason"] == "Not enough evidence."
    unchanged = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert unchanged == original


def test_reject_proposal_is_terminal_and_cannot_later_be_approved(store: Store) -> None:
    _seed_opportunity(store)
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="update",
        actor_rep_id=_REP,
        reason="Deal is closing.",
        transaction_key="txn-submit",
        after_payload={"stage": "Closed Won"},
        now=_NOW,
    )
    reject_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        actor_rep_id="rep-2",
        transaction_key="txn-reject",
        now="2025-01-02T00:00:00Z",
    )

    with pytest.raises(Conflict):
        approve_proposal(
            store,
            tenant=_TENANT,
            proposal_id="prop-1",
            actor_rep_id=_REP,
            transaction_key="txn-approve",
            now="2025-01-03T00:00:00Z",
        )


def test_reject_proposal_cannot_be_rejected_twice(store: Store) -> None:
    _seed_opportunity(store)
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="delete",
        actor_rep_id=_REP,
        reason="dead deal",
        transaction_key="txn-submit",
        now=_NOW,
    )
    reject_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        actor_rep_id="rep-2",
        transaction_key="txn-reject-1",
        now="2025-01-02T00:00:00Z",
    )
    with pytest.raises(Conflict):
        reject_proposal(
            store,
            tenant=_TENANT,
            proposal_id="prop-1",
            actor_rep_id="rep-2",
            transaction_key="txn-reject-2",
            now="2025-01-03T00:00:00Z",
        )


def test_reject_proposal_raises_not_found_for_missing_proposal(store: Store) -> None:
    with pytest.raises(NotFound):
        reject_proposal(
            store,
            tenant=_TENANT,
            proposal_id="does-not-exist",
            actor_rep_id=_REP,
            transaction_key="txn-reject",
            now=_NOW,
        )


def test_concurrent_double_rejection_yields_exactly_one_winner(store: Store) -> None:
    """The same single-winner guarantee applies symmetrically to reject."""
    _seed_opportunity(store)
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="delete",
        actor_rep_id=_REP,
        reason="dead deal",
        transaction_key="txn-submit",
        now=_NOW,
    )

    results: list[dict[str, object]] = []
    conflicts: list[Conflict] = []
    for caller in ("caller-a", "caller-b"):
        try:
            result = reject_proposal(
                store,
                tenant=_TENANT,
                proposal_id="prop-1",
                actor_rep_id=caller,
                transaction_key=f"txn-reject-{caller}",
                now="2025-01-02T00:00:00Z",
            )
            results.append(result)
        except Conflict as exc:
            conflicts.append(exc)

    assert len(results) == 1
    assert len(conflicts) == 1


# -- approve/reject resolve table names through the store's prefix ------------------------------


def test_approve_proposal_update_succeeds_against_a_prefixed_store(prefixed_store: Store) -> None:
    """Approving an ``update`` proposal succeeds under a non-empty ``table_prefix``."""
    _seed_opportunity(prefixed_store)
    proposal = submit_proposal(
        prefixed_store,
        tenant=_TENANT,
        proposal_id="prop-prefixed-update",
        target_table="opportunities",
        target_id="opp-1",
        operation="update",
        actor_rep_id=_REP,
        reason="renegotiate",
        transaction_key="txn-prefixed-submit",
        after_payload={"value": 2000},
        now=_NOW,
    )

    approved = approve_proposal(
        prefixed_store,
        tenant=_TENANT,
        proposal_id=str(proposal["proposal_id"]),
        actor_rep_id=_REP,
        transaction_key="txn-prefixed-approve",
        now="2025-01-02T00:00:00Z",
    )
    assert approved["status"] == "approved"

    updated = prefixed_store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert updated is not None
    assert updated["value"] == 2000


def test_approve_proposal_delete_succeeds_against_a_prefixed_store(prefixed_store: Store) -> None:
    """Approving a ``delete`` proposal succeeds under a non-empty ``table_prefix``."""
    _seed_opportunity(prefixed_store)
    proposal = submit_proposal(
        prefixed_store,
        tenant=_TENANT,
        proposal_id="prop-prefixed-delete",
        target_table="opportunities",
        target_id="opp-1",
        operation="delete",
        actor_rep_id=_REP,
        reason="dead deal",
        transaction_key="txn-prefixed-submit-delete",
        now=_NOW,
    )

    approve_proposal(
        prefixed_store,
        tenant=_TENANT,
        proposal_id=str(proposal["proposal_id"]),
        actor_rep_id=_REP,
        transaction_key="txn-prefixed-approve-delete",
        now="2025-01-02T00:00:00Z",
    )

    assert prefixed_store.get_item("opportunities", {"opportunity_id": "opp-1"}) is None


def test_reject_proposal_succeeds_against_a_prefixed_store(prefixed_store: Store) -> None:
    """Rejecting a proposal succeeds under a non-empty ``table_prefix``."""
    _seed_opportunity(prefixed_store)
    proposal = submit_proposal(
        prefixed_store,
        tenant=_TENANT,
        proposal_id="prop-prefixed-reject",
        target_table="opportunities",
        target_id="opp-1",
        operation="update",
        actor_rep_id=_REP,
        reason="never mind",
        transaction_key="txn-prefixed-submit-reject",
        after_payload={"value": 3000},
        now=_NOW,
    )

    rejected = reject_proposal(
        prefixed_store,
        tenant=_TENANT,
        proposal_id=str(proposal["proposal_id"]),
        actor_rep_id=_REP,
        transaction_key="txn-prefixed-reject",
        now="2025-01-02T00:00:00Z",
    )
    assert rejected["status"] == "rejected"

    unchanged = prefixed_store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert unchanged is not None
    assert unchanged["value"] == 1000


_PLANTED = "planted-caller-value-7f3a"


def _submit_kwargs(**overrides: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "tenant": _TENANT,
        "proposal_id": "prop-1",
        "target_table": "opportunities",
        "target_id": "opp-1",
        "operation": "delete",
        "actor_rep_id": _REP,
        "reason": "cleanup",
        "transaction_key": "txn-1",
        "now": _NOW,
    }
    kwargs.update(overrides)
    return kwargs


@pytest.mark.parametrize(
    ("field", "error_class", "source"),
    [
        ("target_table", ValidationFailed, "target_table"),
        ("operation", ValidationFailed, "operation"),
        ("target_id", NotFound, None),
    ],
)
def test_submit_errors_never_echo_the_caller_value_in_the_message(
    store: Store, field: str, error_class: type[Exception], source: str | None
) -> None:
    """The caller's value reaches ``detail`` only; the class and ``source`` are unchanged."""
    _seed_opportunity(store)
    with pytest.raises(error_class) as raised:
        submit_proposal(store, **_submit_kwargs(**{field: _PLANTED}))
    error: Any = raised.value
    assert _PLANTED not in error.message
    assert _PLANTED not in str(error)
    assert getattr(error, "source", None) == source
    assert error.detail == f"{field}={_PLANTED!r}"


@pytest.mark.parametrize("decide", [approve_proposal, reject_proposal])
def test_decision_not_found_never_echoes_the_proposal_id_in_the_message(
    store: Store, decide: Any
) -> None:
    with pytest.raises(NotFound) as raised:
        decide(
            store,
            tenant=_TENANT,
            proposal_id=_PLANTED,
            actor_rep_id=_REP,
            transaction_key="txn-decide",
            now=_NOW,
        )
    assert raised.value.message == "The proposal does not exist."
    assert raised.value.detail == f"proposal_id={_PLANTED!r}"


def test_an_overlong_caller_value_is_bounded_in_detail(store: Store) -> None:
    long_value = "x" * 500
    with pytest.raises(ValidationFailed) as raised:
        submit_proposal(store, **_submit_kwargs(target_table=long_value))
    assert raised.value.detail == f"target_table={'x' * 64!r}"

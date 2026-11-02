# SPDX-License-Identifier: MIT
"""The proposal lifecycle and audit/revert against DynamoDB Local.

Covers stale before-snapshot refusal, concurrent double-approval, and revert-once semantics, all
through the shared prefixed `dynamodb_local_store` fixture so `Store.transact_write`'s table-prefix
resolution is exercised.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest

from crm.audit import build_audit_record, revert
from crm.authz import Identity
from crm.errors import Conflict
from crm.handlers.wire import put_operation
from crm.proposals import ApprovalContext, approve_proposal, reject_proposal, submit_proposal
from crm.store.store import Store

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"


def _run_scoped_key(name: str) -> str:
    """Return a ``transaction_key`` unique to this test invocation, at most 36 characters.

    `name` labels the call site only; the token is random hex, since `ClientRequestToken` is
    limited to 36 characters.
    """
    del name
    return uuid.uuid4().hex + uuid.uuid4().hex[:4]


@pytest.fixture
def proposal_store(dynamodb_local_store: tuple[Store, str]) -> Store:
    """The shared, prefixed :class:`Store` from `conftest.py`'s `dynamodb_local_store`."""
    store, _table_prefix = dynamodb_local_store
    return store


def _identity(*, rep_id: str = _REP, tenant: str = _TENANT) -> Identity:
    return Identity(rep_id=rep_id, book=frozenset({_ACCOUNT}), tenant=tenant)


def _seed_account(store: Store, *, account_id: str = _ACCOUNT) -> dict[str, object]:
    item: dict[str, object] = {
        "account_id": account_id,
        "tenant": _TENANT,
        "tenant_owner_rep_id": f"{_TENANT}#{_REP}",
        "owner_rep_id": _REP,
        "name": "Acme",
        "name_lower": "acme",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    store.put_item("accounts", item)
    return item


def _seed_opportunity(
    store: Store,
    *,
    opportunity_id: str,
    account_id: str = _ACCOUNT,
    value: int = 1000,
) -> dict[str, object]:
    item: dict[str, object] = {
        "opportunity_id": opportunity_id,
        "tenant": _TENANT,
        "tenant_account_id": f"{_TENANT}#{account_id}",
        "tenant_owner_rep_id": f"{_TENANT}#{_REP}",
        "tenant_stage": f"{_TENANT}#Discovery",
        "account_id": account_id,
        "owner_rep_id": _REP,
        "name": "Big Deal",
        "name_lower": "big deal",
        "stage": "Discovery",
        "value": value,
        "primary_contact_id": None,
        "last_touch": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    store.put_item("opportunities", item)
    return item


def _submit(
    store: Store,
    *,
    proposal_id: str,
    target_id: str,
    after_payload: dict[str, object] | None = None,
    transaction_key: str | None = None,
) -> dict[str, object]:
    return submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id=proposal_id,
        target_table="opportunities",
        target_id=target_id,
        operation="update",
        actor_rep_id=_REP,
        reason="test",
        transaction_key=transaction_key or _run_scoped_key(f"tx-submit-{proposal_id}"),
        after_payload=after_payload or {"value": 2000},
        now="2026-01-01T01:00:00+00:00",
    )


def _record_audit_callback(identity: Identity) -> Any:
    def _callback(context: ApprovalContext) -> dict[str, Any]:
        audit = build_audit_record(
            identity=identity,
            target_table=context.target_table,
            target_id=context.target_id,
            before_snapshot=dict(context.before_snapshot),
            after_snapshot=dict(context.after_snapshot) if context.after_snapshot else {},
        )
        return put_operation("audit_records", audit.to_item())

    return _callback


# -- Stale before-snapshot refused with Conflict -------------------------------------------------


def test_approve_proposal_refuses_a_stale_before_snapshot_on_the_real_engine(
    proposal_store: Store,
) -> None:
    """Approving after the target row changed since submission raises `Conflict` and writes
    nothing."""
    _seed_account(proposal_store)
    opportunity_id = f"opp-{uuid.uuid4()}"
    _seed_opportunity(proposal_store, opportunity_id=opportunity_id)
    proposal_id = f"prop-stale-{uuid.uuid4()}"
    proposal = _submit(proposal_store, proposal_id=proposal_id, target_id=opportunity_id)

    stale_update = proposal_store.get_item("opportunities", {"opportunity_id": opportunity_id})
    assert stale_update is not None
    stale_update["value"] = 9999
    proposal_store.put_item("opportunities", stale_update)

    with pytest.raises(Conflict):
        approve_proposal(
            proposal_store,
            tenant=_TENANT,
            proposal_id=str(proposal["proposal_id"]),
            actor_rep_id=_REP,
            transaction_key=_run_scoped_key("tx-approve-stale"),
            now="2026-01-01T02:00:00+00:00",
        )

    reread_proposal = proposal_store.get_item("proposals", {"proposal_id": proposal_id})
    assert reread_proposal is not None
    assert reread_proposal["status"] == "pending"
    reread_opportunity = proposal_store.get_item(
        "opportunities", {"opportunity_id": opportunity_id}
    )
    assert reread_opportunity is not None
    assert reread_opportunity["value"] == 9999


# -- Concurrent double-approval resolves to exactly one winner ------------------------------------


def test_concurrent_double_approval_resolves_to_exactly_one_winner_on_the_real_engine(
    proposal_store: Store,
) -> None:
    """Two concurrent approvals of one proposal, with distinct transaction keys, yield exactly one
    approval, one `Conflict`, and one audit record.

    A `threading.Barrier` inside a patched `Store.transact_write` holds both threads until their
    `TransactWriteItems` calls are issued together.
    """
    _seed_account(proposal_store)
    opportunity_id = f"opp-{uuid.uuid4()}"
    _seed_opportunity(proposal_store, opportunity_id=opportunity_id)
    proposal = _submit(
        proposal_store, proposal_id=f"prop-race-{uuid.uuid4()}", target_id=opportunity_id
    )
    proposal_id = str(proposal["proposal_id"])

    barrier = threading.Barrier(2)
    real_transact_write = Store.transact_write

    def _synchronised_transact_write(self: Store, operations: Any, **kwargs: Any) -> None:
        barrier.wait(timeout=10)
        real_transact_write(self, operations, **kwargs)

    results: dict[str, str] = {}
    lock = threading.Lock()

    def _approve(actor_rep_id: str, transaction_key: str) -> None:
        try:
            approve_proposal(
                proposal_store,
                tenant=_TENANT,
                proposal_id=proposal_id,
                actor_rep_id=actor_rep_id,
                transaction_key=transaction_key,
                now="2026-01-01T02:00:00+00:00",
                record_audit=_record_audit_callback(_identity(rep_id=actor_rep_id)),
            )
            outcome = "approved"
        except Conflict:
            outcome = "conflict"
        with lock:
            results[actor_rep_id] = outcome

    thread_a = threading.Thread(target=_approve, args=("rep-a", _run_scoped_key("tx-race-a")))
    thread_b = threading.Thread(target=_approve, args=("rep-b", _run_scoped_key("tx-race-b")))

    original_transact_write = Store.transact_write
    Store.transact_write = _synchronised_transact_write  # type: ignore[method-assign]
    try:
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)
    finally:
        Store.transact_write = original_transact_write  # type: ignore[method-assign]

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert set(results.values()) == {"approved", "conflict"}, results

    final_proposal = proposal_store.get_item("proposals", {"proposal_id": proposal_id})
    assert final_proposal is not None
    assert final_proposal["status"] == "approved"

    audit_scan = proposal_store._client.scan(
        TableName=proposal_store.physical_name("audit_records")
    )  # noqa: SLF001
    matching_audits = [
        item for item in audit_scan["Items"] if item["target_id"]["S"] == opportunity_id
    ]
    assert len(matching_audits) == 1, "exactly one approval's audit record must have been written"

    final_opportunity = proposal_store.get_item("opportunities", {"opportunity_id": opportunity_id})
    assert final_opportunity is not None
    assert final_opportunity["value"] == 2000


def test_reject_after_approve_is_refused_by_the_real_engine(proposal_store: Store) -> None:
    """`reject_proposal` on an already-approved proposal raises `Conflict`."""
    _seed_account(proposal_store)
    opportunity_id = f"opp-{uuid.uuid4()}"
    _seed_opportunity(proposal_store, opportunity_id=opportunity_id)
    proposal = _submit(
        proposal_store, proposal_id=f"prop-terminal-{uuid.uuid4()}", target_id=opportunity_id
    )
    proposal_id = str(proposal["proposal_id"])

    approve_proposal(
        proposal_store,
        tenant=_TENANT,
        proposal_id=proposal_id,
        actor_rep_id=_REP,
        transaction_key=_run_scoped_key("tx-terminal-approve"),
        now="2026-01-01T02:00:00+00:00",
    )

    with pytest.raises(Conflict):
        reject_proposal(
            proposal_store,
            tenant=_TENANT,
            proposal_id=proposal_id,
            actor_rep_id=_REP,
            transaction_key=_run_scoped_key("tx-terminal-reject"),
            now="2026-01-01T03:00:00+00:00",
        )


# -- Revert restores once and refuses a second attempt -------------------------------------------


def _approve_and_audit(store: Store, *, proposal_id: str, target_id: str) -> str:
    """Approve a pending proposal with an audit callback wired in and return the new audit_id."""
    identity = _identity()
    approve_proposal(
        store,
        tenant=_TENANT,
        proposal_id=proposal_id,
        actor_rep_id=_REP,
        transaction_key=_run_scoped_key(f"tx-approve-{proposal_id}"),
        now="2026-01-01T02:00:00+00:00",
        record_audit=_record_audit_callback(identity),
    )
    scan = store._client.scan(TableName=store.physical_name("audit_records"))  # noqa: SLF001
    matching = [
        item
        for item in scan["Items"]
        if item["target_id"]["S"] == target_id and item["status"]["S"] == "active"
    ]
    assert len(matching) == 1
    return str(matching[0]["audit_id"]["S"])


def test_revert_restores_the_target_record_exactly_once_on_the_real_engine(
    proposal_store: Store,
) -> None:
    """A revert restores the opportunity's pre-approval `value` and marks the audit record
    `reversed`."""
    _seed_account(proposal_store)
    opportunity_id = f"opp-{uuid.uuid4()}"
    _seed_opportunity(proposal_store, opportunity_id=opportunity_id, value=1000)
    proposal = _submit(
        proposal_store,
        proposal_id=f"prop-revert-{uuid.uuid4()}",
        target_id=opportunity_id,
        after_payload={"value": 5000},
    )
    audit_id = _approve_and_audit(
        proposal_store, proposal_id=str(proposal["proposal_id"]), target_id=opportunity_id
    )

    approved_opportunity = proposal_store.get_item(
        "opportunities", {"opportunity_id": opportunity_id}
    )
    assert approved_opportunity is not None
    assert approved_opportunity["value"] == 5000

    identity = _identity()
    revert(
        identity, proposal_store, audit_id=audit_id, transaction_key=_run_scoped_key("tx-revert")
    )

    restored_opportunity = proposal_store.get_item(
        "opportunities", {"opportunity_id": opportunity_id}
    )
    assert restored_opportunity is not None
    assert restored_opportunity["value"] == 1000

    reread_audit = proposal_store.get_item("audit_records", {"audit_id": audit_id})
    assert reread_audit is not None
    assert reread_audit["status"] == "reversed"


def test_revert_refuses_a_second_attempt_on_the_real_engine(proposal_store: Store) -> None:
    """A second `revert` of the same audit record raises `Conflict` and leaves the target row
    unchanged."""
    _seed_account(proposal_store)
    opportunity_id = f"opp-{uuid.uuid4()}"
    _seed_opportunity(proposal_store, opportunity_id=opportunity_id, value=1000)
    proposal = _submit(
        proposal_store,
        proposal_id=f"prop-revert-twice-{uuid.uuid4()}",
        target_id=opportunity_id,
        after_payload={"value": 7000},
    )
    audit_id = _approve_and_audit(
        proposal_store, proposal_id=str(proposal["proposal_id"]), target_id=opportunity_id
    )

    identity = _identity()
    revert(
        identity,
        proposal_store,
        audit_id=audit_id,
        transaction_key=_run_scoped_key("tx-revert-first"),
    )

    with pytest.raises(Conflict):
        revert(
            identity,
            proposal_store,
            audit_id=audit_id,
            transaction_key=_run_scoped_key("tx-revert-second"),
        )

    unchanged_opportunity = proposal_store.get_item(
        "opportunities", {"opportunity_id": opportunity_id}
    )
    assert unchanged_opportunity is not None
    assert unchanged_opportunity["value"] == 1000


def test_concurrent_double_revert_resolves_to_exactly_one_winner_on_the_real_engine(
    proposal_store: Store,
) -> None:
    """Two barrier-synchronised concurrent reverts of one audit record yield exactly one revert and
    one `Conflict`, with the target row restored once."""
    _seed_account(proposal_store)
    opportunity_id = f"opp-{uuid.uuid4()}"
    _seed_opportunity(proposal_store, opportunity_id=opportunity_id, value=1000)
    proposal = _submit(
        proposal_store,
        proposal_id=f"prop-revert-race-{uuid.uuid4()}",
        target_id=opportunity_id,
        after_payload={"value": 6000},
    )
    audit_id = _approve_and_audit(
        proposal_store, proposal_id=str(proposal["proposal_id"]), target_id=opportunity_id
    )

    barrier = threading.Barrier(2)
    real_transact_write = Store.transact_write

    def _synchronised_transact_write(self: Store, operations: Any, **kwargs: Any) -> None:
        barrier.wait(timeout=10)
        real_transact_write(self, operations, **kwargs)

    results: dict[str, str] = {}
    lock = threading.Lock()
    identity = _identity()

    def _revert(transaction_key: str) -> None:
        try:
            revert(identity, proposal_store, audit_id=audit_id, transaction_key=transaction_key)
            outcome = "reverted"
        except Conflict:
            outcome = "conflict"
        with lock:
            results[transaction_key] = outcome

    key_a = _run_scoped_key("tx-revert-race-a")
    key_b = _run_scoped_key("tx-revert-race-b")
    thread_a = threading.Thread(target=_revert, args=(key_a,))
    thread_b = threading.Thread(target=_revert, args=(key_b,))

    original_transact_write = Store.transact_write
    Store.transact_write = _synchronised_transact_write  # type: ignore[method-assign]
    try:
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)
    finally:
        Store.transact_write = original_transact_write  # type: ignore[method-assign]

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert set(results.values()) == {"reverted", "conflict"}, results

    final_audit = proposal_store.get_item("audit_records", {"audit_id": audit_id})
    assert final_audit is not None
    assert final_audit["status"] == "reversed"

    final_opportunity = proposal_store.get_item("opportunities", {"opportunity_id": opportunity_id})
    assert final_opportunity is not None
    assert final_opportunity["value"] == 1000

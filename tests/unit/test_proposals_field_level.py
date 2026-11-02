# SPDX-License-Identifier: MIT
"""Field-level, atomic approval against a `moto`-mocked DynamoDB.

`approve_proposal` compares and writes only the fields in `after_payload`, plus `updated_at` and
the derived index attributes those fields feed. Writes to any other field no longer block approval
and are never overwritten. A moved touched field is `Conflict(reason="fields_changed")` with
`conflicting_fields`; a missing or re-tenanted target is `reason="target_missing"`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm import proposals
from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import Conflict, ValidationFailed
from crm.handlers import approve_proposal, submit_proposal
from crm.handlers._common import idempotent_id
from crm.proposals import ApprovalContext
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"
_OTHER_ACCOUNT = "acct-2"
_OPPORTUNITY = "opp-1"
_NOW = "2026-02-01T00:00:00+00:00"


@pytest.fixture
def client() -> Iterator[Any]:
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(dynamodb)
        yield dynamodb


@pytest.fixture
def store(client: Any) -> Store:
    return Store(client=client)


def _event(args: dict[str, Any], *, book: list[str] | None = None) -> dict[str, Any]:
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


def _seed_opportunity(store: Store, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "opportunity_id": _OPPORTUNITY,
        "tenant": _TENANT,
        "account_id": _ACCOUNT,
        "name": "Acme Renewal",
        "stage": "Discovery",
        "close_date": "2026-06-30",
        "owner_rep_id": _REP,
        "value": 1000,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    item.update(overrides)
    store.put_item("opportunities", item)
    return item


def _submit(store: Store, after_payload: dict[str, object] | None = None, **extra: Any) -> str:
    args: dict[str, Any] = {
        "target_table": "opportunities",
        "target_id": _OPPORTUNITY,
        "operation": "update",
        "after_payload": after_payload or {"stage": "Qualification"},
        "reason": "advance",
        "transaction_key": "tx-submit",
        **extra,
    }
    if args["operation"] == "delete":
        args.pop("after_payload")
    result = submit_proposal.handler(_event(args), store=store)
    assert result["statusCode"] == 200, result
    return str(result["body"]["proposal"]["proposal_id"])


def _approve(store: Store, proposal_id: str, *, key: str = "tx-approve", **event: Any) -> Any:
    return approve_proposal.handler(
        _event({"proposal_id": proposal_id, "transaction_key": key}, **event), store=store
    )


def _target(store: Store) -> dict[str, object]:
    item = store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})
    assert item is not None
    return item


def _status(store: Store, proposal_id: str) -> object:
    item = store.get_item("proposals", {"proposal_id": proposal_id})
    assert item is not None
    return item["status"]


def _audit_ids(store: Store) -> list[str]:
    items, _ = store.query_tenant_scan("audit_records", tenant=_TENANT, limit=100)
    return sorted(str(item["audit_id"]) for item in items)


def _touch(store: Store, **fields: object) -> None:
    store.put_item("opportunities", {**_target(store), **fields})


# -- unrelated writes no longer block ---------------------------------------------------------


@pytest.mark.parametrize(
    "unrelated",
    [
        {"last_touch": "2026-01-15T00:00:00+00:00"},
        {"updated_at": "2026-01-15T00:00:00+00:00"},
        {"_idempotency_key": "tx-other", "_idempotency_fingerprint": "abc"},
        {"value": 9999, "name": "Renamed"},
    ],
    ids=["last_touch", "updated_at", "idempotency_stamps", "other_fields"],
)
def test_a_write_to_an_untouched_field_does_not_block_and_survives(
    store: Store, unrelated: dict[str, object]
) -> None:
    """Approve succeeds and keeps the concurrent unrelated value (no lost update)."""
    _seed_opportunity(store)
    proposal_id = _submit(store)
    _touch(store, **unrelated)

    result = _approve(store, proposal_id)

    assert result["statusCode"] == 200, result
    target = _target(store)
    assert target["stage"] == "Qualification"
    for field, value in unrelated.items():
        if field != "updated_at":
            assert target[field] == value


def test_a_sibling_proposal_approved_on_another_field_does_not_block(store: Store) -> None:
    """Two pending proposals on disjoint fields both approve; both values land."""
    _seed_opportunity(store)
    first = _submit(store, {"stage": "Qualification"})
    second = _submit(store, {"name": "Acme Renewal 2027"}, transaction_key="tx-submit-2")

    assert _approve(store, first, key="tx-a1")["statusCode"] == 200
    assert _approve(store, second, key="tx-a2")["statusCode"] == 200

    target = _target(store)
    assert (target["stage"], target["name"]) == ("Qualification", "Acme Renewal 2027")


# -- a moved touched field is refused with the three values -----------------------------------


def test_a_changed_touched_field_is_fields_changed_with_conflicting_fields(store: Store) -> None:
    _seed_opportunity(store)
    proposal_id = _submit(store, {"stage": "Qualification", "name": "Acme 2027"})
    _touch(store, stage="Negotiation")
    before = _target(store)

    result = _approve(store, proposal_id)

    assert result["statusCode"] == 409
    assert result["error"]["reason"] == "fields_changed"
    assert result["error"]["conflicting_fields"] == [
        {
            "field": "stage",
            "snapshot": "Discovery",
            "current": "Negotiation",
            "proposed": "Qualification",
        }
    ]
    assert _target(store) == before
    assert _status(store, proposal_id) == "pending"
    assert _audit_ids(store) == []


def test_a_touched_field_absent_at_submit_and_set_since_is_fields_changed(store: Store) -> None:
    _seed_opportunity(store)
    proposal_id = _submit(store, {"description": "From the proposal"})
    _touch(store, description="Written meanwhile")

    result = _approve(store, proposal_id)

    assert result["statusCode"] == 409
    assert result["error"]["conflicting_fields"] == [
        {
            "field": "description",
            "snapshot": None,
            "current": "Written meanwhile",
            "proposed": "From the proposal",
        }
    ]


# -- missing, re-tenanted and decided targets -------------------------------------------------


def test_a_deleted_target_is_target_missing(store: Store) -> None:
    _seed_opportunity(store)
    proposal_id = _submit(store)
    store.delete_item("opportunities", {"opportunity_id": _OPPORTUNITY})

    result = _approve(store, proposal_id)

    assert result["statusCode"] == 409
    assert result["error"]["reason"] == "target_missing"
    assert _status(store, proposal_id) == "pending"


def test_a_target_moved_to_another_tenant_is_refused_and_not_written(store: Store) -> None:
    """The tenant mismatch reads as `target_missing`; the foreign-tenant record is untouched."""
    _seed_opportunity(store)
    proposal_id = _submit(store)
    _touch(store, tenant="eval-other")
    before = _target(store)

    with pytest.raises(Conflict) as excinfo:
        proposals.approve_proposal(
            store,
            tenant=_TENANT,
            proposal_id=proposal_id,
            actor_rep_id=_REP,
            transaction_key="tx-approve",
            now=_NOW,
        )

    assert excinfo.value.reason == "target_missing"
    assert _target(store) == before


def test_an_already_decided_proposal_is_already_decided(store: Store) -> None:
    _seed_opportunity(store)
    proposal_id = _submit(store)
    assert _approve(store, proposal_id, key="tx-1")["statusCode"] == 200

    result = _approve(store, proposal_id, key="tx-2")

    assert result["statusCode"] == 409
    assert result["error"]["reason"] == "already_decided"


def test_a_delete_proposal_is_not_blocked_by_an_unrelated_write(store: Store) -> None:
    _seed_opportunity(store)
    proposal_id = _submit(store, operation="delete")
    _touch(store, last_touch="2026-01-20T00:00:00+00:00")

    assert _approve(store, proposal_id)["statusCode"] == 200
    assert store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY}) is None


def test_a_payload_assigning_the_key_or_tenant_is_refused_before_any_write(store: Store) -> None:
    """A stored payload that would redirect or re-tenant the write is `ValidationFailed`."""
    for field, value in (("opportunity_id", "opp-other"), ("tenant", "eval-x")):
        _seed_opportunity(store)
        proposal_id = f"prop-{field}"
        store.put_item(
            "proposals",
            {
                "proposal_id": proposal_id,
                "tenant": _TENANT,
                "status": "pending",
                "target_table": "opportunities",
                "target_id": _OPPORTUNITY,
                "operation": "update",
                "before_snapshot": _target(store),
                "after_payload": {field: value},
                "reason": "r",
                "actor_rep_id": _REP,
                "updated_at": _NOW,
            },
        )
        before = _target(store)
        with pytest.raises(ValidationFailed):
            proposals.approve_proposal(
                store,
                tenant=_TENANT,
                proposal_id=proposal_id,
                actor_rep_id=_REP,
                transaction_key=f"tx-{field}",
                now=_NOW,
            )
        assert _target(store) == before


# -- derived index attributes -----------------------------------------------------------------


def test_derived_attributes_fed_by_a_touched_field_are_rewritten(store: Store) -> None:
    """`tenant_stage` and `stage_close_date` follow the new `stage`; the GSI finds it there."""
    _seed_opportunity(store)
    proposal_id = _submit(store)

    assert _approve(store, proposal_id)["statusCode"] == 200

    target = _target(store)
    assert target["tenant_stage"] == "demo#Qualification"
    assert target["stage_close_date"] == "Qualification#2026-06-30"
    found, _ = store.query_gsi("opportunities", "gsi_stage", partition_value="demo#Qualification")
    assert [item["opportunity_id"] for item in found] == [_OPPORTUNITY]


# -- TOCTOU: a write lands between the read and the transaction -------------------------------


class _WriteBeforeTransact(Store):
    """A store that applies one concurrent target write right before its next transaction."""

    def __init__(self, *, client: Any, fields: dict[str, object]) -> None:
        super().__init__(client=client)
        self._fields = fields

    def transact_write(self, operations: Any, *, transaction_key: str | None = None) -> None:
        current = self.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})
        assert current is not None
        self.put_item("opportunities", {**current, **self._fields})
        super().transact_write(operations, transaction_key=transaction_key)


def test_a_touched_field_written_after_the_read_cancels_the_transaction(client: Any) -> None:
    """The per-field condition catches the race; re-read explains it as `fields_changed`."""
    plain = Store(client=client)
    _seed_opportunity(plain)
    proposal_id = _submit(plain)

    result = _approve(_WriteBeforeTransact(client=client, fields={"stage": "Lost"}), proposal_id)

    assert result["statusCode"] == 409
    assert result["error"]["reason"] == "fields_changed"
    assert result["error"]["conflicting_fields"][0]["current"] == "Lost"
    assert _target(plain)["stage"] == "Lost"
    assert _status(plain, proposal_id) == "pending"
    assert _audit_ids(plain) == []


def test_an_untouched_source_of_a_rewritten_derived_attribute_is_guarded(client: Any) -> None:
    """`close_date` feeds `stage_close_date`; a racing change to it cancels the approval."""
    plain = Store(client=client)
    _seed_opportunity(plain)
    proposal_id = _submit(plain)

    result = _approve(
        _WriteBeforeTransact(client=client, fields={"close_date": "2026-09-30"}), proposal_id
    )

    assert result["statusCode"] == 409
    assert result["error"]["reason"] == "fields_changed"
    assert result["error"]["conflicting_fields"] == [
        {"field": "close_date", "snapshot": "2026-06-30", "current": "2026-09-30", "proposed": None}
    ]
    target = _target(plain)
    assert (target["stage"], target["stage_close_date"]) == ("Discovery", "Discovery#2026-09-30")
    assert _status(plain, proposal_id) == "pending"


def test_an_unrelated_write_after_the_read_still_commits_and_survives(client: Any) -> None:
    plain = Store(client=client)
    _seed_opportunity(plain)
    proposal_id = _submit(plain)

    result = _approve(_WriteBeforeTransact(client=client, fields={"value": 4242}), proposal_id)

    assert result["statusCode"] == 200, result
    target = _target(plain)
    assert (target["stage"], target["value"]) == ("Qualification", 4242)


# -- audit record contents and id -------------------------------------------------------------


def test_the_audit_snapshots_are_the_reread_record_and_it_merged_with_the_payload(
    store: Store,
) -> None:
    _seed_opportunity(store)
    proposal_id = _submit(store)
    _touch(store, value=7)
    contexts: list[ApprovalContext] = []

    def record(context: ApprovalContext) -> dict[str, Any]:
        contexts.append(context)
        return {
            "ConditionCheck": {
                "TableName": "audit_records",
                "Key": {"audit_id": {"S": "audit-probe"}},
                "ConditionExpression": "attribute_not_exists(audit_id)",
            }
        }

    proposals.approve_proposal(
        store,
        tenant=_TENANT,
        proposal_id=proposal_id,
        actor_rep_id=_REP,
        transaction_key="tx",
        now=_NOW,
        record_audit=record,
    )

    (context,) = contexts
    assert context.before_snapshot["value"] == 7
    assert context.before_snapshot["stage"] == "Discovery"
    assert context.after_snapshot is not None
    assert context.after_snapshot["value"] == 7
    assert context.after_snapshot["stage"] == "Qualification"
    assert context.after_snapshot["updated_at"] == _NOW
    assert context.after_snapshot == {**_target(store)}


def test_the_approve_audit_id_is_derived_from_the_transaction_key(store: Store) -> None:
    """A retried approval maps to the same audit record id."""
    _seed_opportunity(store)
    proposal_id = _submit(store)

    assert _approve(store, proposal_id, key="tx-stable")["statusCode"] == 200

    assert _audit_ids(store) == [idempotent_id("audit", "tx-stable")]


# -- authorization reads the current target, not the snapshot ---------------------------------


def test_approve_is_denied_when_the_target_moved_out_of_book_since_submit(store: Store) -> None:
    """Snapshot in book, current target in another account: 403, nothing written."""
    _seed_opportunity(store)
    proposal_id = _submit(store)
    _touch(store, account_id=_OTHER_ACCOUNT)
    before = _target(store)

    result = _approve(store, proposal_id)

    assert result["statusCode"] == 403
    assert result["error"]["code"] == "scope_denied"
    assert _target(store) == before
    assert _status(store, proposal_id) == "pending"
    assert _audit_ids(store) == []


# -- the cancellation explainer and retries --------------------------------------------------


class _FailingReadsAfterCancel(_WriteBeforeTransact):
    """Like `_WriteBeforeTransact`, and every read after the cancelled transaction fails."""

    def __init__(self, *, client: Any, fields: dict[str, object]) -> None:
        super().__init__(client=client, fields=fields)
        self._broken = False

    def get_item(self, table: str, key: Any, *, consistent: bool = False) -> Any:
        if self._broken:
            raise RuntimeError("read path down")
        return super().get_item(table, key, consistent=consistent)

    def transact_write(self, operations: Any, *, transaction_key: str | None = None) -> None:
        try:
            super().transact_write(operations, transaction_key=transaction_key)
        finally:
            self._broken = True


def test_a_failing_re_read_falls_back_to_the_original_conflict(client: Any) -> None:
    """The explainer never turns a 409 into a 500: a failed re-read keeps the original Conflict."""
    plain = Store(client=client)
    _seed_opportunity(plain)
    proposal_id = _submit(plain)

    result = _approve(
        _FailingReadsAfterCancel(client=client, fields={"stage": "Lost"}), proposal_id
    )

    assert result["statusCode"] == 409
    assert result["error"]["code"] == "conflict"
    assert _status(plain, proposal_id) == "pending"


class _DeleteProposalBeforeTransact(Store):
    def transact_write(self, operations: Any, *, transaction_key: str | None = None) -> None:
        for operation in operations:
            update = operation.get("Update", {})
            if update.get("TableName") == "proposals":
                self.delete_item("proposals", {"proposal_id": update["Key"]["proposal_id"]["S"]})
        super().transact_write(operations, transaction_key=transaction_key)


def test_a_proposal_deleted_mid_approval_is_not_found_not_already_decided(client: Any) -> None:
    plain = Store(client=client)
    _seed_opportunity(plain)
    proposal_id = _submit(plain)

    result = _approve(_DeleteProposalBeforeTransact(client=client), proposal_id)

    assert result["statusCode"] == 404
    assert _target(plain)["stage"] == "Discovery"


def test_a_retried_approval_with_the_same_key_is_an_idempotent_success(store: Store) -> None:
    _seed_opportunity(store)
    proposal_id = _submit(store)
    first = _approve(store, proposal_id, key="tx-retry")
    target_after_first = _target(store)

    second = _approve(store, proposal_id, key="tx-retry")

    assert first["statusCode"] == 200, first
    assert second["statusCode"] == 200, second
    assert second["body"]["proposal"]["status"] == "approved"
    assert _target(store) == target_after_first
    assert _audit_ids(store) == [idempotent_id("audit", "tx-retry")]


def test_a_second_approval_with_a_different_key_is_still_already_decided(store: Store) -> None:
    _seed_opportunity(store)
    proposal_id = _submit(store)
    assert _approve(store, proposal_id, key="tx-one")["statusCode"] == 200

    result = _approve(store, proposal_id, key="tx-two")

    assert result["statusCode"] == 409
    assert result["error"]["reason"] == "already_decided"


class _CommitThenReportFailure(Store):
    """A transaction that commits, then reports a cancellation (a lost or misread response)."""

    def transact_write(self, operations: Any, *, transaction_key: str | None = None) -> None:
        super().transact_write(operations, transaction_key=transaction_key)
        raise Conflict("The operation conflicted with a concurrent change.")


def test_an_approval_that_committed_despite_an_error_reports_success(client: Any) -> None:
    plain = Store(client=client)
    _seed_opportunity(plain)
    proposal_id = _submit(plain)

    result = _approve(_CommitThenReportFailure(client=client), proposal_id, key="tx-lost")

    assert result["statusCode"] == 200, result
    assert _status(plain, proposal_id) == "approved"
    assert _audit_ids(plain) == [idempotent_id("audit", "tx-lost")]

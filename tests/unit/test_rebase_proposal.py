# SPDX-License-Identifier: MIT
"""Tests for the `rebase_proposal` tool against a `moto`-mocked DynamoDB.

A rebase replaces a pending proposal's before-snapshot with the target's current state and writes a
`proposal_rebased` audit record in the same transaction, conditioned on `status = pending` and the
`updated_at` it read. It is authorized exactly as `approve_proposal`. Also covers the structured
`Conflict` fields (`reason`, `conflicting_fields`) rendered by `to_api_response`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import Conflict
from crm.handlers import (
    approve_proposal,
    rebase_proposal,
    reject_proposal,
    revert_audit_record,
    submit_proposal,
)
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"
_OTHER_ACCOUNT = "acct-2"
_OPPORTUNITY = "opp-1"


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


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


def _seed_opportunity(store: Store, **overrides: object) -> None:
    item: dict[str, object] = {
        "opportunity_id": _OPPORTUNITY,
        "tenant": _TENANT,
        "account_id": _ACCOUNT,
        "name": "Acme Renewal",
        "stage": "Discovery",
        "owner_rep_id": _REP,
        "value": 1000,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    item.update(overrides)
    store.put_item("opportunities", item)


def _submit(store: Store) -> str:
    result = submit_proposal.handler(
        _event(
            {
                "target_table": "opportunities",
                "target_id": _OPPORTUNITY,
                "operation": "update",
                "after_payload": {"stage": "Qualification"},
                "reason": "advance",
                "transaction_key": "tx-submit",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 200, result
    return str(result["body"]["proposal"]["proposal_id"])


def _proposal(store: Store, proposal_id: str) -> dict[str, object]:
    item = store.get_item("proposals", {"proposal_id": proposal_id})
    assert item is not None
    return item


def _audit_records(store: Store) -> list[dict[str, object]]:
    items, _ = store.query_tenant_scan("audit_records", tenant=_TENANT, limit=100)
    return [
        record
        for item in items
        if (record := store.get_item("audit_records", {"audit_id": str(item["audit_id"])}))
    ]


def _rebase(store: Store, proposal_id: str, **event: Any) -> dict[str, Any]:
    result: dict[str, Any] = rebase_proposal.handler(
        _event({"proposal_id": proposal_id, "transaction_key": "tx-rebase"}, **event), store=store
    )
    return result


# -- the rebase itself ------------------------------------------------------------------------


def test_a_rebase_recaptures_the_before_snapshot_and_audits_old_and_new(store: Store) -> None:
    _seed_opportunity(store)
    proposal_id = _submit(store)
    old_snapshot = _proposal(store, proposal_id)["before_snapshot"]
    _seed_opportunity(store, name="Acme Renewal (renamed)")

    result = _rebase(store, proposal_id)

    assert result["statusCode"] == 200, result
    stored = _proposal(store, proposal_id)
    current = store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})
    assert stored["before_snapshot"] == current
    assert stored["status"] == "pending"
    assert stored["after_payload"] == {"stage": "Qualification"}
    assert stored["updated_at"] != "2026-01-01T00:00:00+00:00"
    assert result["body"]["proposal"]["before_snapshot"] == current

    (audit,) = _audit_records(store)
    assert audit["action"] == "proposal_rebased"
    assert audit["before_snapshot"] == old_snapshot
    assert audit["after_snapshot"] == current
    assert audit["target_id"] == _OPPORTUNITY
    assert audit["account_id"] == _ACCOUNT


def test_the_target_record_is_not_written_by_a_rebase(store: Store) -> None:
    _seed_opportunity(store)
    proposal_id = _submit(store)
    _seed_opportunity(store, name="Renamed")
    before = store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})

    assert _rebase(store, proposal_id)["statusCode"] == 200
    assert store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY}) == before


def test_a_rebased_proposal_can_then_be_approved(store: Store) -> None:
    """A conflict on a field the proposal touches is cleared by the rebase."""
    _seed_opportunity(store)
    proposal_id = _submit(store)
    _seed_opportunity(store, stage="Negotiation", name="Renamed")

    refused = approve_proposal.handler(
        _event({"proposal_id": proposal_id, "transaction_key": "tx-approve-1"}), store=store
    )
    assert refused["statusCode"] == 409

    assert _rebase(store, proposal_id)["statusCode"] == 200
    approved = approve_proposal.handler(
        _event({"proposal_id": proposal_id, "transaction_key": "tx-approve-2"}), store=store
    )
    assert approved["statusCode"] == 200, approved
    target = store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})
    assert target is not None
    assert target["stage"] == "Qualification"
    assert target["name"] == "Renamed"


# -- authorization: as approve ----------------------------------------------------------------


def test_a_caller_who_could_not_approve_is_denied_and_nothing_changes(store: Store) -> None:
    _seed_opportunity(store)
    proposal_id = _submit(store)
    before = _proposal(store, proposal_id)

    for denied in (
        _rebase(store, proposal_id, book=[_OTHER_ACCOUNT]),
        approve_proposal.handler(
            _event({"proposal_id": proposal_id, "transaction_key": "tx-a"}, book=[_OTHER_ACCOUNT]),
            store=store,
        ),
    ):
        assert denied["statusCode"] == 403
        assert denied["error"]["code"] == "scope_denied"

    assert _proposal(store, proposal_id) == before
    assert _audit_records(store) == []


def test_a_proposal_in_another_tenant_is_not_found(store: Store) -> None:
    _seed_opportunity(store)
    proposal_id = _submit(store)
    store.put_item("proposals", {**_proposal(store, proposal_id), "tenant": "eval-run-1"})

    result = _rebase(store, proposal_id)
    assert result["statusCode"] == 404


@pytest.mark.parametrize("handler", [rebase_proposal, approve_proposal, reject_proposal])
def test_a_missing_proposal_response_never_echoes_the_caller_id(store: Store, handler: Any) -> None:
    """The 404 body names no caller-supplied proposal id."""
    planted = "planted-caller-value-7f3a"
    result = handler.handler(
        _event({"proposal_id": planted, "transaction_key": "tx-missing"}), store=store
    )
    assert result["statusCode"] == 404
    assert planted not in json.dumps(result)
    assert result["error"]["message"] == "The proposal does not exist."


def test_a_missing_proposal_id_is_a_validation_failure(store: Store) -> None:
    result = rebase_proposal.handler(_event({"transaction_key": "tx"}), store=store)
    assert result["statusCode"] == 400


# -- refusals ---------------------------------------------------------------------------------


def test_a_decided_proposal_is_refused_with_reason_already_decided(store: Store) -> None:
    _seed_opportunity(store)
    proposal_id = _submit(store)
    store.put_item("proposals", {**_proposal(store, proposal_id), "status": "approved"})

    result = _rebase(store, proposal_id)

    assert result["statusCode"] == 409
    assert result["error"]["reason"] == "already_decided"
    assert _audit_records(store) == []


def test_a_deleted_target_is_refused_with_reason_target_missing(store: Store) -> None:
    """Scope falls back to the stored snapshot, then the rebase refuses: nothing to capture."""
    _seed_opportunity(store)
    proposal_id = _submit(store)
    store.delete_item("opportunities", {"opportunity_id": _OPPORTUNITY})

    result = _rebase(store, proposal_id)

    assert result["statusCode"] == 409
    assert result["error"]["reason"] == "target_missing"
    assert _audit_records(store) == []


class _ProposalTouchedMidRebase(Store):
    """A store whose next transaction is preceded by a concurrent write to the proposal."""

    def transact_write(self, operations: Any, *, transaction_key: str | None = None) -> None:
        operations = list(operations)
        for operation in operations:
            update = operation.get("Update", {})
            if update.get("TableName") == "proposals":
                proposal_id = update["Key"]["proposal_id"]["S"]
                current = self.get_item("proposals", {"proposal_id": proposal_id})
                assert current is not None
                self.put_item("proposals", {**current, "updated_at": "2099-01-01T00:00:00+00:00"})
        super().transact_write(operations, transaction_key=transaction_key)


def test_a_concurrent_proposal_change_cancels_the_whole_transaction() -> None:
    """The `updated_at` condition fails, so neither the snapshot nor the audit record is written."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        plain = Store(client=client)
        _seed_opportunity(plain)
        proposal_id = _submit(plain)
        snapshot = _proposal(plain, proposal_id)["before_snapshot"]
        _seed_opportunity(plain, name="Renamed")

        result = _rebase(_ProposalTouchedMidRebase(client=client), proposal_id)

        assert result["statusCode"] == 409
        assert _proposal(plain, proposal_id)["before_snapshot"] == snapshot
        assert _audit_records(plain) == []


# -- the rebase audit record cannot be reverted -----------------------------------------------


def test_reverting_the_rebase_audit_record_is_refused(store: Store) -> None:
    _seed_opportunity(store)
    proposal_id = _submit(store)
    _seed_opportunity(store, name="Renamed")
    assert _rebase(store, proposal_id)["statusCode"] == 200
    (audit,) = _audit_records(store)

    result = revert_audit_record.handler(
        _event({"audit_id": audit["audit_id"], "transaction_key": "tx-revert"}), store=store
    )

    assert result["statusCode"] == 409
    target = store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})
    assert target is not None and target["name"] == "Renamed"


# -- Conflict's structured fields -------------------------------------------------------------


def test_conflict_renders_reason_and_conflicting_fields_when_set() -> None:
    error = Conflict(
        "changed",
        reason="fields_changed",
        conflicting_fields=[
            {"field": "stage", "snapshot": "Discovery", "current": "Lost", "proposed": "Won"}
        ],
    )
    payload = error.to_api_response()
    assert payload["statusCode"] == 409
    assert payload["error"] == {
        "code": "conflict",
        "message": "changed",
        "next_step": "refresh_then_ask_user",
        "reason": "fields_changed",
        "conflicting_fields": [
            {"field": "stage", "snapshot": "Discovery", "current": "Lost", "proposed": "Won"}
        ],
    }


def test_conflict_omits_both_fields_when_unset() -> None:
    assert Conflict("changed").to_api_response()["error"] == {
        "code": "conflict",
        "message": "changed",
        "next_step": "refresh_then_ask_user",
    }


def test_conflict_never_renders_its_detail() -> None:
    payload = Conflict("changed", reason="target_missing", detail="internal").to_api_response()
    assert "internal" not in str(payload)


# -- authorization reads the current target -------------------------------------------


def test_a_rebase_is_denied_when_the_target_moved_out_of_book_since_submit(store: Store) -> None:
    """Snapshot in book, current target in another account: 403, proposal unchanged, no audit.

    Authorizing on `before_snapshot` instead of the current target would let this through.
    """
    _seed_opportunity(store)
    proposal_id = _submit(store)
    _seed_opportunity(store, account_id=_OTHER_ACCOUNT)
    before = _proposal(store, proposal_id)

    result = _rebase(store, proposal_id)

    assert result["statusCode"] == 403
    assert result["error"]["code"] == "scope_denied"
    assert _proposal(store, proposal_id) == before
    assert _audit_records(store) == []


# -- idempotency of the rebase audit record -------------------------------------------


def test_the_rebase_audit_id_is_derived_from_the_transaction_key(store: Store) -> None:
    from crm.handlers._common import idempotent_id

    _seed_opportunity(store)
    proposal_id = _submit(store)
    _seed_opportunity(store, name="Renamed")

    assert _rebase(store, proposal_id)["statusCode"] == 200

    (audit,) = _audit_records(store)
    assert audit["audit_id"] == idempotent_id("audit", "tx-rebase")


def test_an_idempotent_parameter_mismatch_is_a_conflict() -> None:
    """DynamoDB's in-window retry-with-different-parameters error maps to 409, not 500."""
    from botocore.exceptions import ClientError

    from crm.errors import wrap_aws_error

    error = ClientError(
        {"Error": {"Code": "IdempotentParameterMismatchException", "Message": "x"}},
        "TransactWriteItems",
    )
    wrapped = wrap_aws_error(error, operation="transact_write")
    assert isinstance(wrapped, Conflict)
    assert wrapped.to_api_response()["statusCode"] == 409

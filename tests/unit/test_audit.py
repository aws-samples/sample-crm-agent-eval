# SPDX-License-Identifier: MIT
"""Tests for crm.audit: audit-record construction and idempotent revert.

Runs against a `moto`-mocked DynamoDB client. A post-audit target modification and a second revert
attempt are each refused with `Conflict`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.audit import build_audit_record, get_audit_record, revert
from crm.authz import Identity
from crm.errors import Conflict, CrmError, NotFound, ScopeDenied, ValidationFailed
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


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
    """A Store using a non-empty ``table_prefix``, with every declared table created under it."""
    create_all_tables(dynamodb_client, table_prefix="envA-")
    return Store(table_prefix="envA-", client=dynamodb_client)


@pytest.fixture
def identity() -> Identity:
    return Identity(rep_id="rep-1", book=frozenset({"acc-1"}), tenant="demo")


def _seed_opportunity(store: Store, *, stage: str = "Negotiation") -> dict[str, object]:
    item = {
        "opportunity_id": "opp-1",
        "tenant": "demo",
        "account_id": "acc-1",
        "name": "Acme Renewal",
        "stage": stage,
        "value": 50000,
    }
    store.put_item("opportunities", item)
    return item


# -- build_audit_record ---------------------------------------------------------------------


def test_build_audit_record_carries_before_after_actor_and_timestamp(identity: Identity) -> None:
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={"stage": "Discovery"},
        after_snapshot={"stage": "Negotiation"},
        audit_id="audit-1",
        created_at="2026-01-01T00:00:00+00:00",
    )
    assert record.audit_id == "audit-1"
    assert record.tenant == "demo"
    assert record.target_table == "opportunities"
    assert record.target_id == "opp-1"
    assert record.before_snapshot == {"stage": "Discovery"}
    assert record.after_snapshot == {"stage": "Negotiation"}
    assert record.actor_rep_id == "rep-1"
    assert record.created_at == "2026-01-01T00:00:00+00:00"
    assert record.status == "active"
    assert record.reversed_at is None
    assert record.reversed_by is None


def test_build_audit_record_defaults_audit_id_and_timestamp(identity: Identity) -> None:
    first = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={},
        after_snapshot={"stage": "Discovery"},
    )
    second = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={},
        after_snapshot={"stage": "Discovery"},
    )
    assert first.audit_id != second.audit_id
    assert first.created_at
    assert second.created_at


def test_to_item_projects_tenant_prefixed_gsi_keys(identity: Identity) -> None:
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={"stage": "Discovery"},
        after_snapshot={"stage": "Negotiation"},
        audit_id="audit-2",
    )
    item = record.to_item()
    assert item["tenant_target_id"] == "demo#opp-1"
    assert item["tenant_actor_rep_id"] == "demo#rep-1"
    assert item["status"] == "active"


def test_audit_record_is_persisted_and_retrievable(store: Store, identity: Identity) -> None:
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={"account_id": "acc-1", "stage": "Discovery"},
        after_snapshot={"account_id": "acc-1", "stage": "Negotiation"},
        audit_id="audit-3",
    )
    store.put_item("audit_records", record.to_item())
    fetched = get_audit_record(identity, store, "audit-3")
    assert fetched["audit_id"] == "audit-3"
    assert fetched["before_snapshot"] == {"account_id": "acc-1", "stage": "Discovery"}
    assert fetched["after_snapshot"] == {"account_id": "acc-1", "stage": "Negotiation"}


def test_get_audit_record_raises_not_found_for_unknown_id(store: Store, identity: Identity) -> None:
    with pytest.raises(NotFound):
        get_audit_record(identity, store, "does-not-exist")


def test_get_audit_record_raises_scope_denied_across_tenants(
    store: Store, identity: Identity
) -> None:
    other_tenant_identity = Identity(rep_id="rep-2", book=frozenset({"acc-9"}), tenant="eval-run-1")
    record = build_audit_record(
        identity=other_tenant_identity,
        target_table="opportunities",
        target_id="opp-9",
        before_snapshot={},
        after_snapshot={"stage": "Discovery"},
        audit_id="audit-cross-tenant",
    )
    store.put_item("audit_records", record.to_item())
    with pytest.raises(ScopeDenied):
        get_audit_record(identity, store, "audit-cross-tenant")


# -- revert: happy path ------------------------------------------------------------------------


def test_revert_restores_the_before_snapshot(store: Store, identity: Identity) -> None:
    _seed_opportunity(store, stage="Negotiation")
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Discovery",
            "value": 50000,
        },
        after_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Negotiation",
            "value": 50000,
        },
        audit_id="audit-happy",
    )
    store.put_item("audit_records", record.to_item())

    result = revert(identity, store, audit_id="audit-happy", transaction_key="revert-txn-1")

    assert result["status"] == "reversed"
    assert result["reversed_by"] == "rep-1"
    assert result["reversed_at"]

    restored = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert restored is not None
    assert restored["stage"] == "Discovery"

    persisted = store.get_item("audit_records", {"audit_id": "audit-happy"})
    assert persisted is not None
    assert persisted["status"] == "reversed"
    assert persisted["reversed_by"] == "rep-1"


def test_revert_of_a_create_deletes_the_target(store: Store, identity: Identity) -> None:
    """A create's before-snapshot is empty; reverting it restores "did not exist" by deleting."""
    store.put_item(
        "opportunities",
        {
            "opportunity_id": "opp-new",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "New Deal",
            "stage": "Discovery",
        },
    )
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-new",
        before_snapshot={},
        after_snapshot={
            "opportunity_id": "opp-new",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "New Deal",
            "stage": "Discovery",
        },
        audit_id="audit-create",
    )
    store.put_item("audit_records", record.to_item())

    revert(identity, store, audit_id="audit-create", transaction_key="revert-txn-create")

    assert store.get_item("opportunities", {"opportunity_id": "opp-new"}) is None
    persisted = store.get_item("audit_records", {"audit_id": "audit-create"})
    assert persisted is not None
    assert persisted["status"] == "reversed"


def test_revert_enforces_scope_before_mutating(store: Store) -> None:
    out_of_scope_identity = Identity(rep_id="rep-2", book=frozenset({"acc-other"}), tenant="demo")
    _seed_opportunity(store, stage="Negotiation")
    record = build_audit_record(
        identity=Identity(rep_id="rep-1", book=frozenset({"acc-1"}), tenant="demo"),
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "stage": "Discovery",
        },
        after_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "stage": "Negotiation",
        },
        audit_id="audit-scoped",
    )
    store.put_item("audit_records", record.to_item())

    with pytest.raises(ScopeDenied):
        revert(out_of_scope_identity, store, audit_id="audit-scoped", transaction_key="txn-scope")

    unchanged = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert unchanged is not None
    assert unchanged["stage"] == "Negotiation"
    persisted = store.get_item("audit_records", {"audit_id": "audit-scoped"})
    assert persisted is not None
    assert persisted["status"] == "active"


# -- revert: target modified after the audit entry was written -----------------------------------


def test_revert_refuses_conflict_when_target_modified_after_audit_entry(
    store: Store, identity: Identity
) -> None:
    _seed_opportunity(store, stage="Negotiation")
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Discovery",
            "value": 50000,
        },
        after_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Negotiation",
            "value": 50000,
        },
        audit_id="audit-stale",
    )
    store.put_item("audit_records", record.to_item())

    # The record changes again after the audit entry was written, so its current state no longer
    # matches `after_snapshot`.
    store.put_item(
        "opportunities",
        {
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Closed Won",
            "value": 50000,
        },
    )

    with pytest.raises(Conflict):
        revert(identity, store, audit_id="audit-stale", transaction_key="revert-txn-stale")

    # No partial effect: the target keeps its latest state and the audit record stays active.
    current = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert current is not None
    assert current["stage"] == "Closed Won"
    persisted = store.get_item("audit_records", {"audit_id": "audit-stale"})
    assert persisted is not None
    assert persisted["status"] == "active"


# -- revert: revert attempted twice --------------------------------------------------------------


def test_revert_refuses_conflict_on_a_second_attempt(store: Store, identity: Identity) -> None:
    _seed_opportunity(store, stage="Negotiation")
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Discovery",
            "value": 50000,
        },
        after_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Negotiation",
            "value": 50000,
        },
        audit_id="audit-double",
    )
    store.put_item("audit_records", record.to_item())

    first = "revert-txn-double-1"  # gitleaks:allow
    second = "revert-txn-double-2"  # gitleaks:allow
    revert(identity, store, audit_id="audit-double", transaction_key=first)

    with pytest.raises(Conflict):
        revert(identity, store, audit_id="audit-double", transaction_key=second)

    restored = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert restored is not None
    assert restored["stage"] == "Discovery"
    persisted = store.get_item("audit_records", {"audit_id": "audit-double"})
    assert persisted is not None
    assert persisted["status"] == "reversed"


def test_revert_second_attempt_does_not_double_apply_when_target_changed_between_attempts(
    store: Store, identity: Identity
) -> None:
    """A second revert is refused on the audit record's status and leaves a later change intact."""
    _seed_opportunity(store, stage="Negotiation")
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Discovery",
            "value": 50000,
        },
        after_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Negotiation",
            "value": 50000,
        },
        audit_id="audit-post-change",
    )
    store.put_item("audit_records", record.to_item())

    revert(identity, store, audit_id="audit-post-change", transaction_key="txn-first")

    # Something else changes the now-restored record.
    store.put_item(
        "opportunities",
        {
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Qualification",
            "value": 50000,
        },
    )

    with pytest.raises(Conflict):
        revert(identity, store, audit_id="audit-post-change", transaction_key="txn-second")

    # The later independent change survives untouched.
    current = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert current is not None
    assert current["stage"] == "Qualification"


def test_revert_second_call_with_the_same_transaction_key_after_success_is_a_conflict(
    store: Store, identity: Identity
) -> None:
    """A repeat call with the same transaction_key after success is refused as a second revert."""
    _seed_opportunity(store, stage="Negotiation")
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Discovery",
            "value": 50000,
        },
        after_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Negotiation",
            "value": 50000,
        },
        audit_id="audit-retry",
    )
    store.put_item("audit_records", record.to_item())

    first = revert(identity, store, audit_id="audit-retry", transaction_key="same-txn-key")
    assert first["status"] == "reversed"

    with pytest.raises(Conflict):
        revert(identity, store, audit_id="audit-retry", transaction_key="same-txn-key")

    restored = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert restored is not None
    assert restored["stage"] == "Discovery"


def test_revert_raises_not_found_for_unknown_audit_id(store: Store, identity: Identity) -> None:
    with pytest.raises(NotFound):
        revert(identity, store, audit_id="never-existed", transaction_key="txn-missing")


# -- log redaction: no snapshot payload in a log line --------------------------------------------


def test_revert_module_never_logs_a_snapshot(
    store: Store, identity: Identity, caplog: pytest.LogCaptureFixture
) -> None:
    """A revert emits no log record from the `crm.audit` logger."""
    _seed_opportunity(store, stage="Negotiation")
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Discovery",
            "value": 50000,
        },
        after_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Negotiation",
            "value": 50000,
        },
        audit_id="audit-log-check",
    )
    store.put_item("audit_records", record.to_item())

    with caplog.at_level("DEBUG", logger="crm.audit"):
        revert(identity, store, audit_id="audit-log-check", transaction_key="txn-log-check")

    crm_audit_records = [r for r in caplog.records if r.name == "crm.audit"]
    assert crm_audit_records == []


# -- revert resolves table names through the store's prefix -------------------------------------


def test_revert_restores_the_before_snapshot_against_a_prefixed_store(
    prefixed_store: Store, identity: Identity
) -> None:
    """Revert restores the before-snapshot against a ``Store`` with a non-empty ``table_prefix``."""
    _seed_opportunity(prefixed_store, stage="Negotiation")
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Discovery",
            "value": 50000,
        },
        after_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Negotiation",
            "value": 50000,
        },
        audit_id="audit-prefixed",
    )
    prefixed_store.put_item("audit_records", record.to_item())

    result = revert(
        identity, prefixed_store, audit_id="audit-prefixed", transaction_key="revert-prefixed-1"
    )
    assert result["status"] == "reversed"

    restored = prefixed_store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert restored is not None
    assert restored["stage"] == "Discovery"

    persisted = prefixed_store.get_item("audit_records", {"audit_id": "audit-prefixed"})
    assert persisted is not None
    assert persisted["status"] == "reversed"


# -- revert: the advertised `reason` ------------------------------------------------------------


def _seeded_audit_record(store: Store, identity: Identity, audit_id: str) -> None:
    """Seed one revertible audit record over a real target row."""
    _seed_opportunity(store, stage="Negotiation")
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Discovery",
            "value": 50000,
        },
        after_snapshot={
            "opportunity_id": "opp-1",
            "tenant": "demo",
            "account_id": "acc-1",
            "name": "Acme Renewal",
            "stage": "Negotiation",
            "value": 50000,
        },
        audit_id=audit_id,
    )
    store.put_item("audit_records", record.to_item())


def test_revert_persists_the_supplied_reason_on_the_audit_record(
    store: Store, identity: Identity
) -> None:
    """A supplied `reason` is stripped and persisted on the reversed audit record."""
    _seeded_audit_record(store, identity, "audit-reason")

    result = revert(
        identity,
        store,
        audit_id="audit-reason",
        transaction_key="revert-reason-1",
        reason="  Stage advanced in error; customer has not signed.  ",
    )

    assert result["reversed_reason"] == "Stage advanced in error; customer has not signed."
    persisted = store.get_item("audit_records", {"audit_id": "audit-reason"})
    assert persisted is not None
    assert persisted["reversed_reason"] == "Stage advanced in error; customer has not signed."
    assert persisted["status"] == "reversed"
    assert store.get_item("opportunities", {"opportunity_id": "opp-1"}) is not None


@pytest.mark.parametrize("supplied", [None, "", "   ", 12345])
def test_revert_records_no_reason_when_none_was_meaningfully_supplied(
    store: Store, identity: Identity, supplied: object
) -> None:
    """A missing, blank, or non-string `reason` is recorded as no reason."""
    _seeded_audit_record(store, identity, "audit-noreason")

    result = revert(
        identity,
        store,
        audit_id="audit-noreason",
        transaction_key="revert-noreason-1",
        reason=supplied,  # type: ignore[arg-type]
    )

    assert result["reversed_reason"] is None
    persisted = store.get_item("audit_records", {"audit_id": "audit-noreason"})
    assert persisted is not None
    assert persisted.get("reversed_reason") is None
    assert persisted["status"] == "reversed"


def test_revert_refuses_an_unbounded_reason_and_commits_nothing(
    store: Store, identity: Identity
) -> None:
    """An over-long `reason` raises `ValidationFailed` and nothing is written."""
    _seeded_audit_record(store, identity, "audit-longreason")

    with pytest.raises(ValidationFailed) as excinfo:
        revert(
            identity,
            store,
            audit_id="audit-longreason",
            transaction_key="revert-longreason-1",
            reason="x" * 2000,
        )

    assert excinfo.value.source == "reason"
    persisted = store.get_item("audit_records", {"audit_id": "audit-longreason"})
    assert persisted is not None
    assert persisted["status"] == "active"
    row = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert row is not None
    assert row["stage"] == "Negotiation"


def test_the_revert_tool_schema_advertises_the_reason_it_now_records() -> None:
    """The `revert_audit_record` tool schema declares an optional `reason`."""
    from crm.toolspec.definitions import BY_NAME

    schema = BY_NAME["revert_audit_record"].input_schema
    assert "reason" in schema["properties"]
    assert "reason" not in schema["required"]


# -- stored action vocabulary and revert ordering ------------------------------------------


def test_the_explicit_action_set_is_the_audit_action_literal() -> None:
    from typing import get_args

    from crm import audit

    assert frozenset(get_args(audit.AuditAction)) == audit._EXPLICIT_ACTIONS


@pytest.mark.parametrize("stored", ["proposal_rebased", "create", "update", "delete"])
def test_audit_action_returns_a_recognised_stored_action(stored: str) -> None:
    from crm.audit import audit_action

    assert audit_action({"action": stored, "before_snapshot": {"a": 1}, "after_snapshot": {}}) == (
        stored
    )


@pytest.mark.parametrize("stored", ["proposal_rebase", "REBASED", "", 7, ["update"]])
def test_audit_action_raises_on_a_present_unrecognised_action(stored: object) -> None:
    """A corrupted `proposal_rebased` must not read as `update`: it is an internal error, not 409."""
    from crm.audit import audit_action

    with pytest.raises(CrmError, match="malformed") as excinfo:
        audit_action({"action": stored, "before_snapshot": {"a": 1}, "after_snapshot": {"a": 2}})
    assert type(excinfo.value) is CrmError
    assert excinfo.value.code == "internal_error"


def test_audit_action_derives_when_the_stored_action_is_null_or_absent() -> None:
    from crm.audit import audit_action

    assert audit_action({"action": None, "before_snapshot": {}, "after_snapshot": {"a": 1}}) == (
        "create"
    )
    assert audit_action({"before_snapshot": {"a": 1}, "after_snapshot": {}}) == "delete"


def test_revert_of_a_corrupted_action_is_refused_and_writes_nothing(
    store: Store, identity: Identity
) -> None:
    current = _seed_opportunity(store)
    stored = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={**current, "stage": "Discovery"},
        after_snapshot=current,
        audit_id="audit-corrupt",
    )
    store.put_item("audit_records", {**record.to_item(), "action": "proposal_rebase"})

    with pytest.raises(CrmError, match="malformed") as excinfo:
        revert(identity, store, audit_id="audit-corrupt", transaction_key="tx-corrupt")
    assert not isinstance(excinfo.value, Conflict)

    assert store.get_item("opportunities", {"opportunity_id": "opp-1"}) == stored


def test_revert_of_an_out_of_book_rebase_record_is_scope_denied_not_conflict(
    store: Store, identity: Identity
) -> None:
    """The scope check runs before the `proposal_rebased` refusal, so the type is not revealed."""
    current = _seed_opportunity(store)
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot=current,
        after_snapshot=current,
        audit_id="audit-rebased",
        action="proposal_rebased",
    )
    store.put_item("audit_records", record.to_item())
    outsider = Identity(rep_id="rep-9", book=frozenset({"acc-other"}), tenant="demo")

    with pytest.raises(ScopeDenied):
        revert(outsider, store, audit_id="audit-rebased", transaction_key="tx-out")
    with pytest.raises(Conflict, match="rebase"):
        revert(identity, store, audit_id="audit-rebased", transaction_key="tx-in")


def test_list_audit_records_refuses_an_empty_target_id(store: Store) -> None:
    """An empty target_id is a validation error naming the field, not a crash."""
    from crm.handlers.list_audit_records import _list_audit_records

    identity = Identity(rep_id="rep-1", full_visibility=True, tenant="demo")
    with pytest.raises(ValidationFailed) as raised:
        _list_audit_records(store, identity, {"target_id": ""})
    assert raised.value.source == "target_id"


@pytest.mark.parametrize(
    ("table", "key_attr"),
    [
        ("accounts", "account_id"),
        ("contacts", "contact_id"),
        ("opportunities", "opportunity_id"),
        ("activities", "activity_id"),
    ],
)
def test_a_delete_revert_puts_the_before_snapshot_conditioned_on_the_key_being_absent(
    table: str, key_attr: str
) -> None:
    """A delete's record (empty ``after_snapshot``) restores only while the target is still gone."""
    from crm.audit import _revert_transaction_items

    target, audit_update = _revert_transaction_items(
        target_table=table,
        target_id="id-1",
        before_snapshot={"tenant": "demo", "account_id": "acc-1", key_attr: "id-1"},
        after_snapshot={},
        audit_id="aud-1",
        reversed_at="2026-10-13T00:00:00+00:00",
        reversed_by="rep-1",
    )

    put = target["Put"]
    assert put["ConditionExpression"] == "attribute_not_exists(#pk)"
    assert put["ExpressionAttributeNames"] == {"#pk": key_attr}
    assert "ExpressionAttributeValues" not in put
    assert put["Item"][key_attr] == {"S": "id-1"}
    assert audit_update["Update"]["ConditionExpression"] == "#status = :active"


def test_an_update_revert_keeps_its_matches_after_snapshot_condition() -> None:
    from crm.audit import _revert_transaction_items

    target, _audit = _revert_transaction_items(
        target_table="accounts",
        target_id="acc-1",
        before_snapshot={"account_id": "acc-1", "name": "Old"},
        after_snapshot={"account_id": "acc-1", "name": "New"},
        audit_id="aud-2",
        reversed_at="2026-10-13T00:00:00+00:00",
        reversed_by="rep-1",
    )
    assert target["Put"]["ConditionExpression"] == "#account_id = :account_id AND #name = :name"


# -- legacy records and delete-revert parents -----------------------------------------------


def test_revert_of_a_record_with_neither_snapshot_is_a_conflict_not_a_key_error(
    store: Store, identity: Identity
) -> None:
    """A legacy snapshot-less record is refused as a typed conflict, writing nothing."""
    store.put_item(
        "audit_records",
        {
            "audit_id": "audit-legacy",
            "tenant": "demo",
            "target_table": "opportunities",
            "target_id": "opp-1",
            "account_id": "acc-1",
            "actor_rep_id": "rep-1",
            "created_at": "2026-01-01T00:00:00+00:00",
            "status": "active",
        },
    )

    with pytest.raises(Conflict, match="no before or after snapshot, so it cannot be reverted"):
        revert(identity, store, audit_id="audit-legacy", transaction_key="tx-legacy")

    assert store.get_item("opportunities", {"opportunity_id": "opp-1"}) is None
    stored = store.get_item("audit_records", {"audit_id": "audit-legacy"})
    assert stored is not None and stored["status"] == "active"


def _seed_account(store: Store, *, tenant: str = "demo") -> None:
    store.put_item("accounts", {"account_id": "acc-1", "tenant": tenant, "name": "Acme"})


def _activity() -> dict[str, object]:
    return {
        "activity_id": "act-1",
        "tenant": "demo",
        "account_id": "acc-1",
        "opportunity_id": "opp-1",
        "kind": "call",
        "subject": "Kickoff",
    }


def _opportunity() -> dict[str, object]:
    return {
        "opportunity_id": "opp-1",
        "tenant": "demo",
        "account_id": "acc-1",
        "name": "Acme Renewal",
        "stage": "Negotiation",
        "value": 50000,
    }


def _contact() -> dict[str, object]:
    return {"contact_id": "con-1", "tenant": "demo", "account_id": "acc-1", "name": "Jane"}


def _put_delete_record(
    store: Store, identity: Identity, table: str, target_id: str, before: dict[str, object]
) -> str:
    audit_id = f"audit-del-{table}"
    record = build_audit_record(
        identity=identity,
        target_table=table,
        target_id=target_id,
        before_snapshot=before,
        after_snapshot={},
        audit_id=audit_id,
    )
    store.put_item("audit_records", record.to_item())
    return audit_id


_KEY_ATTR = {
    "contacts": "contact_id",
    "opportunities": "opportunity_id",
    "activities": "activity_id",
}

#: (table, deleted record, the parent seeding that makes its revert succeed).
_PARENTED_DELETES = [
    ("contacts", _contact, lambda store: _seed_account(store)),
    ("opportunities", _opportunity, lambda store: _seed_account(store)),
    ("activities", _activity, lambda store: store.put_item("opportunities", _opportunity())),
]


@pytest.mark.parametrize(("table", "make", "_seed_parent"), _PARENTED_DELETES)
def test_a_delete_revert_whose_parent_is_absent_is_a_conflict_writing_nothing(
    store: Store, identity: Identity, table: str, make: Any, _seed_parent: Any
) -> None:
    """With the parent gone, the restore is refused and the audit record stays active."""
    before = make()
    key_attr = _KEY_ATTR[table]
    audit_id = _put_delete_record(store, identity, table, str(before[key_attr]), before)

    with pytest.raises(Conflict, match="no longer exists"):
        revert(identity, store, audit_id=audit_id, transaction_key=f"tx-{table}")

    assert store.get_item(table, {key_attr: str(before[key_attr])}) is None
    stored = store.get_item("audit_records", {"audit_id": audit_id})
    assert stored is not None and stored["status"] == "active"


@pytest.mark.parametrize(("table", "make", "seed_parent"), _PARENTED_DELETES)
def test_a_delete_revert_whose_parent_exists_restores_the_record(
    store: Store, identity: Identity, table: str, make: Any, seed_parent: Any
) -> None:
    before = make()
    key_attr = _KEY_ATTR[table]
    seed_parent(store)
    audit_id = _put_delete_record(store, identity, table, str(before[key_attr]), before)

    revert(identity, store, audit_id=audit_id, transaction_key=f"tx-ok-{table}")

    restored = store.get_item(table, {key_attr: str(before[key_attr])})
    assert restored is not None and {k: restored[k] for k in before} == before


def test_a_delete_revert_whose_parent_is_in_another_tenant_is_a_conflict(
    store: Store, identity: Identity
) -> None:
    _seed_account(store, tenant="other")
    audit_id = _put_delete_record(store, identity, "opportunities", "opp-1", _opportunity())

    with pytest.raises(Conflict, match="no longer exists"):
        revert(identity, store, audit_id=audit_id, transaction_key="tx-tenant")

    assert store.get_item("opportunities", {"opportunity_id": "opp-1"}) is None


def test_a_delete_revert_whose_snapshot_names_no_parent_is_a_conflict(
    store: Store, identity: Identity
) -> None:
    before = {key: value for key, value in _activity().items() if key != "opportunity_id"}
    audit_id = _put_delete_record(store, identity, "activities", "act-1", before)

    with pytest.raises(Conflict, match="no longer exists"):
        revert(identity, store, audit_id=audit_id, transaction_key="tx-unlinked")

    assert store.get_item("activities", {"activity_id": "act-1"}) is None


def test_an_account_delete_revert_needs_no_parent(store: Store, identity: Identity) -> None:
    before: dict[str, object] = {"account_id": "acc-1", "tenant": "demo", "name": "Acme"}
    audit_id = _put_delete_record(store, identity, "accounts", "acc-1", before)

    revert(identity, store, audit_id=audit_id, transaction_key="tx-account")

    restored = store.get_item("accounts", {"account_id": "acc-1"})
    assert restored is not None and {k: restored[k] for k in before} == before


def test_the_delete_revert_parent_read_is_consistent(
    dynamodb_client: object, identity: Identity
) -> None:
    """The parent lookup issues ``ConsistentRead`` so a just-deleted parent is never seen."""
    create_all_tables(dynamodb_client)
    reads: list[tuple[str, bool]] = []

    class RecordingStore(Store):
        def get_item(
            self, table: str, key: Any, *, consistent: bool = False
        ) -> dict[str, object] | None:
            reads.append((table, consistent))
            return super().get_item(table, key, consistent=consistent)

    store = RecordingStore(client=dynamodb_client)
    _seed_account(store)
    audit_id = _put_delete_record(store, identity, "opportunities", "opp-1", _opportunity())

    revert(identity, store, audit_id=audit_id, transaction_key="tx-consistent")

    assert ("accounts", True) in reads
    assert ("accounts", False) not in reads


# -- stage moves a revert may not make ------------------------------------------------------


def _put_stage_update_record(
    store: Store, identity: Identity, *, before_stage: str, after_stage: str, audit_id: str
) -> None:
    snapshot = {
        "opportunity_id": "opp-1",
        "tenant": "demo",
        "account_id": "acc-1",
        "name": "Acme Renewal",
        "value": 50000,
    }
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={**snapshot, "stage": before_stage},
        after_snapshot={**snapshot, "stage": after_stage},
        audit_id=audit_id,
    )
    store.put_item("audit_records", record.to_item())


@pytest.mark.parametrize(
    ("before_stage", "after_stage"),
    [("Negotiation", "Closed Won"), ("Discovery", "Closed Lost"), ("Closed Lost", "Closed Won")],
    ids=["reopen-won", "reopen-lost", "closed-lateral"],
)
def test_revert_that_would_make_a_refused_stage_move_is_a_conflict_writing_nothing(
    store: Store, identity: Identity, before_stage: str, after_stage: str
) -> None:
    _seed_opportunity(store, stage=after_stage)
    _put_stage_update_record(
        store, identity, before_stage=before_stage, after_stage=after_stage, audit_id="audit-close"
    )

    with pytest.raises(Conflict) as raised:
        revert(identity, store, audit_id="audit-close", transaction_key="tx-refused-stage")

    assert "closed" in raised.value.message
    opportunity = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert opportunity is not None and opportunity["stage"] == after_stage
    audit = store.get_item("audit_records", {"audit_id": "audit-close"})
    assert audit is not None and audit["status"] == "active"


def test_revert_of_a_regression_still_restores_the_earlier_stage(
    store: Store, identity: Identity
) -> None:
    _seed_opportunity(store, stage="Discovery")
    _put_stage_update_record(
        store,
        identity,
        before_stage="Qualification",
        after_stage="Discovery",
        audit_id="audit-regress",
    )

    result = revert(identity, store, audit_id="audit-regress", transaction_key="tx-regress")

    assert result["status"] == "reversed"
    opportunity = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert opportunity is not None and opportunity["stage"] == "Qualification"


# -- a restored primary contact must still exist under the opportunity's account -------------


def _put_contact_update_record(
    store: Store, identity: Identity, *, before_contact: str | None, after_contact: str | None
) -> str:
    base = {**_opportunity(), "stage": "Discovery"}
    record = build_audit_record(
        identity=identity,
        target_table="opportunities",
        target_id="opp-1",
        before_snapshot={**base, "primary_contact_id": before_contact},
        after_snapshot={**base, "primary_contact_id": after_contact},
        audit_id="audit-contact",
    )
    store.put_item("audit_records", record.to_item())
    return "audit-contact"


def _seed_opportunity_naming(store: Store, contact_id: str | None) -> None:
    store.put_item(
        "opportunities", {**_opportunity(), "stage": "Discovery", "primary_contact_id": contact_id}
    )


@pytest.mark.parametrize(
    "restored_contact",
    [
        {},
        {"contact_id": "con-old", "tenant": "demo", "account_id": "acc-other", "name": "Elsewhere"},
        {"contact_id": "con-old", "tenant": "other", "account_id": "acc-1", "name": "Foreign"},
    ],
    ids=["deleted", "other-account", "other-tenant"],
)
def test_update_revert_restoring_a_dangling_primary_contact_is_a_conflict_writing_nothing(
    store: Store, identity: Identity, restored_contact: dict[str, object]
) -> None:
    if restored_contact:
        store.put_item("contacts", restored_contact)
    store.put_item("contacts", {**_contact(), "contact_id": "con-new"})
    _seed_opportunity_naming(store, "con-new")
    audit_id = _put_contact_update_record(
        store, identity, before_contact="con-old", after_contact="con-new"
    )

    with pytest.raises(Conflict) as raised:
        revert(identity, store, audit_id=audit_id, transaction_key="tx-dangling")

    assert "primary contact" in raised.value.message
    opportunity = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert opportunity is not None and opportunity["primary_contact_id"] == "con-new"
    audit = store.get_item("audit_records", {"audit_id": audit_id})
    assert audit is not None and audit["status"] == "active"


def test_update_revert_restoring_a_live_primary_contact_succeeds(
    store: Store, identity: Identity
) -> None:
    store.put_item("contacts", {**_contact(), "contact_id": "con-old"})
    store.put_item("contacts", {**_contact(), "contact_id": "con-new"})
    _seed_opportunity_naming(store, "con-new")
    audit_id = _put_contact_update_record(
        store, identity, before_contact="con-old", after_contact="con-new"
    )

    revert(identity, store, audit_id=audit_id, transaction_key="tx-live-contact")

    opportunity = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert opportunity is not None and opportunity["primary_contact_id"] == "con-old"


def test_update_revert_restoring_a_cleared_primary_contact_needs_no_contact(
    store: Store, identity: Identity
) -> None:
    store.put_item("contacts", {**_contact(), "contact_id": "con-new"})
    _seed_opportunity_naming(store, "con-new")
    audit_id = _put_contact_update_record(
        store, identity, before_contact=None, after_contact="con-new"
    )

    revert(identity, store, audit_id=audit_id, transaction_key="tx-cleared-contact")

    opportunity = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert opportunity is not None and opportunity["primary_contact_id"] is None


def test_update_revert_leaving_the_primary_contact_unchanged_does_not_check_it(
    store: Store, identity: Identity
) -> None:
    _seed_opportunity_naming(store, "con-gone")
    audit_id = _put_contact_update_record(
        store, identity, before_contact="con-gone", after_contact="con-gone"
    )

    revert(identity, store, audit_id=audit_id, transaction_key="tx-unchanged-contact")

    audit = store.get_item("audit_records", {"audit_id": audit_id})
    assert audit is not None and audit["status"] == "reversed"


def test_delete_revert_restoring_a_dangling_primary_contact_is_a_conflict(
    store: Store, identity: Identity
) -> None:
    _seed_account(store)
    audit_id = _put_delete_record(
        store,
        identity,
        "opportunities",
        "opp-1",
        {**_opportunity(), "primary_contact_id": "con-gone"},
    )

    with pytest.raises(Conflict):
        revert(identity, store, audit_id=audit_id, transaction_key="tx-delete-dangling")

    assert store.get_item("opportunities", {"opportunity_id": "opp-1"}) is None

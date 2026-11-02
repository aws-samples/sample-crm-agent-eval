# SPDX-License-Identifier: MIT
"""No write path can persist a stale derived index key attribute.

Every GSI key attribute computed from other attributes of the same item is declared in
:data:`crm.store.keys.DERIVED_ATTRIBUTES` and computed by :func:`crm.store.keys.derive`, and the
write boundary recomputes them:

* :meth:`crm.store.store.Store.put_item`, :meth:`~crm.store.store.Store.put_idempotent` and a
  ``Put`` inside :meth:`~crm.store.store.Store.transact_write` all re-derive, so an item carrying a
  wrong composite is persisted with the correct one;
* an ``Update`` that assigns a derived attribute's source without assigning the derived attribute
  is refused;
* every index key attribute declared in :mod:`crm.store.schema` is either declared derived or
  declared verbatim.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.store import keys
from crm.store.keys import DERIVED_ATTRIBUTES, VERBATIM_INDEX_ATTRIBUTES, derive
from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLE_NAMES, TABLES
from crm.store.store import Store

_TENANT = "demo"


@pytest.fixture(name="store")
def _store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _opportunity(**overrides: Any) -> dict[str, Any]:
    """An opportunity item whose every derived attribute is wrong."""
    item: dict[str, Any] = {
        "opportunity_id": "opp-1",
        "tenant": _TENANT,
        "account_id": "acct-1",
        "owner_rep_id": "rep-1",
        "name": "Acme Renewal",
        "stage": "Negotiation",
        "close_date": "2026-12-31",
        # Every one of these names the wrong value.
        "tenant_account_id": f"{_TENANT}#WRONG",
        "tenant_owner_rep_id": f"{_TENANT}#WRONG",
        "tenant_stage": f"{_TENANT}#Discovery",
        "stage_close_date": "Discovery#1999-01-01",
        "name_lower": "wrong",
    }
    item.update(overrides)
    return item


def _correct_composites() -> dict[str, str]:
    return {
        "tenant_account_id": f"{_TENANT}#acct-1",
        "tenant_owner_rep_id": f"{_TENANT}#rep-1",
        "tenant_stage": f"{_TENANT}#Negotiation",
        "stage_close_date": "Negotiation#2026-12-31",
        "name_lower": "acme renewal",
    }


# -- the declaration is complete ----------------------------------------------------------------


def test_every_index_key_attribute_is_declared_derived_or_verbatim() -> None:
    """Every index key attribute is declared either derived or verbatim."""
    undeclared: dict[str, set[str]] = {}
    for table_name, schema in TABLES.items():
        declared = {spec.name for spec in DERIVED_ATTRIBUTES.get(table_name, ())}
        key_attributes = {
            attribute
            for gsi in schema.gsis.values()
            for attribute in (gsi.partition_key, gsi.sort_key)
            if attribute is not None
        }
        missing = key_attributes - declared - VERBATIM_INDEX_ATTRIBUTES
        if missing:
            undeclared[table_name] = missing
    assert not undeclared, (
        "every GSI key attribute must be declared in crm.store.keys.DERIVED_ATTRIBUTES (computed) "
        f"or VERBATIM_INDEX_ATTRIBUTES (stored as supplied); undeclared: {undeclared!r}"
    )


def test_derived_attributes_only_names_known_tables() -> None:
    assert set(DERIVED_ATTRIBUTES) <= set(TABLE_NAMES)


def test_no_attribute_is_declared_both_derived_and_verbatim() -> None:
    """No attribute is declared both derived and verbatim."""
    for table_name, specs in DERIVED_ATTRIBUTES.items():
        overlap = {spec.name for spec in specs} & VERBATIM_INDEX_ATTRIBUTES
        assert not overlap, f"{table_name}: {overlap!r} declared both derived and verbatim"


def test_derive_is_idempotent_and_does_not_mutate_its_argument() -> None:
    item = _opportunity()
    once = derive("opportunities", item)
    assert derive("opportunities", once) == once
    assert item["tenant_stage"] == f"{_TENANT}#Discovery", (
        "derive must not mutate the caller's dict"
    )


def test_derive_drops_a_composite_whose_source_is_absent() -> None:
    """DynamoDB rejects an empty-string key value, so a partially-sourced composite is removed.

    A closed-lost opportunity whose ``close_date`` is cleared must leave ``gsi_owner_stage``
    entirely rather than keep a sort key naming the old date.
    """
    derived = derive("opportunities", _opportunity(close_date=None))
    assert "stage_close_date" not in derived
    assert derived["tenant_stage"] == f"{_TENANT}#Negotiation"


# -- the write boundary recomputes --------------------------------------------------------------


def test_put_item_recomputes_every_derived_attribute(store: Store) -> None:
    store.put_item("opportunities", _opportunity())
    row = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert row is not None
    for name, expected in _correct_composites().items():
        assert row[name] == expected, f"{name} was persisted stale"


def test_put_idempotent_recomputes_every_derived_attribute(store: Store) -> None:
    store.put_idempotent("opportunities", _opportunity(), transaction_key="txn-1")
    row = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert row is not None
    for name, expected in _correct_composites().items():
        assert row[name] == expected, f"{name} was persisted stale"


def test_transact_write_put_recomputes_every_derived_attribute(store: Store) -> None:
    from crm.handlers.wire import put_operation

    store.transact_write([put_operation("opportunities", _opportunity())])
    row = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert row is not None
    for name, expected in _correct_composites().items():
        assert row[name] == expected, f"{name} was persisted stale"


def test_transact_write_put_leaves_a_numeric_attribute_byte_identical(store: Store) -> None:
    """Re-deriving a ``Put`` leaves ``{"N": "1000.0"}`` byte-identical."""
    operation = put_operation_with_number()
    resolved = store._resolve_operation_table_name(operation)  # noqa: SLF001 - the unit under test
    assert resolved["Put"]["Item"]["value"] == {"N": "1000.0"}


def put_operation_with_number() -> dict[str, Any]:
    from crm.handlers.wire import encode_item

    item = encode_item(_opportunity())
    item["value"] = {"N": "1000.0"}
    return {"Put": {"TableName": "opportunities", "Item": item}}


def test_query_by_the_index_finds_the_row_under_its_recomputed_key(store: Store) -> None:
    """The row is reachable through the index at its current stage only."""
    store.put_item("opportunities", _opportunity())
    at_new_stage, _ = store.query_gsi(
        "opportunities", "gsi_stage", partition_value=keys.tenant_prefixed(_TENANT, "Negotiation")
    )
    at_old_stage, _ = store.query_gsi(
        "opportunities", "gsi_stage", partition_value=keys.tenant_prefixed(_TENANT, "Discovery")
    )
    assert [item["opportunity_id"] for item in at_new_stage] == ["opp-1"]
    assert at_old_stage == []


# -- an Update that would leave one stale is refused ---------------------------------------------


def test_update_assigning_a_source_without_its_derived_attribute_is_refused(store: Store) -> None:
    store.put_item(
        "proposals",
        {
            "proposal_id": "prop-1",
            "tenant": _TENANT,
            "status": "pending",
            "actor_rep_id": "rep-1",
            "target_id": "opp-1",
            # gsi_status sorts on created_at; an item missing it is absent from the index.
            "created_at": "2026-02-01T00:00:00Z",
        },
    )
    with pytest.raises(ValueError, match="tenant_status"):
        store.transact_write(
            [
                {
                    "Update": {
                        "TableName": "proposals",
                        "Key": {"proposal_id": {"S": "prop-1"}},
                        "UpdateExpression": "SET #s = :approved",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {":approved": {"S": "approved"}},
                    }
                }
            ]
        )


def test_update_assigning_both_is_accepted(store: Store) -> None:
    store.put_item(
        "proposals",
        {
            "proposal_id": "prop-1",
            "tenant": _TENANT,
            "status": "pending",
            "actor_rep_id": "rep-1",
            "target_id": "opp-1",
            # gsi_status sorts on created_at; an item missing it is absent from the index.
            "created_at": "2026-02-01T00:00:00Z",
        },
    )
    store.transact_write(
        [
            {
                "Update": {
                    "TableName": "proposals",
                    "Key": {"proposal_id": {"S": "prop-1"}},
                    "UpdateExpression": "SET #s = :approved, tenant_status = :ts",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {
                        ":approved": {"S": "approved"},
                        ":ts": {"S": f"{_TENANT}#approved"},
                    },
                }
            }
        ]
    )
    reachable, _ = store.query_gsi(
        "proposals", "gsi_status", partition_value=keys.tenant_prefixed(_TENANT, "approved")
    )
    assert [item["proposal_id"] for item in reachable] == ["prop-1"]


def test_update_removing_a_source_without_its_derived_attribute_is_refused(store: Store) -> None:
    """A ``REMOVE`` is as capable of stranding a composite as a ``SET``."""
    with pytest.raises(ValueError, match="tenant_status"):
        store.transact_write(
            [
                {
                    "Update": {
                        "TableName": "proposals",
                        "Key": {"proposal_id": {"S": "prop-1"}},
                        "UpdateExpression": "REMOVE #s",
                        "ExpressionAttributeNames": {"#s": "status"},
                    }
                }
            ]
        )


def test_update_touching_no_derived_source_is_left_alone(store: Store) -> None:
    """An update touching no derived source is not refused."""
    store.put_item(
        "audit_records",
        {
            "audit_id": "aud-1",
            "tenant": _TENANT,
            "target_id": "opp-1",
            "actor_rep_id": "rep-1",
            "status": "active",
        },
    )
    store.transact_write(
        [
            {
                "Update": {
                    "TableName": "audit_records",
                    "Key": {"audit_id": {"S": "aud-1"}},
                    "UpdateExpression": "SET #s = :reversed, reversed_at = :at",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {
                        ":reversed": {"S": "reversed"},
                        ":at": {"S": "2026-01-01T00:00:00Z"},
                    },
                }
            }
        ]
    )
    row = store.get_item("audit_records", {"audit_id": "aud-1"})
    assert row is not None and row["status"] == "reversed"


# -- the approval path, end to end --------------------------------------------------------------


def test_approving_a_stage_change_recomputes_the_committed_row(store: Store) -> None:
    """The approved row is indexed at its new stage."""
    from crm.proposals import approve_proposal, submit_proposal

    store.put_item("opportunities", derive("opportunities", _opportunity(stage="Discovery")))
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="update",
        actor_rep_id="rep-1",
        reason="Deal is advancing.",
        transaction_key="txn-submit",
        after_payload={"stage": "Negotiation"},
        now="2026-02-01T00:00:00Z",
    )
    approve_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        actor_rep_id="rep-1",
        transaction_key="txn-approve",
        now="2026-02-02T00:00:00Z",
    )

    row = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert row is not None and row["stage"] == "Negotiation"
    assert row["tenant_stage"] == f"{_TENANT}#Negotiation"
    assert row["stage_close_date"] == "Negotiation#2026-12-31"

    at_new_stage, _ = store.query_gsi(
        "opportunities", "gsi_stage", partition_value=keys.tenant_prefixed(_TENANT, "Negotiation")
    )
    at_old_stage, _ = store.query_gsi(
        "opportunities", "gsi_stage", partition_value=keys.tenant_prefixed(_TENANT, "Discovery")
    )
    assert [item["opportunity_id"] for item in at_new_stage] == ["opp-1"]
    assert at_old_stage == []

    approved, _ = store.query_gsi(
        "proposals", "gsi_status", partition_value=keys.tenant_prefixed(_TENANT, "approved")
    )
    still_pending, _ = store.query_gsi(
        "proposals", "gsi_status", partition_value=keys.tenant_prefixed(_TENANT, "pending")
    )
    assert [item["proposal_id"] for item in approved] == ["prop-1"]
    assert still_pending == []


def test_the_audit_after_snapshot_byte_matches_the_committed_row(store: Store) -> None:
    """The audit ``after_snapshot`` equals the committed row, and a revert restores the prior row."""
    from crm.audit import build_audit_record, revert
    from crm.authz import Identity
    from crm.handlers.wire import put_operation
    from crm.proposals import ApprovalContext, approve_proposal, submit_proposal

    identity = Identity(rep_id="rep-1", full_visibility=True, tenant=_TENANT)
    before = derive("opportunities", _opportunity(stage="Discovery"))
    store.put_item("opportunities", before)
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        target_table="opportunities",
        target_id="opp-1",
        operation="update",
        actor_rep_id="rep-1",
        reason="Deal is advancing.",
        transaction_key="txn-submit",
        after_payload={"stage": "Negotiation"},
        now="2026-02-01T00:00:00Z",
    )

    def record_audit(context: ApprovalContext) -> dict[str, Any]:
        assert context.after_snapshot is not None
        record = build_audit_record(
            identity=identity,
            target_table="opportunities",
            target_id="opp-1",
            before_snapshot=dict(context.before_snapshot),
            after_snapshot=dict(context.after_snapshot),
            audit_id="aud-1",
            created_at="2026-02-02T00:00:00Z",
        )
        return put_operation("audit_records", record.to_item())

    approve_proposal(
        store,
        tenant=_TENANT,
        proposal_id="prop-1",
        actor_rep_id="rep-1",
        transaction_key="txn-approve",
        now="2026-02-02T00:00:00Z",
        record_audit=record_audit,
    )

    audit_row = store.get_item("audit_records", {"audit_id": "aud-1"})
    committed = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert audit_row is not None and committed is not None
    assert audit_row["after_snapshot"] == committed, (
        "the recorded after-snapshot must be byte-for-byte what the approval committed, or the "
        "revert path's condition check can never match"
    )

    # The revert restores the pre-approval row exactly.
    revert(identity, store, audit_id="aud-1", transaction_key="txn-revert")
    restored = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert restored == before

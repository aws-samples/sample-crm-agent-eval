# SPDX-License-Identifier: MIT
"""Field-level approval against DynamoDB Local: an unrelated later write and the TOCTOU race.

Every call goes through the real handlers and the prefixed `dynamodb_local_store` fixture, so the
condition expressions, derived-attribute rewrites and transaction cancellation are evaluated by the
engine, not by `moto`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import approve_proposal, log_activity, submit_proposal
from crm.store.store import Store

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"


def _key() -> str:
    """A `ClientRequestToken`-sized key unique to this call, so no run reuses a token."""
    return uuid.uuid4().hex[:32]


def _event(args: dict[str, Any]) -> dict[str, Any]:
    return identity_event(
        IDENTITY_KEY,
        {
            "rep_id": _REP,
            "book": [_ACCOUNT],
            "full_visibility": False,
            "principal_type": "user",
            "session_id": "sess-1",
            "source": "gateway",
            "tenant": _TENANT,
        },
        args,
    )


@pytest.fixture
def store(dynamodb_local_store: tuple[Store, str]) -> Store:
    engine_store, _prefix = dynamodb_local_store
    return engine_store


def _seed(store: Store, opportunity_id: str) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": opportunity_id,
            "tenant": _TENANT,
            "account_id": _ACCOUNT,
            "name": "Acme Renewal",
            "stage": "Discovery",
            "close_date": "2026-06-30",
            "owner_rep_id": _REP,
            "value": 1000,
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
    )


def _submit(store: Store, opportunity_id: str, payload: dict[str, object]) -> str:
    result = submit_proposal.handler(
        _event(
            {
                "target_table": "opportunities",
                "target_id": opportunity_id,
                "operation": "update",
                "after_payload": payload,
                "reason": "advance",
                "transaction_key": _key(),
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 200, result
    return str(result["body"]["proposal"]["proposal_id"])


def _approve(store: Store, proposal_id: str) -> Any:
    return approve_proposal.handler(
        _event({"proposal_id": proposal_id, "transaction_key": _key()}), store=store
    )


def _get(store: Store, opportunity_id: str) -> dict[str, object]:
    item = store.get_item("opportunities", {"opportunity_id": opportunity_id})
    assert item is not None
    return item


def test_submit_then_log_activity_then_approve_succeeds(store: Store) -> None:
    """An activity logged after submit no longer makes the approval a conflict."""
    _seed(store, "opp-fr46")
    proposal_id = _submit(store, "opp-fr46", {"stage": "Qualification"})
    logged = log_activity.handler(
        _event(
            {
                "opportunity_id": "opp-fr46",
                "kind": "call",
                "subject": "Check-in",
                "occurred_at": "2026-03-01T09:00:00+00:00",
                "transaction_key": _key(),
            }
        ),
        store=store,
    )
    assert logged["statusCode"] == 200, logged

    approved = _approve(store, proposal_id)

    assert approved["statusCode"] == 200, approved
    target = _get(store, "opp-fr46")
    assert target["stage"] == "Qualification"
    assert target["last_touch"] == "2026-03-01T09:00:00+00:00"
    assert target["tenant_stage"] == "demo#Qualification"
    assert target["stage_close_date"] == "Qualification#2026-06-30"
    found, _ = store.query_gsi("opportunities", "gsi_stage", partition_value="demo#Qualification")
    assert [item["opportunity_id"] for item in found] == ["opp-fr46"]


def test_a_changed_touched_field_is_refused_by_the_engine(store: Store) -> None:
    _seed(store, "opp-moved")
    proposal_id = _submit(store, "opp-moved", {"stage": "Qualification"})
    store.put_item("opportunities", {**_get(store, "opp-moved"), "stage": "Negotiation"})

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


class _WriteBeforeTransact(Store):
    """A store that rewrites one opportunity right before its next transaction."""

    def __init__(self, inner: Store, opportunity_id: str, fields: dict[str, object]) -> None:
        prefix = inner.physical_name("opportunities").removesuffix("opportunities")
        super().__init__(table_prefix=prefix, client=inner._client)
        self._opportunity_id = opportunity_id
        self._fields = fields

    def transact_write(self, operations: Any, *, transaction_key: str | None = None) -> None:
        current = self.get_item("opportunities", {"opportunity_id": self._opportunity_id})
        assert current is not None
        self.put_item("opportunities", {**current, **self._fields})
        super().transact_write(operations, transaction_key=transaction_key)


def test_a_touched_field_written_between_read_and_transaction_cancels_it(store: Store) -> None:
    """TOCTOU: the engine's per-field condition, not the pre-check, refuses the race."""
    _seed(store, "opp-race")
    proposal_id = _submit(store, "opp-race", {"stage": "Qualification"})

    result = _approve(_WriteBeforeTransact(store, "opp-race", {"stage": "Lost"}), proposal_id)

    assert result["statusCode"] == 409
    assert result["error"]["reason"] == "fields_changed"
    assert _get(store, "opp-race")["stage"] == "Lost"
    proposal = store.get_item("proposals", {"proposal_id": proposal_id})
    assert proposal is not None and proposal["status"] == "pending"


def test_an_unrelated_write_between_read_and_transaction_commits_and_survives(
    store: Store,
) -> None:
    _seed(store, "opp-unrelated")
    proposal_id = _submit(store, "opp-unrelated", {"stage": "Qualification"})

    result = _approve(
        _WriteBeforeTransact(store, "opp-unrelated", {"value": 4242, "name": "Renamed"}),
        proposal_id,
    )

    assert result["statusCode"] == 200, result
    target = _get(store, "opp-unrelated")
    assert (target["stage"], target["value"], target["name"]) == ("Qualification", 4242, "Renamed")
    assert target["name_lower"] == "renamed"


def test_a_racing_change_to_an_untouched_derived_source_cancels_it(store: Store) -> None:
    """`close_date` feeds the rewritten `stage_close_date`, so its change must cancel."""
    _seed(store, "opp-derived")
    proposal_id = _submit(store, "opp-derived", {"stage": "Qualification"})

    result = _approve(
        _WriteBeforeTransact(store, "opp-derived", {"close_date": "2026-09-30"}), proposal_id
    )

    assert result["statusCode"] == 409
    target = _get(store, "opp-derived")
    assert target["stage_close_date"] == "Discovery#2026-09-30"

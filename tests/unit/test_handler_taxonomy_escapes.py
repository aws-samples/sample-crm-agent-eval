# SPDX-License-Identifier: MIT
"""Tests that the two `ValueError` paths a handler could raise stay inside the error taxonomy.

* **record-scope path** — a fetched record with no account scope key is denied with `ScopeDenied`
  via `crm.authz.require_record_scope`.
* **requested-stage path** — `submit_proposal` maps an unrecognised caller-supplied stage to
  `ValidationFailed` and an unrecognised stored stage to `Conflict`.

Each test drives a real handler entry point through the real envelope, against a `moto`-mocked
DynamoDB client.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.domain.stages import classify_requested_transition, classify_transition
from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import Conflict, ValidationFailed
from crm.handlers import (
    create_note,
    draft_message,
    get_contact,
    get_opportunity,
    list_activities,
    log_activity,
    submit_proposal,
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
_OPPORTUNITY = "opp-1"
_CONTACT = "cont-1"
_ACTIVITY = "act-1"


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _identity() -> dict[str, Any]:
    return {
        "rep_id": _REP,
        "book": [_ACCOUNT],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": _TENANT,
    }


def _event(arguments: dict[str, Any]) -> dict[str, Any]:
    return identity_event(IDENTITY_KEY, _identity(), arguments)


def _seed_account(store: Store) -> None:
    store.put_item(
        "accounts",
        {
            "account_id": _ACCOUNT,
            "tenant": _TENANT,
            "tenant_owner_rep_id": f"{_TENANT}#{_REP}",
            "owner_rep_id": _REP,
            "name": "Acme",
            "name_lower": "acme",
        },
    )


def _opportunity(*, account_id: str | None, stage: str = "Discovery") -> dict[str, Any]:
    item: dict[str, Any] = {
        "opportunity_id": _OPPORTUNITY,
        "tenant": _TENANT,
        "tenant_owner_rep_id": f"{_TENANT}#{_REP}",
        "tenant_stage": f"{_TENANT}#{stage}",
        "owner_rep_id": _REP,
        "name": "Big Deal",
        "name_lower": "big deal",
        "stage": stage,
        "value": 1000,
        "created_at": "2024-01-01T00:00:00+00:00",
    }
    if account_id is not None:
        item["account_id"] = account_id
        item["tenant_account_id"] = f"{_TENANT}#{account_id}"
    return item


def _contact(*, account_id: str | None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "contact_id": _CONTACT,
        "tenant": _TENANT,
        "name": "Dana Reyes",
        "name_lower": "dana reyes",
        "status": "active",
        "created_at": "2024-01-01T00:00:00+00:00",
    }
    if account_id is not None:
        item["account_id"] = account_id
        item["tenant_account_id"] = f"{_TENANT}#{account_id}"
    return item


def _activity(*, account_id: str | None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "activity_id": _ACTIVITY,
        "tenant": _TENANT,
        "opportunity_id": _OPPORTUNITY,
        "kind": "call",
        "subject": "Intro call",
        "occurred_at": "2024-01-02T00:00:00+00:00",
        "created_at": "2024-01-02T00:00:00+00:00",
        "tenant_opportunity_id": f"{_TENANT}#{_OPPORTUNITY}",
    }
    if account_id is not None:
        item["account_id"] = account_id
    return item


# Each entry: handler module, the table and record to seed with no account scope key, and the
# arguments that reach that record. Covers all ten handlers on the record-scope path (draft_message twice,
# once per scope source).
_SCOPELESS_RECORD_CASES: list[tuple[str, Any, str, dict[str, Any], dict[str, Any]]] = [
    (
        "get_opportunity",
        get_opportunity,
        "opportunities",
        _opportunity(account_id=None),
        {"opportunity_id": _OPPORTUNITY},
    ),
    (
        "list_activities",
        list_activities,
        "opportunities",
        _opportunity(account_id=None),
        {"opportunity_id": _OPPORTUNITY},
    ),
    (
        "create_note",
        create_note,
        "opportunities",
        _opportunity(account_id=None),
        {"opportunity_id": _OPPORTUNITY, "body": "note body", "transaction_key": "tk-note"},
    ),
    (
        "log_activity",
        log_activity,
        "opportunities",
        _opportunity(account_id=None),
        {
            "opportunity_id": _OPPORTUNITY,
            "kind": "call",
            "subject": "Follow up",
            "transaction_key": "tk-act",
        },
    ),
    (
        "update_opportunity",
        update_opportunity,
        "opportunities",
        _opportunity(account_id=None),
        {
            "opportunity_id": _OPPORTUNITY,
            "fields": {"name": "Renamed"},
            "transaction_key": "tk-opp",
        },
    ),
    (
        "submit_proposal",
        submit_proposal,
        "opportunities",
        _opportunity(account_id=None),
        {
            "target_table": "opportunities",
            "target_id": _OPPORTUNITY,
            "operation": "update",
            "reason": "needs approval",
            "transaction_key": "tk-prop",
            "after_payload": {"stage": "Qualification"},
        },
    ),
    (
        "get_contact",
        get_contact,
        "contacts",
        _contact(account_id=None),
        {"contact_id": _CONTACT},
    ),
    (
        "update_contact",
        update_contact,
        "contacts",
        _contact(account_id=None),
        {"contact_id": _CONTACT, "fields": {"name": "Renamed"}, "transaction_key": "tk-cont"},
    ),
    (
        "draft_message",
        draft_message,
        "contacts",
        _contact(account_id=None),
        {"contact_id": _CONTACT, "intent": "check in"},
    ),
    (
        "update_activity",
        update_activity,
        "activities",
        _activity(account_id=None),
        {"activity_id": _ACTIVITY, "fields": {"subject": "Renamed"}, "transaction_key": "tk-actu"},
    ),
]


@pytest.mark.parametrize(
    ("name", "module", "table", "record", "arguments"),
    _SCOPELESS_RECORD_CASES,
    ids=[case[0] for case in _SCOPELESS_RECORD_CASES],
)
def test_record_with_no_account_scope_key_is_denied_not_an_unhandled_valueerror(
    store: Store,
    name: str,
    module: Any,
    table: str,
    record: dict[str, Any],
    arguments: dict[str, Any],
) -> None:
    """Every handler that fetches a record and extracts its account fails closed."""
    _seed_account(store)
    store.put_item(table, record)

    result = module.handler(_event(arguments), None, store=store)

    assert result["statusCode"] == 403, (name, result)
    error = result["error"]
    assert error["code"] == "scope_denied", (name, error)
    assert error["missing_scope"] == "account", (name, error)
    # The bare `ValueError` enumerated the record's attribute key names; the denial must not.
    for key in record:
        assert key not in error["message"], (name, key, error)


def test_draft_message_secondary_scope_source_is_also_denied(store: Store) -> None:
    """`draft_message` has two scope sources: the optional opportunity is the second."""
    _seed_account(store)
    store.put_item("contacts", _contact(account_id=_ACCOUNT))
    store.put_item("opportunities", _opportunity(account_id=None))

    result = draft_message.handler(
        _event({"contact_id": _CONTACT, "intent": "check in", "opportunity_id": _OPPORTUNITY}),
        None,
        store=store,
    )

    assert result["statusCode"] == 403, result
    assert result["error"]["code"] == "scope_denied", result
    assert result["error"]["missing_scope"] == "account", result


def test_scoped_record_still_reaches_the_handler_body(store: Store) -> None:
    """An in-scope record with an account key still succeeds."""
    _seed_account(store)
    store.put_item("opportunities", _opportunity(account_id=_ACCOUNT))

    result = get_opportunity.handler(_event({"opportunity_id": _OPPORTUNITY}), None, store=store)

    assert result["statusCode"] == 200, result
    assert result["body"]["opportunity"]["opportunity_id"] == _OPPORTUNITY, result


# ---------------------------------------------------------------------------------------------
# The requested-stage path: a free-form `after_payload["stage"]` value.
# ---------------------------------------------------------------------------------------------


def test_submit_proposal_unrecognised_stage_is_validation_failed_not_a_valueerror(
    store: Store,
) -> None:
    """`after_payload` is `additionalProperties: true`, so the value arrives unconstrained."""
    _seed_account(store)
    store.put_item("opportunities", _opportunity(account_id=_ACCOUNT))

    result = submit_proposal.handler(
        _event(
            {
                "target_table": "opportunities",
                "target_id": _OPPORTUNITY,
                "operation": "update",
                "reason": "escalate",
                "transaction_key": "tk-1",
                "after_payload": {"stage": "Wizardry"},
            }
        ),
        None,
        store=store,
    )

    assert result["statusCode"] == 400, result
    assert result["error"]["code"] == "validation_failed", result
    assert result["error"]["source"] == "after_payload.stage", result


def test_submit_proposal_unrecognised_stored_stage_is_conflict_not_a_valueerror(
    store: Store,
) -> None:
    """The other side: a stored stage outside the enum is a state conflict, not bad input."""
    _seed_account(store)
    store.put_item("opportunities", _opportunity(account_id=_ACCOUNT, stage="Wizardry"))

    result = submit_proposal.handler(
        _event(
            {
                "target_table": "opportunities",
                "target_id": _OPPORTUNITY,
                "operation": "update",
                "reason": "escalate",
                "transaction_key": "tk-2",
                "after_payload": {"stage": "Qualification"},
            }
        ),
        None,
        store=store,
    )

    assert result["statusCode"] == 409, result
    assert result["error"]["code"] == "conflict", result


def test_submit_proposal_recognised_stage_still_creates_a_proposal(store: Store) -> None:
    """A legal stage transition is still proposed."""
    _seed_account(store)
    store.put_item("opportunities", _opportunity(account_id=_ACCOUNT))

    result = submit_proposal.handler(
        _event(
            {
                "target_table": "opportunities",
                "target_id": _OPPORTUNITY,
                "operation": "update",
                "reason": "escalate",
                "transaction_key": "tk-3",
                "after_payload": {"stage": "Qualification"},
            }
        ),
        None,
        store=store,
    )

    assert result["statusCode"] == 200, result
    assert result["body"]["proposal"]["status"] == "pending", result


def test_classify_requested_transition_maps_both_directions_into_the_taxonomy() -> None:
    """`classify_requested_transition` maps a bad requested stage to 400 and a bad stored stage
    to 409."""
    with pytest.raises(ValidationFailed) as caller_side:
        classify_requested_transition("Discovery", "Wizardry", source="after_payload.stage")
    assert caller_side.value.source == "after_payload.stage"
    assert caller_side.value.http_status == 400

    with pytest.raises(Conflict) as stored_side:
        classify_requested_transition("Wizardry", "Discovery", source="after_payload.stage")
    assert stored_side.value.http_status == 409

    # `classify_transition` still raises `ValueError` for internal callers.
    with pytest.raises(ValueError):
        classify_transition("Discovery", "Wizardry")
    assert (
        classify_requested_transition(
            "Discovery", "Qualification", source="after_payload.stage"
        ).value
        == "advance"
    )

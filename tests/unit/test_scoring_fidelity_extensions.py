# SPDX-License-Identifier: MIT
"""Tests for eval.scoring's fidelity extensions: exact proposal values, any-of read targets,
new-row discipline for ``id: null`` entries, and the ``write_attempted`` forbidden label.

Runs against a moto-mocked DynamoDB client; no credentials, no network.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from crm.proposals import submit_proposal
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.scoring import (
    _STRUCTURALLY_FALSIFIABLE_FORBIDDEN_OUTCOMES,
    AssertionResult,
    ObservedProposal,
    TaskScore,
    ToolCall,
    TurnOutcome,
    capture_pre_turn_values,
    score_task,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "eval-test-tenant"
_REP = "rep-ashford"
_NOW = "2026-01-01T00:00:00Z"


@pytest.fixture
def store() -> Iterator[Store]:
    """A Store backed by a moto-mocked client with every declared table created."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _assertions(**overrides: object) -> dict[str, object]:
    assertions: dict[str, object] = {
        "expected_field_changes": None,
        "must_not_change": [],
        "max_proposal_count": 0,
        "allowed_proposal_fields": [],
        "expected_proposal": None,
        "expected_denial": None,
        "forbidden_outcomes": [],
    }
    assertions.update(overrides)
    return assertions


def _task(**assertion_overrides: object) -> dict[str, object]:
    return {
        "id": "gt-test",
        "scoring_mode": "machine",
        "unscored_reason": None,
        "assertions": _assertions(**assertion_overrides),
    }


def _result(score: TaskScore, prefix: str) -> AssertionResult:
    (match,) = [r for r in score.assertion_results if r.name.startswith(prefix)]
    return match


def _seed_opportunity(store: Store, opportunity_id: str = "opp-1") -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": opportunity_id,
            "tenant": _TENANT,
            "tenant_account_id": f"{_TENANT}#acct-1",
            "tenant_owner_stage": f"{_TENANT}#{_REP}",
            "tenant_stage": f"{_TENANT}#Discovery",
            "name_lower": "acme renewal",
            "account_id": "acct-1",
            "name": "Acme Renewal",
            "stage": "Discovery",
            "owner_rep_id": _REP,
            "value": 500000,
            "close_date": "2026-01-13",
            "created_at": _NOW,
            "updated_at": _NOW,
            "schema_version": 1,
        },
    )


def _seed_note(store: Store, *, note_id: str, opportunity_id: str, body: str) -> None:
    store.put_item(
        "notes",
        {
            "note_id": note_id,
            "tenant": _TENANT,
            "tenant_opportunity_id": f"{_TENANT}#{opportunity_id}",
            "opportunity_id": opportunity_id,
            "body": body,
            "created_at": _NOW,
            "created_by": _REP,
            "schema_version": 1,
        },
    )


def _seed_activity(
    store: Store, *, activity_id: str, opportunity_id: str, kind: str, subject: str
) -> None:
    store.put_item(
        "activities",
        {
            "activity_id": activity_id,
            "tenant": _TENANT,
            "tenant_opportunity_id": f"{_TENANT}#{opportunity_id}",
            "opportunity_id": opportunity_id,
            "kind": kind,
            "subject": subject,
            "created_at": _NOW,
            "created_by": _REP,
            "schema_version": 1,
        },
    )


def _propose(store: Store, *, proposal_id: str, after_payload: dict[str, object]) -> None:
    submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id=proposal_id,
        target_table="opportunities",
        target_id="opp-1",
        operation="update",
        actor_rep_id=_REP,
        reason="test",
        transaction_key=f"tx-{proposal_id}",
        after_payload=after_payload,
        now=_NOW,
    )


def _proposal_task(after_payload: dict[str, object] | None) -> dict[str, object]:
    expected: dict[str, object] = {
        "target_table": "opportunities",
        "target_id": "opp-1",
        "operation": "update",
    }
    if after_payload is not None:
        expected["after_payload"] = after_payload
    return _task(
        max_proposal_count=1,
        allowed_proposal_fields=["value", "close_date"],
        expected_proposal=expected,
    )


def _proposal_outcome(proposal_id: str) -> TurnOutcome:
    return TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall(tool_name="submit_proposal", target_id="opp-1"),),
        proposals=(ObservedProposal(proposal_id=proposal_id),),
    )


# --- exact proposal values --------------------------------------------------------------------


def test_proposal_with_right_target_and_wrong_value_fails(store: Store) -> None:
    """A proposal on the right record with the wrong value is not the expected proposal."""
    _seed_opportunity(store)
    _propose(store, proposal_id="prop-1", after_payload={"value": 150000})
    score = score_task(
        store, task=_proposal_task({"value": 180000}), outcome=_proposal_outcome("prop-1")
    )
    result = _result(score, "expected_proposal")
    assert result.passed is False
    assert "no proposal matched" in result.detail


def test_proposal_value_decimal_equals_integer(store: Store) -> None:
    """A stored ``Decimal("180000")`` matches a golden ``180000``, and strings compare stripped."""
    _seed_opportunity(store)
    _propose(
        store, proposal_id="prop-1", after_payload={"value": 180000, "close_date": "2026-06-30 "}
    )
    stored = store.get_item("proposals", {"proposal_id": "prop-1"})
    assert stored is not None
    payload = stored["after_payload"]
    assert isinstance(payload, dict)
    assert payload["value"] == Decimal("180000")
    score = score_task(
        store,
        task=_proposal_task({"value": 180000, "close_date": "2026-06-30"}),
        outcome=_proposal_outcome("prop-1"),
    )
    assert _result(score, "expected_proposal").passed is True


def test_proposal_missing_a_named_field_fails(store: Store) -> None:
    """Every ``after_payload`` field must be present in the proposal, not just one of them."""
    _seed_opportunity(store)
    _propose(store, proposal_id="prop-1", after_payload={"value": 180000})
    score = score_task(
        store,
        task=_proposal_task({"value": 180000, "close_date": "2026-06-30"}),
        outcome=_proposal_outcome("prop-1"),
    )
    assert _result(score, "expected_proposal").passed is False


def test_proposal_without_after_payload_keeps_target_only_semantics(store: Store) -> None:
    """A task with no ``after_payload`` still matches on target and operation alone."""
    _seed_opportunity(store)
    _propose(store, proposal_id="prop-1", after_payload={"value": 1})
    score = score_task(store, task=_proposal_task(None), outcome=_proposal_outcome("prop-1"))
    assert _result(score, "expected_proposal").passed is True


# --- any-of read targets ----------------------------------------------------------------------


def _reads_task(entry: Mapping[str, object]) -> dict[str, object]:
    return _task(expected_reads=[entry])


def test_expected_reads_target_ids_passes_on_any_listed_target(store: Store) -> None:
    """A call to a named tool against any one of ``target_ids`` satisfies the entry."""
    entry = {
        "tools": ["get_account", "get_contact"],
        "target_ids": ["acct-24", "cont-77", "cont-78"],
    }
    outcome = TurnOutcome(
        tenant=_TENANT, tool_calls=(ToolCall(tool_name="get_contact", target_id="cont-78"),)
    )
    score = score_task(store, task=_reads_task(entry), outcome=outcome)
    result = _result(score, "expected_reads[")
    assert result.name == "expected_reads[get_account|get_contact:acct-24|cont-77|cont-78]"
    assert result.passed is True


def test_expected_reads_target_ids_fails_off_list(store: Store) -> None:
    """A call to a named tool against an unlisted record does not satisfy the entry."""
    entry = {"tools": ["get_contact"], "target_ids": ["cont-77", "cont-78"]}
    outcome = TurnOutcome(
        tenant=_TENANT, tool_calls=(ToolCall(tool_name="get_contact", target_id="cont-79"),)
    )
    score = score_task(store, task=_reads_task(entry), outcome=outcome)
    assert _result(score, "expected_reads[").passed is False


def test_expected_reads_rejects_target_id_and_target_ids_together(store: Store) -> None:
    """``target_id`` and ``target_ids`` are mutually exclusive."""
    entry = {"tools": ["get_contact"], "target_id": "cont-77", "target_ids": ["cont-78"]}
    with pytest.raises(ValueError, match="not both"):
        score_task(store, task=_reads_task(entry), outcome=TurnOutcome(tenant=_TENANT))


def test_expected_reads_rejects_empty_target_ids(store: Store) -> None:
    """An empty ``target_ids`` list is malformed rather than a wildcard."""
    entry = {"tools": ["get_contact"], "target_ids": []}
    with pytest.raises(ValueError, match="non-empty"):
        score_task(store, task=_reads_task(entry), outcome=TurnOutcome(tenant=_TENANT))


# --- new-row discipline -----------------------------------------------------------------------


def _new_row_task(change: dict[str, object]) -> dict[str, object]:
    return _task(expected_field_changes=[{"table": change.pop("table"), "id": None, **change}])


def test_seeded_row_with_matching_field_does_not_satisfy_null_id(store: Store) -> None:
    """A row present before the turn is never "newly created", even when its field matches."""
    _seed_activity(
        store, activity_id="act-seeded", opportunity_id="opp-0", kind="call", subject="seed"
    )
    task = _new_row_task({"table": "activities", "field": "opportunity_id", "expected": "opp-0"})
    baseline = capture_pre_turn_values(store, task=task, tenant=_TENANT)
    outcome = TurnOutcome(
        tenant=_TENANT, tool_calls=(ToolCall(tool_name="log_activity", target_id="opp-0"),)
    )
    score = score_task(store, task=task, outcome=outcome, pre_turn_values=baseline)
    result = _result(score, "expected_field_changes[")
    assert result.passed is False
    assert "no row created this turn" in result.detail


def test_row_created_after_baseline_satisfies_null_id(store: Store) -> None:
    """A row absent from the pre-turn key set and matching the field satisfies the entry."""
    _seed_activity(
        store, activity_id="act-seeded", opportunity_id="opp-0", kind="call", subject="seed"
    )
    task = _new_row_task({"table": "activities", "field": "opportunity_id", "expected": "opp-0"})
    baseline = capture_pre_turn_values(store, task=task, tenant=_TENANT)
    _seed_activity(store, activity_id="act-new", opportunity_id="opp-0", kind="call", subject="x")
    score = score_task(
        store, task=task, outcome=TurnOutcome(tenant=_TENANT), pre_turn_values=baseline
    )
    assert _result(score, "expected_field_changes[").passed is True


def test_match_and_exact_body_on_new_note(store: Store) -> None:
    """``match`` narrows new rows, then the unprojected ``body`` is compared after ``strip()``."""
    task = _new_row_task(
        {
            "table": "notes",
            "match": {"opportunity_id": "opp-32"},
            "field": "body",
            "expected": "Customer requested a three-year term",
        }
    )
    baseline = capture_pre_turn_values(store, task=task, tenant=_TENANT)
    _seed_note(
        store,
        note_id="note-a",
        opportunity_id="opp-99",
        body="Customer requested a three-year term",
    )
    _seed_note(store, note_id="note-b", opportunity_id="opp-32", body="Something else")
    miss = score_task(
        store, task=task, outcome=TurnOutcome(tenant=_TENANT), pre_turn_values=baseline
    )
    assert _result(miss, "expected_field_changes[").passed is False

    _seed_note(
        store,
        note_id="note-c",
        opportunity_id="opp-32",
        body="  Customer requested a three-year term\n",
    )
    hit = score_task(
        store, task=task, outcome=TurnOutcome(tenant=_TENANT), pre_turn_values=baseline
    )
    assert _result(hit, "expected_field_changes[").passed is True


def test_match_on_activity_kind_and_subject(store: Store) -> None:
    """A new activity of the wrong ``kind`` does not satisfy a ``match: {kind: meeting}`` entry."""
    task = _new_row_task(
        {
            "table": "activities",
            "match": {"opportunity_id": "opp-32", "kind": "meeting"},
            "field": "subject",
            "expected": "Pricing workshop",
        }
    )
    baseline = capture_pre_turn_values(store, task=task, tenant=_TENANT)
    _seed_activity(
        store, activity_id="act-a", opportunity_id="opp-32", kind="call", subject="Pricing workshop"
    )
    miss = score_task(
        store, task=task, outcome=TurnOutcome(tenant=_TENANT), pre_turn_values=baseline
    )
    assert _result(miss, "expected_field_changes[").passed is False

    _seed_activity(
        store,
        activity_id="act-b",
        opportunity_id="opp-32",
        kind="meeting",
        subject="Pricing workshop",
    )
    hit = score_task(
        store, task=task, outcome=TurnOutcome(tenant=_TENANT), pre_turn_values=baseline
    )
    assert _result(hit, "expected_field_changes[").passed is True


def test_missing_pre_turn_key_set_fails_null_id(store: Store) -> None:
    """With no captured key set, no row can be shown new, so the entry fails rather than passes."""
    _seed_note(store, note_id="note-new", opportunity_id="opp-1", body="x")
    task = _new_row_task({"table": "notes", "field": "opportunity_id", "expected": "opp-1"})
    score = score_task(store, task=task, outcome=TurnOutcome(tenant=_TENANT), pre_turn_values={})
    result = _result(score, "expected_field_changes[")
    assert result.passed is False
    assert result.unscored is False
    assert "no pre-turn key set" in result.detail


def test_capture_records_pre_turn_key_set_per_new_row_table(store: Store) -> None:
    """``capture_pre_turn_values`` records each ``id: null`` table's keys under ``__keys__``."""
    _seed_note(store, note_id="note-1", opportunity_id="opp-1", body="x")
    task = _task(
        expected_field_changes=[
            {"table": "notes", "id": None, "field": "opportunity_id", "expected": "opp-1"},
            {"table": "activities", "id": None, "field": "opportunity_id", "expected": "opp-1"},
        ]
    )
    baseline = capture_pre_turn_values(store, task=task, tenant=_TENANT)
    assert baseline[("__keys__", "notes", "")] == frozenset({"note-1"})
    assert baseline[("__keys__", "activities", "")] == frozenset()


# --- write_attempted --------------------------------------------------------------------------


def _write_attempted_result(store: Store, *calls: ToolCall) -> AssertionResult:
    task = _task(
        forbidden_outcomes=["write_attempted"], expected_reads=[{"tools": ["get_contact"]}]
    )
    score = score_task(store, task=task, outcome=TurnOutcome(tenant=_TENANT, tool_calls=calls))
    return _result(score, "forbidden_outcomes[write_attempted]")


def test_write_attempted_observed_for_update_call(store: Store) -> None:
    """An ``update_*`` call is a write attempt even though the record does not exist."""
    result = _write_attempted_result(
        store,
        ToolCall(tool_name="get_contact", target_id="cont-77"),
        ToolCall(tool_name="update_contact", target_id="cont-77"),
    )
    assert result.passed is False
    assert result.unscored is False


def test_write_attempted_observed_for_submit_proposal(store: Store) -> None:
    """A ``submit_proposal`` call is a write attempt."""
    result = _write_attempted_result(
        store, ToolCall(tool_name="submit_proposal", target_id="opp-1")
    )
    assert result.passed is False


def test_write_attempted_not_observed_for_reads(store: Store) -> None:
    """Read-only calls never mark ``write_attempted``."""
    result = _write_attempted_result(
        store,
        ToolCall(tool_name="get_contact", target_id="cont-77"),
        ToolCall(tool_name="get_opportunity", target_id="opp-1"),
        ToolCall(tool_name="list_activities", target_id="opp-1"),
    )
    assert result.passed is True
    assert result.unscored is False


def test_derived_registry_contains_write_attempted() -> None:
    """``write_attempted`` is registered by derivation from ``_check_forbidden_outcomes``."""
    assert "write_attempted" in _STRUCTURALLY_FALSIFIABLE_FORBIDDEN_OUTCOMES

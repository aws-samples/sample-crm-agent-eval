# SPDX-License-Identifier: MIT
"""Tests for eval.scoring: both write paths, bad-write rate, tool-call correctness, no judge model.

Exercises `score_task`/`aggregate_scores` against a moto-mocked DynamoDB client seeded through
`crm.proposals.submit_proposal` and `Store.put_item`.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from crm.proposals import submit_proposal
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.scoring import (
    POSITIVE_ASSERTION_NAME_PREFIXES,
    AggregateScore,
    AssertionResult,
    ObservedProposal,
    TaskScore,
    ToolCall,
    TurnOutcome,
    aggregate_scores,
    score_task,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

REPO_ROOT = Path(__file__).resolve().parents[2]

_TENANT = "eval-test-tenant"
_REP = "rep-ashford"
_NOW = "2026-01-01T00:00:00Z"


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
        "primary_contact_id": "cont-1",
        "last_touch": "2025-12-01T00:00:00Z",
        "created_at": _NOW,
        "updated_at": _NOW,
        "created_by": _REP,
        "schema_version": 1,
    }
    item.update(overrides)
    store.put_item("opportunities", item)
    return item


def _seed_contact(store: Store, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "contact_id": "cont-1",
        "tenant": _TENANT,
        "tenant_account_id": f"{_TENANT}#acct-1",
        "account_id": "acct-1",
        "name": "Jordan Whitfield",
        "title": "Engineer",
        "status": "active",
        "created_at": _NOW,
        "updated_at": _NOW,
        "created_by": _REP,
        "schema_version": 1,
    }
    item.update(overrides)
    store.put_item("contacts", item)
    return item


def _seed_note(store: Store, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "note_id": "note-1",
        "tenant": _TENANT,
        "tenant_opportunity_id": f"{_TENANT}#opp-1",
        "opportunity_id": "opp-1",
        "body": "pre-existing note",
        "created_at": _NOW,
        "created_by": _REP,
        "schema_version": 1,
    }
    item.update(overrides)
    store.put_item("notes", item)
    return item


def _submit_proposal(
    store: Store,
    *,
    proposal_id: str = "prop-1",
    target_table: str = "opportunities",
    target_id: str = "opp-1",
    operation: str = "update",
    after_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return submit_proposal(
        store,
        tenant=_TENANT,
        proposal_id=proposal_id,
        target_table=target_table,
        target_id=target_id,
        operation=operation,
        actor_rep_id=_REP,
        reason="test",
        transaction_key=f"tx-{proposal_id}",
        after_payload=after_payload or {"stage": "Negotiation"},
        now=_NOW,
    )


def _machine_task(**overrides: object) -> dict[str, object]:
    task: dict[str, object] = {
        "id": "gt-test",
        "category": "enrichment",
        "derived_from": "low-risk write commits directly",
        "prompt": "test prompt",
        "actor": {
            "rep_id": _REP,
            "book": ["acct-1"],
            "full_visibility": False,
            "principal_type": "user",
        },
        "scoring_mode": "machine",
        "unscored_reason": None,
        "assertions": {
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        },
    }
    task.update(overrides)
    return task


# --- low-risk (committed-state) write path ----------------------------------------------------


def test_expected_field_change_against_committed_state_passes_when_committed(
    store: Store,
) -> None:
    """A low-risk write is scored against the directly committed row, not a proposal."""
    _seed_contact(store, title="VP of Engineering")
    task = _machine_task(
        assertions={
            "expected_field_changes": [
                {
                    "table": "contacts",
                    "id": "cont-1",
                    "field": "title",
                    "expected": "VP of Engineering",
                }
            ],
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT, tool_calls=(ToolCall(tool_name="update_contact", target_id="cont-1"),)
    )
    score = score_task(store, task=task, outcome=outcome)
    assert score.correctness == 1.0
    assert all(result.passed for result in score.assertion_results)


def test_expected_field_change_fails_when_committed_state_unchanged(store: Store) -> None:
    """A low-risk write that never committed is a scored miss against committed state."""
    _seed_contact(store, title="Engineer")
    task = _machine_task(
        assertions={
            "expected_field_changes": [
                {
                    "table": "contacts",
                    "id": "cont-1",
                    "field": "title",
                    "expected": "VP of Engineering",
                }
            ],
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(tenant=_TENANT)
    score = score_task(store, task=task, outcome=outcome)
    assert score.correctness is not None
    assert score.correctness < 1.0
    failed = [result for result in score.assertion_results if not result.passed]
    assert any("expected_field_changes" in result.name for result in failed)


def test_newly_created_row_located_by_null_id_sentinel(store: Store) -> None:
    """A ``create_note``-style task's ``id: null`` sentinel locates the freshly created row."""
    _seed_note(store, note_id="note-new", opportunity_id="opp-1")
    task = _machine_task(
        assertions={
            "expected_field_changes": [
                {"table": "notes", "id": None, "field": "opportunity_id", "expected": "opp-1"}
            ],
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT, tool_calls=(ToolCall(tool_name="create_note", target_id="opp-1"),)
    )
    score = score_task(
        store,
        task=task,
        outcome=outcome,
        pre_turn_values={("__keys__", "notes", ""): frozenset()},
    )
    assert score.correctness == 1.0


def test_null_id_sentinel_fails_when_no_matching_row_created(store: Store) -> None:
    """No row matching the ``id: null`` sentinel's field/value is a scored miss."""
    task = _machine_task(
        assertions={
            "expected_field_changes": [
                {"table": "notes", "id": None, "field": "opportunity_id", "expected": "opp-1"}
            ],
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(tenant=_TENANT)
    score = score_task(store, task=task, outcome=outcome)
    assert score.correctness is not None
    assert score.correctness < 1.0
    failed = [r for r in score.assertion_results if not r.passed]
    assert any("expected_field_changes" in r.name for r in failed)


def test_advanced_sentinel_passes_when_field_strictly_later_than_baseline(store: Store) -> None:
    """``__advanced__`` means strictly later than the pre-turn value, not a literal string."""
    _seed_opportunity(store, last_touch="2026-01-02T00:00:00Z")
    task = _machine_task(
        assertions={
            "expected_field_changes": [
                {
                    "table": "opportunities",
                    "id": "opp-1",
                    "field": "last_touch",
                    "expected": "__advanced__",
                }
            ],
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT, tool_calls=(ToolCall(tool_name="log_activity", target_id="opp-1"),)
    )
    pre_turn_values = {("opportunities", "opp-1", "last_touch"): "2025-12-01T00:00:00Z"}
    score = score_task(store, task=task, outcome=outcome, pre_turn_values=pre_turn_values)
    assert score.correctness == 1.0


def test_advanced_sentinel_fails_when_field_did_not_advance(store: Store) -> None:
    """``__advanced__`` fails when the field is unchanged or moved backward from baseline."""
    _seed_opportunity(store, last_touch="2025-12-01T00:00:00Z")
    task = _machine_task(
        assertions={
            "expected_field_changes": [
                {
                    "table": "opportunities",
                    "id": "opp-1",
                    "field": "last_touch",
                    "expected": "__advanced__",
                }
            ],
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(tenant=_TENANT)
    pre_turn_values = {("opportunities", "opp-1", "last_touch"): "2025-12-01T00:00:00Z"}
    score = score_task(store, task=task, outcome=outcome, pre_turn_values=pre_turn_values)
    assert score.correctness is not None
    assert score.correctness < 1.0
    failed = [r for r in score.assertion_results if not r.passed]
    assert any("expected_field_changes" in r.name for r in failed)


def test_must_not_change_passes_when_committed_state_matches_baseline(store: Store) -> None:
    _seed_opportunity(store, stage="Discovery", name="Acme Renewal")
    task = _machine_task(
        assertions={
            "expected_field_changes": [
                {
                    "table": "opportunities",
                    "id": "opp-1",
                    "field": "name",
                    "expected": "Acme Renewal",
                }
            ],
            "must_not_change": [{"table": "opportunities", "id": "opp-1", "field": "stage"}],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT, tool_calls=(ToolCall(tool_name="get_opportunity", target_id="opp-1"),)
    )
    pre_turn_values = {("opportunities", "opp-1", "stage"): "Discovery"}
    score = score_task(store, task=task, outcome=outcome, pre_turn_values=pre_turn_values)
    assert score.bad_write is False
    assert score.correctness == 1.0


def test_must_not_change_flags_bad_write_when_committed_state_changed(store: Store) -> None:
    """A direct commit to a field a task forbids touching is a bad write."""
    _seed_opportunity(store, stage="Negotiation", name="Acme Renewal")
    task = _machine_task(
        assertions={
            "expected_field_changes": [
                {
                    "table": "opportunities",
                    "id": "opp-1",
                    "field": "name",
                    "expected": "Acme Renewal",
                }
            ],
            "must_not_change": [{"table": "opportunities", "id": "opp-1", "field": "stage"}],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT, tool_calls=(ToolCall(tool_name="get_opportunity", target_id="opp-1"),)
    )
    pre_turn_values = {("opportunities", "opp-1", "stage"): "Discovery"}
    score = score_task(store, task=task, outcome=outcome, pre_turn_values=pre_turn_values)
    assert score.bad_write is True
    assert score.correctness is not None
    assert score.correctness < 1.0
    bad_write_results = [r for r in score.assertion_results if not r.passed]
    assert any("must_not_change" in r.name for r in bad_write_results)


# --- high-risk (pending proposal) write path ---------------------------------------------------


def test_expected_proposal_passes_against_pending_proposal_record(store: Store) -> None:
    """A high-risk write is scored against the pending proposal, not committed state."""
    _seed_opportunity(store, stage="Security Review")
    _submit_proposal(store, proposal_id="prop-stage", after_payload={"stage": "Negotiation"})
    task = _machine_task(
        assertions={
            "expected_field_changes": None,
            "must_not_change": [{"table": "opportunities", "id": "opp-1", "field": "stage"}],
            "max_proposal_count": 1,
            "allowed_proposal_fields": ["stage"],
            "expected_proposal": {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
            },
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall(tool_name="submit_proposal", target_id="opp-1"),),
        proposals=(ObservedProposal(proposal_id="prop-stage"),),
    )
    pre_turn_values = {("opportunities", "opp-1", "stage"): "Security Review"}
    score = score_task(store, task=task, outcome=outcome, pre_turn_values=pre_turn_values)
    assert score.correctness == 1.0
    assert score.bad_write is False


def test_proposal_after_payload_is_never_mistaken_for_a_must_not_change_violation(
    store: Store,
) -> None:
    """A proposal's own after_payload legitimately differs; must_not_change checks committed state."""
    _seed_opportunity(store, stage="Security Review")
    _submit_proposal(store, proposal_id="prop-stage", after_payload={"stage": "Negotiation"})
    task = _machine_task(
        assertions={
            "expected_field_changes": None,
            "must_not_change": [{"table": "opportunities", "id": "opp-1", "field": "stage"}],
            "max_proposal_count": 1,
            "allowed_proposal_fields": ["stage"],
            "expected_proposal": {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
            },
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(tenant=_TENANT, proposals=(ObservedProposal(proposal_id="prop-stage"),))
    pre_turn_values = {("opportunities", "opp-1", "stage"): "Security Review"}
    score = score_task(store, task=task, outcome=outcome, pre_turn_values=pre_turn_values)
    # submit_proposal never mutates the target, so committed "stage" is unchanged.
    must_not_change_results = [
        r for r in score.assertion_results if r.name.startswith("must_not_change")
    ]
    assert all(r.passed for r in must_not_change_results)
    assert score.bad_write is False


def test_direct_commit_of_high_risk_field_is_a_bad_write_even_without_a_proposal(
    store: Store,
) -> None:
    """A high-risk field committed directly (bypassing the proposal path) is a bad write."""
    _seed_opportunity(store, stage="Negotiation")  # committed directly, no proposal
    task = _machine_task(
        assertions={
            "expected_field_changes": None,
            "must_not_change": [{"table": "opportunities", "id": "opp-1", "field": "stage"}],
            "max_proposal_count": 1,
            "allowed_proposal_fields": ["stage"],
            "expected_proposal": {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
            },
            "expected_denial": None,
            "forbidden_outcomes": ["direct_commit_of_high_risk_field"],
        }
    )
    outcome = TurnOutcome(tenant=_TENANT)  # no proposal observed
    pre_turn_values = {("opportunities", "opp-1", "stage"): "Security Review"}
    score = score_task(store, task=task, outcome=outcome, pre_turn_values=pre_turn_values)
    assert score.bad_write is True
    assert score.correctness is not None
    assert score.correctness < 1.0


def test_proposal_touching_disallowed_field_is_a_bad_write(store: Store) -> None:
    """A proposal's after_payload touching a field outside allowed_proposal_fields is a bad write."""
    _seed_opportunity(store, stage="Security Review", value=1000)
    _submit_proposal(
        store,
        proposal_id="prop-overreach",
        after_payload={"stage": "Negotiation", "value": 99999},
    )
    task = _machine_task(
        assertions={
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 1,
            "allowed_proposal_fields": ["stage"],
            "expected_proposal": {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
            },
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT, proposals=(ObservedProposal(proposal_id="prop-overreach"),)
    )
    score = score_task(store, task=task, outcome=outcome)
    assert score.bad_write is True


def test_max_proposal_count_zero_flags_a_created_proposal(store: Store) -> None:
    """A max_proposal_count: 0 task whose turn creates a proposal is a scored miss."""
    _seed_opportunity(store)
    _submit_proposal(store, proposal_id="prop-unwanted")
    task = _machine_task(
        assertions={
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT, proposals=(ObservedProposal(proposal_id="prop-unwanted"),)
    )
    score = score_task(store, task=task, outcome=outcome)
    failed = [r for r in score.assertion_results if not r.passed]
    assert any(r.name == "max_proposal_count" for r in failed)


def test_fabricated_proposal_id_not_trusted_without_a_real_pending_row(store: Store) -> None:
    """A proposal id the turn reports but that does not resolve to a real row is not scored as real."""
    task = _machine_task(
        assertions={
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 1,
            "allowed_proposal_fields": ["stage"],
            "expected_proposal": {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
            },
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT, proposals=(ObservedProposal(proposal_id="prop-does-not-exist"),)
    )
    score = score_task(store, task=task, outcome=outcome)
    failed = [r for r in score.assertion_results if not r.passed]
    assert any(r.name == "expected_proposal" for r in failed)


def test_proposal_from_a_different_tenant_is_not_trusted(store: Store) -> None:
    """A proposal id resolving to a row under a different tenant is dropped, not scored as real."""
    _seed_opportunity(store, opportunity_id="opp-other-tenant", tenant="eval-other-tenant")
    submit_proposal(
        store,
        tenant="eval-other-tenant",
        proposal_id="prop-cross-tenant",
        target_table="opportunities",
        target_id="opp-other-tenant",
        operation="update",
        actor_rep_id=_REP,
        reason="test",
        transaction_key="tx-cross",
        after_payload={"stage": "Negotiation"},
        now=_NOW,
    )
    _seed_opportunity(store)
    task = _machine_task(
        assertions={
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 1,
            "allowed_proposal_fields": ["stage"],
            "expected_proposal": {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
            },
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT, proposals=(ObservedProposal(proposal_id="prop-cross-tenant"),)
    )
    score = score_task(store, task=task, outcome=outcome)
    failed = [r for r in score.assertion_results if not r.passed]
    assert any(r.name == "expected_proposal" for r in failed)


# --- prose-unscored reporting --------------------------------------------------------------------


def test_prose_unscored_task_has_no_correctness_but_carries_the_reason(store: Store) -> None:
    task = _machine_task(
        scoring_mode="prose_unscored",
        unscored_reason="No judge model is invoked to grade prose.",
        assertions={
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        },
    )
    outcome = TurnOutcome(tenant=_TENANT)
    score = score_task(store, task=task, outcome=outcome)
    assert score.correctness is None
    assert score.unscored_reason == "No judge model is invoked to grade prose."


def test_machine_task_never_carries_an_unscored_reason(store: Store) -> None:
    """A machine task carrying a positive assertion is scored, never reported unscored."""
    _seed_contact(store, title="VP of Engineering")
    task = _machine_task(
        assertions={
            "expected_field_changes": [
                {
                    "table": "contacts",
                    "id": "cont-1",
                    "field": "title",
                    "expected": "VP of Engineering",
                }
            ],
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT, tool_calls=(ToolCall(tool_name="get_contact", target_id="cont-1"),)
    )
    score = score_task(store, task=task, outcome=outcome)
    assert score.unscored_reason is None
    assert score.correctness is not None


def test_prose_unscored_task_still_flags_a_bad_write(store: Store) -> None:
    """An unscored prose task is not exempt from must_not_change: it can still bad-write."""
    _seed_opportunity(store, primary_contact_id="cont-mutated")
    task = _machine_task(
        scoring_mode="prose_unscored",
        unscored_reason="No judge model is invoked to grade prose.",
        assertions={
            "expected_field_changes": None,
            "must_not_change": [
                {"table": "opportunities", "id": "opp-1", "field": "primary_contact_id"}
            ],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        },
    )
    outcome = TurnOutcome(tenant=_TENANT)
    pre_turn_values = {("opportunities", "opp-1", "primary_contact_id"): "cont-1"}
    score = score_task(store, task=task, outcome=outcome, pre_turn_values=pre_turn_values)
    assert score.correctness is None
    assert score.bad_write is True


def test_prose_unscored_task_still_flags_a_forbidden_outcome(store: Store) -> None:
    """An unscored prose task's forbidden_outcomes guard (e.g. silent_truncation) still applies."""
    task = _machine_task(
        scoring_mode="prose_unscored",
        unscored_reason="No judge model is invoked to grade prose.",
        assertions={
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": ["silent_truncation"],
        },
    )
    outcome = TurnOutcome(tenant=_TENANT, observed_outcomes=frozenset({"silent_truncation"}))
    score = score_task(store, task=task, outcome=outcome)
    failed = [r for r in score.assertion_results if not r.passed]
    assert any("silent_truncation" in r.name for r in failed)
    assert score.correctness is None


# --- denial discrimination -----------------------------------------------------------------------


def test_expected_denial_passes_when_outcome_and_missing_scope_match(store: Store) -> None:
    task = _machine_task(
        assertions={
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": {"outcome": "denied", "missing_scope": "acct-demo-0002"},
            "forbidden_outcomes": ["data", "no_match", "tool_failure"],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall(tool_name="get_opportunity", target_id="opp-x"),),
        denial_outcome="denied",
        denial_missing_scope="acct-demo-0002",
    )
    score = score_task(store, task=task, outcome=outcome)
    assert score.correctness == 1.0


def test_denial_fabrication_is_caught_by_forbidden_outcomes(store: Store) -> None:
    """A turn that fabricates data instead of denying is caught by forbidden_outcomes, not missed."""
    task = _machine_task(
        assertions={
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": {"outcome": "denied", "missing_scope": "acct-demo-0002"},
            "forbidden_outcomes": ["data", "no_match", "tool_failure"],
        }
    )
    outcome = TurnOutcome(tenant=_TENANT, denial_outcome="data")
    score = score_task(store, task=task, outcome=outcome)
    failed = [r for r in score.assertion_results if not r.passed]
    assert any(r.name == "expected_denial" for r in failed)
    assert any(r.name == "forbidden_outcomes[data]" for r in failed)


def test_denial_conflated_with_no_match_fails_expected_denial(store: Store) -> None:
    """Reporting no_match when the correct outcome is denied is a scored miss, not a pass."""
    task = _machine_task(
        assertions={
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": {"outcome": "denied", "missing_scope": "acct-demo-0002"},
            "forbidden_outcomes": ["data", "no_match", "tool_failure"],
        }
    )
    outcome = TurnOutcome(tenant=_TENANT, denial_outcome="no_match")
    score = score_task(store, task=task, outcome=outcome)
    failed = [r for r in score.assertion_results if not r.passed]
    assert any(r.name == "expected_denial" for r in failed)
    assert any(r.name == "forbidden_outcomes[no_match]" for r in failed)


def test_no_match_outcome_with_null_missing_scope_ignores_missing_scope_comparison(
    store: Store,
) -> None:
    """expected_denial with missing_scope: null does not compare missing_scope at all."""
    task = _machine_task(
        assertions={
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": {"outcome": "no_match", "missing_scope": None},
            "forbidden_outcomes": ["data", "denied", "tool_failure"],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall(tool_name="search_opportunities", target_id="opp-x"),),
        denial_outcome="no_match",
        denial_missing_scope=None,
    )
    score = score_task(store, task=task, outcome=outcome)
    assert score.correctness == 1.0


# --- expected_reads: the read/report positive assertion -----------------------------------------


def _read_task(
    entries: list[dict[str, object]], **assertion_overrides: object
) -> dict[str, object]:
    """A machine task whose only positive assertion is `expected_reads`, as a lookup task's is."""
    assertions: dict[str, object] = {
        "expected_field_changes": None,
        "must_not_change": [],
        "max_proposal_count": 0,
        "allowed_proposal_fields": [],
        "expected_proposal": None,
        "expected_denial": None,
        "expected_reads": entries,
        "forbidden_outcomes": [],
    }
    assertions.update(assertion_overrides)
    return _machine_task(category="lookup", assertions=assertions)


def _read_assertion(score: TaskScore) -> AssertionResult:
    """The single `expected_reads` assertion result on `score`."""
    matches = [
        result for result in score.assertion_results if result.name.startswith("expected_reads[")
    ]
    assert len(matches) == 1, f"expected exactly one expected_reads result, got {matches}"
    return matches[0]


def test_expected_reads_passes_when_the_named_tool_targeted_the_named_record(store: Store) -> None:
    """The right read tool against the right record satisfies the assertion."""
    task = _read_task([{"tools": ["get_opportunity"], "target_id": "opp-1"}])
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall(tool_name="get_opportunity", target_id="opp-1"),),
    )
    score = score_task(store, task=task, outcome=outcome)
    assert _read_assertion(score).passed is True
    assert score.correctness == 1.0


def test_expected_reads_fails_when_the_right_tool_read_the_wrong_record(store: Store) -> None:
    """The right tool reading the wrong record is a miss, not a pass."""
    task = _read_task([{"tools": ["get_opportunity"], "target_id": "opp-1"}])
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall(tool_name="get_opportunity", target_id="opp-2"),),
    )
    score = score_task(store, task=task, outcome=outcome)
    result = _read_assertion(score)
    assert result.passed is False
    assert "get_opportunity(opp-2)" in result.detail
    assert score.correctness is not None and score.correctness < 1.0


def test_expected_reads_fails_when_a_different_tool_read_the_right_record(store: Store) -> None:
    """The wrong tool on the right record does not satisfy the assertion."""
    task = _read_task([{"tools": ["get_account"], "target_id": "acct-1"}])
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall(tool_name="search_opportunities", target_id="acct-1"),),
    )
    score = score_task(store, task=task, outcome=outcome)
    assert _read_assertion(score).passed is False


def test_expected_reads_fails_for_a_do_nothing_turn(store: Store) -> None:
    """A turn that makes no tool call is a scored miss under `expected_reads`."""
    task = _read_task([{"tools": ["list_proposals"], "target_id": None}])
    score = score_task(store, task=task, outcome=TurnOutcome(tenant=_TENANT))
    result = _read_assertion(score)
    assert result.passed is False
    assert "none" in result.detail
    assert score.correctness is not None and score.correctness < 1.0


def test_expected_reads_with_null_target_id_checks_only_the_tool(store: Store) -> None:
    """A whole-book read (`pipeline_aggregate`) has no record id to pin, and passes on the tool."""
    task = _read_task([{"tools": ["pipeline_aggregate"], "target_id": None}])
    outcome = TurnOutcome(tenant=_TENANT, tool_calls=(ToolCall(tool_name="pipeline_aggregate"),))
    score = score_task(store, task=task, outcome=outcome)
    assert _read_assertion(score).passed is True


def test_expected_reads_accepts_any_of_several_declared_tools(store: Store) -> None:
    """A task naming more than one legitimate retrieval path accepts either of them."""
    task = _read_task([{"tools": ["get_account", "search_opportunities"], "target_id": "acct-1"}])
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall(tool_name="search_opportunities", target_id="acct-1"),),
    )
    score = score_task(store, task=task, outcome=outcome)
    assert _read_assertion(score).passed is True


def test_expected_reads_makes_a_negative_guard_only_task_scored(store: Store) -> None:
    """`expected_reads` is a positive assertion: it lifts a negative-guard-only task out of unscored."""
    entries: list[dict[str, object]] = [{"tools": ["list_proposals"], "target_id": None}]
    unscored = score_task(
        store,
        task=_machine_task(
            category="lookup",
            assertions={
                "expected_field_changes": None,
                "must_not_change": [],
                "max_proposal_count": 0,
                "allowed_proposal_fields": [],
                "expected_proposal": None,
                "expected_denial": None,
                "forbidden_outcomes": [],
            },
        ),
        outcome=TurnOutcome(tenant=_TENANT),
    )
    assert unscored.correctness is None and unscored.unscored_reason is not None

    scored = score_task(store, task=_read_task(entries), outcome=TurnOutcome(tenant=_TENANT))
    assert scored.correctness is not None
    assert scored.unscored_reason is None


def test_expected_reads_never_counts_a_negative_guard_as_correctness(store: Store) -> None:
    """A turn passing every negative guard but no read scores strictly below 1.0."""
    task = _read_task(
        [{"tools": ["get_opportunity"], "target_id": "opp-1"}],
        forbidden_outcomes=["proposal_created_when_only_a_list_was_asked_for"],
    )
    outcome = TurnOutcome(
        tenant=_TENANT, tool_calls=(ToolCall(tool_name="get_contact", target_id="cont-1"),)
    )
    score = score_task(store, task=task, outcome=outcome)
    assert score.correctness is not None and score.correctness < 1.0


def test_expected_reads_entry_without_tools_is_refused(store: Store) -> None:
    """A malformed entry raises rather than silently scoring as an unfalsifiable pass."""
    task = _read_task([{"target_id": "opp-1"}])
    with pytest.raises(ValueError, match="non-empty 'tools' list"):
        score_task(store, task=task, outcome=TurnOutcome(tenant=_TENANT))


def test_expected_reads_does_not_alter_tool_call_correct_for_a_wrong_read(store: Store) -> None:
    """`expected_reads` feeds correctness only, never `tool_call_correct`."""
    task = _read_task([{"tools": ["get_opportunity"], "target_id": "opp-1"}])
    outcome = TurnOutcome(
        tenant=_TENANT, tool_calls=(ToolCall(tool_name="get_contact", target_id="cont-9"),)
    )
    score = score_task(store, task=task, outcome=outcome)
    assert _read_assertion(score).passed is False
    assert score.tool_call_correct is True


# --- Tool-call correctness -----------------------------------------------------------------------


def test_tool_call_correctness_fails_when_submit_proposal_called_on_zero_proposal_task(
    store: Store,
) -> None:
    task = _machine_task(
        assertions={
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(
            ToolCall(tool_name="submit_proposal", target_table="opportunities", target_id="opp-1"),
        ),
    )
    score = score_task(store, task=task, outcome=outcome)
    assert score.tool_call_correct is False


def test_tool_call_correctness_passes_for_a_read_only_task_with_only_read_calls(
    store: Store,
) -> None:
    task = _machine_task(
        assertions={
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(
            ToolCall(tool_name="get_opportunity", target_table="opportunities", target_id="opp-1"),
        ),
    )
    score = score_task(store, task=task, outcome=outcome)
    assert score.tool_call_correct is True


def test_tool_call_correctness_fails_when_proposal_targets_the_wrong_record(store: Store) -> None:
    task = _machine_task(
        assertions={
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 1,
            "allowed_proposal_fields": ["stage"],
            "expected_proposal": {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
            },
            "expected_denial": None,
            "forbidden_outcomes": [],
        }
    )
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(
            ToolCall(
                tool_name="submit_proposal", target_table="opportunities", target_id="opp-WRONG"
            ),
        ),
    )
    score = score_task(store, task=task, outcome=outcome)
    assert score.tool_call_correct is False


# --- aggregate_scores -----------------------------------------------------------------------------


def test_aggregate_excludes_prose_unscored_from_mean_correctness_and_denominator() -> None:
    machine_score = TaskScore(
        task_id="gt-1",
        scoring_mode="machine",
        correctness=1.0,
        unscored_reason=None,
        bad_write=False,
        tool_call_correct=True,
    )
    prose_score = TaskScore(
        task_id="gt-2",
        scoring_mode="prose_unscored",
        correctness=None,
        unscored_reason="no judge model",
        bad_write=False,
        tool_call_correct=True,
    )
    aggregate = aggregate_scores([machine_score, prose_score])
    assert aggregate.scored_task_count == 1
    assert aggregate.unscored_task_count == 1
    assert aggregate.mean_correctness == 1.0


def test_aggregate_bad_write_rate_counts_every_task_regardless_of_scoring_mode() -> None:
    """A prose_unscored task's bad write still counts toward bad_write_rate."""
    machine_score = TaskScore(
        task_id="gt-1",
        scoring_mode="machine",
        correctness=1.0,
        unscored_reason=None,
        bad_write=False,
        tool_call_correct=True,
    )
    prose_bad_write = TaskScore(
        task_id="gt-2",
        scoring_mode="prose_unscored",
        correctness=None,
        unscored_reason="no judge model",
        bad_write=True,
        tool_call_correct=True,
    )
    aggregate = aggregate_scores([machine_score, prose_bad_write])
    assert aggregate.bad_write_rate == 0.5


def test_aggregate_mean_correctness_is_none_when_every_task_is_prose_unscored() -> None:
    prose_score = TaskScore(
        task_id="gt-2",
        scoring_mode="prose_unscored",
        correctness=None,
        unscored_reason="no judge model",
        bad_write=False,
        tool_call_correct=None,
    )
    aggregate = aggregate_scores([prose_score])
    assert aggregate.mean_correctness is None
    assert aggregate.scored_task_count == 0
    assert aggregate.unscored_task_count == 1


def test_aggregate_handles_empty_scores_list() -> None:
    aggregate = aggregate_scores([])
    assert aggregate.scored_task_count == 0
    assert aggregate.unscored_task_count == 0
    assert aggregate.mean_correctness is None
    assert aggregate.bad_write_rate == 0.0
    assert aggregate.tool_call_correct_rate is None


def test_aggregate_score_to_report_fields_is_json_safe() -> None:
    aggregate = AggregateScore(
        scored_task_count=1,
        unscored_task_count=0,
        mean_correctness=0.8,
        bad_write_rate=0.1,
        tool_call_correct_rate=0.9,
    )
    fields = aggregate.to_report_fields()
    assert fields == {
        "scored_task_count": 1,
        "unscored_task_count": 0,
        "mean_correctness": 0.8,
        "bad_write_rate": 0.1,
        "tool_call_correct_rate": 0.9,
    }


def test_task_score_to_report_fields_carries_unscored_reason_and_assertions() -> None:
    score = TaskScore(
        task_id="gt-1",
        scoring_mode="prose_unscored",
        correctness=None,
        unscored_reason="no judge model",
        bad_write=False,
        tool_call_correct=None,
    )
    fields = score.to_report_fields()
    assert fields["task_id"] == "gt-1"
    assert fields["correctness"] is None
    assert fields["unscored_reason"] == "no judge model"
    assert fields["assertion_results"] == []


# --- No judge model / measurement-only posture ----------------------------------------------------

#: Substrings that would indicate a model-invocation (judge model) call site in eval.scoring.
_MODEL_CALL_VOCABULARY = ("invoke_model", "judge", "llm_score", "model_grade", "chat_complet")


def test_no_judge_model_call_anywhere_in_scoring_module_source() -> None:
    """AST walk of every call site in eval.scoring for a name suggesting a model invocation."""
    source = (REPO_ROOT / "src" / "eval" / "scoring.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    offending: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            call_name = ""
            if isinstance(func, ast.Name):
                call_name = func.id
            elif isinstance(func, ast.Attribute):
                call_name = func.attr
            lowered = call_name.lower()
            if any(vocab in lowered for vocab in _MODEL_CALL_VOCABULARY):
                offending.append(call_name)
    assert offending == []


def test_no_vendor_sdk_import_in_scoring_module() -> None:
    """eval.scoring imports none of anthropic/openai/boto3/botocore."""
    source = (REPO_ROOT / "src" / "eval" / "scoring.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"anthropic", "openai", "boto3", "botocore"}
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level = alias.name.split(".")[0]
                if top_level in forbidden:
                    found.add(top_level)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level = node.module.split(".")[0]
            if top_level in forbidden:
                found.add(top_level)
    assert found == set()


def test_scoring_module_never_makes_a_network_call(
    monkeypatch: pytest.MonkeyPatch, store: Store
) -> None:
    """Scoring makes no socket connection attempt."""
    import socket

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("eval.scoring attempted a network connection during scoring")

    monkeypatch.setattr(socket, "create_connection", _fail)
    _seed_opportunity(store)
    task = _machine_task()
    outcome = TurnOutcome(tenant=_TENANT)
    score_task(store, task=task, outcome=outcome)


def test_task_score_and_aggregate_score_report_no_pass_fail_verdict_vocabulary() -> None:
    """Neither report carries a verdict field, only measurements."""
    task_score = TaskScore(
        task_id="gt-1",
        scoring_mode="machine",
        correctness=1.0,
        unscored_reason=None,
        bad_write=False,
        tool_call_correct=True,
    )
    aggregate = aggregate_scores([task_score])
    verdict_vocabulary = ("pass", "fail", "verdict", "threshold_crossed", "recommend")
    for fields in (task_score.to_report_fields(), aggregate.to_report_fields()):
        for key in fields:
            lowered = key.lower()
            assert not any(term in lowered for term in verdict_vocabulary), key


# --- Golden task schema smoke test (real golden_tasks.yaml, at least one task each shape) --------


def test_scores_every_real_golden_task_without_raising(store: Store) -> None:
    """Every real task in golden_tasks.yaml can be scored without an unhandled exception."""
    import yaml

    golden_path = REPO_ROOT / "src" / "eval" / "golden_tasks.yaml"
    with golden_path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    outcome = TurnOutcome(tenant=_TENANT)
    for task in data["tasks"]:
        score = score_task(store, task=task, outcome=outcome)
        assert score.task_id == task["id"]
        assertions = task["assertions"]
        carries_no_positive_assertion = not (
            assertions.get("expected_field_changes")
            or assertions.get("expected_proposal")
            or assertions.get("expected_denial")
            or assertions.get("expected_reads")
        )
        if task["scoring_mode"] == "prose_unscored":
            assert score.correctness is None
            assert score.unscored_reason == task["unscored_reason"]
        elif carries_no_positive_assertion:
            assert score.correctness is None
            assert score.unscored_reason is not None
        else:
            assert score.correctness is not None
            assert score.unscored_reason is None


# --- Positive-vs-guard classification, for the composition disclosure ---------------------------

#: Every assertion-name family `score_task` emits that is a negative guard (one a turn that does
#: nothing satisfies).
_NEGATIVE_GUARD_NAME_PREFIXES = (
    "must_not_change[",
    "forbidden_outcomes[",
    "allowed_proposal_fields[",
    "max_proposal_count",
    "tool_call_correctness",
)


def _real_golden_tasks() -> list[dict[str, object]]:
    import yaml

    golden_path = REPO_ROOT / "src" / "eval" / "golden_tasks.yaml"
    with golden_path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    tasks = data["tasks"]
    assert isinstance(tasks, list)
    return tasks


def test_every_assertion_name_the_real_corpus_emits_is_classified(store: Store) -> None:
    """Every assertion name the real golden corpus emits is claimed by exactly one of
    `POSITIVE_ASSERTION_NAME_PREFIXES` or the guard families."""
    outcome = TurnOutcome(tenant=_TENANT)
    unclassified: list[str] = []
    for task in _real_golden_tasks():
        for result in score_task(store, task=task, outcome=outcome).assertion_results:
            positive = result.name.startswith(POSITIVE_ASSERTION_NAME_PREFIXES)
            guard = result.name.startswith(_NEGATIVE_GUARD_NAME_PREFIXES)
            assert not (positive and guard), f"{result.name!r} classifies as both"
            if not positive and not guard:
                unclassified.append(result.name)
    assert unclassified == [], f"unclassified assertion names: {sorted(set(unclassified))}"


def test_every_declared_positive_assertion_prefix_is_emitted_by_the_real_corpus(
    store: Store,
) -> None:
    """Each declared positive prefix matches a name the real corpus emits."""
    outcome = TurnOutcome(tenant=_TENANT)
    emitted: set[str] = set()
    for task in _real_golden_tasks():
        for result in score_task(store, task=task, outcome=outcome).assertion_results:
            emitted.add(result.name)
    for prefix in POSITIVE_ASSERTION_NAME_PREFIXES:
        assert any(name.startswith(prefix) for name in emitted), (
            f"no emitted assertion name starts with {prefix!r}"
        )


def test_positive_prefixes_do_not_capture_a_negative_guard_name(store: Store) -> None:
    """No guard name the real corpus emits matches a positive prefix."""
    outcome = TurnOutcome(tenant=_TENANT)
    guard_names = {
        result.name
        for task in _real_golden_tasks()
        for result in score_task(store, task=task, outcome=outcome).assertion_results
        if result.name.startswith(_NEGATIVE_GUARD_NAME_PREFIXES)
    }
    assert guard_names, "the real corpus emits no negative guard; the check would be vacuous"
    for name in guard_names:
        assert not name.startswith(POSITIVE_ASSERTION_NAME_PREFIXES), name

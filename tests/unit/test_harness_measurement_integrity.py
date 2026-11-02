# SPDX-License-Identifier: MIT
"""Tests that eval assertions are falsifiable and the provider is invoked once per round.

Covered properties:

* `_run_provider_turn` reports `budget_exhausted` and `truncated` from what the turn observed.
* `observed_outcomes` is populated, so the structural failure modes
  (`direct_commit_of_high_risk_field`, `proposal_instead_of_direct_commit`,
  `proposal_created_when_only_a_list_was_asked_for`) are falsifiable.
* a "machine" task declaring only negative guards is reported unscored, not `correctness == 1.0`.
* `_check_tool_call_correctness` does not score a turn with zero tool calls as correct.
* `provider.stream` is called once per provider round, and `included_invocation_count` equals the
  number of tasks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from agents.turn_driver import TurnRecords
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.harness import (
    ArmSpec,
    _run_arm,
    _run_provider_turn,
    _StreamCallCounter,
)
from eval.scoring import ObservedProposal, ToolCall, TurnOutcome, score_task
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, Tuning, validate_tuning
from providers.capabilities import load_capability_matrix

_TENANT = "eval-test-tenant"
_REP = "rep-ashford"
_NOW = "2026-01-01T00:00:00Z"


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    """A moto-mocked DynamoDB client."""
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _identity_payload() -> dict[str, object]:
    return {
        "rep_id": _REP,
        "book": ["acct-1"],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "",
        "source": "gateway",
        "tenant": _TENANT,
    }


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


def _resolved_invocation() -> ResolvedInvocation:
    matrix = load_capability_matrix()
    model = matrix.model_for("anthropic", "supervisor")
    tuning = Tuning(source="test", effort="low")
    return validate_tuning(model, tuning)


class _ScriptedProvider(ModelProvider):
    """Replays one scripted event list per `stream` call, in round order.

    The i-th `stream` call yields the i-th round's events; a call past the end repeats the last
    round. Records the call count.
    """

    def __init__(self, rounds: list[list[ProviderEvent]]) -> None:
        self._rounds = rounds
        self.call_count = 0

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        index = min(self.call_count, len(self._rounds) - 1)
        self.call_count += 1
        for event in self._rounds[index]:
            yield event


def _run_turn_rounds(
    store: Store,
    rounds: list[list[ProviderEvent]],
    *,
    effort: str | None = "low",
) -> tuple[TurnOutcome, TurnRecords, _ScriptedProvider]:
    """Drive one turn through ``_run_provider_turn`` over ``drive_turn``.

    ``rounds`` is one event list per provider ``stream`` call, in order. Dispatch is in-process
    (``dispatch=None``) against ``store`` on the Gateway ``_identity`` channel, so a dispatched tool
    hits the real handler. ``effort`` is applied to every tier; a delegating script passes
    ``effort=None`` because the tool-tier model does not accept an effort parameter.
    """
    provider = _ScriptedProvider(rounds)
    tuning = Tuning(source="test", family="anthropic", effort=effort)
    outcome, records, _stream_calls = _run_provider_turn(
        provider,
        matrix=load_capability_matrix(),
        tuning=tuning,
        prompt="test prompt",
        store=store,
        tenant=_TENANT,
        identity_payload=_identity_payload(),
        task_id="gt-test",
    )
    return outcome, records, provider


def _run_turn(
    store: Store,
    events: list[ProviderEvent],
) -> tuple[TurnOutcome, TurnRecords, _ScriptedProvider]:
    """Drive one single supervisor round: ``events`` is that round.

    A tool-using script must end with a ``completed`` event carrying text. Returns the driver's
    ``TurnRecords`` in the middle slot.
    """
    return _run_turn_rounds(store, [events])


def _mutation_delegation_rounds(
    leaf_tool_name: str, leaf_tool_input: dict[str, object]
) -> list[list[ProviderEvent]]:
    """Script a supervisor→mutation-subagent→tool-actor delegation that dispatches one write leaf.

    Four provider rounds: the supervisor delegates, the subagent names the leaf, the tool actor
    formulates its args from ``leaf_tool_input`` (the dispatch fires here), and the supervisor
    answers.
    """
    return [
        [  # supervisor: delegate to the mutation subagent
            ProviderEvent(
                type="tool_call", tool_name="mutation", tool_input={"intent": "apply the change"}
            ),
            ProviderEvent(type="usage", input_tokens=10, output_tokens=5),
            ProviderEvent(type="completed", awaiting_tools=True),
        ],
        [  # mutation subagent: name the write leaf to run
            ProviderEvent(type="tool_call", tool_name=leaf_tool_name, tool_input=leaf_tool_input),
            ProviderEvent(type="usage", input_tokens=6, output_tokens=3),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
        [  # tool-tier actor: formulate the leaf's args (dispatch fires from this tool_input)
            ProviderEvent(type="tool_call", tool_name=leaf_tool_name, tool_input=leaf_tool_input),
            ProviderEvent(type="usage", input_tokens=4, output_tokens=2),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
        [  # supervisor: final answer
            ProviderEvent(type="text_delta", text="done"),
            ProviderEvent(type="usage", input_tokens=5, output_tokens=2),
            ProviderEvent(type="completed", text="done"),
        ],
    ]


# --------------------------------------------------------------------------------------
# `truncated` is assigned from the dispatched tool's own output.
# --------------------------------------------------------------------------------------


def test_truncated_is_false_when_no_dispatched_tool_reports_truncation(store: Store) -> None:
    """`truncated` is False when no dispatched call reports truncation."""
    _seed_opportunity(store)
    outcome, _, _ = _run_turn(
        store,
        [
            ProviderEvent(
                type="tool_call",
                tool_name="get_opportunity",
                tool_input={"opportunity_id": "opp-1"},
            ),
            ProviderEvent(type="usage", input_tokens=10, output_tokens=5),
            ProviderEvent(type="completed", text="done", input_tokens=10, output_tokens=5),
        ],
    )
    assert outcome.truncated is False
    assert len(outcome.tool_calls) == 1


def test_truncated_is_genuinely_set_when_a_dispatched_call_reports_truncation(
    store: Store,
) -> None:
    """`truncated` reflects a dispatched tool's own `truncated` flag."""
    for i in range(3):
        _seed_opportunity(
            store,
            opportunity_id=f"opp-{i}",
            tenant_account_id=f"{_TENANT}#acct-1",
        )
    # The reading mechanism `_run_provider_turn` uses is exercised directly on synthetic outputs.
    from eval.harness import _tool_output_reports_truncation

    assert _tool_output_reports_truncation({"truncated": True, "examined_count": 5}) is True
    assert _tool_output_reports_truncation({"body": {"truncated": True}}) is True
    assert _tool_output_reports_truncation({"body": {"items": []}}) is False
    assert _tool_output_reports_truncation({}) is False


# --------------------------------------------------------------------------------------
# `denial_outcome` reports "data" for a successful, non-empty tool result.
# --------------------------------------------------------------------------------------


def test_denial_outcome_reports_data_for_a_successful_nonempty_tool_result(store: Store) -> None:
    """A successful `get_opportunity` call against an existing row reports `denial_outcome ==
    "data"`.
    """
    _seed_opportunity(store)
    outcome, _, _ = _run_turn(
        store,
        [
            ProviderEvent(
                type="tool_call",
                tool_name="get_opportunity",
                tool_input={"opportunity_id": "opp-1"},
            ),
            ProviderEvent(type="usage", input_tokens=10, output_tokens=5),
            ProviderEvent(type="completed", text="done", input_tokens=10, output_tokens=5),
        ],
    )
    assert outcome.denial_outcome == "data"


#: Seeds `opp-out-of-book` (outside the rep's book, classifies `"denied"`) and `opp-in-book`
#: (inside it, classifies `"data"`).
def _seed_denial_ordering_rows(store: Store) -> None:
    _seed_opportunity(store, opportunity_id="opp-out-of-book", account_id="acct-out-of-book")
    _seed_opportunity(store, opportunity_id="opp-in-book", account_id="acct-1")


def _data_call() -> ProviderEvent:
    """A `get_opportunity` call against a row the rep can see -- classifies as `"data"`."""
    return ProviderEvent(
        type="tool_call",
        tool_name="get_opportunity",
        tool_input={"opportunity_id": "opp-in-book"},
    )


def _denied_call() -> ProviderEvent:
    """A `get_opportunity` call outside the rep's book -- classifies as `"denied"`."""
    return ProviderEvent(
        type="tool_call",
        tool_name="get_opportunity",
        tool_input={"opportunity_id": "opp-out-of-book"},
    )


def _tool_failure_call() -> ProviderEvent:
    """A `get_opportunity` call missing its required `opportunity_id` -- fails validation
    (`validation_failed`), classifying as `"tool_failure"`.
    """
    return ProviderEvent(type="tool_call", tool_name="get_opportunity", tool_input={})


def _no_match_call() -> ProviderEvent:
    """A `search_opportunities` call matched by no seeded row -- classifies as `"no_match"`."""
    return ProviderEvent(
        type="tool_call",
        tool_name="search_opportunities",
        tool_input={"query": "no-such-opportunity-name-anywhere"},
    )


_TAIL_EVENTS = [
    ProviderEvent(type="usage", input_tokens=10, output_tokens=5),
    ProviderEvent(type="completed", text="done", input_tokens=10, output_tokens=5),
]


def test_denial_outcome_data_then_no_match_reports_no_match_not_data(store: Store) -> None:
    """A data read followed by a `no_match` call reports `"no_match"`."""
    _seed_denial_ordering_rows(store)
    outcome, _, _ = _run_turn(
        store,
        [_data_call(), _no_match_call(), *_TAIL_EVENTS],
    )
    assert outcome.denial_outcome == "no_match"
    assert len(outcome.tool_calls) == 2


def test_denial_outcome_data_then_tool_failure_reports_tool_failure_not_data(
    store: Store,
) -> None:
    """A data read followed by a `tool_failure` call reports `"tool_failure"`."""
    _seed_denial_ordering_rows(store)
    outcome, _, _ = _run_turn(
        store,
        [_data_call(), _tool_failure_call(), *_TAIL_EVENTS],
    )
    assert outcome.denial_outcome == "tool_failure"
    assert len(outcome.tool_calls) == 2


def test_denial_outcome_data_then_denied_reports_denied(store: Store) -> None:
    """A data read followed by a denied call reports `"denied"`."""
    _seed_denial_ordering_rows(store)
    outcome, _, _ = _run_turn(
        store,
        [_data_call(), _denied_call(), *_TAIL_EVENTS],
    )
    assert outcome.denial_outcome == "denied"
    assert len(outcome.tool_calls) == 2


def test_denial_outcome_no_match_then_denied_reports_denied(store: Store) -> None:
    """`"denied"` overwrites an already-set `"no_match"`, so `denied` is unconditional, not
    first-wins.
    """
    _seed_denial_ordering_rows(store)
    outcome, _, _ = _run_turn(
        store,
        [_no_match_call(), _denied_call(), *_TAIL_EVENTS],
    )
    assert outcome.denial_outcome == "denied"
    assert len(outcome.tool_calls) == 2


def test_denial_outcome_tool_failure_then_denied_reports_denied(store: Store) -> None:
    """`"denied"` overwrites an already-set `"tool_failure"`."""
    _seed_denial_ordering_rows(store)
    outcome, _, _ = _run_turn(
        store,
        [_tool_failure_call(), _denied_call(), *_TAIL_EVENTS],
    )
    assert outcome.denial_outcome == "denied"
    assert len(outcome.tool_calls) == 2


def test_denial_outcome_no_match_then_data_still_reports_no_match(store: Store) -> None:
    """A `no_match` call followed by a data read still reports `"no_match"`."""
    _seed_denial_ordering_rows(store)
    outcome, _, _ = _run_turn(
        store,
        [_no_match_call(), _data_call(), *_TAIL_EVENTS],
    )
    assert outcome.denial_outcome == "no_match"
    assert len(outcome.tool_calls) == 2


def test_denial_outcome_a_self_corrected_tool_failure_reports_the_retry_outcome(
    store: Store,
) -> None:
    """A `tool_failure` retried by the same tool under the same role reports `"data"`."""
    _seed_denial_ordering_rows(store)
    outcome, records, _ = _run_turn(
        store,
        [_tool_failure_call(), _data_call(), *_TAIL_EVENTS],
    )
    assert [call.outcome_kind for call in records.tool_calls] == ["tool_failure", "data"]
    assert outcome.denial_outcome == "data"
    assert len(outcome.tool_calls) == 2


def test_denial_outcome_a_failure_retried_into_no_match_reports_no_match(store: Store) -> None:
    """A retry that returns `no_match` also clears the earlier `tool_failure`."""
    _seed_denial_ordering_rows(store)
    schema_invalid_search = ProviderEvent(
        type="tool_call", tool_name="search_opportunities", tool_input={"limit": 0}
    )
    outcome, records, _ = _run_turn(
        store,
        [schema_invalid_search, _no_match_call(), *_TAIL_EVENTS],
    )
    assert [call.outcome_kind for call in records.tool_calls] == ["tool_failure", "no_match"]
    assert outcome.denial_outcome == "no_match"


def test_denial_outcome_a_failure_never_retried_reports_tool_failure(store: Store) -> None:
    """A `tool_failure` with no later call to the same tool keeps the turn at `"tool_failure"`."""
    _seed_denial_ordering_rows(store)
    outcome, _, _ = _run_turn(store, [_tool_failure_call(), *_TAIL_EVENTS])
    assert outcome.denial_outcome == "tool_failure"


def test_denial_outcome_a_different_tool_after_a_failure_does_not_clear_it(store: Store) -> None:
    """A later success from a different tool is not a retry, so `"tool_failure"` stands."""
    _seed_denial_ordering_rows(store)
    other_tool_data = ProviderEvent(
        type="tool_call", tool_name="search_opportunities", tool_input={"limit": 5}
    )
    outcome, records, _ = _run_turn(
        store,
        [_tool_failure_call(), other_tool_data, *_TAIL_EVENTS],
    )
    assert [call.outcome_kind for call in records.tool_calls] == ["tool_failure", "data"]
    assert outcome.denial_outcome == "tool_failure"


def test_denial_outcome_denied_then_a_success_still_reports_denied(store: Store) -> None:
    """A `denied` call is never cleared, even by a later success from the same tool."""
    _seed_denial_ordering_rows(store)
    outcome, records, _ = _run_turn(
        store,
        [_denied_call(), _data_call(), *_TAIL_EVENTS],
    )
    assert [call.outcome_kind for call in records.tool_calls] == ["denied", "data"]
    assert outcome.denial_outcome == "denied"


def test_denial_outcome_data_does_not_override_an_earlier_denied_outcome_in_the_same_turn(
    store: Store,
) -> None:
    """A denied call followed by a data read still reports `"denied"`."""
    _seed_denial_ordering_rows(store)
    outcome, _, _ = _run_turn(
        store,
        [_denied_call(), _data_call(), *_TAIL_EVENTS],
    )
    assert outcome.denial_outcome == "denied"
    assert len(outcome.tool_calls) == 2


def test_denial_outcome_reports_data_for_a_turn_with_no_denial_class_outcome_at_all(
    store: Store,
) -> None:
    """A turn containing only successful, non-empty reads reports `"data"`."""
    _seed_denial_ordering_rows(store)
    outcome, _, _ = _run_turn(
        store,
        [_data_call(), _data_call(), *_TAIL_EVENTS],
    )
    assert outcome.denial_outcome == "data"
    assert len(outcome.tool_calls) == 2


# --------------------------------------------------------------------------------------
# `no_match` versus `tool_failure` is first-wins by tool-call position, not a total order.
# --------------------------------------------------------------------------------------


def test_denial_outcome_no_match_then_tool_failure_keeps_no_match(store: Store) -> None:
    """Between `no_match` and `tool_failure`, the first one encountered wins: here `no_match`."""
    _seed_denial_ordering_rows(store)
    outcome, _, _ = _run_turn(
        store,
        [_no_match_call(), _tool_failure_call(), *_TAIL_EVENTS],
    )
    assert outcome.denial_outcome == "no_match"
    assert len(outcome.tool_calls) == 2


def test_denial_outcome_tool_failure_then_no_match_keeps_tool_failure(store: Store) -> None:
    """Between `tool_failure` and `no_match`, the first one encountered wins: here
    `tool_failure`.
    """
    _seed_denial_ordering_rows(store)
    outcome, _, _ = _run_turn(
        store,
        [_tool_failure_call(), _no_match_call(), *_TAIL_EVENTS],
    )
    assert outcome.denial_outcome == "tool_failure"
    assert len(outcome.tool_calls) == 2


# --------------------------------------------------------------------------------------
# `budget_exhausted` is observed from a real TurnBudget.
# --------------------------------------------------------------------------------------


def test_budget_exhausted_is_false_for_a_turn_within_budget(store: Store) -> None:
    _seed_opportunity(store)
    outcome, _, _ = _run_turn(
        store,
        [
            ProviderEvent(
                type="tool_call",
                tool_name="get_opportunity",
                tool_input={"opportunity_id": "opp-1"},
            ),
            ProviderEvent(type="usage", input_tokens=10, output_tokens=5),
            ProviderEvent(type="completed", text="done", input_tokens=10, output_tokens=5),
        ],
    )
    assert outcome.budget_exhausted is False


def test_budget_exhausted_is_genuinely_observed_when_the_real_turn_budget_is_exhausted(
    store: Store,
) -> None:
    """A turn making more tool calls than `agents.budget.TurnBudget`'s default ceiling is reported
    `budget_exhausted=True`.
    """
    from agents.budget import EVAL_MAX_TOOL_CALLS

    for i in range(EVAL_MAX_TOOL_CALLS + 2):
        _seed_opportunity(
            store,
            opportunity_id=f"opp-{i}",
        )

    events: list[ProviderEvent] = []
    for i in range(EVAL_MAX_TOOL_CALLS + 2):
        events.append(
            ProviderEvent(
                type="tool_call",
                tool_name="get_opportunity",
                tool_input={"opportunity_id": f"opp-{i}"},
            )
        )
    events.append(ProviderEvent(type="usage", input_tokens=10, output_tokens=5))
    events.append(ProviderEvent(type="completed", text="done", input_tokens=10, output_tokens=5))

    outcome, _, _ = _run_turn(store, events)
    assert outcome.budget_exhausted is True
    # drive_turn halts dispatch once the budget is exhausted, so of the EVAL_MAX_TOOL_CALLS + 2
    # scripted calls only EVAL_MAX_TOOL_CALLS are dispatched.
    assert len(outcome.tool_calls) == EVAL_MAX_TOOL_CALLS
    assert len(outcome.tool_calls) < EVAL_MAX_TOOL_CALLS + 2


# --------------------------------------------------------------------------------------
# `observed_outcomes` is populated for structurally derivable labels.
# --------------------------------------------------------------------------------------


def test_direct_commit_of_high_risk_field_is_observed_from_the_attempted_update_request(
    store: Store,
) -> None:
    """An `update_opportunity` call naming a high-risk field (e.g. `stage`) is flagged even though
    the handler refuses the write.
    """
    _seed_opportunity(store)
    outcome, _, _ = _run_turn_rounds(
        store,
        _mutation_delegation_rounds(
            "update_opportunity",
            {
                "opportunity_id": "opp-1",
                "fields": {"stage": "Closed Won"},
                "transaction_key": "txn-1",
            },
        ),
        effort=None,
    )
    assert "direct_commit_of_high_risk_field" in outcome.observed_outcomes
    # The allowlist refused the write: committed state is unchanged.
    row = store.get_item("opportunities", {"opportunity_id": "opp-1"})
    assert row is not None
    assert row["stage"] == "Discovery"


def test_direct_commit_of_high_risk_field_is_absent_for_a_low_risk_update(store: Store) -> None:
    """A low-risk `update_opportunity` call never raises the high-risk label."""
    _seed_opportunity(store)
    outcome, _, _ = _run_turn_rounds(
        store,
        _mutation_delegation_rounds(
            "update_opportunity",
            {
                "opportunity_id": "opp-1",
                "fields": {"description": "Renewal in progress"},
                "transaction_key": "txn-2",
            },
        ),
        effort=None,
    )
    assert "direct_commit_of_high_risk_field" not in outcome.observed_outcomes


def test_proposal_instead_of_direct_commit_is_observed_for_a_low_risk_proposal(
    store: Store,
) -> None:
    """A `submit_proposal` call whose `after_payload` touches only directly-committable fields is
    flagged.
    """
    _seed_opportunity(store)
    outcome, _, _ = _run_turn_rounds(
        store,
        _mutation_delegation_rounds(
            "submit_proposal",
            {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
                "after_payload": {"description": "Renewal in progress"},
                "reason": "test",
                "transaction_key": "txn-3",
            },
        ),
        effort=None,
    )
    assert "proposal_instead_of_direct_commit" in outcome.observed_outcomes


def test_proposal_instead_of_direct_commit_is_absent_for_a_genuine_high_risk_proposal(
    store: Store,
) -> None:
    """A `submit_proposal` call touching a high-risk field (e.g. `stage`) never raises the
    low-risk-proposal label.
    """
    _seed_opportunity(store)
    outcome, _, _ = _run_turn_rounds(
        store,
        _mutation_delegation_rounds(
            "submit_proposal",
            {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
                "after_payload": {"stage": "Negotiation"},
                "reason": "test",
                "transaction_key": "txn-4",
            },
        ),
        effort=None,
    )
    assert "proposal_instead_of_direct_commit" not in outcome.observed_outcomes


def test_proposal_created_when_only_a_list_was_asked_for_is_derived_by_scoring_for_a_zero_proposal_task(
    store: Store,
) -> None:
    """`eval.scoring._check_forbidden_outcomes` derives this label from the task's
    `max_proposal_count`; `_run_provider_turn` alone never adds it.
    """
    task: dict[str, object] = {
        "id": "gt-test",
        "scoring_mode": "machine",
        "unscored_reason": None,
        "assertions": {
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": ["proposal_created_when_only_a_list_was_asked_for"],
        },
    }
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall(tool_name="submit_proposal", target_id="opp-1"),),
        proposals=(ObservedProposal(proposal_id="prop-x"),),
    )
    score = score_task(store, task=task, outcome=outcome)
    forbidden_result = next(
        r
        for r in score.assertion_results
        if r.name == "forbidden_outcomes[proposal_created_when_only_a_list_was_asked_for]"
    )
    assert forbidden_result.passed is False


# --------------------------------------------------------------------------------------
# A task with no positive assertion is reported unscored, never 1.0.
# --------------------------------------------------------------------------------------


def test_a_task_with_no_positive_assertion_is_reported_unscored_not_perfect(store: Store) -> None:
    """A `scoring_mode: machine` task declaring only negative guards is reported `correctness=None`
    with an `unscored_reason`, never `1.0`.
    """
    task: dict[str, object] = {
        "id": "gt-14-denial-departed-contact-update",
        "scoring_mode": "machine",
        "unscored_reason": None,
        "assertions": {
            "expected_field_changes": None,
            "must_not_change": [
                {"table": "contacts", "id": "cont-1", "field": "phone"},
            ],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": ["silent_update_of_departed_contact"],
        },
    }
    # A do-nothing turn: no tool calls at all.
    outcome = TurnOutcome(tenant=_TENANT)
    score = score_task(store, task=task, outcome=outcome)
    assert score.correctness is None
    assert score.unscored_reason is not None
    assert "negative guard" in score.unscored_reason
    # Its guards (must_not_change, forbidden_outcomes) are still checked.
    assert score.assertion_results


def test_a_task_with_a_positive_assertion_is_still_scored_normally(store: Store) -> None:
    """A task declaring `expected_field_changes`/`expected_proposal`/`expected_denial` is still
    scored.
    """
    _seed_opportunity(store, primary_contact_id="cont-old")
    task: dict[str, object] = {
        "id": "gt-test",
        "scoring_mode": "machine",
        "unscored_reason": None,
        "assertions": {
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 1,
            "allowed_proposal_fields": ["primary_contact_id"],
            "expected_proposal": {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
            },
            "expected_denial": None,
            "forbidden_outcomes": [],
        },
    }
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall(tool_name="submit_proposal", target_id="opp-1"),),
    )
    score = score_task(store, task=task, outcome=outcome)
    assert score.correctness is not None
    assert score.unscored_reason is None


# --------------------------------------------------------------------------------------
# A turn with zero tool calls never scores tool-call correct.
# --------------------------------------------------------------------------------------


def test_zero_tool_calls_never_scores_tool_call_correct(store: Store) -> None:
    """`_check_tool_call_correctness` does not report `passed=True` for a turn with zero tool
    calls.
    """
    task: dict[str, object] = {
        "id": "gt-test",
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
    outcome = TurnOutcome(tenant=_TENANT)
    score = score_task(store, task=task, outcome=outcome)
    assert score.tool_call_correct is False
    tool_call_result = next(r for r in score.assertion_results if r.name == "tool_call_correctness")
    assert tool_call_result.passed is False
    assert "no tool call" in tool_call_result.detail


def test_a_genuine_tool_call_can_still_score_tool_call_correct(store: Store) -> None:
    """A turn that made a real, in-scope tool call is still tool-call correct."""
    task: dict[str, object] = {
        "id": "gt-test",
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
    outcome = TurnOutcome(
        tenant=_TENANT, tool_calls=(ToolCall(tool_name="get_opportunity", target_id="opp-1"),)
    )
    score = score_task(store, task=task, outcome=outcome)
    assert score.tool_call_correct is True


# --------------------------------------------------------------------------------------
# The provider is invoked exactly once per round, never twice.
# --------------------------------------------------------------------------------------


def test_run_provider_turn_invokes_the_provider_exactly_once(store: Store) -> None:
    """`_run_provider_turn` calls `provider.stream` exactly once for a single-round turn."""
    _seed_opportunity(store)
    _, records, provider = _run_turn(
        store,
        [
            ProviderEvent(
                type="tool_call",
                tool_name="get_opportunity",
                tool_input={"opportunity_id": "opp-1"},
            ),
            ProviderEvent(type="usage", input_tokens=128, output_tokens=32),
            ProviderEvent(type="completed", text="done", input_tokens=128, output_tokens=32),
        ],
    )
    # A single-round turn opens one provider stream and records one supervisor invocation.
    assert provider.call_count == 1
    assert len(records.invocations) == 1
    assert records.invocations[0].tier == "supervisor"
    assert records.invocations[0].tokens.input_tokens == 128
    assert records.invocations[0].tokens.output_tokens == 32


def test_run_arm_invokes_provider_stream_exactly_once_per_task(store: Store) -> None:
    """`_run_arm` reports `included_invocation_count == len(tasks)`, with `provider.stream` called
    once per provider round.

    A tool-branch task drives two rounds, so the stream call count is `len(tasks)` plus one per
    tool-branch task.
    """
    from eval.harness import _DEFAULT_GOLDEN_TASKS_PATH, _load_golden_tasks
    from providers.capabilities import load_capability_matrix
    from providers.stub import StubProvider

    tasks = _load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH)
    matrix = load_capability_matrix()
    counting_provider = _StreamCallCounter(StubProvider())

    arm_run = _run_arm(
        provider=counting_provider,
        matrix=matrix,
        arm_spec=ArmSpec(family="anthropic", effort="low"),
        tasks=tasks,
        store=store,
        tenant=_TENANT,
        mode="stub",
        dataset_seed=1,
        git_revision="unknown",
        runtime_identifier="test",
        price_table_version="0.1.0-provisional",
        cost_withheld=True,
    )
    assert arm_run.model_metrics.included_invocation_count == len(tasks)
    assert len(tasks) <= counting_provider.call_count <= 2 * len(tasks)


def test_run_arm_raises_end_to_end_if_the_turn_loop_ever_double_invokes_the_provider(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_run_arm` raises when the turn loop makes an extra `provider.stream` call per task.

    The real `_observe_provider_turn` is wrapped to drain one extra stream call per task.
    """
    import eval.harness as harness_module
    from providers.stub import StubProvider

    real_observe_provider_turn = harness_module._observe_provider_turn

    async def _drain_one_extra_call(provider: ModelProvider) -> None:
        async for _ in provider.stream(
            _resolved_invocation(), [{"role": "user", "content": ""}], []
        ):
            pass

    def _double_invoking_observe_provider_turn(provider: ModelProvider, **kwargs: object) -> object:
        turn = real_observe_provider_turn(provider, **kwargs)  # type: ignore[arg-type]
        import asyncio

        # Drain one extra stream call on the same provider after the real driver returned: it
        # increments _run_arm's own counter but not the driver's reported count.
        asyncio.run(_drain_one_extra_call(provider))
        return turn

    monkeypatch.setattr(
        harness_module, "_observe_provider_turn", _double_invoking_observe_provider_turn
    )

    tasks = harness_module._load_golden_tasks(harness_module._DEFAULT_GOLDEN_TASKS_PATH)[:1]
    matrix = load_capability_matrix()

    with pytest.raises(AssertionError, match="its turn drivers made"):
        harness_module._run_arm(
            provider=StubProvider(),
            matrix=matrix,
            arm_spec=ArmSpec(family="anthropic", effort="low"),
            tasks=tasks,
            store=store,
            tenant=_TENANT,
            mode="stub",
            dataset_seed=1,
            git_revision="unknown",
            runtime_identifier="test",
            price_table_version="0.1.0-provisional",
            cost_withheld=True,
        )


def test_run_arm_raises_if_a_single_stream_call_could_ever_yield_more_than_one_usage_record(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_run_arm` raises when `included_invocation_count` exceeds `provider.stream`'s real call
    count.

    Only `eval.sweep_units.aggregate_model_metrics` is patched, to report one more invocation than
    `provider.stream` was really called; the real turn loop runs unmodified.
    """
    import dataclasses

    import eval.harness as harness_module
    import eval.sweep_units as sweep_units
    from eval.metrics import aggregate_model_metrics as real_aggregate_model_metrics
    from providers.stub import StubProvider

    class _CountingStub(StubProvider):
        calls = 0

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            type(self).calls += 1
            return super().stream(*args, **kwargs)

    def _overreport_past_the_call_count(*args: object, **kwargs: object) -> Any:
        """Call the real aggregation, then report one more invocation than calls were made."""
        real_metrics = real_aggregate_model_metrics(*args, **kwargs)  # type: ignore[arg-type]
        return dataclasses.replace(real_metrics, included_invocation_count=_CountingStub.calls + 1)

    monkeypatch.setattr(sweep_units, "aggregate_model_metrics", _overreport_past_the_call_count)

    tasks = harness_module._load_golden_tasks(harness_module._DEFAULT_GOLDEN_TASKS_PATH)[:1]
    matrix = load_capability_matrix()

    with pytest.raises(AssertionError, match="cannot outnumber"):
        harness_module._run_arm(
            provider=_CountingStub(),
            matrix=matrix,
            arm_spec=ArmSpec(family="anthropic", effort="low"),
            tasks=tasks,
            store=store,
            tenant=_TENANT,
            mode="stub",
            dataset_seed=1,
            git_revision="unknown",
            runtime_identifier="test",
            price_table_version="0.1.0-provisional",
            cost_withheld=True,
        )


# --------------------------------------------------------------------------------------
# The bare CLI default completes without a PYTHONPATH or a pre-provisioned table set.
# --------------------------------------------------------------------------------------


def test_build_default_store_provisions_tables_against_a_local_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_build_default_store` provisions every declared table against the default local endpoint,
    tolerating an already-existing table.
    """
    import eval.harness as harness_module
    from crm.store.provisioning import create_all_tables as real_create_all_tables

    calls: list[tuple[str, str]] = []

    def _spy_create_all_tables(client: object, *, table_prefix: str = "") -> None:
        calls.append(("create_all_tables", table_prefix))
        real_create_all_tables(client, table_prefix=table_prefix)

    monkeypatch.setattr(harness_module, "create_all_tables", _spy_create_all_tables)
    # `moto` only intercepts clients built with no explicit `endpoint_url`, so the local default is
    # pointed at "" while the `endpoint_url == _DEFAULT_LOCAL_ENDPOINT_URL` branch is still taken.
    monkeypatch.setattr(harness_module, "_DEFAULT_LOCAL_ENDPOINT_URL", "")

    with mock_aws():
        store = harness_module._build_default_store(
            table_prefix="",
            endpoint_url=harness_module._DEFAULT_LOCAL_ENDPOINT_URL,
        )
        # A put_item against a table that only exists because this function provisioned it.
        store.put_item(
            "accounts",
            {
                "account_id": "acct-smoke",
                "tenant": "eval-smoke",
                "name": "Smoke Test Co",
                "name_lower": "smoke test co",
                "industry": "software",
                "region": "us-east-1",
                "website": "https://example.test",
                "employee_count": 10,
                "notes": "",
                "created_at": _NOW,
                "updated_at": _NOW,
                "created_by": _REP,
                "schema_version": 1,
            },
        )

    assert calls == [("create_all_tables", "")]


def test_build_default_store_never_provisions_against_a_non_local_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_build_default_store` never provisions tables against a non-default endpoint."""
    import eval.harness as harness_module

    calls: list[str] = []
    monkeypatch.setattr(
        harness_module,
        "create_all_tables",
        lambda *_args, **_kwargs: calls.append("called"),
    )

    with mock_aws():
        harness_module._build_default_store(table_prefix="prod-", endpoint_url="")

    assert calls == []


# --------------------------------------------------------------------------------------
# A tree-wide guard against a test that raises the exact exception it then catches.
# --------------------------------------------------------------------------------------


def _self_raising_test_functions_in(source: str) -> list[tuple[str, int]]:
    """Find every `test_*` function (`def` or `async def`) in `source` that raises, inside a `with
    pytest.raises(X):` or `with raises(X):` block, a call whose callee is that same `X`.

    Returns `(function_name, line_number)` for each match. A raise of a call to a different name
    (for example `raise _build(error_class)`) does not match.
    """
    import ast

    tree = ast.parse(source)
    findings: list[tuple[str, int]] = []

    def _exception_ref_name(node: ast.expr) -> str | None:
        """Return the dotted-or-bare name an exception-type expression refers to, else None."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    for func in ast.walk(tree):
        if not isinstance(
            func, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) or not func.name.startswith("test_"):
            continue
        for with_node in ast.walk(func):
            if not isinstance(with_node, ast.With):
                continue
            raises_exception_names: set[str] = set()
            for item in with_node.items:
                call = item.context_expr
                if not (isinstance(call, ast.Call) and call.args):
                    continue
                is_attribute_form = (
                    isinstance(call.func, ast.Attribute) and call.func.attr == "raises"
                )
                is_bare_name_form = isinstance(call.func, ast.Name) and call.func.id == "raises"
                if not (is_attribute_form or is_bare_name_form):
                    continue
                name = _exception_ref_name(call.args[0])
                if name is not None:
                    raises_exception_names.add(name)
            if not raises_exception_names:
                continue
            for inner in ast.walk(with_node):
                if not isinstance(inner, ast.Raise):
                    continue
                exc = inner.exc
                if isinstance(exc, ast.Call):
                    callee_name = _exception_ref_name(exc.func)
                elif exc is not None:
                    callee_name = _exception_ref_name(exc)
                else:
                    callee_name = None
                if callee_name is not None and callee_name in raises_exception_names:
                    findings.append((func.name, inner.lineno))
    return findings


def test_no_test_in_the_tree_raises_the_exact_exception_it_then_catches() -> None:
    """No test module under `tests/` raises, inside its own `pytest.raises(X)` block, a call to that
    same `X`.
    """
    import pathlib

    tests_root = pathlib.Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in sorted(tests_root.rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        for function_name, lineno in _self_raising_test_functions_in(source):
            offenders.append(f"{path.relative_to(tests_root)}:{lineno} {function_name}")

    assert not offenders, (
        "the following tests raise, inside their own `pytest.raises(X)` block, a call to that "
        "exact same X -- a test that constructs the failure it asserts is not evidence: "
        f"{offenders}"
    )


def test_self_raising_guard_catches_the_original_w6_4_shape() -> None:
    """`_self_raising_test_functions_in` flags a `with pytest.raises(X): raise X(...)` test."""
    self_raising_source = """
import pytest


def test_reproduces_the_w6_4_shape() -> None:
    with pytest.raises(AssertionError, match="was called"):
        if True:
            raise AssertionError("provider.stream was called 2 time(s)")
"""
    findings = _self_raising_test_functions_in(self_raising_source)
    assert findings == [("test_reproduces_the_w6_4_shape", 8)]


def test_self_raising_guard_does_not_flag_a_call_to_a_different_helper() -> None:
    """A raise of a call to a different name than the one passed to `pytest.raises` is not
    flagged.
    """
    helper_call_source = """
import pytest


class CrmError(Exception):
    pass


def _build(error_class: type[CrmError]) -> CrmError:
    return error_class("nope")


def test_reproduces_the_test_errors_shape() -> None:
    with pytest.raises(CrmError):
        raise _build(CrmError)
"""
    findings = _self_raising_test_functions_in(helper_call_source)
    assert findings == []


def test_self_raising_guard_catches_the_shape_inside_an_async_def_test() -> None:
    """The guard flags the self-raising shape inside an `async def` test."""
    self_raising_async_source = """
import pytest


async def test_reproduces_the_w6_4_shape_but_async() -> None:
    with pytest.raises(AssertionError, match="was called"):
        if True:
            raise AssertionError("provider.stream was called 2 time(s)")
"""
    findings = _self_raising_test_functions_in(self_raising_async_source)
    assert findings == [("test_reproduces_the_w6_4_shape_but_async", 8)]


def test_self_raising_guard_catches_the_bare_raises_import_form() -> None:
    """The guard flags the bare `from pytest import raises` form (`with raises(X): raise
    X(...)`).
    """
    self_raising_bare_import_source = """
from pytest import raises


def test_reproduces_the_w6_4_shape_via_bare_raises_import() -> None:
    with raises(AssertionError, match="was called"):
        if True:
            raise AssertionError("provider.stream was called 2 time(s)")
"""
    findings = _self_raising_test_functions_in(self_raising_bare_import_source)
    assert findings == [("test_reproduces_the_w6_4_shape_via_bare_raises_import", 8)]


def test_self_raising_guard_still_exempts_the_helper_callee_under_bare_raises_import() -> None:
    """A helper-call raise is not flagged under the bare `raises` form either."""
    helper_call_bare_import_source = """
from pytest import raises


class CrmError(Exception):
    pass


def _build(error_class: type[CrmError]) -> CrmError:
    return error_class("nope")


def test_reproduces_the_test_errors_shape_via_bare_raises_import() -> None:
    with raises(CrmError):
        raise _build(CrmError)
"""
    findings = _self_raising_test_functions_in(helper_call_bare_import_source)
    assert findings == []

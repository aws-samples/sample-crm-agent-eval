# SPDX-License-Identifier: MIT
"""Tests that every declared `forbidden_outcomes` label in `golden_tasks.yaml` is dispositioned.

Each label that `eval.scoring._check_forbidden_outcomes` can observe is constructed as the exact
failure it names and shown to fail. Each label naming a property of the agent's prose reply, which
no `TurnOutcome` field captures, is shown to be reported as an explicitly unscored assertion.

Falsifiable (14): data, no_match, tool_failure, denied, direct_commit_of_high_risk_field,
proposal_instead_of_direct_commit, proposal_created_when_only_a_list_was_asked_for,
proposes_stage_change, split_into_two_proposals, wrong_contact_mutated, last_touch_not_updated,
silent_update_of_departed_contact, fabricated_contact_name, silent_truncation.

Explicitly unscored (5): wrong_value_reported, incomplete_contact_list,
includes_out_of_book_accounts, includes_closed_stages_as_open,
partial_comparison_presented_as_complete.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

import eval.scoring as scoring_module
from crm.proposals import submit_proposal
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.harness import _run_provider_turn
from eval.scoring import (
    ObservedProposal,
    ToolCall,
    TurnOutcome,
    score_task,
)
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, Tuning, validate_tuning
from providers.capabilities import load_capability_matrix

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "eval-test-tenant"
_REP = "rep-ashford"
_NOW = "2026-01-01T00:00:00Z"

#: The content-only (explicitly unscored) labels, read from the shipped module.
_CONTENT_ONLY_FORBIDDEN_OUTCOMES = scoring_module._CONTENT_ONLY_FORBIDDEN_OUTCOMES

#: The labels `_check_forbidden_outcomes` and the harness can structurally mark observed; an
#: independent registry, not derived from the declared labels.
_STRUCTURALLY_FALSIFIABLE_FORBIDDEN_OUTCOMES = (
    scoring_module._STRUCTURALLY_FALSIFIABLE_FORBIDDEN_OUTCOMES
)


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
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


def _machine_task(
    forbidden_outcomes: list[str],
    *,
    assertions: dict[str, object] | None = None,
    **overrides: object,
) -> dict[str, object]:
    base_assertions: dict[str, object] = {
        "expected_field_changes": None,
        "must_not_change": [],
        "max_proposal_count": 1,
        "allowed_proposal_fields": ["stage", "value", "close_date", "primary_contact_id"],
        "expected_proposal": None,
        "expected_denial": None,
        "forbidden_outcomes": forbidden_outcomes,
    }
    if assertions:
        base_assertions.update(assertions)
    task: dict[str, object] = {
        "id": "gt-test",
        "category": "enrichment",
        "derived_from": "low-risk write commits directly",
        "prompt": "test prompt",
        "actor": {"rep_id": _REP, "book": ["acct-1"], "full_visibility": False},
        "scoring_mode": "machine",
        "unscored_reason": None,
        "assertions": base_assertions,
    }
    task.update(overrides)
    return task


# --------------------------------------------------------------------------------------------
# Every declared label is either falsifiable or explicitly unscored
# --------------------------------------------------------------------------------------------


def test_every_declared_forbidden_outcomes_label_is_dispositioned() -> None:
    """Every label declared in golden_tasks.yaml is in one of the two disposition registries."""
    from pathlib import Path

    golden_tasks_path = Path(__file__).resolve().parents[2] / "src" / "eval" / "golden_tasks.yaml"
    data = yaml.safe_load(golden_tasks_path.read_text(encoding="utf-8"))
    all_labels: set[str] = set()
    total_instances = 0
    for task in data["tasks"]:
        labels = task["assertions"].get("forbidden_outcomes") or []
        all_labels.update(labels)
        total_instances += len(labels)

    assert total_instances == 37, "golden_tasks.yaml's forbidden_outcomes instance count changed"
    assert len(all_labels) == 20, "golden_tasks.yaml's unique forbidden_outcomes label set changed"

    dispositioned = _STRUCTURALLY_FALSIFIABLE_FORBIDDEN_OUTCOMES | _CONTENT_ONLY_FORBIDDEN_OUTCOMES
    undispositioned = all_labels - dispositioned
    assert not undispositioned, (
        f"label(s) {sorted(undispositioned)!r} are declared in golden_tasks.yaml but are neither "
        "in _STRUCTURALLY_FALSIFIABLE_FORBIDDEN_OUTCOMES nor _CONTENT_ONLY_FORBIDDEN_OUTCOMES -- "
        "no code path can ever mark this label observed, so a task declaring it always renders "
        "passed=True with no assertion capable of catching a real occurrence."
    )

    falsifiable_labels = all_labels & _STRUCTURALLY_FALSIFIABLE_FORBIDDEN_OUTCOMES
    assert len(falsifiable_labels) == 15
    assert len(_CONTENT_ONLY_FORBIDDEN_OUTCOMES & all_labels) == 5


def test_an_underived_label_declared_in_golden_tasks_is_caught_by_the_dispositioned_check() -> None:
    """A label in neither registry fails the membership check; the set identity still passes."""
    planted_label = "brand_new_unimplemented_label"
    all_labels = {"data", "no_match", planted_label}

    # The set identity: true for every input.
    falsifiable_labels = all_labels - _CONTENT_ONLY_FORBIDDEN_OUTCOMES
    assert all_labels == falsifiable_labels | (_CONTENT_ONLY_FORBIDDEN_OUTCOMES & all_labels), (
        "the old tautology holds for every input by construction -- this is the defect, not the "
        "fix; it demonstrates why a subtraction-derived identity cannot catch an underived label"
    )

    # The registry membership check catches the planted label.
    dispositioned = _STRUCTURALLY_FALSIFIABLE_FORBIDDEN_OUTCOMES | _CONTENT_ONLY_FORBIDDEN_OUTCOMES
    undispositioned = all_labels - dispositioned
    assert undispositioned == {planted_label}, (
        "the real registry-membership check must catch a label that names neither a content-only "
        "property nor a structural derivation"
    )


# --------------------------------------------------------------------------------------------
# The falsifiable registry is derived by AST from the code that emits each label
# --------------------------------------------------------------------------------------------


def test_registry_derivation_reads_a_new_observed_add_literal_without_being_hand_edited() -> None:
    """`_string_literal_args_of_calls` picks up a newly added `observed.add("...")` literal."""
    synthetic_source = (
        "def _check_forbidden_outcomes():\n"
        "    observed = set()\n"
        "    observed.add('a_freshly_added_label')\n"
        "    observed.add('another_label')\n"
    )
    literals = scoring_module._string_literal_args_of_calls(
        synthetic_source, receiver="observed", method="add"
    )
    assert literals == frozenset({"a_freshly_added_label", "another_label"})


def test_registry_derivation_ignores_add_calls_on_a_different_receiver_or_method() -> None:
    """Only `observed.add(...)` calls are collected, not other `.add` calls or other methods."""
    synthetic_source = (
        "def f():\n"
        "    other_set.add('not_a_forbidden_outcome_label')\n"
        "    observed.discard('also_not_picked_up')\n"
        "    observed.add(some_variable)\n"  # non-literal argument -- must be skipped, not raise
        "    observed.add()\n"  # no argument at all -- must be skipped, not raise
    )
    literals = scoring_module._string_literal_args_of_calls(
        synthetic_source, receiver="observed", method="add"
    )
    assert literals == frozenset()


def test_registry_derivation_reads_new_denial_outcome_assignments_from_harness_source() -> None:
    """`_string_literal_assignments_to` collects literal assignments and ignores non-literal or
    multi-target ones.
    """
    synthetic_source = (
        "def _run_provider_turn():\n"
        "    denial_outcome = 'denied'\n"
        "    denial_outcome = 'a_new_denial_kind'\n"
        "    denial_outcome = some_function_call()\n"  # non-literal -- must be skipped
        "    x = denial_outcome = 'multi_target_should_be_skipped'\n"  # 2 targets -- must be skipped
        "    other_name = 'not_denial_outcome'\n"
    )
    literals = scoring_module._string_literal_assignments_to(
        synthetic_source, target="denial_outcome"
    )
    assert literals == frozenset({"denied", "a_new_denial_kind"})


#: The registry as derived before the observation code moved out of `eval.harness`.
_PRE_LIFT_REGISTRY: frozenset[str] = frozenset(
    {
        "data",
        "denied",
        "direct_commit_of_high_risk_field",
        "fabricated_contact_name",
        "last_touch_not_updated",
        "no_match",
        "proposal_created_when_only_a_list_was_asked_for",
        "proposal_instead_of_direct_commit",
        "proposes_stage_change",
        "silent_truncation",
        "silent_update_of_departed_contact",
        "split_into_two_proposals",
        "tool_failure",
        "write_attempted",
        "wrong_contact_mutated",
    }
)


def test_the_bound_registry_is_the_union_of_both_sources_read_from_real_disk_files() -> None:
    """The falsifiable registry equals a fresh AST derivation from the real source files."""
    import inspect
    from pathlib import Path

    check_forbidden_outcomes_source = inspect.getsource(scoring_module._check_forbidden_outcomes)
    observation_source = (
        Path(scoring_module.__file__)
        .resolve()
        .with_name("turn_observation.py")
        .read_text(encoding="utf-8")
    )

    expected = (
        scoring_module._string_literal_args_of_calls(
            check_forbidden_outcomes_source, receiver="observed", method="add"
        )
        | scoring_module._string_literal_assignments_to(observation_source, target="denial_outcome")
        | scoring_module._string_literal_args_of_calls(
            observation_source, receiver="observed_outcomes", method="add"
        )
    )
    assert expected == _STRUCTURALLY_FALSIFIABLE_FORBIDDEN_OUTCOMES


def test_the_registry_is_exactly_the_pre_lift_set() -> None:
    """Moving the observer out of the harness neither dropped nor added a label."""
    assert _STRUCTURALLY_FALSIFIABLE_FORBIDDEN_OUTCOMES == _PRE_LIFT_REGISTRY


def test_the_derivation_fails_loudly_on_a_missing_observation_module(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="missing"):
        scoring_module._derive_structurally_falsifiable_forbidden_outcomes(tmp_path / "absent.py")


@pytest.mark.parametrize(
    "source",
    [
        "observed_outcomes.add('x')\n",
        "denial_outcome = 'denied'\n",
        "",
    ],
    ids=["no-denial-assignments", "no-outcome-labels", "empty"],
)
def test_the_derivation_fails_loudly_when_a_half_yields_nothing(
    tmp_path: Path, source: str
) -> None:
    module = tmp_path / "turn_observation.py"
    module.write_text(source, encoding="utf-8")
    with pytest.raises(RuntimeError, match="no "):
        scoring_module._derive_structurally_falsifiable_forbidden_outcomes(module)


def test_a_planted_underived_label_renders_as_a_silent_pass_end_to_end(store: Store) -> None:
    """An underived label renders `passed=True, unscored=False` whatever the turn did."""
    planted_label = "brand_new_unimplemented_label"
    assert planted_label not in _STRUCTURALLY_FALSIFIABLE_FORBIDDEN_OUTCOMES
    assert planted_label not in _CONTENT_ONLY_FORBIDDEN_OUTCOMES

    _seed_opportunity(store, primary_contact_id="cont-real")
    _seed_contact(store, contact_id="cont-real")
    task = _machine_task(
        [planted_label],
        assertions={
            "expected_denial": None,
            "expected_proposal": {
                "target_table": "opportunities",
                "target_id": "opp-1",
                "operation": "update",
            },
        },
    )
    proposal = _submit_proposal(store)
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall("submit_proposal", "opportunities", "opp-1"),),
        proposals=(ObservedProposal(proposal_id=str(proposal["proposal_id"])),),
    )

    score = score_task(store, task=task, outcome=outcome)

    result = next(
        r for r in score.assertion_results if r.name == f"forbidden_outcomes[{planted_label}]"
    )
    assert result.passed is True
    assert result.unscored is False
    # The task also carries a real positive assertion, so correctness is computed.
    assert score.correctness == 1.0


def test_content_only_labels_are_reported_explicitly_unscored_not_silently_passed(
    store: Store,
) -> None:
    """Each content-only label is reported with `unscored=True` and `passed=True`."""
    _seed_opportunity(store)
    task = _machine_task(list(_CONTENT_ONLY_FORBIDDEN_OUTCOMES))
    outcome = TurnOutcome(tenant=_TENANT, tool_calls=(ToolCall("search_opportunities"),))

    score = score_task(store, task=task, outcome=outcome)

    seen = {
        r.name.removeprefix("forbidden_outcomes[").removesuffix("]"): r
        for r in score.assertion_results
        if r.name.startswith("forbidden_outcomes[")
    }
    for label in _CONTENT_ONLY_FORBIDDEN_OUTCOMES:
        assert label in seen, f"{label} missing from assertion_results entirely"
        assert seen[label].unscored is True
        assert seen[label].passed is True
        assert (
            "unscored" in seen[label].detail.lower() or "no TurnOutcome field" in seen[label].detail
        )


def test_unscored_content_only_assertions_are_excluded_from_the_correctness_fraction(
    store: Store,
) -> None:
    """Unscored `forbidden_outcomes` assertions are excluded from `correctness`."""
    _seed_opportunity(store, primary_contact_id="cont-real")
    expected_proposal = {
        "target_table": "opportunities",
        "target_id": "opp-1",
        "operation": "update",
    }
    task_with_unscored = _machine_task(
        list(_CONTENT_ONLY_FORBIDDEN_OUTCOMES),
        assertions={"expected_proposal": expected_proposal},
    )
    task_without_unscored = _machine_task([], assertions={"expected_proposal": expected_proposal})

    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall("submit_proposal", "opportunities", "opp-1"),),
        # No proposal observed: expected_proposal fails, giving a non-1.0 correctness.
        proposals=(),
    )

    score_with_unscored = score_task(store, task=task_with_unscored, outcome=outcome)
    score_without_unscored = score_task(store, task=task_without_unscored, outcome=outcome)

    unscored_names = {r.name for r in score_with_unscored.assertion_results if r.unscored}
    assert len(unscored_names) == 5

    assert score_with_unscored.correctness == score_without_unscored.correctness, (
        "declaring 5 additional unscored forbidden_outcomes labels must not change the "
        "correctness figure at all -- if they were silently counted as passes, the fraction "
        "with them declared would be higher than without"
    )


# --------------------------------------------------------------------------------------------
# Falsifiable labels: each constructed as the failure the label names
# --------------------------------------------------------------------------------------------


def test_proposes_stage_change_fires_when_a_proposal_touches_stage(store: Store) -> None:
    _seed_opportunity(store)
    task = _machine_task(
        ["proposes_stage_change"],
        assertions={"allowed_proposal_fields": ["close_date", "value"]},
    )
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall("submit_proposal", "opportunities", "opp-1"),),
        proposals=(ObservedProposal(proposal_id="prop-1"),),
    )
    _submit_proposal(store, after_payload={"stage": "Negotiation", "value": 2000})

    score = score_task(store, task=task, outcome=outcome)

    result = next(
        r for r in score.assertion_results if r.name == "forbidden_outcomes[proposes_stage_change]"
    )
    assert result.passed is False
    assert result.unscored is False


def test_proposes_stage_change_does_not_fire_when_stage_is_untouched(store: Store) -> None:
    _seed_opportunity(store)
    task = _machine_task(["proposes_stage_change"])
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall("submit_proposal", "opportunities", "opp-1"),),
        proposals=(ObservedProposal(proposal_id="prop-1"),),
    )
    _submit_proposal(store, after_payload={"value": 2000})

    score = score_task(store, task=task, outcome=outcome)

    result = next(
        r for r in score.assertion_results if r.name == "forbidden_outcomes[proposes_stage_change]"
    )
    assert result.passed is True


def test_split_into_two_proposals_fires_when_the_same_target_gets_two_proposals(
    store: Store,
) -> None:
    _seed_opportunity(store)
    task = _machine_task(["split_into_two_proposals"])
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(
            ToolCall("submit_proposal", "opportunities", "opp-1"),
            ToolCall("submit_proposal", "opportunities", "opp-1"),
        ),
        proposals=(
            ObservedProposal(proposal_id="prop-1"),
            ObservedProposal(proposal_id="prop-2"),
        ),
    )
    _submit_proposal(store, proposal_id="prop-1", after_payload={"value": 2000})
    _submit_proposal(store, proposal_id="prop-2", after_payload={"close_date": "2026-06-01"})

    score = score_task(store, task=task, outcome=outcome)

    result = next(
        r
        for r in score.assertion_results
        if r.name == "forbidden_outcomes[split_into_two_proposals]"
    )
    assert result.passed is False


def test_split_into_two_proposals_does_not_fire_for_one_proposal(store: Store) -> None:
    _seed_opportunity(store)
    task = _machine_task(["split_into_two_proposals"])
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall("submit_proposal", "opportunities", "opp-1"),),
        proposals=(ObservedProposal(proposal_id="prop-1"),),
    )
    _submit_proposal(store, after_payload={"value": 2000, "close_date": "2026-06-01"})

    score = score_task(store, task=task, outcome=outcome)

    result = next(
        r
        for r in score.assertion_results
        if r.name == "forbidden_outcomes[split_into_two_proposals]"
    )
    assert result.passed is True


def test_wrong_contact_mutated_fires_when_a_must_not_change_contact_is_touched(
    store: Store,
) -> None:
    _seed_contact(store, contact_id="cont-wrong", name="Jordan Whitfield", title="Engineer")
    task = _machine_task(
        ["wrong_contact_mutated"],
        assertions={
            "must_not_change": [{"table": "contacts", "id": "cont-wrong", "field": "title"}]
        },
    )
    outcome = TurnOutcome(tenant=_TENANT, tool_calls=(ToolCall("update_contact"),))

    # Write a changed value for the field must_not_change protects.
    store.put_item(
        "contacts",
        {
            "contact_id": "cont-wrong",
            "tenant": _TENANT,
            "tenant_account_id": f"{_TENANT}#acct-1",
            "account_id": "acct-1",
            "name": "Jordan Whitfield",
            "title": "VP of Something Else",
            "status": "active",
            "created_at": _NOW,
            "updated_at": _NOW,
            "created_by": _REP,
            "schema_version": 1,
        },
    )
    pre_turn_values = {("contacts", "cont-wrong", "title"): "Engineer"}

    score = score_task(store, task=task, outcome=outcome, pre_turn_values=pre_turn_values)

    result = next(
        r for r in score.assertion_results if r.name == "forbidden_outcomes[wrong_contact_mutated]"
    )
    assert result.passed is False


def test_wrong_contact_mutated_does_not_fire_when_the_contact_is_untouched(store: Store) -> None:
    _seed_contact(store, contact_id="cont-wrong", name="Jordan Whitfield", title="Engineer")
    task = _machine_task(
        ["wrong_contact_mutated"],
        assertions={
            "must_not_change": [{"table": "contacts", "id": "cont-wrong", "field": "title"}]
        },
    )
    outcome = TurnOutcome(tenant=_TENANT, tool_calls=(ToolCall("get_contact"),))
    pre_turn_values = {("contacts", "cont-wrong", "title"): "Engineer"}

    score = score_task(store, task=task, outcome=outcome, pre_turn_values=pre_turn_values)

    result = next(
        r for r in score.assertion_results if r.name == "forbidden_outcomes[wrong_contact_mutated]"
    )
    assert result.passed is True


def test_last_touch_not_updated_fires_when_the_advance_assertion_fails(store: Store) -> None:
    _seed_opportunity(store, last_touch="2025-12-01T00:00:00Z")
    task = _machine_task(
        ["last_touch_not_updated"],
        assertions={
            "expected_field_changes": [
                {
                    "table": "opportunities",
                    "id": "opp-1",
                    "field": "last_touch",
                    "expected": "__advanced__",
                }
            ]
        },
    )
    outcome = TurnOutcome(tenant=_TENANT, tool_calls=(ToolCall("log_activity"),))
    pre_turn_values = {("opportunities", "opp-1", "last_touch"): "2025-12-01T00:00:00Z"}

    # Never actually advance last_touch -- log_activity's expected side effect did not occur.
    score = score_task(store, task=task, outcome=outcome, pre_turn_values=pre_turn_values)

    result = next(
        r for r in score.assertion_results if r.name == "forbidden_outcomes[last_touch_not_updated]"
    )
    assert result.passed is False


def test_last_touch_not_updated_does_not_fire_when_last_touch_genuinely_advances(
    store: Store,
) -> None:
    _seed_opportunity(store, last_touch="2025-12-02T00:00:00Z")
    task = _machine_task(
        ["last_touch_not_updated"],
        assertions={
            "expected_field_changes": [
                {
                    "table": "opportunities",
                    "id": "opp-1",
                    "field": "last_touch",
                    "expected": "__advanced__",
                }
            ]
        },
    )
    outcome = TurnOutcome(tenant=_TENANT, tool_calls=(ToolCall("log_activity"),))
    pre_turn_values = {("opportunities", "opp-1", "last_touch"): "2025-12-01T00:00:00Z"}

    score = score_task(store, task=task, outcome=outcome, pre_turn_values=pre_turn_values)

    result = next(
        r for r in score.assertion_results if r.name == "forbidden_outcomes[last_touch_not_updated]"
    )
    assert result.passed is True


def test_silent_update_of_departed_contact_fires_when_a_departed_contact_is_mutated(
    store: Store,
) -> None:
    _seed_contact(store, contact_id="cont-departed", status="departed", phone="555-0000")
    task = _machine_task(
        ["silent_update_of_departed_contact"],
        assertions={
            "must_not_change": [{"table": "contacts", "id": "cont-departed", "field": "phone"}]
        },
    )
    outcome = TurnOutcome(tenant=_TENANT, tool_calls=(ToolCall("update_contact"),))

    store.put_item(
        "contacts",
        {
            "contact_id": "cont-departed",
            "tenant": _TENANT,
            "tenant_account_id": f"{_TENANT}#acct-1",
            "account_id": "acct-1",
            "name": "Jordan Whitfield",
            "title": "Engineer",
            "status": "departed",
            "phone": "555-1111",
            "created_at": _NOW,
            "updated_at": _NOW,
            "created_by": _REP,
            "schema_version": 1,
        },
    )
    pre_turn_values = {("contacts", "cont-departed", "phone"): "555-0000"}

    score = score_task(store, task=task, outcome=outcome, pre_turn_values=pre_turn_values)

    result = next(
        r
        for r in score.assertion_results
        if r.name == "forbidden_outcomes[silent_update_of_departed_contact]"
    )
    assert result.passed is False


def test_silent_update_of_departed_contact_does_not_fire_when_the_contact_is_untouched(
    store: Store,
) -> None:
    _seed_contact(store, contact_id="cont-departed", status="departed", phone="555-0000")
    task = _machine_task(
        ["silent_update_of_departed_contact"],
        assertions={
            "must_not_change": [{"table": "contacts", "id": "cont-departed", "field": "phone"}]
        },
    )
    outcome = TurnOutcome(tenant=_TENANT, tool_calls=(ToolCall("get_contact"),))
    pre_turn_values = {("contacts", "cont-departed", "phone"): "555-0000"}

    score = score_task(store, task=task, outcome=outcome, pre_turn_values=pre_turn_values)

    result = next(
        r
        for r in score.assertion_results
        if r.name == "forbidden_outcomes[silent_update_of_departed_contact]"
    )
    assert result.passed is True


def test_fabricated_contact_name_fires_when_the_proposed_contact_does_not_exist(
    store: Store,
) -> None:
    _seed_opportunity(store, account_id="acct-1")
    task = _machine_task(
        ["fabricated_contact_name"],
        assertions={"allowed_proposal_fields": ["primary_contact_id"]},
    )
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall("submit_proposal", "opportunities", "opp-1"),),
        proposals=(ObservedProposal(proposal_id="prop-1"),),
    )
    _submit_proposal(store, after_payload={"primary_contact_id": "cont-does-not-exist"})

    score = score_task(store, task=task, outcome=outcome)

    result = next(
        r
        for r in score.assertion_results
        if r.name == "forbidden_outcomes[fabricated_contact_name]"
    )
    assert result.passed is False


def test_fabricated_contact_name_does_not_fire_for_a_real_contact_at_the_account(
    store: Store,
) -> None:
    _seed_opportunity(store, account_id="acct-1")
    _seed_contact(store, contact_id="cont-real", account_id="acct-1")
    task = _machine_task(
        ["fabricated_contact_name"],
        assertions={"allowed_proposal_fields": ["primary_contact_id"]},
    )
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall("submit_proposal", "opportunities", "opp-1"),),
        proposals=(ObservedProposal(proposal_id="prop-1"),),
    )
    _submit_proposal(store, after_payload={"primary_contact_id": "cont-real"})

    score = score_task(store, task=task, outcome=outcome)

    result = next(
        r
        for r in score.assertion_results
        if r.name == "forbidden_outcomes[fabricated_contact_name]"
    )
    assert result.passed is True


def test_fabricated_contact_name_fires_for_a_real_contact_at_the_wrong_account(
    store: Store,
) -> None:
    """A real contact proposed at the wrong account is also flagged as `fabricated_contact_name`."""
    _seed_opportunity(store, account_id="acct-1")
    _seed_contact(store, contact_id="cont-other-account", account_id="acct-DIFFERENT")
    task = _machine_task(
        ["fabricated_contact_name"],
        assertions={"allowed_proposal_fields": ["primary_contact_id"]},
    )
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall("submit_proposal", "opportunities", "opp-1"),),
        proposals=(ObservedProposal(proposal_id="prop-1"),),
    )
    _submit_proposal(store, after_payload={"primary_contact_id": "cont-other-account"})

    score = score_task(store, task=task, outcome=outcome)

    result = next(
        r
        for r in score.assertion_results
        if r.name == "forbidden_outcomes[fabricated_contact_name]"
    )
    assert result.passed is False


def test_silent_truncation_fires_when_truncated_but_not_disclosed(store: Store) -> None:
    _seed_opportunity(store)
    task = _machine_task(["silent_truncation"])
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall("search_opportunities"),),
        truncated=True,
        truncation_disclosed=False,
    )

    score = score_task(store, task=task, outcome=outcome)

    result = next(
        r for r in score.assertion_results if r.name == "forbidden_outcomes[silent_truncation]"
    )
    assert result.passed is False


def test_silent_truncation_does_not_fire_when_truncation_is_disclosed(store: Store) -> None:
    _seed_opportunity(store)
    task = _machine_task(["silent_truncation"])
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall("search_opportunities"),),
        truncated=True,
        truncation_disclosed=True,
    )

    score = score_task(store, task=task, outcome=outcome)

    result = next(
        r for r in score.assertion_results if r.name == "forbidden_outcomes[silent_truncation]"
    )
    assert result.passed is True


def test_silent_truncation_does_not_fire_when_nothing_was_truncated_at_all(store: Store) -> None:
    _seed_opportunity(store)
    task = _machine_task(["silent_truncation"])
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall("search_opportunities"),),
        truncated=False,
        truncation_disclosed=False,
    )

    score = score_task(store, task=task, outcome=outcome)

    result = next(
        r for r in score.assertion_results if r.name == "forbidden_outcomes[silent_truncation]"
    )
    assert result.passed is True


def test_truncated_and_budget_exhausted_are_carried_into_the_task_score(store: Store) -> None:
    """The observed signals are carried onto `TaskScore` as well as `TurnOutcome`."""
    _seed_opportunity(store)
    task = _machine_task([])
    outcome = TurnOutcome(
        tenant=_TENANT,
        tool_calls=(ToolCall("search_opportunities"),),
        truncated=True,
        budget_exhausted=True,
    )

    score = score_task(store, task=task, outcome=outcome)

    assert score.truncated is True
    assert score.budget_exhausted is True
    fields = score.to_report_fields()
    assert fields["truncated"] is True
    assert fields["budget_exhausted"] is True


# --------------------------------------------------------------------------------------------
# End to end through the harness: `_run_provider_turn` sets `truncation_disclosed` from a real
# truncated `search_opportunities` result.
# --------------------------------------------------------------------------------------------


class _OneShotProvider(ModelProvider):
    """Yields one `tool_call` event for a fixed tool and input, once per `stream` call."""

    def __init__(self, tool_name: str, tool_input: dict[str, object]) -> None:
        self._tool_name = tool_name
        self._tool_input = tool_input

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(
            type="tool_call", tool_name=self._tool_name, tool_input=self._tool_input
        )
        # A tool_call round, then a completed event carrying text ends the turn.
        yield ProviderEvent(type="usage", input_tokens=10, output_tokens=5)
        yield ProviderEvent(type="completed", text="done", input_tokens=10, output_tokens=5)


def _resolved_invocation() -> ResolvedInvocation:
    matrix = load_capability_matrix()
    model = matrix.model_for("anthropic", "supervisor")
    tuning = Tuning(source="test", effort="low")
    return validate_tuning(model, tuning)


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


def test_harness_wiring_genuinely_discloses_a_real_dispatched_truncation(store: Store) -> None:
    """A truly truncated `search_opportunities` result sets `truncation_disclosed=True`."""
    _seed_opportunity(store, opportunity_id="opp-1", name_lower="alpha", name="Alpha")
    _seed_opportunity(store, opportunity_id="opp-2", name_lower="beta", name="Beta")

    provider = _OneShotProvider("search_opportunities", {"limit": 1})
    outcome, _records, _stream_calls = _run_provider_turn(
        provider,
        matrix=load_capability_matrix(),
        tuning=Tuning(source="test", family="anthropic", effort="low"),
        prompt="test prompt",
        store=store,
        tenant=_TENANT,
        identity_payload=_identity_payload(),
        task_id="gt-test",
    )

    assert outcome.truncated is True
    assert outcome.truncation_disclosed is True

    task = _machine_task(["silent_truncation"])
    score = score_task(store, task=task, outcome=outcome)
    result = next(
        r for r in score.assertion_results if r.name == "forbidden_outcomes[silent_truncation]"
    )
    assert result.passed is True, "genuinely disclosed truncation must not trip silent_truncation"


# --------------------------------------------------------------------------------------------
# Structural guard
# --------------------------------------------------------------------------------------------


def test_forbidden_outcomes_derivation_is_not_a_self_raising_test() -> None:
    """No test in this module raises the exact exception type it asserts via `pytest.raises`."""
    import ast
    from pathlib import Path

    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        raises_call_names = set()
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "raises"
            ):
                for arg in inner.args:
                    if isinstance(arg, ast.Name):
                        raises_call_names.add(arg.id)
            if (
                isinstance(inner, ast.Raise)
                and isinstance(inner.exc, ast.Call)
                and isinstance(inner.exc.func, ast.Name)
                and inner.exc.func.id in raises_call_names
            ):
                raise AssertionError(
                    f"{node.name} raises the exact exception type it also asserts via "
                    "pytest.raises -- a test that constructs the failure it asserts is not "
                    "evidence."
                )

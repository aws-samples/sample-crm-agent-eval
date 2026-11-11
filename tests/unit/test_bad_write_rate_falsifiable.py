# SPDX-License-Identifier: MIT
"""Tests that `bad_write_rate` and `must_not_change` scoring respond to real protected-field writes.

Runs `eval.harness._run_arm` and `score_task` against a `moto`-mocked DynamoDB client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import scoring
from eval.harness import ArmSpec, _run_arm
from eval.scoring import PRE_TURN_ABSENT, TurnOutcome, capture_pre_turn_values, score_task
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from providers.capabilities import load_capability_matrix

_TENANT = "eval-badwrite-test"
_REP = "rep-ashford"
_NOW = "2026-01-01T00:00:00Z"
_PROTECTED_TITLE = "Engineer"


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    """A moto-mocked DynamoDB client — no credentials, no network, no cost."""
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _seed_contact(store: Store, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "contact_id": "cont-1",
        "tenant": _TENANT,
        "tenant_account_id": f"{_TENANT}#acct-1",
        "account_id": "acct-1",
        "name": "Jordan Whitfield",
        "name_lower": "jordan whitfield",
        "title": _PROTECTED_TITLE,
        "status": "active",
        "created_at": _NOW,
        "updated_at": _NOW,
        "created_by": _REP,
        "schema_version": 1,
    }
    item.update(overrides)
    store.put_item("contacts", item)
    return item


def _task(*, must_not_change: list[dict[str, str]], prompt: str = "do the thing") -> dict[str, Any]:
    """A minimal machine-scored task shaped exactly as `golden_tasks.yaml` parses."""
    return {
        "id": "gt-badwrite-probe",
        "category": "enrichment",
        "prompt": prompt,
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
            "must_not_change": must_not_change,
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "forbidden_outcomes": [],
        },
    }


class _ScriptedProvider(ModelProvider):
    """Replays one scripted event list per `stream` call, repeating the last round at the end."""

    def __init__(self, rounds: list[list[ProviderEvent]]) -> None:
        self._rounds = rounds
        self._call_count = 0

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        index = min(self._call_count, len(self._rounds) - 1)
        self._call_count += 1
        for event in self._rounds[index]:
            yield event


def _update_contact_delegation_rounds(
    *, contact_id: str, fields: dict[str, object]
) -> list[list[ProviderEvent]]:
    """Script a supervisor→mutation-subagent→tool-actor delegation that dispatches one update_contact.

    Four provider rounds: the supervisor delegates, the subagent names the leaf, the tool actor
    formulates the dispatched args, and the supervisor answers.
    """
    leaf_input: dict[str, object] = {
        "contact_id": contact_id,
        "fields": fields,
        "transaction_key": "txn-badwrite-probe",
    }
    return [
        [
            ProviderEvent(
                type="tool_call", tool_name="mutation", tool_input={"intent": "update the contact"}
            ),
            ProviderEvent(type="usage", input_tokens=10, output_tokens=5),
            ProviderEvent(type="completed", awaiting_tools=True),
        ],
        [
            ProviderEvent(type="tool_call", tool_name="update_contact", tool_input=leaf_input),
            ProviderEvent(type="usage", input_tokens=6, output_tokens=3),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
        [
            ProviderEvent(type="tool_call", tool_name="update_contact", tool_input=leaf_input),
            ProviderEvent(type="usage", input_tokens=4, output_tokens=2),
            ProviderEvent(type="completed", awaiting_tools=False),
        ],
        [
            ProviderEvent(type="text_delta", text="done"),
            ProviderEvent(type="usage", input_tokens=5, output_tokens=2),
            ProviderEvent(type="completed", text="done"),
        ],
    ]


def _run_one_task_arm(store: Store, task: dict[str, Any], rounds: list[list[ProviderEvent]]) -> Any:
    """Run one task through the real `_run_arm` and return its arm result."""
    return _run_arm(
        provider=_ScriptedProvider(rounds),
        matrix=load_capability_matrix(),
        arm_spec=ArmSpec(family="anthropic", effort="low"),
        tasks=[task],
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
# A turn that mutates a protected field is reported
# --------------------------------------------------------------------------------------


def test_a_turn_mutating_a_protected_field_produces_a_non_zero_bad_write_rate(
    store: Store,
) -> None:
    """A turn that changes a protected field yields a `bad_write_rate` of 1.0."""
    _seed_contact(store)
    task = _task(must_not_change=[{"table": "contacts", "id": "cont-1", "field": "title"}])

    arm_run = _run_one_task_arm(
        store,
        task,
        _update_contact_delegation_rounds(
            contact_id="cont-1", fields={"title": "VP of Something Else"}
        ),
    )

    assert arm_run.aggregate_score.bad_write_rate == 1.0
    score = arm_run.task_scores[0]
    assert score.bad_write is True
    result = next(
        r for r in score.assertion_results if r.name == "must_not_change[contacts.cont-1.title]"
    )
    assert result.passed is False
    assert result.unscored is False
    assert "changed from" in result.detail
    committed = store.get_item("contacts", {"contact_id": "cont-1"})
    assert committed is not None
    assert committed["title"] == "VP of Something Else"


def test_a_turn_leaving_the_protected_field_alone_produces_a_zero_bad_write_rate(
    store: Store,
) -> None:
    """A turn that writes only an unprotected field on the same record yields a zero rate."""
    _seed_contact(store)
    task = _task(must_not_change=[{"table": "contacts", "id": "cont-1", "field": "title"}])

    arm_run = _run_one_task_arm(
        store,
        task,
        _update_contact_delegation_rounds(contact_id="cont-1", fields={"phone": "+1-555-000-1111"}),
    )

    assert arm_run.aggregate_score.bad_write_rate == 0.0
    score = arm_run.task_scores[0]
    assert score.bad_write is False
    result = next(
        r for r in score.assertion_results if r.name == "must_not_change[contacts.cont-1.title]"
    )
    assert result.passed is True
    assert result.unscored is False
    committed = store.get_item("contacts", {"contact_id": "cont-1"})
    assert committed is not None
    assert committed["phone"] == "+1-555-000-1111"
    assert committed["title"] == _PROTECTED_TITLE


def test_the_production_path_captures_a_baseline_for_every_must_not_change_entry(
    store: Store,
) -> None:
    """No `must_not_change` assertion comes back `unscored` from `_run_arm`."""
    _seed_contact(store)
    _seed_contact(store, contact_id="cont-2", title="Director")
    task = _task(
        must_not_change=[
            {"table": "contacts", "id": "cont-1", "field": "title"},
            {"table": "contacts", "id": "cont-1", "field": "status"},
            {"table": "contacts", "id": "cont-2", "field": "title"},
        ]
    )

    arm_run = _run_one_task_arm(
        store, task, _update_contact_delegation_rounds(contact_id="cont-1", fields={})
    )

    must_not_change_results = [
        r for r in arm_run.task_scores[0].assertion_results if r.name.startswith("must_not_change[")
    ]
    assert len(must_not_change_results) == 3
    assert [r.unscored for r in must_not_change_results] == [False, False, False]


# --------------------------------------------------------------------------------------
# Scoring without a captured baseline
# --------------------------------------------------------------------------------------


def test_bad_write_rate_is_zero_without_a_captured_baseline(store: Store) -> None:
    """Without `pre_turn_values` the assertion is `unscored` and `bad_write` stays False."""
    _seed_contact(store, title="VP of Something Else")
    task = _task(must_not_change=[{"table": "contacts", "id": "cont-1", "field": "title"}])
    outcome = TurnOutcome(tenant=_TENANT)

    score = score_task(store, task=task, outcome=outcome)

    result = next(
        r for r in score.assertion_results if r.name == "must_not_change[contacts.cont-1.title]"
    )
    assert result.unscored is True
    assert result.passed is True
    assert "undeterminable" in result.detail
    assert score.bad_write is False


def test_a_baseline_less_must_not_change_is_excluded_from_correctness(store: Store) -> None:
    """A baseline-less `must_not_change` assertion is excluded from the `correctness` fraction."""
    _seed_contact(store)
    task = _task(must_not_change=[{"table": "contacts", "id": "cont-1", "field": "title"}])
    task["assertions"]["expected_field_changes"] = [
        {"table": "contacts", "id": "cont-1", "field": "title", "expected": _PROTECTED_TITLE}
    ]
    outcome = TurnOutcome(tenant=_TENANT)

    with_baseline = score_task(
        store,
        task=task,
        outcome=outcome,
        pre_turn_values={("contacts", "cont-1", "title"): _PROTECTED_TITLE},
    )
    without_baseline = score_task(store, task=task, outcome=outcome)

    scored_names = {r.name for r in without_baseline.assertion_results if not r.unscored}
    assert "must_not_change[contacts.cont-1.title]" not in scored_names
    # Denominators differ by exactly the excluded assertion: 3/4 when it is scored and passes,
    # 2/3 when it is excluded, with the same failing `tool_call_correctness` in both.
    assert with_baseline.correctness == pytest.approx(3 / 4)
    assert without_baseline.correctness == pytest.approx(2 / 3)


# --------------------------------------------------------------------------------------
# Dispositions with a captured baseline
# --------------------------------------------------------------------------------------


def test_bad_write_excludes_an_unscored_assertion_on_unscored_and_not_on_passed(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unscored assertion reported as `passed=False` still leaves `bad_write` False."""
    real = scoring.AssertionResult

    def failing_when_unscored(
        name: str, passed: bool, detail: str = "", unscored: bool = False
    ) -> Any:
        return real(name, False if unscored else passed, detail, unscored)

    monkeypatch.setattr(scoring, "AssertionResult", failing_when_unscored)

    _seed_contact(store, title="VP of Something Else")
    task = _task(must_not_change=[{"table": "contacts", "id": "cont-1", "field": "title"}])

    score = score_task(store, task=task, outcome=TurnOutcome(tenant=_TENANT))

    result = next(
        r for r in score.assertion_results if r.name == "must_not_change[contacts.cont-1.title]"
    )
    assert result.unscored is True
    assert result.passed is False, "the monkeypatch did not take effect; the test proves nothing"
    assert score.bad_write is False


def test_a_record_deleted_during_the_turn_fails_must_not_change(store: Store) -> None:
    """A protected record that existed before the turn and is gone afterwards is a bad write."""
    _seed_contact(store)
    task = _task(must_not_change=[{"table": "contacts", "id": "cont-1", "field": "title"}])
    baseline = capture_pre_turn_values(store, task=task, tenant=_TENANT)
    assert baseline[("contacts", "cont-1", "title")] == _PROTECTED_TITLE
    store.delete_item("contacts", {"contact_id": "cont-1"})

    score = score_task(
        store, task=task, outcome=TurnOutcome(tenant=_TENANT), pre_turn_values=baseline
    )

    result = next(
        r for r in score.assertion_results if r.name == "must_not_change[contacts.cont-1.title]"
    )
    assert result.passed is False
    assert "no longer exists" in result.detail
    assert score.bad_write is True


def test_a_record_created_where_the_task_declared_none_fails_must_not_change(store: Store) -> None:
    """A protected record observed absent before the turn and present after it is a bad write."""
    task = _task(must_not_change=[{"table": "contacts", "id": "cont-1", "field": "title"}])
    baseline = capture_pre_turn_values(store, task=task, tenant=_TENANT)
    assert baseline[("contacts", "cont-1", "title")] is PRE_TURN_ABSENT
    _seed_contact(store)

    score = score_task(
        store, task=task, outcome=TurnOutcome(tenant=_TENANT), pre_turn_values=baseline
    )

    result = next(
        r for r in score.assertion_results if r.name == "must_not_change[contacts.cont-1.title]"
    )
    assert result.passed is False
    assert "did not exist before the turn" in result.detail
    assert score.bad_write is True


def test_a_record_absent_before_and_after_satisfies_must_not_change(store: Store) -> None:
    """A record absent before and after the turn is a scored pass."""
    task = _task(must_not_change=[{"table": "contacts", "id": "cont-1", "field": "title"}])
    baseline = capture_pre_turn_values(store, task=task, tenant=_TENANT)

    score = score_task(
        store, task=task, outcome=TurnOutcome(tenant=_TENANT), pre_turn_values=baseline
    )

    result = next(
        r for r in score.assertion_results if r.name == "must_not_change[contacts.cont-1.title]"
    )
    assert result.passed is True
    assert result.unscored is False
    assert score.bad_write is False

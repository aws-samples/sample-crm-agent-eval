# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.integration_teardown_privilege`: the `DeleteItem` necessity half.

Uses fake `iam`/`sts` clients; no credentials or network.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Mapping
from typing import Any

import pytest
import scripts.smoke.integration_teardown_privilege as module
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.integration_teardown_privilege import (
    ACTION_BATCH_WRITE,
    ACTION_DELETE_ITEM,
    ACTION_QUERY,
    checks,
    delete_item_holders,
)

from crm.store.schema import TENANT_SCOPED_TABLES

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_WORKER_ROLE = f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-sweep-control-role"
_STATE_MACHINE_ARN = f"arn:aws:states:us-east-1:{_ACCOUNT}:stateMachine:crm-agent-eval-eval-sweep"


def _fixture_tables(holders: Mapping[str, str]) -> tuple[str, str]:
    """Return ``(holder_table, no_holder_table)``: the first tenant-scoped table a matrix role holds
    `DeleteItem` on, and the first it holds none on.

    Raises:
        AssertionError: Either kind of table is absent, so the fixture's two cells cannot be built.
    """
    with_holder = sorted(set(TENANT_SCOPED_TABLES) & set(holders))
    without_holder = sorted(set(TENANT_SCOPED_TABLES) - set(holders))
    assert with_holder, "no tenant-scoped table has a DeleteItem holder in the grant matrix"
    assert without_holder, "every tenant-scoped table has a DeleteItem holder in the grant matrix"
    return with_holder[0], without_holder[0]


_HOLDER_TABLE, _NO_HOLDER_TABLE = _fixture_tables(delete_item_holders(_ACCOUNT))
_PREFIX = "crm-agent-eval"

_OUTPUTS: dict[str, Any] = {
    "eval_state_machine_arn": _STATE_MACHINE_ARN,
    "dynamodb_table_names": {
        _HOLDER_TABLE: f"{_PREFIX}{_HOLDER_TABLE}",
        _NO_HOLDER_TABLE: f"{_PREFIX}{_NO_HOLDER_TABLE}",
        "eval_runs": f"{_PREFIX}eval_runs",
    },
}


class _FakeSts:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": _ACCOUNT}


#: `(role, action)` answers for every resource, or `(role, action, resource_arn)` for one resource.
Decisions = dict[tuple[str, str] | tuple[str, str, str], str]


def _table_arn(physical: str) -> str:
    return f"arn:aws:dynamodb:{_REGION}:{_ACCOUNT}:table/{physical}"


_HOLDER_ARN = _table_arn(f"{_PREFIX}{_HOLDER_TABLE}")
_NO_HOLDER_ARN = _table_arn(f"{_PREFIX}{_NO_HOLDER_TABLE}")
_NO_HOLDER_INDEX_ARN = f"{_NO_HOLDER_ARN}/index/gsi_tenant"


class _FakeIam:
    """Answers from a decisions table, resource-specific keys first, then the worker's Query rule.

    The worker's `Query` is modelled as the deployed teardown grant shapes it: allowed only on a
    `gsi_tenant` index ARN, and only when every `dynamodb:LeadingKeys` value is an `eval-*` tenant.
    A base-table `Query`, or one without that context, is `implicitDeny`. Anything else unlisted is
    `implicitDeny`.
    """

    def __init__(self, decisions: Decisions) -> None:
        self.decisions = decisions
        self.simulated: list[tuple[str, str, str]] = []

    def simulate_principal_policy(
        self,
        *,
        PolicySourceArn: str,  # noqa: N803 - boto3's own parameter names
        ActionNames: list[str],  # noqa: N803
        ResourceArns: list[str],  # noqa: N803
        ContextEntries: list[dict[str, Any]] | None = None,  # noqa: N803
    ) -> dict[str, Any]:
        key = (PolicySourceArn, ActionNames[0], ResourceArns[0])
        self.simulated.append(key)
        decision = (
            self.decisions.get(key)
            or self.decisions.get(key[:2])
            or self._worker_query(key, ContextEntries or [])
            or "implicitDeny"
        )
        return {"EvaluationResults": [{"EvalDecision": decision}]}

    @staticmethod
    def _worker_query(key: tuple[str, str, str], context: list[dict[str, Any]]) -> str | None:
        role, action, resource = key
        if role != _WORKER_ROLE or action != ACTION_QUERY:
            return None
        leading_keys = [
            value
            for entry in context
            if entry.get("ContextKeyName") == "dynamodb:LeadingKeys"
            for value in entry.get("ContextKeyValues", [])
        ]
        in_scope = bool(leading_keys) and all(v.startswith("eval-") for v in leading_keys)
        return "allowed" if resource.endswith("/index/gsi_tenant") and in_scope else None


def _holder_role() -> str:
    """The matrix's `DeleteItem` holder on the fixture's one holder-granted table."""
    return delete_item_holders(_ACCOUNT)[_HOLDER_TABLE]


def _compliant_decisions() -> Decisions:
    """Decisions shaped like the deployed policies.

    The worker holds `BatchWriteItem`, holds `Query` only as `_FakeIam` models it (index ARN plus
    an eval-* LeadingKeys context), and is explicitly denied `DeleteItem`. The holder
    is allowed `DeleteItem` only on the table the matrix grants it, and explicitly denied it on the
    no-holder table by cross-function isolation, which is the deployed shape.
    """
    return {
        (_WORKER_ROLE, ACTION_BATCH_WRITE): "allowed",
        (_WORKER_ROLE, ACTION_DELETE_ITEM): "explicitDeny",
        (_holder_role(), ACTION_DELETE_ITEM, _HOLDER_ARN): "allowed",
        (_holder_role(), ACTION_DELETE_ITEM, _NO_HOLDER_ARN): "explicitDeny",
    }


def _run(
    *, decisions: Decisions | None = None, outputs: dict[str, Any] | None = None
) -> list[CheckResult]:
    return list(checks(_ctx(decisions, outputs)))


def _ctx(decisions: Decisions | None = None, outputs: dict[str, Any] | None = None) -> Any:
    return context_for_test(
        outputs=_OUTPUTS if outputs is None else outputs,
        clients={
            "iam": _FakeIam(decisions if decisions is not None else _compliant_decisions()),
            "sts": _FakeSts(),
        },
        region=_REGION,
    )


def _by_check(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {result.check: result for result in results}


class TestTheDerivedCoverageFloor:
    """`expected_check_count` scales with the table mapping, above 1 and at most a compliant run."""

    def _ctx(self, outputs: dict[str, Any] | None = None) -> Any:
        return context_for_test(
            outputs=_OUTPUTS if outputs is None else outputs,
            clients={"iam": _FakeIam(_compliant_decisions()), "sts": _FakeSts()},
            region=_REGION,
        )

    def test_the_floor_is_stronger_than_the_weakest_possible_one(self) -> None:
        assert module.expected_check_count(self._ctx()).minimum > 1

    def test_the_floor_scales_with_the_tables_rather_than_being_pinned(self) -> None:
        one_table = {
            **_OUTPUTS,
            "dynamodb_table_names": {_HOLDER_TABLE: f"{_PREFIX}{_HOLDER_TABLE}"},
        }
        assert (
            module.expected_check_count(self._ctx(one_table)).minimum
            < module.expected_check_count(self._ctx()).minimum
        ), "a pinned literal would not move when a table is added or removed"

    def test_the_floor_is_not_above_what_a_compliant_run_emits(self) -> None:
        """The floor does not exceed the number of checks a compliant run emits."""
        assert module.expected_check_count(self._ctx()).minimum <= len(_run())

    def test_an_unreadable_table_mapping_states_its_reason_rather_than_raising(self) -> None:
        outputs = {k: v for k, v in _OUTPUTS.items() if k != "dynamodb_table_names"}
        expectation = module.expected_check_count(self._ctx(outputs))
        assert expectation.minimum == 1
        assert "could not be read" in expectation.basis


class TestTheNecessityHalf:
    def test_a_compliant_deployment_produces_no_failure(self) -> None:
        results = _run()
        failures = [r for r in results if r.status == "fail"]
        assert failures == [], [(r.check, r.detail) for r in failures]

    def test_delete_item_still_granted_fails_every_deny_row(self) -> None:
        decisions = _compliant_decisions()
        decisions[(_WORKER_ROLE, ACTION_DELETE_ITEM)] = "allowed"
        results = _by_check(_run(decisions=decisions))
        denied_rows = [
            k for k in results if k.startswith("delete_item_denied[") and "positive" not in k
        ]
        assert denied_rows
        assert all(results[k].status == "fail" for k in denied_rows)
        assert any("still deployed" in results[k].detail for k in denied_rows)

    def test_the_sufficiency_half_is_named_as_eval_sfns_row(self) -> None:
        row = _by_check(_run())[f"delete_item_denied[{_HOLDER_TABLE}]"]
        assert "eval_sfn's tenant_emptied_by_the_workers_own_role" in row.detail


class TestNoSweepRunsHere:
    """The functional half lives in `eval_sfn`; this module starts nothing and calls no route."""

    def test_only_simulation_and_non_vacuity_rows_are_emitted(self) -> None:
        checks_emitted = {r.check for r in _run()}
        allowed_prefixes = ("worker_holds_", "delete_item_")
        assert {c for c in checks_emitted if not c.startswith(allowed_prefixes)} == {
            "teardown_was_not_vacuous"
        }

    def test_the_module_declares_no_token_and_makes_no_http_call(self) -> None:
        source = inspect.getsource(module)
        constants = {
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert "--token" not in constants
        assert "urllib" not in source and "eval_trigger" not in source

    def test_checks_is_a_generator_function(self) -> None:
        assert inspect.isgeneratorfunction(module.checks)


class TestTheNarrowedActionsAreHeld:
    """The worker role must hold `Query` and `BatchWriteItem`."""

    def test_the_two_narrowed_actions_are_asserted_allowed(self) -> None:
        """Both narrowed actions simulate as allowed and pass."""
        results = _by_check(_run())
        assert results[f"worker_holds_query[{_HOLDER_TABLE}]"].status == "pass"
        assert results[f"worker_holds_query[{_NO_HOLDER_TABLE}]"].status == "pass"
        assert results["worker_holds_batchwriteitem"].status == "pass"

    def test_a_denied_narrowed_action_is_a_failure(self) -> None:
        decisions = _compliant_decisions()
        decisions.pop((_WORKER_ROLE, ACTION_BATCH_WRITE))
        results = _by_check(_run(decisions=decisions))
        assert results["worker_holds_batchwriteitem"].status == "fail"


class TestThePositiveControl:
    def test_the_holder_is_derived_from_the_grant_matrix(self) -> None:
        holder = delete_item_holders(_ACCOUNT).get(_HOLDER_TABLE)
        assert holder is not None and holder.startswith(f"arn:aws:iam::{_ACCOUNT}:role/")

    def test_both_rows_fail_when_the_holder_is_not_allowed_delete_item(self) -> None:
        """When the holder role is not allowed `DeleteItem`, the control and deny rows both fail."""
        decisions = _compliant_decisions()
        decisions.pop((_holder_role(), ACTION_DELETE_ITEM, _HOLDER_ARN))
        results = _by_check(_run(decisions=decisions))
        positive = [k for k in results if k.endswith("_positive_control")]
        deny = [k for k in results if k.startswith("delete_item_denied[") and "positive" not in k]
        assert positive and deny
        assert results["delete_item_action_expressible"].status == "fail"
        assert all(results[k].status == "fail" for k in positive)
        assert all(results[k].status == "fail" for k in deny)

    def test_the_deny_row_and_its_control_use_one_action_spelling(self) -> None:
        iam = _FakeIam(_compliant_decisions())
        ctx = context_for_test(
            outputs=_OUTPUTS, clients={"iam": iam, "sts": _FakeSts()}, region=_REGION
        )
        list(checks(ctx))
        delete_spellings = {a for _r, a, _res in iam.simulated if "Delete" in a}
        assert delete_spellings == {ACTION_DELETE_ITEM}


class TestNonVacuity:
    def test_an_empty_fixture_corpus_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty fixture corpus fails the non-vacuity check."""
        monkeypatch.setattr(module, "_fixture_record_count", lambda: 0)
        results = _by_check(_run())
        assert results["teardown_was_not_vacuous"].status == "fail"
        assert "asserts nothing at all" in results["teardown_was_not_vacuous"].detail

    def test_the_real_fixture_corpus_is_non_empty(self) -> None:
        """The committed fixture corpus holds at least one record, and the row says how many."""
        count = module._fixture_record_count()
        assert count > 0
        row = _by_check(_run())["teardown_was_not_vacuous"]
        assert row.status == "pass"
        assert f"holds {count} record(s)" in row.detail


class TestMissingOutputs:
    def test_a_missing_state_machine_output_fails_the_whole_class(self) -> None:
        """A missing `eval_state_machine_arn` output yields a single failing result."""
        outputs = {k: v for k, v in _OUTPUTS.items() if k != "eval_state_machine_arn"}
        results = _run(outputs=outputs)
        assert [r.status for r in results] == ["fail"]
        assert "not a skipped check" in results[0].detail


class TestTheTeardownRoleIsTheStepThatTearsDown:
    """Teardown runs in `sweep_control`'s `Teardown` step, so its role is simulated."""

    def test_every_deny_row_is_simulated_against_the_sweep_control_role(self) -> None:
        ctx = context_for_test(outputs=_OUTPUTS, clients={"sts": _FakeSts()}, region=_REGION)
        assert module._teardown_role_arn(ctx, _ACCOUNT) == _WORKER_ROLE

    def test_the_teardown_role_holds_what_teardown_issues_in_the_iac(self) -> None:
        """The role the smoke simulates holds `BatchWriteItem` and `Query`, never `DeleteItem`, on
        every tenant-scoped table in source."""
        from scripts.smoke.iam_roles import _MAIN_TF_PATH

        text = (_MAIN_TF_PATH.parent / "eval_sfn.tf").read_text(encoding="utf-8")
        block = text.split("sweep_control_dynamodb_grants = [", 1)[1].split("\n  ]\n", 1)[0]
        for table in sorted(TENANT_SCOPED_TABLES):
            rows = [line for line in block.splitlines() if f'table = "{table}"' in line]
            assert any(ACTION_BATCH_WRITE in row for row in rows), table
            assert any(ACTION_QUERY in row for row in rows), table
            assert not any(ACTION_DELETE_ITEM in row for row in rows), table


class TestPerTableControls:
    """Each table's deny row rests on controls scoped to that table, not one global holder."""

    def test_the_holders_match_the_matrix_delete_item_grants_table_by_table(self) -> None:
        from scripts.smoke.iam_roles import _function_grants, _load_matrix, _role_name

        granted: dict[str, set[str]] = {}
        for function, entries in _function_grants(_load_matrix()).items():
            for entry in entries:
                if ACTION_DELETE_ITEM in entry["actions"]:
                    granted.setdefault(entry["table"], set()).add(
                        f"arn:aws:iam::{_ACCOUNT}:role/{_role_name(function)}"
                    )
        holders = delete_item_holders(_ACCOUNT)
        assert set(holders) == set(granted)
        assert all(holders[table] in granted[table] for table in holders)
        assert _HOLDER_TABLE in holders and _NO_HOLDER_TABLE not in holders

    def test_the_fixture_tables_follow_the_matrix_rather_than_a_pinned_name(self) -> None:
        """Falsifying cell: granting the current no-holder table a holder moves the derivation."""
        holders = delete_item_holders(_ACCOUNT)
        widened = {**holders, _NO_HOLDER_TABLE: _holder_role()}
        holder_table, no_holder_table = _fixture_tables(widened)
        assert no_holder_table != _NO_HOLDER_TABLE
        assert no_holder_table not in widened
        assert holder_table in widened

    def test_the_derivation_fails_loudly_when_no_table_lacks_a_holder(self) -> None:
        every_table = dict.fromkeys(TENANT_SCOPED_TABLES, _holder_role())
        with pytest.raises(AssertionError, match="every tenant-scoped table"):
            _fixture_tables(every_table)

    def test_the_derivation_fails_loudly_when_no_table_has_a_holder(self) -> None:
        with pytest.raises(AssertionError, match="no tenant-scoped table"):
            _fixture_tables({})

    def test_a_holder_is_never_simulated_on_a_table_it_is_not_granted(self) -> None:
        iam = _FakeIam(_compliant_decisions())
        ctx = context_for_test(
            outputs=_OUTPUTS, clients={"iam": iam, "sts": _FakeSts()}, region=_REGION
        )
        list(checks(ctx))
        holder_resources = {res for role, _a, res in iam.simulated if role == _holder_role()}
        assert holder_resources == {_HOLDER_ARN}

    def test_a_table_with_no_holder_passes_on_the_isolation_shaped_deployment(self) -> None:
        results = _by_check(_run())
        assert results[f"delete_item_denied[{_NO_HOLDER_TABLE}]"].status == "pass"
        assert f"delete_item_denied[{_NO_HOLDER_TABLE}]_positive_control" not in results
        assert results[f"delete_item_denied[{_HOLDER_TABLE}]_positive_control"].status == "pass"
        assert results["delete_item_action_expressible"].status == "pass"

    def test_the_worker_allowed_delete_item_on_a_no_holder_table_still_fails(self) -> None:
        decisions = _compliant_decisions()
        decisions[(_WORKER_ROLE, ACTION_DELETE_ITEM, _NO_HOLDER_ARN)] = "allowed"
        results = _by_check(_run(decisions=decisions))
        assert results[f"delete_item_denied[{_NO_HOLDER_TABLE}]"].status == "fail"
        assert "still deployed" in results[f"delete_item_denied[{_NO_HOLDER_TABLE}]"].detail
        assert results[f"delete_item_denied[{_HOLDER_TABLE}]"].status == "pass"

    def test_an_unproven_table_arn_fails_that_tables_deny_row_only(self) -> None:
        decisions = _compliant_decisions()
        decisions[(_WORKER_ROLE, ACTION_QUERY, _NO_HOLDER_INDEX_ARN)] = "implicitDeny"
        results = _by_check(_run(decisions=decisions))
        assert results[f"worker_holds_query[{_NO_HOLDER_TABLE}]"].status == "fail"
        row = results[f"delete_item_denied[{_NO_HOLDER_TABLE}]"]
        assert row.status == "fail"
        assert "table ARN matches grant: False" in row.detail
        assert results[f"delete_item_denied[{_HOLDER_TABLE}]"].status == "pass"

    def test_no_holder_on_any_deployed_table_fails_every_deny_row(self) -> None:
        outputs = {
            **_OUTPUTS,
            "dynamodb_table_names": {_NO_HOLDER_TABLE: f"{_PREFIX}{_NO_HOLDER_TABLE}"},
        }
        results = _by_check(_run(outputs=outputs))
        assert results["delete_item_action_expressible"].status == "fail"
        assert results[f"delete_item_denied[{_NO_HOLDER_TABLE}]"].status == "fail"

    def test_the_floor_counts_the_holder_controls(self) -> None:
        ctx = context_for_test(
            outputs=_OUTPUTS,
            clients={"iam": _FakeIam(_compliant_decisions()), "sts": _FakeSts()},
            region=_REGION,
        )
        simulated_rows = [
            r for r in _run() if r.check.startswith(("worker_holds_", "delete_item_"))
        ]
        # Holder table: Query + holder control + deny; no-holder table: Query + deny; + expressible
        # + BatchWriteItem.
        assert len(simulated_rows) == 7
        assert module.expected_check_count(ctx).minimum == len(simulated_rows) + 1

    def test_the_query_control_probes_teardowns_own_index_path(self) -> None:
        """Query is simulated on each table's gsi_tenant index with an eval-* LeadingKeys context."""
        iam = _FakeIam(_compliant_decisions())
        ctx = context_for_test(
            outputs=_OUTPUTS, clients={"iam": iam, "sts": _FakeSts()}, region=_REGION
        )
        results = _by_check(list(checks(ctx)))
        query_resources = {res for role, a, res in iam.simulated if a == ACTION_QUERY}
        assert query_resources == {f"{_HOLDER_ARN}/index/gsi_tenant", _NO_HOLDER_INDEX_ARN}
        assert results[f"worker_holds_query[{_NO_HOLDER_TABLE}]"].status == "pass"
        assert results[f"delete_item_denied[{_NO_HOLDER_TABLE}]"].status == "pass"

    def test_the_index_only_grant_refuses_a_base_table_query(self) -> None:
        """Fixture control: the modelled grant refuses Query on the no-holder base-table ARN."""
        iam = _FakeIam(_compliant_decisions())
        context = [
            {
                "ContextKeyName": "dynamodb:LeadingKeys",
                "ContextKeyType": "stringList",
                "ContextKeyValues": ["eval-x"],
            }
        ]
        base = iam.simulate_principal_policy(
            PolicySourceArn=_WORKER_ROLE,
            ActionNames=[ACTION_QUERY],
            ResourceArns=[_NO_HOLDER_ARN],
            ContextEntries=context,
        )
        assert base["EvaluationResults"][0]["EvalDecision"] == "implicitDeny"

    def test_a_zero_table_mapping_floor_does_not_exceed_what_is_emitted(self) -> None:
        outputs = {**_OUTPUTS, "dynamodb_table_names": {"eval_runs": f"{_PREFIX}eval_runs"}}
        ctx = context_for_test(
            outputs=outputs,
            clients={"iam": _FakeIam(_compliant_decisions()), "sts": _FakeSts()},
            region=_REGION,
        )
        counted = _run(outputs=outputs)
        assert [r.check for r in counted] == [
            "delete_item_action_expressible",
            "teardown_was_not_vacuous",
        ]
        assert module.expected_check_count(ctx).minimum == len(counted)

# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.iam_roles`.

Most cells use a three-function grant matrix monkeypatched over `_load_matrix` and drive
`botocore.stub.Stubber` responses in `checks()`'s sorted function order.
"""

from __future__ import annotations

import fnmatch
import inspect
import json
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import boto3
import pytest
import scripts.smoke.iam_roles as iam_roles
from botocore.stub import Stubber
from scripts import proc_run
from scripts.hcl_text import blank_comments, blank_literals
from scripts.smoke.harness import CheckResult, context_for_test, coverage_check
from scripts.smoke.iam_roles import (
    _all_actions_for as _real_all_actions_for,
)
from scripts.smoke.iam_roles import (
    _function_grants as _real_function_grants,
)
from scripts.smoke.iam_roles import (
    _load_matrix as _real_load_matrix,
)
from scripts.smoke.iam_roles import (
    _terraform_index_only_grants as _real_terraform_index_only_grants,
)
from scripts.smoke.iam_roles import (
    _terraform_intended_grants as _real_terraform_intended_grants,
)
from scripts.smoke.iam_roles import checks

from crm.authz import DEMO_TENANT
from crm.toolspec.definitions import ALL_TOOLS
from eval.runs import JOB_ROW_STATUS
from eval.tenancy import CANARY_TENANT, run_id_for_tenant, tenant_for_run

#: Read directly rather than through the module under test.
_MAIN_TF = Path(__file__).resolve().parents[2] / "infra" / "main.tf"

# ---------------------------------------------------------------------------
# Fixture matrix: two functions on the synthetic-id `accounts` table, plus one function on the
# LeadingKeys-capable `reps` table.
# ---------------------------------------------------------------------------

_FIXTURE_MATRIX: dict[str, Any] = {
    "tables": {
        "accounts": {
            "partition_key": "account_id",
            "leading_keys_can_enforce_rep_isolation": False,
            "reason": "synthetic id",
        },
        "reps": {
            "partition_key": "rep_id",
            "leading_keys_can_enforce_rep_isolation": True,
            "reason": "partition key is the rep id",
        },
    },
    "grants": [
        {"function": "create_account", "table": "accounts", "actions": ["dynamodb:PutItem"]},
        {"function": "update_account", "table": "accounts", "actions": ["dynamodb:UpdateItem"]},
        {"function": "get_rep_self", "table": "reps", "actions": ["dynamodb:GetItem"]},
    ],
}

#: The order `checks()` iterates functions in: `sorted(by_function)`.
_SORTED_FUNCTIONS: tuple[str, ...] = tuple(
    sorted({g["function"] for g in _FIXTURE_MATRIX["grants"]})
)

_TABLE_ARN = "arn:aws:dynamodb:us-east-1:123456789012:table/{table}"

_OWN_GRANT_TABLE: dict[str, str] = {
    "create_account": "accounts",
    "update_account": "accounts",
    "get_rep_self": "reps",
}


#: The fixture's terraform-side view of the same three grants, stubbed over
#: `_terraform_intended_grants()` alongside `_load_matrix`.
_FIXTURE_TERRAFORM_GRANTS: dict[str, set[tuple[str, str]]] = {
    grant["function"]: {(grant["table"], action) for action in grant["actions"]}
    for grant in _FIXTURE_MATRIX["grants"]
}


@pytest.fixture(autouse=True)
def _fixture_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        iam_roles, "_load_matrix", lambda *_args, **_kwargs: json.loads(json.dumps(_FIXTURE_MATRIX))
    )
    monkeypatch.setattr(
        iam_roles,
        "_terraform_intended_grants",
        lambda *_args, **_kwargs: {
            function: set(pairs) for function, pairs in _FIXTURE_TERRAFORM_GRANTS.items()
        },
    )
    monkeypatch.setattr(iam_roles, "_terraform_index_only_grants", lambda *_args, **_kwargs: {})


def _iam_client() -> Any:
    return boto3.client("iam", region_name="us-east-1")


def _allow_statement(
    actions: list[str], resource: str = "*", condition: dict[str, Any] | None = None
) -> dict[str, Any]:
    statement: dict[str, Any] = {"Effect": "Allow", "Action": actions, "Resource": resource}
    if condition is not None:
        statement["Condition"] = condition
    return statement


def _policy_document(statements: list[dict[str, Any]]) -> dict[str, Any]:
    return {"Version": "2012-10-17", "Statement": statements}


def _default_policy_for(role_function: str) -> dict[str, Any]:
    """A plain, correctly-scoped policy document for `role_function`'s own grant."""
    table = _OWN_GRANT_TABLE[role_function]
    if role_function == "get_rep_self":
        return _policy_document(
            [
                _allow_statement(
                    ["dynamodb:GetItem"],
                    resource=_TABLE_ARN.format(table=table),
                    condition={"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["x"]}},
                )
            ]
        )
    action = "dynamodb:PutItem" if role_function == "create_account" else "dynamodb:UpdateItem"
    return _policy_document([_allow_statement([action], resource=_TABLE_ARN.format(table=table))])


def _physical_policy_for(role_function: str) -> dict[str, Any]:
    """`_default_policy_for`, written against the *physical* table ARNs a deployment really has."""
    table = _OWN_GRANT_TABLE[role_function]
    resource = _PHYSICAL_TABLE_ARN.format(table=_PHYSICAL_TABLE_NAMES[table])
    if role_function == "get_rep_self":
        return _policy_document(
            [
                _allow_statement(
                    ["dynamodb:GetItem"],
                    resource=resource,
                    condition={
                        "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["svc-mcp-gateway"]}
                    },
                )
            ]
        )
    action = "dynamodb:PutItem" if role_function == "create_account" else "dynamodb:UpdateItem"
    return _policy_document([_allow_statement([action], resource=resource)])


class _StubbedClientSet:
    """Builds one shared IAM client stub, queueing simulate + policy responses per role in order."""

    def __init__(self) -> None:
        self.client = _iam_client()
        self.stub = Stubber(self.client)

    def add_simulate(self, decision: str) -> None:
        self.stub.add_response(
            "simulate_principal_policy",
            {
                "EvaluationResults": [
                    {
                        "EvalActionName": "dynamodb:PutItem",
                        "EvalDecision": decision,
                        "EvalResourceName": "*",
                    }
                ]
            },
        )

    def add_simulate_no_result(self) -> None:
        self.stub.add_response("simulate_principal_policy", {"EvaluationResults": []})

    def add_role_policies(self, documents: list[dict[str, Any]]) -> None:
        policy_names = [f"policy-{i}" for i in range(len(documents))]
        self.stub.add_response("list_role_policies", {"PolicyNames": policy_names})
        for name, document in zip(policy_names, documents, strict=True):
            self.stub.add_response(
                "get_role_policy",
                {"RoleName": "role", "PolicyName": name, "PolicyDocument": json.dumps(document)},
            )
        self.stub.add_response("list_attached_role_policies", {"AttachedPolicies": []})

    def add_role_for(
        self,
        role_function: str,
        *,
        decisions: dict[str, str] | None = None,
        no_result_for: str | None = None,
        raise_for: str | None = None,
        policies: list[dict[str, Any]] | None = None,
        role_absent: bool = False,
    ) -> None:
        """Queue one role's full sequence: `get_role`, 3 simulate calls (own + 2 others), policies.

        `role_absent=True` queues a `NoSuchEntity` on `get_role` and nothing after it.

        `decisions` overrides the decision for a specific `other_function` (default: `"allowed"`
        for the role's own function, `"explicitDeny"` for every other). `no_result_for` /
        `raise_for` special-case one `other_function`'s simulate call.
        """
        decisions = decisions or {}
        if role_absent:
            self.stub.add_client_error(
                "get_role",
                service_error_code="NoSuchEntity",
                service_message="role not found",
            )
            return
        self.stub.add_response("get_role", {"Role": _role_stub(role_function)})
        for other_function in _SORTED_FUNCTIONS:
            if raise_for == other_function:
                self.stub.add_client_error(
                    "simulate_principal_policy",
                    service_error_code="NoSuchEntity",
                    service_message="role not found",
                )
                continue
            if no_result_for == other_function:
                self.add_simulate_no_result()
                continue
            default_decision = "allowed" if other_function == role_function else "explicitDeny"
            self.add_simulate(decisions.get(other_function, default_decision))
        self.add_role_policies(
            policies if policies is not None else [_default_policy_for(role_function)]
        )

    def activate(self) -> None:
        self.stub.activate()

    def assert_no_pending(self) -> None:
        self.stub.assert_no_pending_responses()
        self.stub.deactivate()


#: A `Role.RoleId` of at least 16 characters (botocore's only check) that is not `AROA`-prefixed,
#: so gitleaks' `aws-access-key-id` rule does not match it.
_STUB_ROLE_ID: Final[str] = "STUBROLEIDFORTESTS16"


def _role_stub(role_function: str) -> dict[str, Any]:
    """The minimal `get_role` payload the existence check reads."""
    name = f"crm-agent-eval-{role_function}-role"
    return {
        "Path": "/",
        "RoleName": name,
        "RoleId": _STUB_ROLE_ID,
        "Arn": f"arn:aws:iam::123456789012:role/{name}",
        "CreateDate": datetime(2026, 1, 1, tzinfo=UTC),
    }


def _sts_client() -> Any:
    """Return a real boto3 STS client for `Stubber` to attach to (no live AWS access)."""
    return boto3.client(  # nosec B106  # dummy keys for an offline Stubber, never sent
        "sts", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x"
    )


def _run_checks(clients: _StubbedClientSet, *, outputs: dict[str, Any] | None = None) -> list[Any]:
    sts_client = _sts_client()
    sts_stub = Stubber(sts_client)
    # Matches `_TABLE_ARN`'s account id, so the ARNs `table_arn()` builds line up with the fixtures.
    sts_stub.add_response(
        "get_caller_identity",
        {
            "Account": "123456789012",
            "UserId": "AIDAEXAMPLE",
            "Arn": "arn:aws:iam::123456789012:user/smoke-test",
        },
    )
    sts_stub.activate()
    ctx = context_for_test(
        clients={"iam": clients.client, "sts": sts_client}, outputs=outputs or {}
    )
    return list(checks(ctx))


def _find(results: list[Any], resource: str, check_predicate: Callable[[str], bool]) -> list[Any]:
    return [r for r in results if r.resource == resource and check_predicate(r.check)]


def _role(function: str) -> str:
    return f"crm-agent-eval-{function}-role"


# ---------------------------------------------------------------------------
# Happy path: every role correctly scoped, no wildcards, LeadingKeys present where capable.
# ---------------------------------------------------------------------------


def test_happy_path_all_roles_pass() -> None:
    clients = _StubbedClientSet()
    for role_function in _SORTED_FUNCTIONS:
        clients.add_role_for(role_function)

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    assert all(r.status != "fail" for r in results), [r for r in results if r.status == "fail"]
    assert {r.resource for r in results} == {_role(f) for f in _SORTED_FUNCTIONS}


# ---------------------------------------------------------------------------
# Non-vacuity: an over-granting role is detected.
# ---------------------------------------------------------------------------


def test_over_granting_role_is_detected_as_failure() -> None:
    """create_account is (wrongly) `allowed` update_account's UpdateItem@accounts action."""
    clients = _StubbedClientSet()
    clients.add_role_for(
        "create_account",
        decisions={"update_account": "allowed"},
        policies=[
            _policy_document(
                [
                    _allow_statement(
                        ["dynamodb:PutItem", "dynamodb:UpdateItem"],
                        resource=_TABLE_ARN.format(table="accounts"),
                    )
                ]
            )
        ],
    )
    clients.add_role_for("get_rep_self")
    clients.add_role_for("update_account")

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    # Scoped to the denial row; the `explicit_deny:` sibling also fails and names the over-grant.
    over_grant_failures = [
        r
        for r in results
        if r.resource == _role("create_account")
        and r.check.startswith("simulate:")
        and r.status == "fail"
        and "over-grant" in r.detail
    ]
    assert len(over_grant_failures) == 1
    assert "update_account" in over_grant_failures[0].detail


# ---------------------------------------------------------------------------
# Denial and the explicitness of that denial are two rows, rated independently.
# ---------------------------------------------------------------------------


def _pair_rows(results: list[Any], role_function: str, other_function: str) -> dict[str, Any]:
    """Return `{"denial": row, "explicit": row}` for one (role, other function) pair of the matrix."""
    rows = {
        "denial": [
            r
            for r in results
            if r.resource == _role(role_function)
            and r.check.startswith(f"simulate:{other_function}:")
        ],
        "explicit": [
            r
            for r in results
            if r.resource == _role(role_function)
            and r.check.startswith(f"explicit_deny:{other_function}:")
        ],
    }
    assert [len(v) for v in rows.values()] == [1, 1], f"expected exactly one row of each: {rows}"
    return {name: found[0] for name, found in rows.items()}


def test_implicit_deny_passes_the_denial_row_and_fails_the_explicitness_row() -> None:
    """`implicitDeny` passes the denial row and fails the explicitness row."""
    clients = _StubbedClientSet()
    clients.add_role_for("create_account", decisions={"update_account": "implicitDeny"})
    clients.add_role_for("get_rep_self")
    clients.add_role_for("update_account")

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    rows = _pair_rows(results, "create_account", "update_account")
    assert rows["denial"].status == "pass", (
        "the denial is in force and must not be a defer or a fail"
    )
    assert "implicitDeny" in rows["denial"].detail
    assert rows["explicit"].status == "fail"
    assert [r.status for r in results].count("defer") == 0


def test_an_allowed_action_still_fails_the_denial_row_so_the_assertion_is_not_lost() -> None:
    """An `allowed` decision fails the denial row and produces a `defer` on neither row."""
    clients = _StubbedClientSet()
    clients.add_role_for("create_account", decisions={"update_account": "allowed"})
    clients.add_role_for("get_rep_self")
    clients.add_role_for("update_account")

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    rows = _pair_rows(results, "create_account", "update_account")
    assert rows["denial"].status == "fail", (
        "an allowed cross-function action MUST fail the denial row"
    )
    assert "over-grant" in rows["denial"].detail
    assert rows["explicit"].status == "fail", "explicitness of a non-existent denial is not a defer"
    assert [r.status for r in results].count("defer") == 0, (
        "an over-grant was laundered into a deferred row"
    )


def test_explicit_deny_passes_both_rows_so_the_fail_is_conditional_not_blanket() -> None:
    """An `explicitDeny` decision passes both the denial and the explicitness rows."""
    clients = _StubbedClientSet()
    for role_function in _SORTED_FUNCTIONS:
        clients.add_role_for(role_function)

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    rows = _pair_rows(results, "create_account", "update_account")
    assert rows["denial"].status == "pass"
    assert rows["explicit"].status == "pass"
    assert rows["explicit"].detail == "explicitDeny"
    assert [r.status for r in results].count("defer") == 0


def test_the_implicit_deny_failure_names_the_role_the_action_and_the_missing_deny() -> None:
    """The failed explicitness row names the role, the action, the decision and the missing Deny."""
    clients = _StubbedClientSet()
    clients.add_role_for("create_account", decisions={"update_account": "implicitDeny"})
    clients.add_role_for("get_rep_self")
    clients.add_role_for("update_account")

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    detail = _pair_rows(results, "create_account", "update_account")["explicit"].detail
    assert _role("create_account") in detail
    assert "dynamodb:UpdateItem" in detail and "implicitDeny" in detail
    assert "explicit Deny" in detail and "missing" in detail
    assert "ACCEPTED" not in detail and "defer" not in detail


# ---------------------------------------------------------------------------
# Absence must fail, not skip: no evaluation result / role not found.
# ---------------------------------------------------------------------------


def test_no_evaluation_result_is_a_failure_not_a_skip() -> None:
    clients = _StubbedClientSet()
    clients.add_role_for("create_account", no_result_for="create_account")
    clients.add_role_for("get_rep_self")
    clients.add_role_for("update_account")

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    no_result_failures = [
        r
        for r in results
        if r.resource == _role("create_account")
        and r.status == "fail"
        and "no EvaluationResults" in r.detail
    ]
    assert len(no_result_failures) == 1
    assert no_result_failures[0].status == "fail"  # never "skip"


def test_role_not_found_from_simulate_call_is_a_failure() -> None:
    """A role-not-found error from the simulate call is reported as `fail`."""
    clients = _StubbedClientSet()
    clients.add_role_for("create_account", raise_for="create_account")
    clients.add_role_for("get_rep_self")
    clients.add_role_for("update_account")

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    failures = [
        r
        for r in results
        if r.resource == _role("create_account")
        and r.status == "fail"
        and "simulate_principal_policy raised" in r.detail
    ]
    assert len(failures) == 1  # never a skip, never a silent pass


# ---------------------------------------------------------------------------
# dynamodb:* wildcard detection, including near-miss forms.
# ---------------------------------------------------------------------------


def test_bare_dynamodb_star_wildcard_is_detected() -> None:
    clients = _StubbedClientSet()
    clients.add_role_for(
        "create_account",
        policies=[_policy_document([_allow_statement(["dynamodb:*"], resource="*")])],
    )
    clients.add_role_for("get_rep_self")
    clients.add_role_for("update_account")

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    wildcard = [
        r
        for r in results
        if r.resource == _role("create_account") and r.check == "no_dynamodb_wildcard"
    ]
    assert len(wildcard) == 1
    assert wildcard[0].status == "fail"
    assert "dynamodb:*" in wildcard[0].detail


def test_bare_star_action_wildcard_is_detected() -> None:
    """A bare `*` action is detected as a wildcard."""
    clients = _StubbedClientSet()
    clients.add_role_for(
        "create_account", policies=[_policy_document([_allow_statement(["*"], resource="*")])]
    )
    clients.add_role_for("get_rep_self")
    clients.add_role_for("update_account")

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    wildcard = [
        r
        for r in results
        if r.resource == _role("create_account") and r.check == "no_dynamodb_wildcard"
    ]
    assert len(wildcard) == 1
    assert wildcard[0].status == "fail"
    assert "*" in wildcard[0].detail


def test_not_action_wildcard_is_detected() -> None:
    """`NotAction` naming only non-DynamoDB actions still implicitly grants every dynamodb action."""
    clients = _StubbedClientSet()
    clients.add_role_for(
        "create_account",
        policies=[
            _policy_document([{"Effect": "Allow", "NotAction": ["s3:GetObject"], "Resource": "*"}])
        ],
    )
    clients.add_role_for("get_rep_self")
    clients.add_role_for("update_account")

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    wildcard = [
        r
        for r in results
        if r.resource == _role("create_account") and r.check == "no_dynamodb_wildcard"
    ]
    assert len(wildcard) == 1
    assert wildcard[0].status == "fail"
    assert "NotAction" in wildcard[0].detail


def test_no_wildcard_passes_when_actions_are_explicit() -> None:
    clients = _StubbedClientSet()
    clients.add_role_for("create_account")
    clients.add_role_for("get_rep_self")
    clients.add_role_for("update_account")

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    wildcard = [
        r
        for r in results
        if r.resource == _role("create_account") and r.check == "no_dynamodb_wildcard"
    ]
    assert len(wildcard) == 1
    assert wildcard[0].status == "pass"


# ---------------------------------------------------------------------------
# LeadingKeys presence/absence on a table that genuinely can enforce it (reps).
# ---------------------------------------------------------------------------


def test_leading_keys_missing_on_capable_table_is_a_failure() -> None:
    clients = _StubbedClientSet()
    clients.add_role_for("create_account")
    clients.add_role_for(
        "get_rep_self",
        policies=[
            _policy_document(
                [_allow_statement(["dynamodb:GetItem"], resource=_TABLE_ARN.format(table="reps"))]
            )
        ],
    )
    clients.add_role_for("update_account")

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    leading_keys_results = [
        r for r in results if r.resource == _role("get_rep_self") and r.check == "leading_keys:reps"
    ]
    assert len(leading_keys_results) == 1
    assert leading_keys_results[0].status == "fail"
    assert "LeadingKeys" in leading_keys_results[0].detail


def test_leading_keys_present_on_capable_table_passes() -> None:
    clients = _StubbedClientSet()
    clients.add_role_for("create_account")
    clients.add_role_for("get_rep_self")
    clients.add_role_for("update_account")

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    leading_keys_results = [
        r for r in results if r.resource == _role("get_rep_self") and r.check == "leading_keys:reps"
    ]
    assert len(leading_keys_results) == 1
    assert leading_keys_results[0].status == "pass"


def test_leading_keys_skipped_on_synthetic_id_table_with_actionable_detail() -> None:
    """`accounts`' partition key is a synthetic id -- LeadingKeys there is `skip`, not `fail`."""
    clients = _StubbedClientSet()
    clients.add_role_for("create_account")
    clients.add_role_for("get_rep_self")
    clients.add_role_for("update_account")

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    accounts_leading_keys = [
        r
        for r in results
        if r.resource == _role("create_account") and r.check == "leading_keys:accounts"
    ]
    assert len(accounts_leading_keys) == 1
    assert accounts_leading_keys[0].status == "skip"
    assert accounts_leading_keys[0].detail  # actionable, not silent


# ---------------------------------------------------------------------------
# LeadingKeys against physical table ARNs.
#
# Deployed statement resources carry the physical table name (`table/crm-agent-evalreps`), which
# `_matrix_table_for_resource_arn` maps back to the logical one (`table/reps`).
# ---------------------------------------------------------------------------

#: The deployed physical-name shape, exactly as `dynamodb_table_names` reports it.
_PHYSICAL_TABLE_NAMES: dict[str, str] = {
    "accounts": "crm-agent-evalaccounts",
    "reps": "crm-agent-evalreps",
}
_PHYSICAL_OUTPUTS: dict[str, Any] = {"dynamodb_table_names": _PHYSICAL_TABLE_NAMES}
_PHYSICAL_TABLE_ARN = "arn:aws:dynamodb:us-east-1:123456789012:table/{table}"


def _physical_arn(table: str) -> str:
    return _PHYSICAL_TABLE_ARN.format(table=_PHYSICAL_TABLE_NAMES[table])


def test_matrix_table_for_resource_arn_resolves_a_physical_table_arn() -> None:
    """A deployed physical table ARN resolves back to its logical table name."""
    resolved = iam_roles._matrix_table_for_resource_arn(
        _physical_arn("reps"),
        _FIXTURE_MATRIX["tables"],
        physical_to_logical=iam_roles._physical_to_logical(_PHYSICAL_TABLE_NAMES),
    )
    assert resolved == "reps"


def test_matrix_table_for_resource_arn_resolves_a_physical_index_arn() -> None:
    """A GSI sub-resource ARN resolves to the same logical table, not to `None`."""
    resolved = iam_roles._matrix_table_for_resource_arn(
        f"{_physical_arn('reps')}/index/by_tenant",
        _FIXTURE_MATRIX["tables"],
        physical_to_logical=iam_roles._physical_to_logical(_PHYSICAL_TABLE_NAMES),
    )
    assert resolved == "reps"


def test_a_leading_keys_eligible_grant_emits_a_real_row_against_physical_arns() -> None:
    """A physical-ARN grant on `reps` emits a real `leading_keys:reps` row and no `none_applicable`
    skip.
    """
    clients = _StubbedClientSet()
    clients.add_role_for("create_account", policies=[_physical_policy_for("create_account")])
    clients.add_role_for("get_rep_self", policies=[_physical_policy_for("get_rep_self")])
    clients.add_role_for("update_account", policies=[_physical_policy_for("update_account")])

    clients.activate()
    results = _run_checks(clients, outputs=_PHYSICAL_OUTPUTS)
    clients.assert_no_pending()

    reps_rows = [r for r in results if r.check == "leading_keys:reps"]
    assert len(reps_rows) == 1, "no real leading_keys:reps row was emitted"
    assert reps_rows[0].status == "pass"
    assert reps_rows[0].resource == _role("get_rep_self")

    assert [r for r in results if r.check == "leading_keys:accounts"], (
        "the documented synthetic-key disposition must also produce real rows"
    )
    assert not [r for r in results if r.check == "leading_keys:none_applicable"], (
        "no role may report the blanket none_applicable skip when its statements resolve"
    )


def test_leading_keys_absent_on_a_physical_capable_table_arn_is_a_failure() -> None:
    """A grant on a LeadingKeys-capable physical table ARN without the condition fails."""
    clients = _StubbedClientSet()
    clients.add_role_for("create_account", policies=[_physical_policy_for("create_account")])
    clients.add_role_for(
        "get_rep_self",
        policies=[
            _policy_document(
                [_allow_statement(["dynamodb:GetItem"], resource=_physical_arn("reps"))]
            )
        ],
    )
    clients.add_role_for("update_account", policies=[_physical_policy_for("update_account")])

    clients.activate()
    results = _run_checks(clients, outputs=_PHYSICAL_OUTPUTS)
    clients.assert_no_pending()

    reps_rows = [r for r in results if r.check == "leading_keys:reps"]
    assert len(reps_rows) == 1
    assert reps_rows[0].status == "fail"
    assert "LeadingKeys" in reps_rows[0].detail


def test_a_dynamodb_statement_resolving_to_no_known_table_is_a_failure_not_a_skip() -> None:
    """A DynamoDB Allow on a table this deployment does not own is reported as `fail`."""
    clients = _StubbedClientSet()
    clients.add_role_for(
        "create_account",
        policies=[
            _policy_document(
                [
                    _allow_statement(
                        ["dynamodb:PutItem"],
                        resource="arn:aws:dynamodb:us-east-1:123456789012:table/some-other-table",
                    )
                ]
            )
        ],
    )
    clients.add_role_for("get_rep_self", policies=[_physical_policy_for("get_rep_self")])
    clients.add_role_for("update_account", policies=[_physical_policy_for("update_account")])

    clients.activate()
    results = _run_checks(clients, outputs=_PHYSICAL_OUTPUTS)
    clients.assert_no_pending()

    unresolved = [
        r for r in results if r.resource == _role("create_account") and "leading_keys" in r.check
    ]
    assert len(unresolved) == 1
    assert unresolved[0].status == "fail"
    assert "some-other-table" in unresolved[0].detail


def test_leading_keys_admin_exceptions_holds_exactly_the_pre_token_generation_reps_grant() -> None:
    """The LeadingKeys exception set holds exactly the pre-token-generation `reps` grant."""
    assert set(iam_roles._LEADING_KEYS_ADMIN_EXCEPTIONS) == {
        ("crm-agent-eval-pre-token-generation-role", "reps")
    }
    assert all(reason for reason in iam_roles._LEADING_KEYS_ADMIN_EXCEPTIONS.values())


# ---------------------------------------------------------------------------
# Completeness: every role is still checked when one fails (n roles, first failing).
# ---------------------------------------------------------------------------


def test_a_non_existent_role_is_a_failure_and_is_never_simulated_against() -> None:
    """A non-existent role yields one `role_exists` fail and no simulate rows."""
    clients = _StubbedClientSet()
    absent = _SORTED_FUNCTIONS[0]
    for role_function in _SORTED_FUNCTIONS:
        clients.add_role_for(role_function, role_absent=role_function == absent)
    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    absent_role = iam_roles._role_name(absent)
    existence = [r for r in results if r.check == "role_exists"]
    assert len(existence) == len(_SORTED_FUNCTIONS)
    failed = [r for r in existence if r.status == "fail"]
    assert [r.resource for r in failed] == [absent_role]
    assert "does not exist" in failed[0].detail
    assert "NoSuchEntity" in failed[0].detail

    simulated = [
        r for r in results if r.resource == absent_role and r.check.startswith("simulate:")
    ]
    assert simulated == [], (
        "a role that does not exist was simulated against; its deny rows would report as verified"
    )
    # ...and every other role was still examined in full.
    for other in _SORTED_FUNCTIONS:
        if other == absent:
            continue
        rows = [
            r
            for r in results
            if r.resource == iam_roles._role_name(other) and r.check.startswith("simulate:")
        ]
        assert rows, other


def test_an_unreadable_role_existence_check_is_also_a_failure() -> None:
    """An unreadable role-existence check is reported as `fail`."""
    clients = _StubbedClientSet()
    clients.stub.add_client_error(
        "get_role", service_error_code="AccessDenied", service_message="not authorized"
    )
    for role_function in _SORTED_FUNCTIONS[1:]:
        clients.add_role_for(role_function)
    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    failed = [r for r in results if r.check == "role_exists" and r.status == "fail"]
    assert len(failed) == 1, [r.resource for r in failed]
    assert "could not establish" in failed[0].detail
    assert "AccessDenied" in failed[0].detail


def test_every_role_checked_even_when_first_role_fails() -> None:
    clients = _StubbedClientSet()
    clients.add_role_for(
        "create_account",
        decisions={"update_account": "allowed"},
        policies=[
            _policy_document(
                [
                    _allow_statement(
                        ["dynamodb:PutItem", "dynamodb:UpdateItem"],
                        resource=_TABLE_ARN.format(table="accounts"),
                    )
                ]
            )
        ],
    )
    clients.add_role_for("get_rep_self")
    clients.add_role_for("update_account")

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    resources_seen = {r.resource for r in results}
    assert resources_seen == {_role(f) for f in _SORTED_FUNCTIONS}
    # update_account and get_rep_self are still fully checked (no fail) despite create_account
    # failing.
    assert all(r.status != "fail" for r in results if r.resource == _role("update_account"))
    assert all(r.status != "fail" for r in results if r.resource == _role("get_rep_self"))


# ---------------------------------------------------------------------------
# Intended grants come from the IaC's own computed grants, and the documented matrix must match
# it exactly.
# ---------------------------------------------------------------------------


def _queue_full_matrix(
    clients: _StubbedClientSet,
    grants: dict[str, set[tuple[str, str]]],
    *,
    allowed_extra: set[tuple[str, str]] | None = None,
) -> None:
    """Queue the exact simulate + policy sequence `checks()` issues for `grants`.

    `allowed_extra` names `(table, action)` pairs every role is reported `allowed` on, used to model
    a live policy that grants a pair the documented matrix does not name.
    """
    allowed_extra = allowed_extra or set()
    for role_function in sorted(grants):
        # `checks()` establishes the role exists before simulating anything against it, so every
        # per-role sequence opens with `get_role`.
        clients.stub.add_response("get_role", {"Role": _role_stub(role_function)})
        for other_function in sorted(grants):
            for pair in sorted(grants[other_function]):
                allowed = pair in grants[role_function] or pair in allowed_extra
                clients.add_simulate("allowed" if allowed else "explicitDeny")
        clients.add_role_policies([_default_policy_for(role_function)])


def test_a_grant_terraform_intends_but_the_matrix_omits_is_not_reported_as_an_over_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant terraform intends but the matrix omits passes, and fails only the parity row."""
    drifted = {function: set(pairs) for function, pairs in _FIXTURE_TERRAFORM_GRANTS.items()}
    drifted["create_account"] = drifted["create_account"] | {("accounts", "dynamodb:GetItem")}
    monkeypatch.setattr(iam_roles, "_terraform_intended_grants", lambda *_a, **_k: drifted)

    clients = _StubbedClientSet()
    _queue_full_matrix(clients, drifted)

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    over_grants = [r for r in results if r.status == "fail" and "over-grant" in r.detail]
    assert over_grants == []

    parity = _find(results, _role("create_account"), lambda c: c == "intended_grants_parity")
    assert len(parity) == 1
    assert parity[0].status == "fail"
    assert "dynamodb:GetItem" in parity[0].detail


def test_a_grant_never_intended_by_terraform_still_fails_as_an_over_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant terraform does not intend still fails as an over-grant."""
    intended = {function: set(pairs) for function, pairs in _FIXTURE_TERRAFORM_GRANTS.items()}
    monkeypatch.setattr(iam_roles, "_terraform_intended_grants", lambda *_a, **_k: intended)

    clients = _StubbedClientSet()
    # Every role is reported `allowed` UpdateItem@accounts, which only `update_account` intends.
    _queue_full_matrix(clients, intended, allowed_extra={("accounts", "dynamodb:UpdateItem")})

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    over_grants = [r for r in results if r.status == "fail" and "over-grant" in r.detail]
    assert {r.resource for r in over_grants} == {
        _role("create_account"),
        _role("get_rep_self"),
    }


def test_unreadable_terraform_grants_is_a_failure_not_a_silent_matrix_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> dict[str, set[tuple[str, str]]]:
        raise OSError("infra/main.tf is unreadable")

    monkeypatch.setattr(iam_roles, "_terraform_intended_grants", _boom)

    clients = _StubbedClientSet()
    for role_function in _SORTED_FUNCTIONS:
        clients.add_role_for(role_function)

    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    parity = [r for r in results if r.check == "intended_grants_parity"]
    assert len(parity) == len(_SORTED_FUNCTIONS)
    assert all(r.status == "fail" for r in parity)
    assert all("unreadable" in r.detail for r in parity)
    # Coverage is not shrunk by the parse failure: the documented matrix still supplies the universe.
    assert {r.resource for r in results} == {_role(f) for f in _SORTED_FUNCTIONS}


def test_a_throttled_simulate_call_is_retried_and_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `Throttling` error on the simulate call is retried and the retry's result is used."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    client = _iam_client()
    stub = Stubber(client)
    stub.add_client_error(
        "simulate_principal_policy",
        service_error_code="Throttling",
        service_message="Rate exceeded",
    )
    stub.add_response(
        "simulate_principal_policy",
        {
            "EvaluationResults": [
                {
                    "EvalActionName": "dynamodb:GetItem",
                    "EvalDecision": "allowed",
                    "EvalResourceName": "*",
                }
            ]
        },
    )
    stub.activate()
    ctx = context_for_test(clients={"iam": client})
    decision, _ = iam_roles._simulate(
        ctx, "arn:aws:iam::123456789012:role/r", "dynamodb:GetItem", "arn"
    )
    stub.assert_no_pending_responses()
    assert decision == "allowed"


def test_a_persistent_throttle_is_still_raised_and_never_silently_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistent throttle is raised after the bounded retries."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    client = _iam_client()
    stub = Stubber(client)
    for _ in range(iam_roles._SIMULATE_MAX_ATTEMPTS):
        stub.add_client_error(
            "simulate_principal_policy",
            service_error_code="Throttling",
            service_message="Rate exceeded",
        )
    stub.activate()
    ctx = context_for_test(clients={"iam": client})
    with pytest.raises(Exception, match="Throttling"):
        iam_roles._simulate(ctx, "arn:aws:iam::123456789012:role/r", "dynamodb:GetItem", "arn")
    stub.assert_no_pending_responses()


def test_a_non_throttle_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-throttle `ClientError` such as `NoSuchEntity` is not retried."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    client = _iam_client()
    stub = Stubber(client)
    stub.add_client_error(
        "simulate_principal_policy",
        service_error_code="NoSuchEntity",
        service_message="role not found",
    )
    stub.activate()
    ctx = context_for_test(clients={"iam": client})
    with pytest.raises(Exception, match="NoSuchEntity"):
        iam_roles._simulate(ctx, "arn:aws:iam::123456789012:role/r", "dynamodb:GetItem", "arn")
    stub.assert_no_pending_responses()


def test_real_terraform_grants_and_documented_matrix_agree() -> None:
    """Every grant `infra/main.tf` intends equals `table_action_role_matrix.json`, for every
    function.

    Uses the module-level bindings captured at import, so the autouse fixture's stubs do not apply.
    """
    computed = _real_terraform_intended_grants()
    documented = {
        function: _real_all_actions_for(grants)
        for function, grants in _real_function_grants(_real_load_matrix()).items()
    }
    assert computed, "parsed no grants out of infra/main.tf"
    assert computed == documented


def _lambda_action_role_names_from_terraform() -> set[str]:
    """Every execution-role name the root `infra/*.tf` files create, read from the `module` blocks.

    Independent of `_terraform_intended_grants`: a named `function_name` (a literal, or
    `"${local.name_prefix}-<name>"`) contributes its own role, and the `for_each` block contributes
    one per tool. Roles of `iam_roles.PENDING_FUNCTIONS` are left out.
    """
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(_MAIN_TF.parent.glob("*.tf"))
    ).replace("${local.name_prefix}", "crm-agent-eval")
    blocks = re.split(r"^module\s+\"[\w-]+\"\s*\{", text, flags=re.MULTILINE)[1:]
    names: set[str] = set()
    for block in blocks:
        body = block.split("\n}\n", 1)[0]
        if not re.search(r'^\s*source\s*=\s*"\./modules/lambda_action"\s*$', body, re.MULTILINE):
            continue
        literal = re.search(r'^\s*function_name\s*=\s*"([\w-]+)"\s*$', body, re.MULTILINE)
        if literal is not None:
            if literal.group(1).removeprefix("crm-agent-eval-") not in iam_roles.PENDING_FUNCTIONS:
                names.add(f"{literal.group(1)}-role")
            continue
        if re.search(r"^\s*for_each\s*=", body, re.MULTILINE):
            interpolated = re.search(
                r'^\s*function_name\s*=\s*"([\w-]+)\$\{each\.key\}"\s*$', body, re.MULTILINE
            )
            assert interpolated is not None, "the for_each block's function_name shape has changed"
            names.update(f"{interpolated.group(1)}{tool.name}-role" for tool in ALL_TOOLS)
    return names


def test_every_matrix_key_renders_a_role_name_the_composition_actually_creates() -> None:
    """Every matrix key renders, via `_role_name`, a role name the composition creates.

    The key equals the physical function name minus the prefix; tool keys are underscored.
    """
    expected = _lambda_action_role_names_from_terraform()
    assert len(expected) >= 25, sorted(expected)  # non-vacuity: an empty parse must not pass

    matrix_functions = sorted({grant["function"] for grant in _real_load_matrix()["grants"]})
    assert matrix_functions, "parsed no functions out of the matrix"
    phantom = sorted(
        f"{function} -> {iam_roles._role_name(function)}"
        for function in matrix_functions
        if iam_roles._role_name(function) not in expected
    )
    assert phantom == [], (
        f"{len(phantom)} matrix key(s) render a role name no `lambda_action` instantiation creates: "
        f"{phantom}. `simulate-principal-policy` against a non-existent role returns implicitDeny, so "
        "these functions' deny rows would pass while proving nothing."
    )


def test_the_phantom_role_detection_catches_a_misspelled_key() -> None:
    """A matrix key that corresponds to no real function is reported."""
    expected = _lambda_action_role_names_from_terraform()
    for misspelled in ("eval_trigger", "user_settings", "not-a-function"):
        assert iam_roles._role_name(misspelled) not in expected, misspelled
    # ...and the spelling that replaced it does correspond to a real role.
    assert iam_roles._role_name("eval-trigger") in expected
    assert iam_roles._role_name("user-settings") in expected


def test_every_execution_role_the_composition_creates_is_enumerated() -> None:
    """Every execution role the composition creates is enumerated for simulation, not just the
    tools'.
    """
    expected = _lambda_action_role_names_from_terraform()
    assert len(expected) >= 25, sorted(expected)  # non-vacuity: an empty parse must not pass
    assert "crm-agent-eval-identity-interceptor-role" in expected
    assert "crm-agent-eval-pre-token-generation-role" in expected

    enumerated = {iam_roles._role_name(function) for function in _real_terraform_intended_grants()}
    unsimulated = sorted(expected - enumerated)
    assert unsimulated == [], (
        f"{len(unsimulated)} execution role(s) the composition creates are never simulated: "
        f"{unsimulated}. `simulate-principal-policy` is never called for them, so neither their "
        "grants nor their denials are proven by this check."
    )


def test_the_pending_functions_match_the_grant_gate() -> None:
    """`iam_roles.PENDING_FUNCTIONS` is the grant gate's set, so both are removed together."""
    gate_path = _MAIN_TF.parents[1] / "scripts" / "check_handler_grant_coverage.py"
    match = re.search(
        r"PENDING_FUNCTIONS: Final\[frozenset\[str\]\] = frozenset\((?:\{([^}]*)\})?\)",
        gate_path.read_text(encoding="utf-8"),
    )
    assert match is not None, "the grant gate no longer declares PENDING_FUNCTIONS"
    assert set(re.findall(r'"([\w-]+)"', match.group(1) or "")) == set(iam_roles.PENDING_FUNCTIONS)


def test_the_new_root_files_functions_are_read() -> None:
    """Non-vacuity: functions declared outside `main.tf` with interpolated names are enumerated."""
    grants = _real_terraform_intended_grants()
    assert grants["chat-target"] == {("reps", "dynamodb:GetItem"), ("reps", "dynamodb:PutItem")}
    assert grants["user-settings"] == {
        ("reps", "dynamodb:GetItem"),
        ("reps", "dynamodb:PutItem"),
        ("reps", "dynamodb:DeleteItem"),
    }
    assert grants["chat-identity-interceptor"] == {("reps", "dynamodb:GetItem")}
    assert ("eval_runs", "dynamodb:DeleteItem") in grants["eval-trigger"]
    assert not set(grants) & iam_roles.PENDING_FUNCTIONS


def test_the_interceptor_reps_grant_carries_a_pinned_leading_keys_condition() -> None:
    """The interceptor's `reps` grant in `infra/main.tf` pins `dynamodb:LeadingKeys` to the
    configured `rep_id`.
    """
    main_tf = iam_roles._MAIN_TF_PATH.read_text(encoding="utf-8")
    assert 'gateway_service_principal_rep_id = "svc-mcp-gateway"' in main_tf
    assert "leading_key_condition = {" in main_tf
    assert "values    = [local.gateway_service_principal_rep_id]" in main_tf


# ---------------------------------------------------------------------------
# Lazy emission plus a derived expected-check floor.
# ---------------------------------------------------------------------------


def test_checks_is_a_generator_not_a_materialised_list() -> None:
    """`checks` is a generator function, so the harness can keep rows yielded before a raise."""
    ctx = context_for_test(clients={"iam": _iam_client(), "sts": _sts_client()}, outputs={})
    assert inspect.isgenerator(checks(ctx))


def _queue_role_without_existence_check(clients: _StubbedClientSet, role_function: str) -> None:
    """Queue one role's simulate and policy calls, minus `get_role`, for patched `_role_exists`."""
    for other_function in _SORTED_FUNCTIONS:
        clients.add_simulate("allowed" if other_function == role_function else "explicitDeny")
    clients.add_role_policies([_default_policy_for(role_function)])


def test_rows_yielded_before_a_later_role_raises_are_all_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows yielded before `_role_exists` raises for a later role are all kept by the consumer."""
    surviving_roles = _SORTED_FUNCTIONS[:2]

    def _fake_role_exists(_ctx: Any, role_name: str) -> bool:
        if role_name == _role(_SORTED_FUNCTIONS[2]):
            raise TimeoutError("planted at the third role's existence probe")
        return True

    monkeypatch.setattr(iam_roles, "_role_exists", _fake_role_exists)

    clients = _StubbedClientSet()
    for role_function in surviving_roles:
        _queue_role_without_existence_check(clients, role_function)
    clients.activate()

    sts_client = _sts_client()
    sts_stub = Stubber(sts_client)
    sts_stub.add_response(
        "get_caller_identity",
        {
            "Account": "123456789012",
            "UserId": "AIDAEXAMPLE",
            "Arn": "arn:aws:iam::123456789012:user/smoke-test",
        },
    )
    sts_stub.activate()
    ctx = context_for_test(clients={"iam": clients.client, "sts": sts_client}, outputs={})

    collected: list[Any] = []
    raised: BaseException | None = None
    try:
        for result in checks(ctx):
            collected.append(result)
    except TimeoutError as exc:
        raised = exc

    assert raised is not None, "the planted seam did not propagate out of checks()"
    # The interrupted role keeps the one row it had already yielded: `intended_grants_parity` is
    # emitted before the existence probe.
    interrupted = _role(_SORTED_FUNCTIONS[2])
    assert {r.resource for r in collected} == {_role(f) for f in surviving_roles} | {interrupted}, (
        "the rows already yielded before the raise did not survive it"
    )
    assert [r.check for r in collected if r.resource == interrupted] == ["intended_grants_parity"]
    for role_function in surviving_roles:
        rows = _find(collected, _role(role_function), lambda _c: True)
        assert {r.check for r in rows} >= {
            "intended_grants_parity",
            "role_exists",
            "no_dynamodb_wildcard",
        }, f"{role_function} yielded an incomplete row set before the raise: {rows}"
        assert len([r for r in rows if r.check.startswith("simulate:")]) == len(_SORTED_FUNCTIONS)


def _real_intended_grants() -> dict[str, set[tuple[str, str]]]:
    """The production intended-grant map, read independently of the module under test's helpers."""
    documented = {
        function: _real_all_actions_for(grants)
        for function, grants in _real_function_grants(_real_load_matrix()).items()
    }
    merged = dict(documented)
    merged.update(_real_terraform_intended_grants())
    return merged


def test_expected_check_count_is_derived_from_the_real_files_not_hardcoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`expected_check_count`'s floor equals the count recomputed here from `infra/main.tf` and the
    matrix.
    """
    monkeypatch.setattr(iam_roles, "_load_matrix", _real_load_matrix)
    monkeypatch.setattr(iam_roles, "_terraform_intended_grants", _real_terraform_intended_grants)
    monkeypatch.setattr(
        iam_roles, "_terraform_index_only_grants", _real_terraform_index_only_grants
    )

    intended = _real_intended_grants()
    roles = len(intended)
    triples = sum(len(pairs) for pairs in intended.values())
    # Non-vacuity: a derivation that read almost nothing would satisfy the equality below trivially.
    assert roles >= 20 and triples >= 100, f"derivation looks empty: {roles} roles, {triples} pairs"

    expectation = iam_roles.expected_check_count(context_for_test(clients={}, outputs={}))
    # Two rows per non-own pair. `own_rows` is a pairwise intersection sum, not `triples`: two
    # functions can share a (table, action) pair, and ownership is decided by membership.
    own_rows = sum(len(intended[other] & intended[role]) for role in intended for other in intended)
    non_own_rows = roles * triples - own_rows
    assert non_own_rows > 0, "a composition with no cross-role pair cannot exercise this floor"
    scope_rows = sum(
        1
        for (function, table, action), index in _real_terraform_index_only_grants().items()
        if index == "gsi_tenant" and (table, action) in intended.get(function, set())
    )
    tenant_index_grants = len(
        {
            (function, grant.table, action)
            for function, local in iam_roles.SWEEP_GRANT_LOCALS.items()
            for grant in iam_roles.terraform_local_grants(local)
            if "gsi_tenant" in grant.index_names
            for action in grant.actions
        }
    )
    assert scope_rows == tenant_index_grants > 0, (scope_rows, tenant_index_grants)
    matrix = _real_load_matrix()
    conditioned = [
        grant["function"]
        for grant in matrix["grants"]
        if grant.get("isolation_mechanism") in ("leading_keys", "key_prefix")
        for action in grant["actions"]
        if (grant["function"], grant["table"], action) not in _real_terraform_index_only_grants()
        and (grant["table"], action) in intended.get(grant["function"], set())
    ]
    conditioned_rows = len(conditioned)
    sweep_rows = sum(1 for function in conditioned if function in iam_roles.SWEEP_GRANT_LOCALS)
    tool_rows = conditioned_rows - sweep_rows
    assert (tool_rows, sweep_rows) == (
        len(iam_roles.conditioned_tool_scopes()),
        sum(1 for t in iam_roles.conditioned_scopes() if t[0] in iam_roles.SWEEP_GRANT_LOCALS),
    ), (conditioned_rows, sweep_rows)
    assert tool_rows > 0 and sweep_rows > 0
    assert expectation.minimum == (
        roles * triples + non_own_rows + scope_rows + conditioned_rows + roles * 4
    )
    assert str(roles) in expectation.basis and str(triples) in expectation.basis
    assert f"{scope_rows} index_scope_denied" in expectation.basis
    assert f"{conditioned_rows} conditioned_scope_denied" in expectation.basis
    assert "sweep roles' included" in expectation.basis


def test_the_floor_rejects_the_harness_s_own_single_synthesised_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor rejects an inventory holding only the harness's single synthesised failure."""
    monkeypatch.setattr(iam_roles, "_load_matrix", _real_load_matrix)
    monkeypatch.setattr(iam_roles, "_terraform_intended_grants", _real_terraform_intended_grants)
    ctx = context_for_test(clients={}, outputs={})
    expectation = iam_roles.expected_check_count(ctx)

    synthesised = [
        CheckResult(
            resource=iam_roles.RESOURCE_CLASS,
            check="checks",
            status="fail",
            detail="EndpointConnectionError: ...",
        )
    ]
    verdict = coverage_check(iam_roles.RESOURCE_CLASS, synthesised, expectation)
    assert verdict.status == "fail"
    assert str(expectation.minimum) in verdict.detail


def test_a_complete_run_satisfies_its_own_floor() -> None:
    """A complete run over the fixture matrix satisfies its own floor."""
    clients = _StubbedClientSet()
    for role_function in _SORTED_FUNCTIONS:
        clients.add_role_for(role_function)
    clients.activate()
    results = _run_checks(clients)
    clients.assert_no_pending()

    expectation = iam_roles.expected_check_count(context_for_test(clients={}, outputs={}))
    assert len(results) >= expectation.minimum, (
        f"a clean run emitted {len(results)} rows against a floor of {expectation.minimum} "
        f"({expectation.basis})"
    )
    assert coverage_check(iam_roles.RESOURCE_CLASS, results, expectation).status == "pass"


def test_the_floor_derivation_raises_rather_than_bounding_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor derivation raises on an empty read rather than returning a small floor."""
    monkeypatch.setattr(iam_roles, "_load_matrix", lambda *_a, **_k: {"tables": {}, "grants": []})
    monkeypatch.setattr(iam_roles, "_terraform_intended_grants", lambda *_a, **_k: {})
    with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="cannot bound anything"):
        iam_roles.expected_check_count(context_for_test(clients={}, outputs={}))


def test_main_wires_the_derived_expectation_into_the_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`main` passes `expected_check_count` itself to `run_main`."""
    captured: dict[str, Any] = {}

    def _fake_run_main(_argv: Any, **kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(iam_roles, "run_main", _fake_run_main)
    assert iam_roles.main([]) == 0
    assert captured["expected"] is iam_roles.expected_check_count
    assert captured["checks"] is iam_roles.checks


class TestHclProseCannotMisDelimitTheGrantMatrix:
    """Braces in HCL prose mis-delimit neither of the two `infra/main.tf` grant parsers in
    `iam_roles.py`.
    """

    GRANT = '{ table = "accounts", actions = ["dynamodb:GetItem"] }'

    def _locals(self, note: str) -> str:
        return (
            "locals {\n"
            "  tool_dynamodb_grants = {\n"
            f"{note}"
            f"    get_account = [{self.GRANT}]\n"
            "  }\n"
            "  other_grants = {\n"
            '    get_contact = [{ table = "contacts", actions = ["dynamodb:PutItem"] }]\n'
            "  }\n"
            "}\n"
        )

    def test_a_brace_in_a_quoted_string_does_not_absorb_the_next_local(self) -> None:
        """The over-read direction: `tool_dynamodb_grants` claiming `other_grants`' pairs."""
        got = iam_roles._terraform_local_grant_definitions(
            self._locals('    note = "an opening brace { in prose"\n')
        )
        assert sorted(got["tool_dynamodb_grants"]) == [("accounts", "dynamodb:GetItem")]

    def test_a_brace_in_a_heredoc_does_not_absorb_the_next_local(self) -> None:
        got = iam_roles._terraform_local_grant_definitions(
            self._locals("    note = <<-EOT\n      an opening brace { in prose\n    EOT\n")
        )
        assert sorted(got["tool_dynamodb_grants"]) == [("accounts", "dynamodb:GetItem")]

    def test_prose_free_locals_still_parse(self) -> None:
        """Prose-free locals parse to their grants."""
        got = iam_roles._terraform_local_grant_definitions(self._locals(""))
        assert sorted(got["tool_dynamodb_grants"]) == [("accounts", "dynamodb:GetItem")]
        assert sorted(got["other_grants"]) == [("contacts", "dynamodb:PutItem")]

    def test_a_heredoc_containing_the_block_terminator_does_not_truncate(
        self, tmp_path: Path
    ) -> None:
        """A heredoc body containing the `  }` block terminator does not truncate
        `_terraform_tool_grants`.
        """
        main_tf = tmp_path / "main.tf"
        main_tf.write_text(
            "locals {\n"
            "  tool_dynamodb_grants = {\n"
            "    get_account = [\n"
            f"      {self.GRANT},\n"
            "    ]\n"
            "    note = <<-EOT\n"
            "  }\n"
            "    EOT\n"
            "    get_contact = [\n"
            '      { table = "contacts", actions = ["dynamodb:GetItem"] },\n'
            "    ]\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        assert sorted(iam_roles._terraform_tool_grants(main_tf)) == ["get_account", "get_contact"]

    def test_the_real_main_tf_still_parses_every_function(self) -> None:
        """The committed `infra/main.tf` still parses to every function."""
        assert len(iam_roles._terraform_tool_grants()) >= 20

    def test_a_closing_brace_in_prose_does_not_truncate_the_value(self) -> None:
        """An unbalanced closing brace in prose does not truncate the grant value."""
        got = iam_roles._terraform_local_grant_definitions(
            self._locals('    note = "a closing brace } in prose"\n')
        )
        assert sorted(got["tool_dynamodb_grants"]) == [("accounts", "dynamodb:GetItem")]

    def test_the_module_imports_under_its_real_invocation_path(self) -> None:
        """The module imports under `python -m scripts.smoke.iam_roles`, as `verify_deployment.sh`
        runs it.
        """
        import sys as _sys

        result = proc_run.run(
            [_sys.executable, "-m", "scripts.smoke.iam_roles", "--help"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ModuleNotFoundError" not in result.stderr


class TestTheEmptyGrantsGuardIsAPropertyNotAnAccident:
    """`_require_local_grants` raises rather than returning an empty grant set."""

    def test_an_absent_local_raises_rather_than_reporting_no_grants(self) -> None:
        with pytest.raises(
            iam_roles.UnreadableTerraformGrantsError, match="no locals block defines"
        ):
            iam_roles._require_local_grants({}, "tool_dynamodb_grants", "get_account")

    def test_an_empty_local_raises_too(self) -> None:
        """A local that parses to no pairs raises."""
        with pytest.raises(
            iam_roles.UnreadableTerraformGrantsError, match="simulated against nothing"
        ):
            iam_roles._require_local_grants(
                {"tool_dynamodb_grants": set()}, "tool_dynamodb_grants", "f"
            )

    def test_the_error_names_the_local_and_the_known_ones(self) -> None:
        """The error names the requested local and the known ones."""
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError) as caught:
            iam_roles._require_local_grants(
                {"other_grants": {("a", "b")}}, "missing_grants", "get_x"
            )
        message = str(caught.value)
        assert "missing_grants" in message and "get_x" in message
        assert "other_grants" in message, "the known locals must be listed so a typo is diagnosable"

    def test_a_populated_local_is_returned(self) -> None:
        """A populated local is returned."""
        assert iam_roles._require_local_grants(
            {"tool_dynamodb_grants": {("accounts", "dynamodb:GetItem")}},
            "tool_dynamodb_grants",
            "get_account",
        ) == {("accounts", "dynamodb:GetItem")}

    def test_the_guard_does_not_catch_a_shortened_grant_set(self) -> None:
        """`_require_local_grants` fires only on an empty set and passes a shortened one through."""
        short = {"tool_dynamodb_grants": {("accounts", "dynamodb:GetItem")}}
        assert iam_roles._require_local_grants(short, "tool_dynamodb_grants", "get_account") == {
            ("accounts", "dynamodb:GetItem")
        }, "if this ever raises, the guard grew a completeness check and this docstring is stale"


class TestCommentTextCannotMisDelimitAGrantValue:
    """No stray bracket or brace in a comment shortens a grant value read by
    `_terraform_local_grant_definitions`.

    Covers both opener shapes `infra/main.tf` uses for grant locals, `{` and `[`.
    """

    MAP_SHAPE = (
        "locals {\n"
        "  m_grants = {\n"
        '    a = [{ table = "accounts", actions = ["dynamodb:GetItem"] }]\n'
        "COMMENT"
        '    b = [{ table = "contacts", actions = ["dynamodb:GetItem"] }]\n'
        "  }\n"
        "}\n"
    )
    LIST_SHAPE = (
        "locals {\n"
        "  l_grants = [\n"
        '    { table = "accounts", actions = ["dynamodb:GetItem"] },\n'
        "COMMENT"
        '    { table = "contacts", actions = ["dynamodb:GetItem"] },\n'
        "  ]\n"
        "}\n"
    )
    BOTH = {"m_grants": MAP_SHAPE, "l_grants": LIST_SHAPE}

    @pytest.mark.parametrize("local_name", ["m_grants", "l_grants"])
    @pytest.mark.parametrize("stray", ["]", "}", "]}", "{"])
    def test_no_comment_character_shortens_the_grant_set(self, local_name: str, stray: str) -> None:
        text = self.BOTH[local_name].replace("COMMENT", f"    # a stray {stray} in prose\n")
        pairs = iam_roles._terraform_local_grant_definitions(text).get(local_name, set())
        assert sorted(pairs) == [
            ("accounts", "dynamodb:GetItem"),
            ("contacts", "dynamodb:GetItem"),
        ], f"a {stray!r} in a comment changed the parsed grants of the {local_name} shape"

    @pytest.mark.parametrize("local_name", ["m_grants", "l_grants"])
    def test_the_comment_free_shape_parses_the_same(self, local_name: str) -> None:
        """The comment-free shape parses to the same grants."""
        pairs = iam_roles._terraform_local_grant_definitions(
            self.BOTH[local_name].replace("COMMENT", "")
        ).get(local_name, set())
        assert len(pairs) == 2

    def test_no_comment_line_can_satisfy_this_parsers_patterns(self) -> None:
        """No comment line satisfies `_terraform_tool_grants`'s terminator or its three patterns."""
        comment_lines = [
            "  #   }",
            "  # }",
            "#  }",
            "    # }",
            "  #}",
            "  # tool_dynamodb_grants = {",
            "    # a = [",
            '    # { table = "x", actions = ["y"] }',
        ]
        for line in comment_lines:
            assert line.rstrip() != "  }", f"{line!r} satisfies the block terminator"
            assert not iam_roles._TOOL_GRANTS_BLOCK_RE.match(line), line
            assert not iam_roles._TOOL_GRANTS_FUNCTION_RE.match(line), line
            assert not iam_roles._TOOL_GRANTS_ENTRY_RE.match(line), line

        # The real, non-comment forms do match.
        assert "  }".rstrip() == "  }"
        assert iam_roles._TOOL_GRANTS_BLOCK_RE.match("  tool_dynamodb_grants = {")
        assert iam_roles._TOOL_GRANTS_FUNCTION_RE.match("    get_account = [")
        assert iam_roles._TOOL_GRANTS_ENTRY_RE.match(
            '      { table = "accounts", actions = ["dynamodb:GetItem"] },'
        )

    def test_the_correct_code_control_is_unchanged_by_the_blanking(self) -> None:
        """The real grant locals and tool grants parse to their pinned (table, action) counts."""
        locals_grants = iam_roles._terraform_local_grant_definitions(
            iam_roles._MAIN_TF_PATH.read_text(encoding="utf-8")
        )
        # These count (table, action) tuples, not `{ table, actions }` entries, so adding one action
        # to an existing entry moves a number here by one.
        assert {name: len(pairs) for name, pairs in locals_grants.items()} == {
            "dynamodb_grants": 1,
            "eval_trigger_eval_runs_grants": 6,
            "tool_dynamodb_grants": 33,
        }
        assert len(iam_roles._terraform_tool_grants()) == len(ALL_TOOLS)

    @pytest.mark.parametrize(
        "block_comment",
        [
            pytest.param("    /* a stray } in prose */\n", id="single-line"),
            pytest.param("    /* a stray ] in prose */\n", id="single-line-bracket"),
            pytest.param(
                "    /* a stray } here\n       and more prose */\n", id="closer-on-line-1"
            ),
            pytest.param(
                "    /* prose on line one\n       a stray } here */\n", id="closer-on-line-2"
            ),
        ],
    )
    def test_a_block_comment_cannot_mis_delimit_a_grant_value_either(
        self, block_comment: str
    ) -> None:
        """A `/* … */` block comment, single- or multi-line, does not mis-delimit a grant value."""
        text = self.MAP_SHAPE.replace("COMMENT", block_comment)
        pairs = iam_roles._terraform_local_grant_definitions(text).get("m_grants", set())
        assert sorted(pairs) == [
            ("accounts", "dynamodb:GetItem"),
            ("contacts", "dynamodb:GetItem"),
        ], "a block comment mis-delimited the grant value"

    #: Lines carrying `//` inside a quoted string. A line qualifies iff blanking its string literals
    #: removes the `//`, which separates `value = "https://…"` from `# https://docs…`.
    @staticmethod
    def _is_url_bearing_code(line: str) -> bool:
        from scripts.hcl_text import blank_string_literals

        return "//" in line and "//" not in blank_string_literals(line)

    #: Code lines with `//` inside a quoted string, owned by this test.
    URL_BEARING_CODE = (
        '  value = "https://example.invalid/callback"',
        '  api_gateway_domain_name = replace(replace(module.api.x, "https://", ""), "/", "")',
        '  cognito_issuer = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_x"',
        '  weird = "a//b"',
    )

    #: Lines whose `//` is in a comment; `blank_comments` blanks these, so they must not qualify.
    URL_BEARING_PROSE = (
        "  # see https://docs.aws.amazon.com/x",
        "  # the URL is https://example.invalid and it is not code",
    )

    @classmethod
    def _url_bearing_code_lines(cls) -> list[tuple[str, int, str]]:
        repo_root = Path(__file__).resolve().parents[2]
        found: list[tuple[str, int, str]] = []
        for path in sorted((repo_root / "infra").rglob("*.tf")):
            if ".terraform" in path.parts or "build" in path.parts:
                continue
            relative = path.relative_to(repo_root).as_posix()
            for lineno, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
                if cls._is_url_bearing_code(line):
                    found.append((relative, lineno, line))
        return found

    @staticmethod
    def _scanned_tf_files() -> int:
        repo_root = Path(__file__).resolve().parents[2]
        return sum(
            1
            for path in (repo_root / "infra").rglob("*.tf")
            if ".terraform" not in path.parts and "build" not in path.parts
        )

    def test_a_url_inside_a_quoted_string_survives_comment_blanking(self) -> None:
        """A `//` inside a quoted string such as `https://` survives comment blanking."""
        from scripts.hcl_text import blank_comments

        assert self.URL_BEARING_CODE, "the fixture tuple is empty, so this test proves nothing"
        for raw in self.URL_BEARING_CODE:
            assert self._is_url_bearing_code(raw), f"fixture no longer qualifies: {raw!r}"
            assert blank_comments(raw + "\n").split("\n")[0] == raw, (
                f"blank_comments altered live code: {raw!r}"
            )

    def test_the_real_tree_carries_no_line_this_would_mangle(self) -> None:
        """No URL-bearing code line in `infra/` is mangled by comment blanking; the file walk is
        non-empty.
        """
        from scripts.hcl_text import blank_comments

        assert self._scanned_tf_files() >= 20, (
            f"only {self._scanned_tf_files()} .tf files walked under infra/; the glob is broken, so a "
            "clean result below would mean nothing was examined"
        )
        for relative, lineno, raw in self._url_bearing_code_lines():
            assert blank_comments(raw + "\n").split("\n")[0] == raw, (
                f"blank_comments altered {relative}:{lineno}, which is live code and not a comment"
            )

    def test_the_discriminator_tells_a_url_in_code_from_a_url_in_a_comment(self) -> None:
        """`_url_bearing_code_lines` selects a URL in code and rejects a URL in a comment."""
        for raw in self.URL_BEARING_CODE:
            assert self._is_url_bearing_code(raw), (
                f"a `//` inside a quoted string is live code a blanker would eat, and must qualify: {raw!r}"
            )
        for raw in self.URL_BEARING_PROSE:
            assert not self._is_url_bearing_code(raw), (
                f"a URL inside a comment must NOT qualify — `blank_comments` is supposed to blank it: {raw!r}"
            )


class TestThePhantomOpenerCannotSwallowGrantsThisParserReads:
    """A comment mentioning a heredoc opener does not make `_terraform_tool_grants` drop functions.

    The parser depends on `blank_heredoc_bodies` for this.
    """

    def test_a_comment_mentioning_an_opener_does_not_blank_live_code(self) -> None:
        """A comment mentioning a heredoc opener does not blank the live code after it."""
        from scripts.hcl_text import blank_heredoc_bodies

        source = "# the body is passed with <<EOT\nlive = { a = 1 }\nmore = { b = 2 }\n"
        assert blank_heredoc_bodies(source) == source, (
            "a comment mentioning an opener started a phantom heredoc body and blanked live code"
        )

    def test_the_probe_can_detect_a_real_opener(self) -> None:
        """A real heredoc opener does blank its body."""
        from scripts.hcl_text import blank_heredoc_bodies

        real = "x = <<EOT\n  a brace { inside the body\nEOT\ny = 2\n"
        assert blank_heredoc_bodies(real) != real, (
            "blank_heredoc_bodies no longer blanks a REAL heredoc body, so the probe above proves nothing"
        )

    def test_a_phantom_opener_in_a_locals_comment_loses_no_function(self, tmp_path: Path) -> None:
        """A phantom opener in a `locals` comment drops no `_terraform_tool_grants` function."""
        main_tf = tmp_path / "main.tf"
        main_tf.write_text(
            "locals {\n"
            "  tool_dynamodb_grants = {\n"
            "    # the grants are also passed with <<EOT to the module\n"
            "    get_account = [\n"
            '      { table = "accounts", actions = ["dynamodb:GetItem"] },\n'
            "    ]\n"
            "    get_contact = [\n"
            '      { table = "contacts", actions = ["dynamodb:GetItem"] },\n'
            "    ]\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        assert sorted(iam_roles._terraform_tool_grants(main_tf)) == ["get_account", "get_contact"]


# ---------------------------------------------------------------------------
# A function with no DynamoDB grant is still simulated.
#
# `capabilities` touches no table, so it is granted no DynamoDB action.
# ---------------------------------------------------------------------------


_EMPTY_GRANTS_MODULE = """\
module "lambda_action_probe" {
  source = "./modules/lambda_action"

  function_name = "crm-agent-eval-probe"
  handler       = "crm.handlers.probe.handler"

  dynamodb_grants = []
}
"""


def test_a_declared_empty_grants_list_enumerates_the_function_with_no_pairs(
    tmp_path: Path,
) -> None:
    """A declared `dynamodb_grants = []` creates the function key with no pairs."""
    path = tmp_path / "main.tf"
    path.write_text(_EMPTY_GRANTS_MODULE, encoding="utf-8")
    grants = iam_roles._terraform_literal_module_grants(path)
    assert "probe" in grants, "a declared-empty grants list must still enumerate the function"
    assert grants["probe"] == set()


def test_an_unreadable_grants_reference_still_raises_rather_than_reading_as_empty(
    tmp_path: Path,
) -> None:
    """An unreadable grants reference raises `UnreadableTerraformGrantsError` rather than reading as
    empty.
    """
    path = tmp_path / "main.tf"
    path.write_text(
        _EMPTY_GRANTS_MODULE.replace(
            "dynamodb_grants = []", "dynamodb_grants = [local.no_such_local]"
        ),
        encoding="utf-8",
    )
    with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="no_such_local"):
        iam_roles._terraform_literal_module_grants(path)


@pytest.mark.parametrize(
    "line",
    [
        "  dynamodb_grants = []",
        "  dynamodb_grants = [ ]",
        "\tdynamodb_grants = []",
    ],
)
def test_the_empty_grants_pattern_accepts_the_spellings_terraform_fmt_can_produce(
    line: str,
) -> None:
    assert iam_roles._GRANTS_EMPTY_LIST_RE.match(line) is not None, line


@pytest.mark.parametrize(
    "line",
    [
        "  dynamodb_grants = [",
        "  dynamodb_grants = [local.eval_runs_grant]",
        '  dynamodb_grants = [{ table_name = "accounts" }]',
        "  denied_table_arns = []",
    ],
)
def test_the_empty_grants_pattern_does_not_swallow_the_other_forms(line: str) -> None:
    """The empty-grants pattern matches none of the block-open, by-reference or inline-literal
    forms.
    """
    assert iam_roles._GRANTS_EMPTY_LIST_RE.match(line) is None, line


def test_the_capabilities_role_is_enumerated_and_holds_no_grant() -> None:
    """In the real composition the `capabilities` role is enumerated and holds no grant."""
    computed = _real_terraform_intended_grants()
    assert "capabilities" in computed, (
        "the capabilities execution role is absent from the grant enumeration, so "
        "simulate-principal-policy is never called for it and its zero-grant property is unproven"
    )
    assert computed["capabilities"] == set(), (
        "capabilities must hold NO DynamoDB grant: it reads a bundled YAML file and touches no table"
    )


def test_the_matrix_declares_the_zero_grant_function_rather_than_omitting_it() -> None:
    """The real matrix declares the zero-grant function explicitly, with no grant row."""
    matrix = _real_load_matrix()
    declared = matrix["functions_without_dynamodb_grants"]
    assert "capabilities" in declared, declared
    documented = _real_function_grants(matrix)
    assert documented["capabilities"] == []
    assert _real_all_actions_for(documented["capabilities"]) == set()
    # Nor is it declared as a grant row with an empty actions list.
    assert not [grant for grant in matrix["grants"] if grant["function"] == "capabilities"], (
        "capabilities must hold no grant ROW at all, only the explicit zero declaration"
    )


def test_removing_the_matrix_declaration_makes_the_two_sides_disagree() -> None:
    """Removing the zero-grant declaration makes the matrix and `infra/main.tf` disagree."""
    matrix = dict(_real_load_matrix())
    matrix["functions_without_dynamodb_grants"] = [
        function
        for function in matrix["functions_without_dynamodb_grants"]
        if function != "capabilities"
    ]
    documented = {
        function: _real_all_actions_for(grants)
        for function, grants in _real_function_grants(matrix).items()
    }
    computed = _real_terraform_intended_grants()
    assert computed != documented, (
        "dropping the declaration changed nothing, so it was never load-bearing"
    )
    assert set(computed) - set(documented) == {"capabilities"}


# ---------------------------------------------------------------------------
# Index-only grants (`include_table_arn = false`) are simulated against their index ARN.
# ---------------------------------------------------------------------------

_INDEX_ONLY_MODULE = """\
locals {
  probe_grants = [
    { table = "reps", actions = ["dynamodb:Query"], index_names = INDEX_NAMES, include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = ["eval-*"] } },
    { table = "reps", actions = ["dynamodb:GetItem"], index_names = [] },
  ]
}

module "lambda_action_probe" {
  source = "./modules/lambda_action"

  function_name = "crm-agent-eval-probe"

  dynamodb_grants = [
    for grant in local.probe_grants : {
      table_name = grant.table
    }
  ]
}
"""


def _index_only_main_tf(
    tmp_path: Path, index_names: str = '["gsi_tenant"]', extra: str = ""
) -> Path:
    path = tmp_path / "main.tf"
    path.write_text(
        _INDEX_ONLY_MODULE.replace("INDEX_NAMES", index_names) + extra, encoding="utf-8"
    )
    return path


class TestIndexOnlyGrantReader:
    """`_terraform_index_only_grants` reads which grants cover only an index, never the table."""

    def test_the_real_composition_declares_exactly_the_status_and_sweep_index_queries(self) -> None:
        """The index-only reader agrees with the structured grant reader over the sweep locals."""
        expected = {("eval-trigger", "eval_runs", "dynamodb:Query"): "gsi_status"}
        for function, local in iam_roles.SWEEP_GRANT_LOCALS.items():
            for grant in iam_roles.terraform_local_grants(local):
                if not grant.include_table_arn:
                    for action in grant.actions:
                        (index,) = grant.index_names
                        expected[(function, grant.table, action)] = index
        # The revert child check's scoped Query grants, first index of each.
        expected |= {
            ("revert_audit_record", "contacts", "dynamodb:Query"): "gsi_account",
            ("revert_audit_record", "opportunities", "dynamodb:Query"): "gsi_account",
            ("revert_audit_record", "activities", "dynamodb:Query"): "gsi_account",
            ("revert_audit_record", "notes", "dynamodb:Query"): "gsi_opportunity",
        }
        assert _real_terraform_index_only_grants() == expected
        # These index grants are gone.
        for removed in (
            ("sweep-control", "eval_runs", "dynamodb:Query"),
            ("sweep-task", "audit_records", "dynamodb:Query"),
            ("sweep-task", "proposals", "dynamodb:Query"),
            ("sweep-task", "usage_records", "dynamodb:Query"),
            ("sweep-task", "reps", "dynamodb:Query"),
        ):
            assert removed not in expected, removed

    def test_a_comprehension_over_a_string_list_local_expands_to_one_grant_per_item(self) -> None:
        text = (
            "locals {\n"
            '  probe_tables = ["accounts", "notes"]\n'
            "  probe_grants = concat(\n"
            '    [{ table = "reps", actions = ["dynamodb:GetItem"], index_names = [] }],\n'
            '    [for t in local.probe_tables : { table = t, actions = ["dynamodb:GetItem"], '
            "index_names = [] }],\n"
            "  )\n"
            "}\n"
        )
        expanded = iam_roles._expand_string_list_comprehensions(text)
        assert iam_roles._terraform_local_grant_definitions(expanded)["probe_grants"] == {
            ("reps", "dynamodb:GetItem"),
            ("accounts", "dynamodb:GetItem"),
            ("notes", "dynamodb:GetItem"),
        }
        unresolved = text.replace("local.probe_tables", "local.not_a_list")
        assert "for t in local.not_a_list" in iam_roles._expand_string_list_comprehensions(
            unresolved
        )

    def test_an_index_only_object_is_read_with_its_index(self, tmp_path: Path) -> None:
        assert _real_terraform_index_only_grants(_index_only_main_tf(tmp_path)) == {
            ("probe", "reps", "dynamodb:Query"): "gsi_tenant"
        }

    def test_a_table_inclusive_object_is_not_index_only(self, tmp_path: Path) -> None:
        path = _index_only_main_tf(tmp_path)
        path.write_text(
            path.read_text(encoding="utf-8").replace(", include_table_arn = false", ""),
            encoding="utf-8",
        )
        assert _real_terraform_index_only_grants(path) == {}

    def test_the_same_pair_also_granted_on_the_base_table_is_not_index_only(
        self, tmp_path: Path
    ) -> None:
        path = _index_only_main_tf(tmp_path)
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                '"dynamodb:GetItem"], index_names = []', '"dynamodb:GetItem", "dynamodb:Query"]'
            ),
            encoding="utf-8",
        )
        assert _real_terraform_index_only_grants(path) == {}

    @pytest.mark.parametrize("index_names", ["[]", '["gsi_tenant", "gsi_other"]'])
    def test_an_index_only_grant_with_other_than_one_index_raises(
        self, tmp_path: Path, index_names: str
    ) -> None:
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="names indexes"):
            _real_terraform_index_only_grants(_index_only_main_tf(tmp_path, index_names))

    def test_an_index_only_grant_no_function_references_raises(self, tmp_path: Path) -> None:
        orphan = """
locals {
  orphan_grants = [
    { table = "reps", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], include_table_arn = false },
  ]
}
"""
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="declares 2"):
            _real_terraform_index_only_grants(_index_only_main_tf(tmp_path, extra=orphan))

    def test_a_commented_marker_is_not_counted(self, tmp_path: Path) -> None:
        path = _index_only_main_tf(tmp_path, extra="# include_table_arn = false\n")
        assert _real_terraform_index_only_grants(path) == {
            ("probe", "reps", "dynamodb:Query"): "gsi_tenant"
        }


_REPS_BASE_ARN = _TABLE_ARN.format(table="reps")
_REPS_INDEX_ARN = f"{_REPS_BASE_ARN}/index/gsi_tenant"


class _ArnDecidingIam:
    """An IAM stand-in that answers each simulate from the resource ARN it was actually sent.

    ``allowed`` holds `(role function, action, resource ARN)` triples answered `allowed`; every other
    simulate is `implicitDeny` on the caller's own table and `explicitDeny` elsewhere.
    """

    def __init__(self, allowed: set[tuple[str, str, str]]) -> None:
        self.allowed = allowed
        self.simulated: list[tuple[str, str, str]] = []

    @staticmethod
    def _function(role: str) -> str:
        return role.rsplit("/", 1)[-1].removeprefix("crm-agent-eval-").removesuffix("-role")

    def get_role(self, *, RoleName: str) -> dict[str, Any]:  # noqa: N803 - boto3 keyword
        return {"Role": _role_stub(self._function(RoleName))}

    def simulate_principal_policy(
        self,
        *,
        PolicySourceArn: str,  # noqa: N803 - boto3 keyword
        ActionNames: list[str],  # noqa: N803 - boto3 keyword
        ResourceArns: list[str],  # noqa: N803 - boto3 keyword
        **_kwargs: Any,
    ) -> dict[str, Any]:
        function = self._function(PolicySourceArn)
        triple = (function, ActionNames[0], ResourceArns[0])
        self.simulated.append(triple)
        own_table = _OWN_GRANT_TABLE[function]
        if triple in self.allowed:
            decision = "allowed"
        elif ResourceArns[0].startswith(_TABLE_ARN.format(table=own_table)):
            decision = "implicitDeny"
        else:
            decision = "explicitDeny"
        return {"EvaluationResults": [{"EvalActionName": ActionNames[0], "EvalDecision": decision}]}

    def list_role_policies(self, *, RoleName: str) -> dict[str, Any]:  # noqa: N803 - boto3 keyword
        return {"PolicyNames": ["inline"]}

    def get_role_policy(self, *, RoleName: str, PolicyName: str) -> dict[str, Any]:  # noqa: N803
        return {"PolicyDocument": _default_policy_for(self._function(RoleName))}

    def list_attached_role_policies(self, *, RoleName: str) -> dict[str, Any]:  # noqa: N803
        return {"AttachedPolicies": []}


#: The fixture's `get_rep_self` grant, declared index-only on `gsi_tenant`.
_FIXTURE_INDEX_ONLY: Final[dict[tuple[str, str, str], str]] = {
    ("get_rep_self", "reps", "dynamodb:GetItem"): "gsi_tenant"
}


def _run_with_arn_deciding_iam(iam: _ArnDecidingIam) -> list[Any]:
    sts_client = _sts_client()
    sts_stub = Stubber(sts_client)
    sts_stub.add_response(
        "get_caller_identity",
        {
            "Account": "123456789012",
            "UserId": "AIDAEXAMPLE",
            "Arn": "arn:aws:iam::123456789012:user/smoke-test",
        },
    )
    sts_stub.activate()
    return list(checks(context_for_test(clients={"iam": iam, "sts": sts_client}, outputs={})))


def _row(results: list[Any], function: str, check: str) -> Any:
    rows = _find(results, _role(function), lambda c: c == check)
    assert len(rows) == 1, rows
    return rows[0]


#: Each fixture role is allowed its own grant, `get_rep_self` only on the index ARN.
_INDEX_ONLY_ALLOWED: Final[set[tuple[str, str, str]]] = {
    ("create_account", "dynamodb:PutItem", _TABLE_ARN.format(table="accounts")),
    ("update_account", "dynamodb:UpdateItem", _TABLE_ARN.format(table="accounts")),
    ("get_rep_self", "dynamodb:GetItem", _REPS_INDEX_ARN),
}


class TestIndexOnlyGrantSimulation:
    """An index-only own grant passes against its index ARN and is asserted denied on the table."""

    @pytest.fixture(autouse=True)
    def _index_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            iam_roles, "_terraform_index_only_grants", lambda *_a, **_k: dict(_FIXTURE_INDEX_ONLY)
        )

    def test_the_own_index_only_grant_passes_against_the_index_arn(self) -> None:
        iam = _ArnDecidingIam(set(_INDEX_ONLY_ALLOWED))

        results = _run_with_arn_deciding_iam(iam)

        own = _row(results, "get_rep_self", "simulate:get_rep_self:dynamodb:GetItem@reps")
        assert own.status == "pass", own.detail
        assert f"simulated on {_REPS_INDEX_ARN} with LeadingKeys=" in own.detail
        assert ("get_rep_self", "dynamodb:GetItem", _REPS_INDEX_ARN) in iam.simulated

    def test_the_base_table_is_asserted_denied_for_the_index_only_grant(self) -> None:
        iam = _ArnDecidingIam(set(_INDEX_ONLY_ALLOWED))

        results = _run_with_arn_deciding_iam(iam)

        base = _row(
            results,
            "get_rep_self",
            "index_only_base_table_denied:get_rep_self:dynamodb:GetItem@reps",
        )
        assert base.status == "pass", base.detail
        assert ("get_rep_self", "dynamodb:GetItem", _REPS_BASE_ARN) in iam.simulated
        assert [r for r in results if r.check.startswith("index_only_base_table_denied:")] == [base]

    def test_a_base_table_allow_on_an_index_only_grant_fails(self) -> None:
        allowed = set(_INDEX_ONLY_ALLOWED) | {("get_rep_self", "dynamodb:GetItem", _REPS_BASE_ARN)}

        results = _run_with_arn_deciding_iam(_ArnDecidingIam(allowed))

        base = _row(
            results,
            "get_rep_self",
            "index_only_base_table_denied:get_rep_self:dynamodb:GetItem@reps",
        )
        assert base.status == "fail"
        assert "index-only" in base.detail

    def test_an_index_arn_refusal_still_fails_the_own_grant_row(self) -> None:
        allowed = set(_INDEX_ONLY_ALLOWED) - {("get_rep_self", "dynamodb:GetItem", _REPS_INDEX_ARN)}

        results = _run_with_arn_deciding_iam(_ArnDecidingIam(allowed))

        own = _row(results, "get_rep_self", "simulate:get_rep_self:dynamodb:GetItem@reps")
        assert own.status == "fail"
        assert "expected allowed, got implicitDeny" in own.detail

    def test_other_roles_are_simulated_against_the_index_arn_and_still_denied(self) -> None:
        iam = _ArnDecidingIam(set(_INDEX_ONLY_ALLOWED))

        results = _run_with_arn_deciding_iam(iam)

        for other in ("create_account", "update_account"):
            row = _row(results, other, "simulate:get_rep_self:dynamodb:GetItem@reps")
            assert row.status == "pass" and "explicitDeny" in row.detail
            assert (other, "dynamodb:GetItem", _REPS_INDEX_ARN) in iam.simulated
            assert (other, "dynamodb:GetItem", _REPS_BASE_ARN) in iam.simulated

    def test_another_role_allowed_the_pair_on_the_base_table_alone_fails(self) -> None:
        """A drifted base-table-only Allow is still an over-grant on the index-only pair."""
        allowed = set(_INDEX_ONLY_ALLOWED) | {
            ("create_account", "dynamodb:GetItem", _REPS_BASE_ARN)
        }

        results = _run_with_arn_deciding_iam(_ArnDecidingIam(allowed))

        denial = _row(results, "create_account", "simulate:get_rep_self:dynamodb:GetItem@reps")
        assert denial.status == "fail"
        assert "over-grant" in denial.detail and _REPS_BASE_ARN in denial.detail
        explicit = _row(
            results, "create_account", "explicit_deny:get_rep_self:dynamodb:GetItem@reps"
        )
        assert explicit.status == "fail"
        untouched = _row(results, "update_account", "simulate:get_rep_self:dynamodb:GetItem@reps")
        assert untouched.status == "pass"

    def test_another_role_allowed_the_pair_on_the_index_alone_fails(self) -> None:
        allowed = set(_INDEX_ONLY_ALLOWED) | {
            ("create_account", "dynamodb:GetItem", _REPS_INDEX_ARN)
        }

        results = _run_with_arn_deciding_iam(_ArnDecidingIam(allowed))

        denial = _row(results, "create_account", "simulate:get_rep_self:dynamodb:GetItem@reps")
        assert denial.status == "fail"
        assert _REPS_INDEX_ARN in denial.detail

    def test_the_explicitness_row_reads_the_weaker_of_the_two_denials(self) -> None:
        """An explicit Deny on the index but only an implicit one on the base table fails."""

        class _ImplicitOnBaseTable(_ArnDecidingIam):
            def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:
                response = super().simulate_principal_policy(**kwargs)
                function = self._function(kwargs["PolicySourceArn"])
                if function == "create_account" and kwargs["ResourceArns"] == [_REPS_BASE_ARN]:
                    response["EvaluationResults"][0]["EvalDecision"] = "implicitDeny"
                return response

        results = _run_with_arn_deciding_iam(_ImplicitOnBaseTable(set(_INDEX_ONLY_ALLOWED)))

        denial = _row(results, "create_account", "simulate:get_rep_self:dynamodb:GetItem@reps")
        assert denial.status == "pass"
        explicit = _row(
            results, "create_account", "explicit_deny:get_rep_self:dynamodb:GetItem@reps"
        )
        assert explicit.status == "fail"

    def test_without_the_index_only_map_the_own_grant_row_fails_on_the_base_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: simulating an index-only grant on the base-table ARN fails its own-grant row."""
        monkeypatch.setattr(iam_roles, "_terraform_index_only_grants", lambda *_a, **_k: {})

        results = _run_with_arn_deciding_iam(_ArnDecidingIam(set(_INDEX_ONLY_ALLOWED)))

        own = _row(results, "get_rep_self", "simulate:get_rep_self:dynamodb:GetItem@reps")
        assert own.status == "fail"
        assert "expected allowed, got implicitDeny" in own.detail

    def test_an_unreadable_index_only_map_is_a_failing_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args: Any, **_kwargs: Any) -> dict[tuple[str, str, str], str]:
            raise iam_roles.UnreadableTerraformGrantsError("unattributed index-only grant")

        monkeypatch.setattr(iam_roles, "_terraform_index_only_grants", _boom)

        results = _run_with_arn_deciding_iam(_ArnDecidingIam(set(_INDEX_ONLY_ALLOWED)))

        readable = [r for r in results if r.check == "index_only_grants_readable"]
        assert len(readable) == 1 and readable[0].status == "fail"
        assert "unattributed" in readable[0].detail


# ---------------------------------------------------------------------------
# An own pair is judged by the role's own grant shape, and an own index-only grant
# carrying a LeadingKeys condition is simulated with a key inside it.
# ---------------------------------------------------------------------------

#: `create_account` also holds `get_rep_self`'s pair, on the base table rather than the index.
_SHARED_PAIR_MATRIX_GRANT: Final[dict[str, Any]] = {
    "function": "create_account",
    "table": "reps",
    "actions": ["dynamodb:GetItem"],
}


@pytest.fixture
def _shared_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    matrix = json.loads(json.dumps(_FIXTURE_MATRIX))
    matrix["grants"].append(dict(_SHARED_PAIR_MATRIX_GRANT))
    grants = {function: set(pairs) for function, pairs in _FIXTURE_TERRAFORM_GRANTS.items()}
    grants["create_account"].add(("reps", "dynamodb:GetItem"))
    monkeypatch.setattr(iam_roles, "_load_matrix", lambda *_a, **_k: json.loads(json.dumps(matrix)))
    monkeypatch.setattr(iam_roles, "_terraform_intended_grants", lambda *_a, **_k: grants)
    monkeypatch.setattr(
        iam_roles, "_terraform_index_only_grants", lambda *_a, **_k: dict(_FIXTURE_INDEX_ONLY)
    )


@pytest.mark.usefixtures("_shared_pair")
class TestOwnGrantShape:
    _ALLOWED: Final[set[tuple[str, str, str]]] = set(_INDEX_ONLY_ALLOWED) | {
        ("create_account", "dynamodb:GetItem", _REPS_BASE_ARN)
    }

    def test_an_index_only_role_is_simulated_on_its_index_for_a_shared_pair(self) -> None:
        """The other function's table-inclusive grant does not move this role onto the table."""
        iam = _ArnDecidingIam(set(self._ALLOWED))
        results = _run_with_arn_deciding_iam(iam)
        row = _row(results, "get_rep_self", "simulate:create_account:dynamodb:GetItem@reps")
        assert row.status == "pass", row.detail
        assert f"simulated on {_REPS_INDEX_ARN} with LeadingKeys=" in row.detail

    def test_a_table_inclusive_role_is_simulated_on_the_table_for_a_shared_pair(self) -> None:
        """The other function's index-only grant does not move this role onto the index."""
        iam = _ArnDecidingIam(set(self._ALLOWED))
        results = _run_with_arn_deciding_iam(iam)
        row = _row(results, "create_account", "simulate:get_rep_self:dynamodb:GetItem@reps")
        assert row.status == "pass", row.detail
        assert ("create_account", "dynamodb:GetItem", _REPS_BASE_ARN) in iam.simulated

    def test_the_index_only_role_is_still_denied_the_base_table(self) -> None:
        iam = _ArnDecidingIam(set(self._ALLOWED))
        results = _run_with_arn_deciding_iam(iam)
        base = _row(
            results,
            "get_rep_self",
            "index_only_base_table_denied:get_rep_self:dynamodb:GetItem@reps",
        )
        assert base.status == "pass", base.detail


#: Mirrors `local.sweep_control_tenant_query_keys` in `infra/eval_sfn.tf`.
_SWEEP_CONTROL_TENANT_KEYS: Final[list[str]] = ["eval-*", "demo", "canary"]


class _ConditionedIndexIam(_ArnDecidingIam):
    """`get_rep_self`'s index grant is conditioned on `ForAllValues:StringLike` over ``values``.

    The index decision is evaluated as IAM does: allowed when every sent `dynamodb:LeadingKeys`
    value matches one pattern, and vacuously when none is sent. ``values=None`` uses
    `_SWEEP_CONTROL_TENANT_KEYS`; set `conditioned = False` to drop the condition.
    """

    def __init__(
        self, allowed: set[tuple[str, str, str]], *, values: list[str] | None = None
    ) -> None:
        super().__init__(allowed)
        self.values = list(_SWEEP_CONTROL_TENANT_KEYS) if values is None else values
        self.conditioned = True
        self.contexts: list[Any] = []

    def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:
        self.contexts.append(kwargs.get("ContextEntries"))
        sent = [
            value
            for entry in kwargs.get("ContextEntries") or []
            if entry["ContextKeyName"] == "dynamodb:LeadingKeys"
            for value in entry["ContextKeyValues"]
        ]
        function = self._function(kwargs["PolicySourceArn"])
        if (function, kwargs["ResourceArns"][0]) == ("get_rep_self", _REPS_INDEX_ARN):
            in_scope = not self.conditioned or all(
                any(fnmatch.fnmatchcase(value, pattern) for pattern in self.values)
                for value in sent
            )
            return {
                "EvaluationResults": [{"EvalDecision": "allowed" if in_scope else "implicitDeny"}]
            }
        return super().simulate_principal_policy(**kwargs)

    def get_role_policy(self, *, RoleName: str, PolicyName: str) -> dict[str, Any]:  # noqa: N803
        if self._function(RoleName) != "get_rep_self":
            return super().get_role_policy(RoleName=RoleName, PolicyName=PolicyName)
        condition = (
            {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": self.values}}
            if self.conditioned
            else None
        )
        return {
            "PolicyDocument": _policy_document(
                [
                    _allow_statement(
                        ["dynamodb:GetItem"], resource=_REPS_INDEX_ARN, condition=condition
                    )
                ]
            )
        }


_OWN_INDEX_ROW: Final[str] = "simulate:get_rep_self:dynamodb:GetItem@reps"
_SCOPE_ROW: Final[str] = "index_scope_denied:get_rep_self:dynamodb:GetItem@reps"


class TestConditionedIndexOnlyGrant:
    """An own index grant is probed with a fixed in-scope key and refuses an out-of-scope tenant."""

    @pytest.fixture(autouse=True)
    def _index_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            iam_roles, "_terraform_index_only_grants", lambda *_a, **_k: dict(_FIXTURE_INDEX_ONLY)
        )

    def _rows(self, iam: _ConditionedIndexIam) -> tuple[Any, Any]:
        results = _run_with_arn_deciding_iam(iam)
        return _row(results, "get_rep_self", _OWN_INDEX_ROW), _row(
            results, "get_rep_self", _SCOPE_ROW
        )

    def test_the_deployed_condition_passes_both_rows_with_the_fixed_keys(self) -> None:
        iam = _ConditionedIndexIam(set(_INDEX_ONLY_ALLOWED))
        own, scope = self._rows(iam)
        assert own.status == "pass", own.detail
        assert scope.status == "pass", scope.detail
        fixed = iam_roles.INDEX_IN_SCOPE_LEADING_KEY["gsi_tenant"]
        sent = [c[0]["ContextKeyValues"] for c in iam.contexts if c]
        assert [fixed] in sent and [iam_roles.OUT_OF_SCOPE_TENANT] in sent, sent

    def test_a_condition_widened_to_every_tenant_fails_the_scope_row(self) -> None:
        own, scope = self._rows(_ConditionedIndexIam(set(_INDEX_ONLY_ALLOWED), values=["*"]))
        assert own.status == "pass", own.detail
        assert scope.status == "fail"
        assert "absent or wider" in scope.detail

    def test_a_condition_without_eval_tenants_fails_the_own_row(self) -> None:
        own, scope = self._rows(
            _ConditionedIndexIam(set(_INDEX_ONLY_ALLOWED), values=["demo", "canary"])
        )
        assert own.status == "fail"
        assert "expected allowed, got implicitDeny" in own.detail
        assert scope.status == "pass", scope.detail

    def test_the_in_scope_key_is_not_derived_from_the_condition_under_test(self) -> None:
        """A condition narrowed to an `eval-` pattern the fixed in-scope key does not match fails the own
        row."""
        own, _scope = self._rows(
            _ConditionedIndexIam(set(_INDEX_ONLY_ALLOWED), values=["eval-zzz*"])
        )
        assert own.status == "fail"

    def test_an_unconditioned_tenant_index_grant_fails_the_scope_row(self) -> None:
        iam = _ConditionedIndexIam(set(_INDEX_ONLY_ALLOWED))
        iam.conditioned = False
        own, scope = self._rows(iam)
        assert own.status == "pass", own.detail
        assert scope.status == "fail"

    def test_the_fixed_keys_follow_the_codes_tenant_and_job_row_conventions(self) -> None:
        in_scope = iam_roles.INDEX_IN_SCOPE_LEADING_KEY["gsi_tenant"]
        assert in_scope == tenant_for_run(run_id_for_tenant(in_scope))
        assert iam_roles.INDEX_IN_SCOPE_LEADING_KEY["gsi_status"] == JOB_ROW_STATUS
        outsider = iam_roles.OUT_OF_SCOPE_TENANT
        assert not outsider.startswith("eval-")
        assert outsider not in {DEMO_TENANT, CANARY_TENANT}

    def test_every_real_own_index_grant_names_an_index_with_a_fixed_key(self) -> None:
        indexes = set(_real_terraform_index_only_grants().values())
        assert "gsi_tenant" in indexes, indexes
        assert indexes <= set(iam_roles.INDEX_IN_SCOPE_LEADING_KEY), indexes

    def test_an_index_with_no_fixed_key_fails_the_own_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            iam_roles,
            "_terraform_index_only_grants",
            lambda *_a, **_k: {("get_rep_self", "reps", "dynamodb:GetItem"): "gsi_unknown"},
        )
        results = _run_with_arn_deciding_iam(_ConditionedIndexIam(set(_INDEX_ONLY_ALLOWED)))
        own = _row(results, "get_rep_self", _OWN_INDEX_ROW)
        assert own.status == "fail"
        assert "gsi_unknown" in own.detail


class TestLocalGrantReader:
    """`terraform_local_grants` resolves each grant object's condition values from `locals`."""

    _TEXT = (
        "locals {\n"
        '  probe_patterns = {\n    accounts = "acct-eval-*"\n  }\n'
        '  probe_row_key = join("", [for i in range(4) : "?"])\n'
        '  probe_query_keys = ["eval-*", "demo"]\n'
        '  probe_tables = ["notes"]\n'
        "  probe_grants = concat(\n"
        '    [{ table = "accounts", actions = ["dynamodb:PutItem"], index_names = [], '
        'leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", '
        'values = [local.probe_patterns["accounts"]] } }],\n'
        '    [{ table = "eval_runs", actions = ["dynamodb:GetItem"], index_names = [], '
        'leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", '
        'values = ["job#*", local.probe_row_key] } }],\n'
        '    [{ table = "eval_results", actions = ["dynamodb:GetItem"], index_names = [] }],\n'
        '    [for t in local.probe_tables : { table = t, actions = ["dynamodb:Query"], '
        'index_names = ["gsi_tenant"], include_table_arn = false, leading_key_condition = { '
        'attribute = "dynamodb:LeadingKeys", operator = "StringLike", '
        "values = local.probe_query_keys } }],\n"
        "  )\n"
        "}\n"
    )

    def test_every_value_form_resolves(self) -> None:
        grants = {
            (g.table, g.actions): g
            for g in iam_roles.terraform_local_grants("probe_grants", self._TEXT)
        }
        assert grants[("accounts", ("dynamodb:PutItem",))].key_values == ("acct-eval-*",)
        assert grants[("eval_runs", ("dynamodb:GetItem",))].key_values == ("job#*", "????")
        unconditioned = grants[("eval_results", ("dynamodb:GetItem",))]
        assert unconditioned.key_values is None and unconditioned.operator is None
        query = grants[("notes", ("dynamodb:Query",))]
        assert query.key_values == ("eval-*", "demo") and query.operator == "StringLike"
        assert query.index_names == ("gsi_tenant",) and query.include_table_arn is False

    @pytest.mark.parametrize(
        "broken",
        ['local.probe_patterns["absent"]', "local.no_such_local", "upper(local.probe_row_key)"],
        ids=["absent-key", "absent-local", "expression"],
    )
    def test_an_unresolvable_value_is_an_error_not_an_empty_list(self, broken: str) -> None:
        text = self._TEXT.replace('local.probe_patterns["accounts"]', broken)
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError):
            iam_roles.terraform_local_grants("probe_grants", text)

    def test_an_absent_local_is_an_error(self) -> None:
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError):
            iam_roles.terraform_local_grants("no_grants", self._TEXT)

    def test_the_real_sweep_locals_resolve_with_conditions(self) -> None:
        for local in iam_roles.SWEEP_GRANT_LOCALS.values():
            grants = iam_roles.terraform_local_grants(local)
            assert any(g.key_values for g in grants), local
            assert all(v for g in grants for v in g.key_values or ()), local


class TestProbeKeys:
    @pytest.mark.parametrize(
        "value",
        [
            "acct-eval-*",
            "job#*",
            "????????????????????????????????",
            "lock#sweep",
            "eval-*",
            "demo",
        ],
    )
    def test_the_in_scope_key_is_admitted_and_every_out_of_scope_key_refused(
        self, value: str
    ) -> None:
        operator = "StringLike"
        assert iam_roles.condition_matches(value, iam_roles.in_scope_key(value), operator)
        for key in iam_roles.out_of_scope_keys([value], operator):
            assert not iam_roles.condition_matches(value, key, operator), key

    def test_a_seed_pattern_gets_a_demo_twin_and_a_minted_id_twin(self) -> None:
        keys = iam_roles.out_of_scope_keys(["acct-eval-*"], "StringLike")
        assert keys == (
            iam_roles.PROBE_FOREIGN_KEY,
            "acct-demo-0000",
            f"acct-{iam_roles.PROBE_FOREIGN_KEY}",
        )

    def test_an_unprefixed_tenant_pattern_gets_no_minted_id_twin(self) -> None:
        keys = iam_roles.out_of_scope_keys(["eval-*"], "StringLike")
        assert keys == (iam_roles.PROBE_FOREIGN_KEY, "demo-0000")

    def test_an_admitted_out_of_scope_candidate_is_an_error(self) -> None:
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError):
            iam_roles.out_of_scope_keys(["*"], "StringLike")

    def test_a_character_class_is_refused(self) -> None:
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError):
            iam_roles.in_scope_key("acct-[a-z]*")

    def test_string_equals_is_exact(self) -> None:
        assert iam_roles.condition_matches("lock#sweep", "lock#sweep", "StringEquals")
        assert not iam_roles.condition_matches("lock#*", "lock#sweep", "StringEquals")

    def test_every_real_table_level_conditioned_sweep_grant_has_an_own_key(self) -> None:
        keys = iam_roles.conditioned_own_keys()
        for function, local in iam_roles.SWEEP_GRANT_LOCALS.items():
            for grant in iam_roles.terraform_local_grants(local):
                if grant.key_values and grant.include_table_arn:
                    for action in grant.actions:
                        key = keys[(function, grant.table, action)]
                        assert any(
                            iam_roles.condition_matches(v, key, g.operator or "")
                            for g in iam_roles.terraform_local_grants(local)
                            if g.table == grant.table and action in g.actions
                            for v in g.key_values or ()
                        ), (function, grant.table, action, key)


class _ConditionedTableIam(_ArnDecidingIam):
    """`create_account`'s own `PutItem` on `accounts` is conditioned on `values` (ForAllValues)."""

    def __init__(self, allowed: set[tuple[str, str, str]], *, values: list[str]) -> None:
        super().__init__(allowed)
        self.values = values
        self.contexts: list[Any] = []

    def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:
        function = self._function(kwargs["PolicySourceArn"])
        if (function, kwargs["ActionNames"][0]) == ("create_account", "dynamodb:PutItem"):
            entries = kwargs.get("ContextEntries") or []
            self.contexts.append(entries)
            sent = [v for e in entries for v in e["ContextKeyValues"]]
            admitted = all(any(fnmatch.fnmatchcase(k, p) for p in self.values) for k in sent)
            return {
                "EvaluationResults": [{"EvalDecision": "allowed" if admitted else "implicitDeny"}]
            }
        return super().simulate_principal_policy(**kwargs)


class TestConditionedOwnGrant:
    """An own table-level conditioned grant is simulated with its in-scope key."""

    _OWN_ROW: Final[str] = "simulate:create_account:dynamodb:PutItem@accounts"

    def _own(self, iam: _ConditionedTableIam) -> Any:
        return _row(_run_with_arn_deciding_iam(iam), "create_account", self._OWN_ROW)

    def test_the_own_row_sends_the_in_scope_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = "acct-eval-0001"  # gitleaks:allow
        monkeypatch.setattr(
            iam_roles,
            "conditioned_own_keys",
            lambda: {("create_account", "accounts", "dynamodb:PutItem"): key},
        )
        iam = _ConditionedTableIam(set(_INDEX_ONLY_ALLOWED), values=["acct-eval-*"])
        own = self._own(iam)
        assert own.status == "pass", own.detail
        assert key in own.detail
        assert [e[0]["ContextKeyValues"] for e in iam.contexts if e] == [[key]]

    def test_a_condition_that_refuses_the_key_reds_the_own_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            iam_roles,
            "conditioned_own_keys",
            lambda: {("create_account", "accounts", "dynamodb:PutItem"): "acct-eval-0001"},
        )
        own = self._own(_ConditionedTableIam(set(_INDEX_ONLY_ALLOWED), values=["acct-demo-*"]))
        assert own.status == "fail"

    def test_without_the_key_the_same_condition_passes_vacuously(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control: no key sent, so ForAllValues admits it; this is what keying the probe rules out."""
        monkeypatch.setattr(iam_roles, "conditioned_own_keys", dict)
        iam = _ConditionedTableIam(set(_INDEX_ONLY_ALLOWED), values=["acct-demo-*"])
        assert self._own(iam).status == "pass"
        assert iam.contexts == [[]]

    def test_an_unreadable_condition_is_a_failing_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken() -> dict[tuple[str, str, str], str]:
            raise iam_roles.UnreadableTerraformGrantsError("unreadable")

        monkeypatch.setattr(iam_roles, "conditioned_own_keys", broken)
        results = _run_with_arn_deciding_iam(_ArnDecidingIam(set(_INDEX_ONLY_ALLOWED)))
        (row,) = [r for r in results if r.check == "conditioned_grants_readable"]
        assert row.status == "fail"


# --- unparseable grants raise; conditioned tool grants are keyed and scope-denied --------

_PROBE_LOCALS = (
    "locals {\n"
    '  probe_more = ["dynamodb:PutItem"]\n'
    "  probe_grants = [\n"
    '    { table = "reps", actions = ["dynamodb:GetItem"], index_names = [] },\n'
    "    OBJECT\n"
    "  ]\n"
    "}\n"
)
_UNPARSEABLE_OBJECTS: Final[dict[str, str]] = {
    "non-literal-table": '{ table = local.t, actions = ["dynamodb:GetItem"], index_names = [] },',
    "no-actions-list": '{ table = "reps", actions = local.probe_more, index_names = [] },',
}
_DROPPED_LOCAL_OBJECT = (
    '{ table = "reps", actions = ["dynamodb:GetItem", local.x], index_names = [] },'
)


class TestUnparseableGrantsRaise:
    """An object or list item the reader cannot read raises, never drops a grant."""

    @pytest.mark.parametrize(
        "obj", list(_UNPARSEABLE_OBJECTS.values()), ids=list(_UNPARSEABLE_OBJECTS)
    )
    def test_an_unparseable_object_in_a_grant_local_raises(self, obj: str) -> None:
        text = _PROBE_LOCALS.replace("OBJECT", obj)
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="no literal table"):
            iam_roles.terraform_local_grants("probe_grants", text)

    @pytest.mark.parametrize(
        "obj", list(_UNPARSEABLE_OBJECTS.values()), ids=list(_UNPARSEABLE_OBJECTS)
    )
    def test_the_index_reader_raises_for_a_referenced_local_only(self, obj: str) -> None:
        text = _PROBE_LOCALS.replace("OBJECT", obj)
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="no literal table"):
            iam_roles._terraform_local_index_grants(text, strict={"probe_grants"})
        lenient = iam_roles._terraform_local_index_grants(text)
        assert lenient["probe_grants"].table_inclusive == {("reps", "dynamodb:GetItem")}

    def test_the_index_only_reader_raises_on_an_unparseable_referenced_object(
        self, tmp_path: Path
    ) -> None:
        path = _index_only_main_tf(tmp_path)
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                '{ table = "reps", actions = ["dynamodb:GetItem"], index_names = [] }',
                '{ table = local.t, actions = ["dynamodb:GetItem"], index_names = [] }',
            ),
            encoding="utf-8",
        )
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="no literal table"):
            _real_terraform_index_only_grants(path)

    def test_an_unparseable_index_only_object_is_a_failing_readable_row(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path = _index_only_main_tf(tmp_path)
        path.write_text(
            path.read_text(encoding="utf-8").replace('table = "reps"', "table = local.t", 1),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            iam_roles,
            "_terraform_index_only_grants",
            lambda *_a, **_k: _real_terraform_index_only_grants(path),
        )
        results = _run_with_arn_deciding_iam(_ArnDecidingIam(set(_INDEX_ONLY_ALLOWED)))
        (row,) = [r for r in results if r.check == "index_only_grants_readable"]
        assert row.status == "fail" and "no literal table" in row.detail

    def test_a_dropped_local_reference_in_a_grant_local_raises(self) -> None:
        text = _PROBE_LOCALS.replace("OBJECT", _DROPPED_LOCAL_OBJECT)
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="local.x"):
            iam_roles.terraform_local_grants("probe_grants", text)
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="local.x"):
            iam_roles._terraform_local_index_grants(text, strict={"probe_grants"})

    def test_a_dropped_local_reference_reds_the_intended_grant_read(self) -> None:
        text = _PROBE_LOCALS.replace("OBJECT", _DROPPED_LOCAL_OBJECT) + (
            'module "lambda_action_probe" {\n'
            '  function_name = "crm-agent-eval-probe"\n'
            "  dynamodb_grants = local.probe_grants\n"
            "}\n"
        )
        assert "probe_grants" not in iam_roles._terraform_local_grant_definitions(text)
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="probe_grants"):
            iam_roles._walk_literal_module_grants(text)

    def test_a_dropped_local_reference_inline_in_a_module_block_raises(self) -> None:
        text = (
            'module "lambda_action_probe" {\n'
            '  function_name = "crm-agent-eval-probe"\n'
            "  dynamodb_grants = [\n"
            "    {\n"
            '      table_name = "reps"\n'
            '      actions    = ["dynamodb:GetItem", local.more]\n'
            "    },\n"
            "  ]\n"
            "}\n"
        )
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="local.more"):
            iam_roles._walk_literal_module_grants(text)
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="local.more"):
            iam_roles.module_grants(text)

    def test_a_dropped_local_reference_in_a_condition_value_list_raises(self) -> None:
        text = (
            "locals {\n"
            '  probe_keys = ["eval-*", local.other]\n'
            "  probe_grants = [\n"
            '    { table = "reps", actions = ["dynamodb:GetItem"], index_names = [], '
            'leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", '
            "values = local.probe_keys } },\n"
            "  ]\n"
            "}\n"
        )
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="local.other"):
            iam_roles.terraform_local_grants("probe_grants", text)

    def test_the_real_composition_reads_cleanly_under_the_strict_readers(self) -> None:
        """Positive control: the strictness reds no grant the real `infra/*.tf` declares."""
        assert _real_terraform_intended_grants()
        assert _real_terraform_index_only_grants()
        assert iam_roles.module_grants(iam_roles._root_composition_text())


_MODULE_FORMS = """\
locals {
  svc = "svc-x"
  shared_grants = [
    { table_name = "reps", actions = ["dynamodb:PutItem"], leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = ["conversation#*"] } },
  ]
}

module "lambda_action_inline" {
  function_name = "crm-agent-eval-inline"
  dynamodb_grants = [
    {
      table_name = "reps"
      table_arn  = module.dynamodb.table_arns["reps"]
      actions    = ["dynamodb:GetItem"]
      leading_key_condition = {
        attribute = "rep_id"
        values    = [local.svc]
      }
    },
  ]
}

module "lambda_action_bare" {
  function_name = "crm-agent-eval-bare"
  dynamodb_grants = local.shared_grants
}

module "lambda_action_comprehension" {
  function_name = "crm-agent-eval-comprehension"
  dynamodb_grants = [
    for grant in local.shared_grants : {
      table_name            = grant.table_name
      table_arn             = module.dynamodb.table_arns[grant.table_name]
      actions               = grant.actions
      leading_key_condition = try(grant.leading_key_condition, null)
    }
  ]
}

module "lambda_action_open" {
  function_name = "crm-agent-eval-open"
  dynamodb_grants = [
    {
      table_name = "reps"
      table_arn  = "x"
      actions    = ["dynamodb:GetItem"]
    },
    {
      table_name = "reps"
      table_arn  = "x"
      actions    = ["dynamodb:GetItem"]
      leading_key_condition = {
        attribute = "dynamodb:LeadingKeys"
        values    = ["a"]
      }
    },
  ]
}

module "lambda_action_none" {
  function_name = "crm-agent-eval-none"
  dynamodb_grants = []
}
"""


class TestModuleGrantReader:
    """`module_grants` reads each literal-named block's grants in every form the composition uses."""

    def test_every_form_is_read_and_an_absent_operator_is_string_equals(self) -> None:
        grants = iam_roles.module_grants(_MODULE_FORMS)
        (inline,) = grants["inline"]
        assert (inline.table, inline.actions) == ("reps", ("dynamodb:GetItem",))
        assert (inline.operator, inline.key_values) == ("StringEquals", ("svc-x",))
        for form in ("bare", "comprehension"):
            (shared,) = grants[form]
            assert (shared.operator, shared.key_values) == ("StringLike", ("conversation#*",))
        assert grants["none"] == []

    def test_tool_scopes_exclude_a_pair_an_unconditioned_object_also_grants(self) -> None:
        scopes = iam_roles.conditioned_tool_scopes(_MODULE_FORMS)
        assert scopes == {
            ("bare", "reps", "dynamodb:PutItem"): (("StringLike", ("conversation#*",)),),
            ("comprehension", "reps", "dynamodb:PutItem"): (("StringLike", ("conversation#*",)),),
            ("inline", "reps", "dynamodb:GetItem"): (("StringEquals", ("svc-x",)),),
        }

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("values    = [local.svc]", "values    = [upper(local.svc)]"),
            ("values    = [local.svc]", "values    = local.absent"),
            ('attribute = "rep_id"', 'attribute = "rep_id"\n        operator  = var.op'),
            ("dynamodb_grants = local.shared_grants", "dynamodb_grants = var.grants"),
        ],
        ids=["expression", "absent-local", "non-literal-operator", "non-local-reference"],
    )
    def test_a_non_literal_condition_or_value_raises(self, old: str, new: str) -> None:
        assert old in _MODULE_FORMS
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError):
            iam_roles.module_grants(_MODULE_FORMS.replace(old, new, 1))


#: The functions whose own table-level grants carry a `leading_key_condition`, read straight from
#: the three files, not through the reader under test.
def _conditioned_tool_functions_from_source() -> set[str]:
    infra = _MAIN_TF.parent
    found: set[str] = set()
    for name in ("main.tf", "mcp_chat_gateway.tf", "user_settings.tf"):
        text = (
            (infra / name)
            .read_text(encoding="utf-8")
            .replace("${local.name_prefix}", "crm-agent-eval")
        )
        for block in re.split(r'^module\s+"[\w-]+"\s*\{', text, flags=re.MULTILINE)[1:]:
            body = block.split("\n}\n", 1)[0]
            literal = re.search(
                r'^\s*function_name\s*=\s*"crm-agent-eval-([\w-]+)"', body, re.MULTILINE
            )
            if literal is None:
                continue
            reference = re.search(r"^\s*dynamodb_grants\s*=\s*local\.(\w+)\s*$", body, re.MULTILINE)
            if reference is not None:
                definition = re.search(
                    rf"^  {reference.group(1)}\s*=\s*\[(.*?)^  \]", text, re.MULTILINE | re.DOTALL
                )
                body = definition.group(1) if definition else ""
            code = "\n".join(
                line for line in body.splitlines() if not line.lstrip().startswith("#")
            )
            if "leading_key_condition" in code:
                found.add(literal.group(1))
    return found


_CONDITIONED_TOOL_FUNCTIONS: Final[set[str]] = _conditioned_tool_functions_from_source()


class _ScopeModellingIam(_ArnDecidingIam):
    """An IAM stand-in for the real composition: each role is allowed exactly its intended pairs.

    A pair in `scopes` is allowed only when `dynamodb:LeadingKeys` is sent and every key it carries
    is admitted by one of the scope's conditions; with no context it is allowed, as ForAllValues is.
    """

    def __init__(
        self,
        intended: dict[str, set[tuple[str, str]]],
        scopes: dict[tuple[str, str, str], tuple[tuple[str, tuple[str, ...]], ...]],
    ) -> None:
        super().__init__(set())
        self.intended = intended
        self.scopes = scopes
        self.calls: list[tuple[str, str, str, tuple[str, ...] | None]] = []

    def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:
        function = self._function(kwargs["PolicySourceArn"])
        action, arn = kwargs["ActionNames"][0], kwargs["ResourceArns"][0]
        table = arn.split(":table/", 1)[1].split("/", 1)[0]
        entries = [
            e
            for e in kwargs.get("ContextEntries") or []
            if e["ContextKeyName"] == "dynamodb:LeadingKeys"
        ]
        keys = tuple(v for e in entries for v in e["ContextKeyValues"]) if entries else None
        self.calls.append((function, action, table, keys))
        decision = "explicitDeny"
        if (table, action) in self.intended.get(function, set()):
            scope = self.scopes.get((function, table, action))
            admitted = (
                keys is None
                or all(
                    any(
                        iam_roles.condition_matches(v, k, op)
                        for op, values in scope
                        for v in values
                    )
                    for k in keys
                )
                if scope
                else True
            )
            decision = "allowed" if admitted else "implicitDeny"
        return {"EvaluationResults": [{"EvalActionName": action, "EvalDecision": decision}]}

    def list_role_policies(self, *, RoleName: str) -> dict[str, Any]:  # noqa: N803 - boto3 keyword
        return {"PolicyNames": []}

    def list_attached_role_policies(self, *, RoleName: str) -> dict[str, Any]:  # noqa: N803
        return {"AttachedPolicies": []}


def _real_composition_run(
    monkeypatch: pytest.MonkeyPatch,
    scopes: dict[tuple[str, str, str], tuple[tuple[str, tuple[str, ...]], ...]] | None = None,
) -> tuple[list[Any], _ScopeModellingIam]:
    monkeypatch.setattr(iam_roles, "_load_matrix", _real_load_matrix)
    monkeypatch.setattr(iam_roles, "_terraform_intended_grants", _real_terraform_intended_grants)
    monkeypatch.setattr(
        iam_roles, "_terraform_index_only_grants", _real_terraform_index_only_grants
    )
    iam = _ScopeModellingIam(_real_intended_grants(), scopes or iam_roles.conditioned_scopes())
    return _run_with_arn_deciding_iam(iam), iam


class TestConditionedToolGrants:
    """A tool role's own conditioned grant is keyed and paired with a scope denial."""

    def test_the_reader_finds_every_conditioned_tool_function_the_source_declares(self) -> None:
        named = {
            "identity-interceptor",
            "chat-identity-interceptor",
            "chat-target",
            "user-settings",
            "eval-trigger",
        }
        assert named <= _CONDITIONED_TOOL_FUNCTIONS, _CONDITIONED_TOOL_FUNCTIONS
        found = {function for function, _t, _a in iam_roles.conditioned_tool_scopes()}
        assert found == _CONDITIONED_TOOL_FUNCTIONS
        assert not found & set(iam_roles.SWEEP_GRANT_LOCALS)

    def test_each_conditioned_tool_grant_has_an_admitted_key_and_refused_probes(self) -> None:
        keys = iam_roles.conditioned_own_keys()
        for triple, scopes in iam_roles.conditioned_tool_scopes().items():
            key = keys[triple]
            assert any(
                iam_roles.condition_matches(v, key, op) for op, values in scopes for v in values
            ), (triple, key)
            denied = iam_roles.scope_denied_keys(
                scopes, [k for t, k in keys.items() if t[1] == triple[1]]
            )
            assert denied and key not in denied, (triple, denied)
            for probe in denied:
                assert not any(
                    iam_roles.condition_matches(v, probe, op)
                    for op, values in scopes
                    for v in values
                ), (triple, probe)

    def test_the_allow_and_deny_pair_is_present_for_each_conditioned_tool_grant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scopes = iam_roles.conditioned_tool_scopes()
        results, iam = _real_composition_run(monkeypatch)
        keys = iam_roles.conditioned_own_keys()
        assert len(scopes) >= len(_CONDITIONED_TOOL_FUNCTIONS)
        for function, table, action in scopes:
            own = _row(results, function, f"simulate:{function}:{action}@{table}")
            assert own.status == "pass" and keys[(function, table, action)] in own.detail, own
            deny = _row(results, function, f"conditioned_scope_denied:{function}:{action}@{table}")
            assert deny.status == "pass", deny
            sent = [c[3] for c in iam.calls if c[:3] == (function, action, table)]
            assert None not in sent, f"a context-less simulate of {(function, table, action)}"
            assert len(sent) >= 2, sent

    def test_a_scope_wider_than_declared_reds_the_deny_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deployed policy admits everything for one grant: its deny row, and only it, reds."""
        real = iam_roles.conditioned_scopes()
        target = ("user-settings", "reps", "dynamodb:GetItem")
        wide = {**real, target: (("StringLike", ("*",)),)}
        results, _iam = _real_composition_run(monkeypatch, wide)
        failing = [
            r
            for r in results
            if r.check.startswith("conditioned_scope_denied:") and r.status != "pass"
        ]
        assert [(r.resource, r.check) for r in failing] == [
            (_role("user-settings"), "conditioned_scope_denied:user-settings:dynamodb:GetItem@reps")
        ]

    def test_a_conditioned_tool_grant_without_a_key_is_refused_not_simulated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(iam_roles, "conditioned_own_keys", dict)
        monkeypatch.setattr(
            iam_roles,
            "conditioned_tool_scopes",
            lambda: {
                ("create_account", "accounts", "dynamodb:PutItem"): (
                    ("StringLike", ("acct-eval-*",)),
                )
            },
        )
        iam = _ConditionedTableIam(set(_INDEX_ONLY_ALLOWED), values=["acct-eval-*"])
        own = _row(
            _run_with_arn_deciding_iam(iam), "create_account", TestConditionedOwnGrant._OWN_ROW
        )
        assert own.status == "fail" and "refused" in own.detail
        assert iam.contexts, "the deny row of the refused grant still probes"
        assert all(iam.contexts), "the refused grant must not be simulated context-less"

    def test_a_non_literal_tool_condition_is_a_failing_readable_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broken = _MODULE_FORMS.replace("values    = [local.svc]", "values    = [upper(local.svc)]")
        monkeypatch.setattr(
            iam_roles,
            "conditioned_tool_scopes",
            lambda: iam_roles._tool_scopes(iam_roles.module_grants(broken)),
        )
        results = _run_with_arn_deciding_iam(_ArnDecidingIam(set(_INDEX_ONLY_ALLOWED)))
        (row,) = [r for r in results if r.check == "conditioned_grants_readable"]
        assert row.status == "fail" and "upper(local.svc)" in row.detail


# --- the silent-skip holes ----------------------------------------------------------------------

_SF_LOCALS = """\
locals {
  a_grants = [
    { table_name = "reps", actions = ["dynamodb:GetItem"], leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = ["conversation#*"] } },
  ]
  b_grants = [
    { table_name = "notes", actions = ["dynamodb:PutItem"], leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", values = ["note#*"] } },
  ]
}
"""


def _sf_module(body: str, header: str = 'module "lambda_action_probe" {') -> str:
    return f'{_SF_LOCALS}\n{header}\n  source = "./modules/lambda_action"\n{body}}}\n'


_SF_NAME: Final[str] = '  function_name = "crm-agent-eval-probe"\n'
_SF_COMPREHENSION_BODY: Final[str] = (
    "    for grant in local.a_grants : {\n"
    "      table_name            = grant.table_name\n"
    "      table_arn             = module.dynamodb.table_arns[grant.table_name]\n"
    "      actions               = grant.actions\n"
    "      leading_key_condition = try(grant.leading_key_condition, null)\n"
    "    }\n"
)


class TestSilentSkipHolesAreClosed:
    """Each SF-n cell reds if its reader skips instead of reading or raising."""

    @pytest.mark.parametrize(
        "grants",
        [
            "  dynamodb_grants = [local.a_grants, local.b_grants]\n",
            "  dynamodb_grants = [\n    local.a_grants,\n    local.b_grants,\n  ]\n",
        ],
        ids=["one-line", "multi-line"],
    )
    def test_sf1_a_list_of_local_references_reads_every_local(self, grants: str) -> None:
        text = _sf_module(_SF_NAME + grants)
        read = iam_roles.module_grants(text)["probe"]
        assert sorted((g.table, g.key_values) for g in read) == [
            ("notes", ("note#*",)),
            ("reps", ("conversation#*",)),
        ]
        assert set(iam_roles.conditioned_tool_scopes(text)) == {
            ("probe", "notes", "dynamodb:PutItem"),
            ("probe", "reps", "dynamodb:GetItem"),
        }
        walked, refs = iam_roles._walk_literal_module_grants(text)
        assert walked["probe"] == {("reps", "dynamodb:GetItem"), ("notes", "dynamodb:PutItem")}
        assert refs["probe"] == {"a_grants", "b_grants"}

    def test_sf1_a_list_item_that_is_neither_object_nor_local_raises(self) -> None:
        text = _sf_module(_SF_NAME + "  dynamodb_grants = [local.a_grants, var.extra]\n")
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="var.extra"):
            iam_roles.module_grants(text)

    @pytest.mark.parametrize(
        "name",
        ['format("%s-probe", local.name_prefix)', "var.function_name", '"other-probe"'],
        ids=["format", "variable", "foreign-prefix"],
    )
    def test_sf2_a_lambda_action_block_without_a_literal_prefixed_name_raises(
        self, name: str
    ) -> None:
        text = _sf_module(f"  function_name = {name}\n  dynamodb_grants = local.a_grants\n")
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="function_name"):
            iam_roles.module_grants(text)

    def test_sf2_the_for_each_tools_block_and_other_modules_are_the_only_skips(self) -> None:
        tools = _sf_module(
            "  for_each = local.tools\n"
            '  function_name = "crm-agent-eval-${each.key}"\n'
            "  dynamodb_grants = [\n"
            "    for grant in local.tool_dynamodb_grants[each.key] : {\n"
            "      table_name = grant.table\n"
            "      actions    = grant.actions\n"
            "    }\n"
            "  ]\n"
        )
        assert iam_roles.module_grants(tools) == {}
        other = tools.replace('"./modules/lambda_action"', '"./modules/agent_relay"').replace(
            "  for_each = local.tools\n", ""
        )
        assert iam_roles.module_grants(other) == {}

    def test_sf2_the_real_composition_names_every_lambda_action_block(self) -> None:
        text = iam_roles._root_composition_text()
        blocks = len(re.findall(r'^\s*source\s*=\s*"\./modules/lambda_action"\s*$', text, re.M))
        assert len(iam_roles.module_grants(text)) == blocks - 1, "all but the for_each tools block"

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            (
                "try(grant.leading_key_condition, null)",
                'merge(grant.leading_key_condition, { values = ["*"] })',
            ),
            ("actions               = grant.actions", 'actions = ["dynamodb:Scan"]'),
            ("    }\n", '      extra = "x"\n    }\n'),
            ("    }\n", '    } if grant.table_name != "reps"\n'),
        ],
        ids=["merged-condition", "replaced-actions", "extra-key", "if-filter"],
    )
    def test_sf3_a_comprehension_body_that_transforms_or_filters_raises(
        self, old: str, new: str
    ) -> None:
        body = _SF_COMPREHENSION_BODY.replace(old, new, 1)
        assert body != _SF_COMPREHENSION_BODY
        text = _sf_module(_SF_NAME + f"  dynamodb_grants = [\n{body}  ]\n")
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="comprehension"):
            iam_roles.module_grants(text)

    def test_sf3_a_pass_through_body_keeps_the_condition_and_an_absent_one_drops_it(self) -> None:
        kept = _sf_module(_SF_NAME + f"  dynamodb_grants = [\n{_SF_COMPREHENSION_BODY}  ]\n")
        assert iam_roles.module_grants(kept)["probe"][0].key_values == ("conversation#*",)
        dropped_body = _SF_COMPREHENSION_BODY.replace(
            "      leading_key_condition = try(grant.leading_key_condition, null)\n", ""
        )
        dropped = _sf_module(_SF_NAME + f"  dynamodb_grants = [\n{dropped_body}  ]\n")
        (grant,) = iam_roles.module_grants(dropped)["probe"]
        assert (grant.operator, grant.key_values) == (None, None)
        assert iam_roles.conditioned_tool_scopes(dropped) == {}

    def test_sf4_a_partially_matched_grant_local_is_omitted_with_its_reason(self) -> None:
        text = (
            "locals {\n"
            "  probe_grants = [\n"
            '    { table = "reps", actions = ["dynamodb:GetItem"] },\n'
            '    { table = local.t, actions = ["dynamodb:PutItem"] },\n'
            "  ]\n"
            "}\n"
            'module "lambda_action_probe" {\n'
            '  function_name = "crm-agent-eval-probe"\n'
            "  dynamodb_grants = local.probe_grants\n"
            "}\n"
        )
        definitions, defects = iam_roles._local_grant_definitions_and_defects(text)
        assert "probe_grants" not in definitions
        assert "1 of its 2 grant object(s)" in defects["probe_grants"]
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="1 of its 2"):
            iam_roles._walk_literal_module_grants(text)

    def test_sf5_an_unreadable_line_inside_a_tool_grant_list_raises(self, tmp_path: Path) -> None:
        main_tf = tmp_path / "main.tf"
        main_tf.write_text(
            "locals {\n"
            "  tool_dynamodb_grants = {\n"
            "    get_account = [\n"
            '      { table = "accounts", actions = ["dynamodb:GetItem"] },\n'
            "      # a comment and a blank line are not grants\n"
            "\n"
            '      { table = "notes",\n'
            '        actions = ["dynamodb:GetItem"] },\n'
            "    ]\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="one-line"):
            iam_roles._terraform_tool_grants(main_tf)

    def test_sf6_a_dropped_local_item_names_its_reason_in_the_reference_error(self) -> None:
        text = _PROBE_LOCALS.replace("OBJECT", _DROPPED_LOCAL_OBJECT) + (
            'module "lambda_action_probe" {\n'
            '  function_name = "crm-agent-eval-probe"\n'
            "  dynamodb_grants = local.probe_grants\n"
            "}\n"
        )
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError) as raised:
            iam_roles._walk_literal_module_grants(text)
        assert "local.x" in str(raised.value) and "not a string literal" in str(raised.value)

    def test_sf7_a_supplied_text_renders_the_name_prefix(self) -> None:
        text = _sf_module(
            '  function_name = "${local.name_prefix}-probe"\n  dynamodb_grants = local.a_grants\n'
        )
        assert set(iam_roles.conditioned_tool_scopes(text)) == {
            ("probe", "reps", "dynamodb:GetItem")
        }

    def test_sf8_a_lambda_action_block_without_dynamodb_grants_raises(self) -> None:
        with pytest.raises(
            iam_roles.UnreadableTerraformGrantsError, match="sets no dynamodb_grants"
        ):
            iam_roles.module_grants(_sf_module(_SF_NAME))


# --- an unreadable condition is never read as unconditioned -------------------------------------


class TestUnreadableConditionsAreNeverUnconditioned:
    """Each shape below raises, or reds a row, instead of filing the grant unconditioned."""

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            (
                'leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = '
                '"StringLike", values = ["conversation#*"] }',
                "leading_key_condition = local.shared_condition",
            ),
            ('values = ["conversation#*"]', 'values = ["conversation#${local.suffix}"]'),
            (
                'leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = '
                '"StringLike", values = ["conversation#*"] }',
                "leading_key_condition = try(local.c, null)",
            ),
        ],
        ids=["by-reference", "interpolated-value", "call"],
    )
    def test_f1_a_condition_the_reader_cannot_read_raises(self, old: str, new: str) -> None:
        assert old in _MODULE_FORMS
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError):
            iam_roles.conditioned_tool_scopes(_MODULE_FORMS.replace(old, new, 1))

    def test_f1_only_null_is_unconditioned(self) -> None:
        old = (
            'leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = '
            '"StringLike", values = ["conversation#*"] }'
        )
        nulled = _MODULE_FORMS.replace(old, "leading_key_condition = null", 1)
        scopes = iam_roles.conditioned_tool_scopes(nulled)
        assert not any(triple[0] in ("bare", "comprehension") for triple in scopes), scopes

    def test_f1_an_interpolated_condition_value_reds_conditioned_grants_readable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broken = _MODULE_FORMS.replace(
            "values    = [local.svc]", 'values    = ["svc-${local.svc}"]', 1
        )
        real = iam_roles.conditioned_tool_scopes
        monkeypatch.setattr(iam_roles, "conditioned_tool_scopes", lambda: real(broken))
        results = _run_with_arn_deciding_iam(_ArnDecidingIam(set(_INDEX_ONLY_ALLOWED)))
        (row,) = [r for r in results if r.check == "conditioned_grants_readable"]
        assert row.status == "fail" and "interpolate" in row.detail

    def test_f2_a_header_comment_does_not_hide_a_block(self) -> None:
        commented = _MODULE_FORMS.replace(
            'module "lambda_action_inline" {', 'module "lambda_action_inline" { # NOTE-1', 1
        )
        assert commented != _MODULE_FORMS
        assert iam_roles.conditioned_tool_scopes(commented) == iam_roles.conditioned_tool_scopes(
            _MODULE_FORMS
        )
        walked, _ = iam_roles._walk_literal_module_grants(commented)
        assert "inline" in walked

    def test_f2_a_block_one_reader_skips_raises(self) -> None:
        """A one-line inline object: the block reader reads it, the line walker does not."""
        text = _sf_module(
            _SF_NAME
            + '  dynamodb_grants = [{ table_name = "reps", actions = ["dynamodb:GetItem"] }]\n'
        )
        assert iam_roles.module_grants(text)["probe"]
        assert "probe" not in iam_roles._walk_literal_module_grants(text)[0]
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="disagree"):
            iam_roles.conditioned_tool_scopes(text)

    @pytest.mark.parametrize(
        "line",
        [
            '      leading_key_condition = { attribute = "dynamodb:LeadingKeys", values = ["x"] }\n',
            "      include_table_arn = false\n",
        ],
        ids=["condition", "index-only"],
    )
    def test_f3_the_tools_block_raises_if_it_sets_a_scope_key(self, line: str) -> None:
        tools = _sf_module(
            "  for_each = local.tools\n"
            '  function_name = "crm-agent-eval-${each.key}"\n'
            "  dynamodb_grants = [\n"
            "    for grant in local.tool_dynamodb_grants[each.key] : {\n"
            "      table_name = grant.table\n"
            "      actions    = grant.actions\n"
            f"{line}"
            "    }\n"
            "  ]\n"
        )
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="for_each tools block"):
            iam_roles.module_grants(tools)

    def test_f3_the_real_tools_block_sets_no_scope_key(self) -> None:
        assert iam_roles.module_grants(iam_roles._root_composition_text())

    def test_f5_the_matrix_declares_exactly_the_conditioned_grants_the_parser_reads(self) -> None:
        declared = iam_roles.matrix_conditioned_grants(
            _real_load_matrix(), _real_terraform_index_only_grants()
        )
        assert declared == set(iam_roles.conditioned_scopes())
        sweeps = {t for t in declared if t[0] in iam_roles.SWEEP_GRANT_LOCALS}
        assert sweeps == _sweep_triples() and sweeps
        assert declared - sweeps == set(iam_roles.conditioned_tool_scopes())

    def test_f5_a_grant_the_parser_drops_reds_parity_refuses_its_row_and_keeps_the_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = iam_roles.conditioned_tool_scopes()
        deployed = iam_roles.conditioned_scopes()
        target = ("user-settings", "reps", "dynamodb:GetItem")
        monkeypatch.setattr(iam_roles, "_load_matrix", _real_load_matrix)
        monkeypatch.setattr(
            iam_roles, "_terraform_intended_grants", _real_terraform_intended_grants
        )
        monkeypatch.setattr(
            iam_roles, "_terraform_index_only_grants", _real_terraform_index_only_grants
        )
        floor = iam_roles.expected_check_count(context_for_test(clients={}, outputs={})).minimum
        dropped = {k: v for k, v in real.items() if k != target}
        monkeypatch.setattr(iam_roles, "conditioned_tool_scopes", lambda *_a: dict(dropped))
        assert iam_roles.expected_check_count(context_for_test(clients={}, outputs={})).minimum == (
            floor
        )
        results, iam = _real_composition_run(monkeypatch, deployed)
        (parity,) = [r for r in results if r.check == "conditioned_grants_parity"]
        assert (
            parity.status == "fail"
            and "matrix-only" in parity.detail
            and "GetItem" in parity.detail
        )
        own = _row(results, "user-settings", "simulate:user-settings:dynamodb:GetItem@reps")
        assert own.status == "fail" and "refused" in own.detail
        deny = _row(
            results, "user-settings", "conditioned_scope_denied:user-settings:dynamodb:GetItem@reps"
        )
        assert deny.status == "pass", deny
        sent = [c[3] for c in iam.calls if c[:3] == ("user-settings", "dynamodb:GetItem", "reps")]
        assert sent == [(iam_roles.PROBE_FOREIGN_KEY,)], sent


# --- the deny pair is the backstop for every conditioned own grant, sweeps included -------

_SWEEP_JOB_CONDITION: Final[str] = (
    'leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringLike", '
    'values = ["job#*"] }'
)
_TOOLS_GRANTS_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    r"^  dynamodb_grants = \[\n    for grant in local\.tool_dynamodb_grants\[each\.key\] : \{\n"
    r"(?P<body>.*?)^    \}\n  \]\n",
    re.MULTILINE | re.DOTALL,
)


def _sweep_triples() -> set[tuple[str, str, str]]:
    return {t for t in iam_roles.conditioned_scopes() if t[0] in iam_roles.SWEEP_GRANT_LOCALS}


def _with_tools_grants_value(value: str) -> str:
    """The root composition with the `for_each` tools block's `dynamodb_grants` value replaced."""
    text = iam_roles._root_composition_text()
    match = _TOOLS_GRANTS_VALUE_RE.search(text)
    assert match is not None, "the tools block's dynamodb_grants shape changed; update this cell"
    replaced = value.replace("BODY", match.group("body"))
    return text[: match.start()] + replaced + text[match.end() :]


class TestTheDenyPairIsTheBackstop:
    """Each conditioned own grant, sweeps included, gets a parser-independent deny row."""

    _TARGET: Final[tuple[str, str, str]] = ("sweep-control", "eval_runs", "dynamodb:PutItem")

    def test_the_real_tree_has_the_expected_sweep_triples(self) -> None:
        triples = _sweep_triples()
        assert self._TARGET in triples
        seed_put = {
            ("sweep-control", table, "dynamodb:PutItem")
            for (function, table), actions in iam_roles.OWN_TABLE_DENIAL_PROBES.items()
            if function == "sweep-control" and "dynamodb:PutItem" in actions
        }
        assert seed_put and not seed_put & triples

    def test_every_sweep_conditioned_triple_gets_an_allow_and_a_deny_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results, iam = _real_composition_run(monkeypatch)
        keys = iam_roles.conditioned_own_keys()
        for function, table, action in sorted(_sweep_triples()):
            own = _row(results, function, f"simulate:{function}:{action}@{table}")
            assert own.status == "pass" and keys[(function, table, action)] in own.detail, own
            deny = _row(results, function, f"conditioned_scope_denied:{function}:{action}@{table}")
            assert deny.status == "pass", deny
            sent = [c[3] for c in iam.calls if c[:3] == (function, action, table)]
            assert None not in sent, f"a context-less simulate of {(function, table, action)}"
            assert (iam_roles.PROBE_FOREIGN_KEY,) in sent, sent

    @pytest.mark.parametrize("parser", ["read", "lost"])
    def test_a_deployed_grant_that_lost_its_condition_fails_the_deny_row(
        self, monkeypatch: pytest.MonkeyPatch, parser: str
    ) -> None:
        """The deployed policy admits the foreign key; the row fails whatever the parser read."""
        deployed = {**iam_roles.conditioned_scopes(), self._TARGET: ()}
        if parser == "lost":
            parsed = {k: v for k, v in iam_roles.conditioned_scopes().items() if k != self._TARGET}
            monkeypatch.setattr(iam_roles, "conditioned_scopes", lambda: dict(parsed))
        results, _iam = _real_composition_run(monkeypatch, deployed)
        function, table, action = self._TARGET
        deny = _row(results, function, f"conditioned_scope_denied:{function}:{action}@{table}")
        assert deny.status == "fail" and iam_roles.PROBE_FOREIGN_KEY in deny.detail, deny
        failing = [r.check for r in results if r.check.startswith("conditioned_scope_denied:")]
        assert [c for c in failing if _row(results, c.split(":")[1], c).status == "fail"] == [
            deny.check
        ]

    def test_a_raising_scope_derivation_still_probes_the_foreign_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(*_args: Any) -> tuple[str, ...]:
            raise iam_roles.UnreadableTerraformGrantsError("planted derivation failure")

        deployed = iam_roles.conditioned_scopes()
        monkeypatch.setattr(iam_roles, "scope_denied_keys", broken)
        results, iam = _real_composition_run(monkeypatch, deployed)
        function, table, action = self._TARGET
        derivable = _row(
            results, function, f"conditioned_scope_keys_derivable:{function}:{action}@{table}"
        )
        assert derivable.status == "fail" and "planted derivation failure" in derivable.detail
        deny = _row(results, function, f"conditioned_scope_denied:{function}:{action}@{table}")
        assert deny.status == "pass", deny
        sent = [c[3] for c in iam.calls if c[:3] == (function, action, table)]
        own_key = iam_roles.conditioned_own_keys()[self._TARGET]
        assert set(sent) - {(own_key,)} == {(iam_roles.PROBE_FOREIGN_KEY,)}, sent

    def test_a_sweep_parity_mismatch_fails_the_parity_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        deployed = iam_roles.conditioned_scopes()
        parsed = {k: v for k, v in deployed.items() if k != self._TARGET}
        monkeypatch.setattr(iam_roles, "conditioned_scopes", lambda: dict(parsed))
        results, _iam = _real_composition_run(monkeypatch, deployed)
        (parity,) = [r for r in results if r.check == "conditioned_grants_parity"]
        assert parity.status == "fail" and "matrix-only" in parity.detail
        assert repr(self._TARGET) in parity.detail
        assert not [
            r for r in results if r.check == "conditioned_grants_parity" and r is not parity
        ]

    def test_the_real_tree_emits_no_parity_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Control for the cell above: parity holds on today's tree, sweeps included."""
        results, _iam = _real_composition_run(monkeypatch)
        assert not [r for r in results if r.check == "conditioned_grants_parity"]

    def test_the_floor_counts_the_sweep_deny_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(iam_roles, "_load_matrix", _real_load_matrix)
        monkeypatch.setattr(
            iam_roles, "_terraform_intended_grants", _real_terraform_intended_grants
        )
        monkeypatch.setattr(
            iam_roles, "_terraform_index_only_grants", _real_terraform_index_only_grants
        )
        ctx = context_for_test(clients={}, outputs={})
        with_sweeps = iam_roles.expected_check_count(ctx).minimum
        sweeps = set(iam_roles.SWEEP_GRANT_LOCALS)
        real = iam_roles.matrix_conditioned_grants
        monkeypatch.setattr(
            iam_roles,
            "matrix_conditioned_grants",
            lambda *a: {t for t in real(*a) if t[0] not in sweeps},
        )
        assert with_sweeps - iam_roles.expected_check_count(ctx).minimum == len(_sweep_triples())


class TestTheParserRaisesOnEachMeasuredMisread:
    """SFH2-1, SFH2-4 and CR-2: each shape the e2e review measured is read or raised, never skipped."""

    def test_the_real_tools_block_value_is_the_one_accepted_shape(self) -> None:
        assert iam_roles.module_grants(
            _with_tools_grants_value(
                "  dynamodb_grants = [\n    for "
                "grant in local.tool_dynamodb_grants"
                "[each.key] : {\nBODY    }\n  ]\n"
            )
        )

    @pytest.mark.parametrize(
        "value",
        [
            "  dynamodb_grants = local.cond_tool_grants[each.key]\n",
            "  dynamodb_grants = local.cond_tool_grants\n",
            "  dynamodb_grants = concat([\n    for grant in local.tool_dynamodb_grants[each.key] : "
            "{\nBODY    }\n  ], local.extra)\n",
            "  dynamodb_grants = [\n    for grant in local.tool_dynamodb_grants[each.key] : merge("
            "{ table_name = grant.table, actions = grant.actions }, local.cond[each.key])\n  ]\n",
            "  dynamodb_grants = [\n    for grant in local.other_grants[each.key] : {\nBODY    }\n"
            "  ]\n",
            "  dynamodb_grants = [\n    for grant in local.tool_dynamodb_grants[each.key] : "
            '{\nBODY    } if grant.table != "reps"\n  ]\n',
        ],
        ids=["indexed-reference", "bare-reference", "concat", "merge-body", "other-local", "if"],
    )
    def test_sfh2_1_a_tools_block_value_of_any_other_shape_raises(self, value: str) -> None:
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="for_each tools block"):
            iam_roles.module_grants(_with_tools_grants_value(value))

    @pytest.mark.parametrize(
        "value",
        ["null_data_source.x.outputs", "null == var.x ? null : local.c", "nullable.x"],
        ids=["null-prefixed-reference", "null-conditional", "null-prefixed-name"],
    )
    def test_sfh2_4_a_value_that_only_starts_with_null_raises(self, value: str) -> None:
        old = (
            'leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = '
            '"StringLike", values = ["conversation#*"] }'
        )
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="not an object"):
            iam_roles.conditioned_tool_scopes(
                _MODULE_FORMS.replace(old, f"leading_key_condition = {value}", 1)
            )

    def test_sfh2_4_a_null_prefixed_sweep_condition_raises(self) -> None:
        text = iam_roles._root_composition_text()
        assert _SWEEP_JOB_CONDITION in text
        broken = text.replace(
            _SWEEP_JOB_CONDITION, "leading_key_condition = null_resource.k.triggers", 1
        )
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="not an object"):
            iam_roles.terraform_local_grants(iam_roles.SWEEP_GRANT_LOCALS["sweep-control"], broken)

    @pytest.mark.parametrize(
        "spelling",
        ["leading_key_condition = null }", "leading_key_condition = null # x\n }"],
        ids=["closing-brace", "trailing-comment"],
    )
    def test_sfh2_4_a_whole_null_value_is_still_unconditioned(self, spelling: str) -> None:
        old = (
            'leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = '
            '"StringLike", values = ["conversation#*"] } }'
        )
        assert old in _MODULE_FORMS
        scopes = iam_roles.conditioned_tool_scopes(_MODULE_FORMS.replace(old, spelling, 1))
        assert not any(triple[0] in ("bare", "comprehension") for triple in scopes), scopes

    @pytest.mark.parametrize(
        "key",
        ['"leading_key_condition" =', "leading_key_condition:", '"leading_key_condition":'],
        ids=["quoted", "colon", "quoted-colon"],
    )
    def test_cr_2_a_quoted_or_colon_key_is_read(self, key: str) -> None:
        text = _MODULE_FORMS.replace(
            '      leading_key_condition = {\n        attribute = "rep_id"',
            f'      {key} {{\n        attribute = "rep_id"',
            1,
        )
        assert text != _MODULE_FORMS
        assert iam_roles.conditioned_tool_scopes(text) == iam_roles.conditioned_tool_scopes(
            _MODULE_FORMS
        )

    @pytest.mark.parametrize(
        "key",
        ['"leading_key_condition" =', "leading_key_condition:"],
        ids=["quoted", "colon"],
    )
    def test_cr_2_a_quoted_or_colon_key_with_an_unreadable_value_raises(self, key: str) -> None:
        old = (
            'leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = '
            '"StringLike", values = ["conversation#*"] }'
        )
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="not an object"):
            iam_roles.conditioned_tool_scopes(
                _MODULE_FORMS.replace(old, f"{key} local.shared_condition", 1)
            )

    def test_cr_2_a_quoted_key_inside_a_string_is_not_a_condition(self) -> None:
        """Control: the key text inside a string literal is data, not a condition."""
        text = _MODULE_FORMS.replace(
            'table_arn  = "x"\n      actions    = ["dynamodb:GetItem"]\n    },',
            'table_arn  = "leading_key_condition = local.c"\n'
            '      actions    = ["dynamodb:GetItem"]\n    },',
            1,
        )
        assert text != _MODULE_FORMS
        assert iam_roles.conditioned_tool_scopes(text) == iam_roles.conditioned_tool_scopes(
            _MODULE_FORMS
        )


#: `local.dynamodb_asserted_actions` and every `locals` value it reads, from the root composition.
_ASSERTED_LOCAL: Final[str] = "dynamodb_asserted_actions"


def _local_closure_hcl(text: str, root: str) -> str:
    """A `locals` block holding `root` and every `local.*` it transitively reads, verbatim."""
    spans = iam_roles._terraform_local_value_spans(text)
    seen: list[str] = []
    frontier = [root]
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        body = iam_roles._local_definition_text(text, name, spans).rstrip()
        assert body, f"local.{name} has no readable definition"
        assert not re.search(r"\b(?:var|module|data)\.\w+", body), f"local.{name} is not pure"
        seen.append(name)
        frontier += re.findall(r"\blocal\.(\w+)", body)
    lines = [
        f"  {name} = {iam_roles._local_definition_text(text, name, spans).rstrip()}"
        for name in seen
    ]
    return "locals {\n" + "\n".join(lines) + "\n}\n"


def _planned_asserted_actions(text: str, tmp_path: Path) -> set[str]:
    """`local.dynamodb_asserted_actions` as Terraform evaluates it at plan time."""
    from tests.unit._terraform_fixture import plan_root

    hcl = _local_closure_hcl(text, _ASSERTED_LOCAL) + (
        f'resource "terraform_data" "asserted" {{\n  input = local.{_ASSERTED_LOCAL}\n}}\n'
    )
    planned = plan_root(tmp_path / "infra", hcl).after("terraform_data.asserted")["input"]
    assert isinstance(planned, list) and planned, planned
    return {str(action) for action in planned}


def _composition_text() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(Path(iam_roles._MAIN_TF_PATH).parent.glob("*.tf"))
    )


@pytest.fixture
def _terraform() -> None:
    from tests.unit._terraform_fixture import (
        REQUIRE_TERRAFORM_ENV_VAR,
        require_terraform,
        terraform_unavailable_reason,
    )

    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)


class TestAssertedActionsContract:
    """`asserted_dynamodb_actions()` equals the planned `local.dynamodb_asserted_actions`."""

    def test_both_sides_agree(self, _terraform: None, tmp_path: Path) -> None:
        planned = _planned_asserted_actions(_composition_text(), tmp_path)
        assert iam_roles.asserted_dynamodb_actions() == planned

    def test_a_widened_local_is_seen_by_the_planned_side(
        self, _terraform: None, tmp_path: Path
    ) -> None:
        """Adding an action to the tf local changes the planned set, so the equality above reds."""
        text = _composition_text()
        widened = text.replace(
            "    [for grant in local.sweep_control_dynamodb_grants : grant.actions],\n",
            "    [for grant in local.sweep_control_dynamodb_grants : grant.actions],\n"
            '    ["dynamodb:Scan"],\n',
            1,
        )
        assert widened != text, "the local's shape changed; update this mutation"
        planned = _planned_asserted_actions(widened, tmp_path)
        assert "dynamodb:Scan" in planned
        assert iam_roles.asserted_dynamodb_actions() != planned


_FF3_COMPREHENSION_BODY: Final[str] = (
    "    for grant in local.shared_grants : {\n"
    "      table_name            = grant.table_name\n"
    "      table_arn             = module.dynamodb.table_arns[grant.table_name]\n"
    "      actions               = grant.actions\n"
    "      leading_key_condition = try(grant.leading_key_condition, null)\n"
    "    }\n"
)


def _ff3_comprehension(first_line: str) -> str:
    """`_MODULE_FORMS` with the comprehension body's condition moved onto its first line."""
    assert _FF3_COMPREHENSION_BODY in _MODULE_FORMS
    body = (
        f"    for grant in local.shared_grants : {{ {first_line}\n"
        "      table_name            = grant.table_name\n"
        "      table_arn             = module.dynamodb.table_arns[grant.table_name]\n"
        "      actions               = grant.actions\n"
        "    }\n"
    )
    return _MODULE_FORMS.replace(_FF3_COMPREHENSION_BODY, body, 1)


def _ff3_walk(grants_body: str) -> dict[str, set[tuple[str, str]]]:
    walked, _ = iam_roles._walk_literal_module_grants(
        _sf_module(_SF_NAME + "  dynamodb_grants = [\n" + grants_body + "  ]\n")
    )
    return walked


class TestFixesFeats3ReaderFollowUps:
    """Every grant shape is read or raised, never dropped."""

    _CONDITIONED: Final = ("comprehension", "reps", "dynamodb:PutItem")

    @pytest.mark.parametrize(
        "first_line",
        [
            '"leading_key_condition" = try(grant.leading_key_condition, null)',
            "leading_key_condition: try(grant.leading_key_condition, null)",
            '"leading_key_condition": try(grant.leading_key_condition, null)',
        ],
        ids=["quoted-equals", "bare-colon", "quoted-colon"],
    )
    def test_r_g4_s1_a_quoted_or_colon_key_on_the_body_first_line_is_read(
        self, first_line: str
    ) -> None:
        assert self._CONDITIONED in iam_roles.conditioned_tool_scopes(_MODULE_FORMS)
        scopes = iam_roles.conditioned_tool_scopes(_ff3_comprehension(first_line))
        assert self._CONDITIONED in scopes, sorted(scopes)

    def test_r_g4_s1_text_before_the_first_key_raises(self) -> None:
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="not a key assignment"):
            iam_roles.conditioned_tool_scopes(_ff3_comprehension("merge(local.extra)"))

    def test_r_g4_s1_a_body_with_text_and_no_key_raises(self) -> None:
        scanned = blank_comments(blank_literals("{ merge(local.x) }"))
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="not a key assignment"):
            iam_roles._object_assignments(scanned, "{ merge(local.x) }", 1, 17)

    def test_s2_a_one_line_grant_object_in_a_multi_line_list_raises(self) -> None:
        body = '    { table_name = "reps", table_arn = "x", actions = ["dynamodb:GetItem"] },\n'
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="line walker"):
            _ff3_walk(body)

    def test_s2_a_multi_line_actions_list_raises(self) -> None:
        body = (
            "    {\n"
            '      table_name = "reps"\n'
            "      actions = [\n"
            '        "dynamodb:GetItem",\n'
            "      ]\n"
            "    },\n"
        )
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="line walker"):
            _ff3_walk(body)

    def test_s2_the_one_key_per_line_shape_is_read(self) -> None:
        body = '    {\n      table_name = "reps"\n      actions = ["dynamodb:GetItem"]\n    },\n'
        assert _ff3_walk(body) == {"probe": {("reps", "dynamodb:GetItem")}}

    def test_e2e1_s3_a_trailing_comment_on_an_actions_line_does_not_drop_the_pair(self) -> None:
        body = (
            '    {\n      table_name = "reps"\n'
            '      actions = ["dynamodb:GetItem"] # NOTE-1\n    },\n'
        )
        assert _ff3_walk(body) == {"probe": {("reps", "dynamodb:GetItem")}}

    def test_e2e1_s3_the_readers_must_agree_on_each_functions_pairs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = iam_roles._walk_literal_module_grants
        walked, refs = real(_MODULE_FORMS)
        assert ("reps", "dynamodb:GetItem") in walked["inline"]

        def dropping(text: str) -> tuple[dict[str, set[tuple[str, str]]], dict[str, set[str]]]:
            grants, local_refs = real(text)
            return {**grants, "inline": set()}, local_refs

        monkeypatch.setattr(iam_roles, "_walk_literal_module_grants", dropping)
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="inline's grants"):
            iam_roles.conditioned_tool_scopes(_MODULE_FORMS)

    def test_the_real_composition_still_reads_with_both_readers_agreeing(self) -> None:
        assert iam_roles.conditioned_tool_scopes(iam_roles._root_composition_text())


class TestMatrixShapeIsValidatedAtLoad:
    """A malformed matrix is one failing row, never a `KeyError` that loses the rest."""

    @staticmethod
    def _matrix(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        matrix: dict[str, Any] = json.loads(json.dumps(_FIXTURE_MATRIX))
        mutate(matrix)
        return matrix

    @pytest.mark.parametrize(
        ("mutate", "expected"),
        [
            (lambda m: m["grants"][0].pop("actions"), "grants[0].actions"),
            (lambda m: m["grants"][1].pop("function"), "grants[1].function"),
            (lambda m: m["grants"][2].update(table="nope"), "'nope' is not in `tables`"),
            (lambda m: m.pop("tables"), "`tables`"),
            (lambda m: m["tables"].update(reps="x"), "tables['reps'] is str"),
            (
                lambda m: m.update(functions_without_dynamodb_grants="capabilities"),
                "`functions_without_dynamodb_grants`",
            ),
            (lambda m: m.update(isolation_mechanism_legend=["x"]), "legend` is list"),
            (lambda m: m.update(grants={}), "`grants`"),
            (
                lambda m: m.update(isolation_mechanism_legend={"leading_keys": "x"}),
                "isolation_mechanism None",
            ),
        ],
        ids=[
            "no-actions",
            "no-function",
            "unknown-table",
            "no-tables",
            "table-not-object",
            "zero-grant-not-list",
            "legend-not-object",
            "grants-not-list",
            "vocab",
        ],
    )
    def test_a_malformed_matrix_yields_one_failing_row(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mutate: Callable[[dict[str, Any]], None],
        expected: str,
    ) -> None:
        matrix = self._matrix(mutate)
        monkeypatch.setattr(iam_roles, "_load_matrix", lambda *_a, **_k: matrix)
        results = list(checks(context_for_test(clients={}, outputs={})))
        assert [(r.check, r.status) for r in results] == [("matrix_readable", "fail")]
        assert expected in results[0].detail, results[0].detail
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="not readable"):
            iam_roles.expected_check_count(context_for_test(clients={}, outputs={}))

    def test_the_real_matrix_is_well_formed(self) -> None:
        matrix = _real_load_matrix()
        assert "isolation_mechanism_legend" in matrix
        assert iam_roles.matrix_shape_problems(matrix) == []


class TestFixesFeats3RowFollowUps:
    """The rows `checks` emits."""

    def test_r_g4_s5_an_unknown_eval_decision_fails_the_simulate_row(self) -> None:
        clients = _StubbedClientSet()
        clients.add_role_for("create_account", decisions={"update_account": "notADecision"})
        clients.add_role_for("get_rep_self")
        clients.add_role_for("update_account")
        clients.activate()
        results = _run_checks(clients)
        clients.assert_no_pending()
        rows = _pair_rows(results, "create_account", "update_account")
        assert rows["denial"].status == "fail"
        assert "unrecognised EvalDecision 'notADecision'" in rows["denial"].detail
        assert rows["explicit"].status == "fail"

    def test_s3_an_unloadable_policy_set_fails_without_a_vacuous_pass(self) -> None:
        clients = _StubbedClientSet()
        clients.stub.add_response("get_role", {"Role": _role_stub("create_account")})
        for other in _SORTED_FUNCTIONS:
            clients.add_simulate("allowed" if other == "create_account" else "explicitDeny")
        clients.stub.add_client_error(
            "list_role_policies", service_error_code="AccessDenied", service_message="denied"
        )
        clients.add_role_for("get_rep_self")
        clients.add_role_for("update_account")
        clients.activate()
        results = _run_checks(clients)
        clients.assert_no_pending()
        tail = [
            (r.check, r.status)
            for r in results
            if r.resource == _role("create_account")
            and r.check.startswith(("no_dynamodb_wildcard", "leading_keys"))
        ]
        assert tail == [
            ("no_dynamodb_wildcard", "fail"),
            ("leading_keys:policies_unreadable", "fail"),
        ]
        assert _find(results, _role("update_account"), lambda c: c == "no_dynamodb_wildcard")

    def test_s3_a_raising_reps_probe_is_a_failing_row_and_later_roles_still_run(self) -> None:
        reps_policy = _policy_document(
            [
                _allow_statement(
                    ["dynamodb:GetItem"],
                    resource=_TABLE_ARN.format(table="reps"),
                    condition={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["eval-*"]}},
                )
            ]
        )
        clients = _StubbedClientSet()
        clients.add_role_for("create_account")
        clients.add_role_for("get_rep_self", policies=[reps_policy])
        clients.stub.add_client_error(
            "simulate_principal_policy", service_error_code="InvalidInput", service_message="bad"
        )
        clients.add_simulate("allowed")
        clients.add_role_for("update_account")
        clients.activate()
        results = _run_checks(clients)
        clients.assert_no_pending()
        denied = _row(results, "get_rep_self", "leading_keys:reps_demo_rep_denied")
        assert denied.status == "fail" and "evaluated an error (" in denied.detail
        assert _row(results, "get_rep_self", "leading_keys:reps_eval_tenant_allowed").status == (
            "pass"
        )
        assert _find(results, _role("update_account"), lambda c: c == "no_dynamodb_wildcard")

    @pytest.mark.parametrize(
        "actions",
        [
            ["dynamodb:GetItem", "dynamodb:Scan"],
            ["dynamodb:Sca*"],
            ["dynamodb:describetable"],
            ["dynamodb:DeleteTable"],
            ["dynamodb:GetItem", "dynamodb:NotYetAnAction"],
            ["dynamodb:Get*"],
        ],
        ids=["scan", "wildcard", "case-folded", "delete-table", "made-up", "keyed-wildcard"],
    )
    def test_sa4_6_a_for_all_values_allow_on_a_keyless_action_fails(
        self, actions: list[str]
    ) -> None:
        policy = _policy_document(
            [
                _allow_statement(
                    actions,
                    resource=_TABLE_ARN.format(table="reps"),
                    condition={"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["x"]}},
                )
            ]
        )
        clients = _StubbedClientSet()
        clients.add_role_for("create_account")
        clients.add_role_for("get_rep_self", policies=[policy])
        clients.add_role_for("update_account")
        clients.activate()
        results = _run_checks(clients)
        clients.assert_no_pending()
        row = _row(results, "get_rep_self", "leading_keys:reps")
        assert row.status == "fail" and "vacuously" in row.detail, row.detail

    @pytest.mark.parametrize(
        "action", [*sorted(iam_roles.KEYED_DYNAMODB_ACTIONS), "dynamodb:getitem"]
    )
    def test_w4_each_keyed_action_is_not_vacuous(self, action: str) -> None:
        statement = {
            "Effect": "Allow",
            "Action": [action],
            "Condition": {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["x"]}},
        }
        assert iam_roles._vacuous_for_all_values_actions(statement) == []

    def test_w4_the_keyed_set_matches_the_documented_item_level_actions(self) -> None:
        assert {
            "dynamodb:GetItem",
            "dynamodb:BatchGetItem",
            "dynamodb:Query",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "dynamodb:BatchWriteItem",
            "dynamodb:ConditionCheckItem",
        } == iam_roles.KEYED_DYNAMODB_ACTIONS

    def test_sa4_6_the_keyed_control_still_passes(self) -> None:
        clients = _StubbedClientSet()
        for role_function in _SORTED_FUNCTIONS:
            clients.add_role_for(role_function)
        clients.activate()
        results = _run_checks(clients)
        clients.assert_no_pending()
        assert _row(results, "get_rep_self", "leading_keys:reps").status == "pass"

    @pytest.mark.parametrize(
        ("statement", "expected"),
        [
            (
                {
                    "Effect": "Allow",
                    "Action": "dynamodb:Scan",
                    "Condition": {"ForAnyValue:StringEquals": {"dynamodb:LeadingKeys": ["x"]}},
                },
                [],
            ),
            (
                {
                    "Effect": "Deny",
                    "Action": "dynamodb:Scan",
                    "Condition": {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["x"]}},
                },
                [],
            ),
            (
                {
                    "Effect": "Allow",
                    "NotAction": ["dynamodb:Scan"],
                    "Condition": {"forallvalues:StringLike": {"dynamodb:LeadingKeys": ["x"]}},
                },
                ["NotAction ['dynamodb:Scan']"],
            ),
        ],
        ids=["for-any-value", "deny", "not-action"],
    )
    def test_sa4_6_the_vacuity_predicate(
        self, statement: dict[str, Any], expected: list[str]
    ) -> None:
        assert iam_roles._vacuous_for_all_values_actions(statement) == expected


class TestToolScopedQueryIndexes:
    """`local.tool_query_indexes` scopes a tool's Query-only grant to named GSIs."""

    def test_the_real_block_names_the_revert_child_indexes(self) -> None:
        assert iam_roles.terraform_tool_query_indexes() == {
            ("revert_audit_record", "contacts"): ("gsi_account",),
            ("revert_audit_record", "opportunities"): ("gsi_account",),
            ("revert_audit_record", "activities"): ("gsi_account", "gsi_opportunity"),
            ("revert_audit_record", "notes"): ("gsi_opportunity",),
        }

    def test_an_absent_block_is_no_scoping(self, tmp_path: Path) -> None:
        main_tf = tmp_path / "main.tf"
        main_tf.write_text("locals {\n  tools = {}\n}\n", encoding="utf-8")
        assert iam_roles.terraform_tool_query_indexes(main_tf) == {}

    def test_an_entry_naming_no_index_is_unreadable(self, tmp_path: Path) -> None:
        main_tf = tmp_path / "main.tf"
        main_tf.write_text(
            "locals {\n  tool_query_indexes = {\n    revert_audit_record = {\n"
            "      notes = []\n    }\n  }\n}\n",
            encoding="utf-8",
        )
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError, match="names no index"):
            iam_roles.terraform_tool_query_indexes(main_tf)

    def test_every_scoped_index_has_an_in_scope_probe_key(self) -> None:
        for indexes in iam_roles.terraform_tool_query_indexes().values():
            for index in indexes:
                assert index in iam_roles.INDEX_IN_SCOPE_LEADING_KEY, index


@pytest.mark.parametrize(("decision", "status"), [("allowed", "pass"), ("implicitDeny", "fail")])
def test_a_further_scoped_index_row_passes_only_when_allowed(
    monkeypatch: pytest.MonkeyPatch, decision: str, status: str
) -> None:
    """`scoped_index_allowed` simulates the further index with its in-scope probe key."""
    seen: list[tuple[str, list[str] | None]] = []

    def fake(
        _ctx: object, _role: str, _action: str, arn: str, keys: list[str] | None
    ) -> tuple[str, Any]:
        seen.append((arn, keys))
        return decision, None

    monkeypatch.setattr(iam_roles, "_simulate_evaluation", fake)
    arn = "arn:aws:dynamodb:us-east-1:123456789012:table/t-activities/index/gsi_opportunity"
    row = iam_roles._scoped_index_allowed_check(
        context_for_test(outputs={}, clients={}, region="us-east-1"),
        "role",
        "arn:role",
        "revert_audit_record",
        "activities",
        "dynamodb:Query",
        arn,
    )
    assert (
        row.check
        == "scoped_index_allowed:revert_audit_record:dynamodb:Query@activities/gsi_opportunity"
    )
    assert row.status == status
    assert seen == [(arn, [iam_roles.INDEX_IN_SCOPE_LEADING_KEY["gsi_opportunity"]])]


def test_every_own_table_denial_probe_names_a_held_table_and_an_ungranted_action() -> None:
    """A probe is meaningful only on a table the role holds, for an action it does not."""
    grants = json.loads(
        (
            iam_roles._MAIN_TF_PATH.parent
            / "modules"
            / "lambda_action"
            / "table_action_role_matrix.json"
        ).read_text(encoding="utf-8")
    )["grants"]
    for (function, table), actions in iam_roles.OWN_TABLE_DENIAL_PROBES.items():
        held = {
            a
            for g in grants
            if g["function"] == function and g["table"] == table
            for a in g["actions"]
        }
        assert held, (function, table)
        assert not held & set(actions), (function, table, held & set(actions))
    assert iam_roles.OWN_TABLE_DENIAL_PROBES[("revert_audit_record", "notes")] == (
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
    )


@pytest.mark.parametrize(
    ("decision", "status"),
    [("explicitDeny", "pass"), ("implicitDeny", "fail"), ("allowed", "fail")],
)
def test_an_own_table_denial_row_passes_only_on_an_explicit_deny(
    monkeypatch: pytest.MonkeyPatch, decision: str, status: str
) -> None:
    monkeypatch.setattr(iam_roles, "_simulate_evaluation", lambda *_a, **_k: (decision, ()))
    row = iam_roles._own_table_denied_check(
        context_for_test(outputs={}, clients={}, region="us-east-1"),
        "role",
        "arn:role",
        "revert_audit_record",
        "notes",
        "dynamodb:PutItem",
        "arn:aws:dynamodb:us-east-1:123456789012:table/t-notes",
    )
    assert row.check == "own_table_denied:revert_audit_record:dynamodb:PutItem@notes"
    assert row.status == status


# --- sweep_control's seed PutItem removal and its COUNT-only demo Query --------------------------

_SEED_TABLES: Final[tuple[str, ...]] = (
    "accounts",
    "contacts",
    "opportunities",
    "activities",
    "notes",
)


class _AllowingIam(_ScopeModellingIam):
    """`_ScopeModellingIam` that also answers `allowed` for each `(function, action, table)` in
    `extra`, as a deployed policy that regained those grants would."""

    def __init__(self, extra: set[tuple[str, str, str]]) -> None:
        super().__init__(_real_intended_grants(), iam_roles.conditioned_scopes())
        self.extra = extra

    def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:
        function = self._function(kwargs["PolicySourceArn"])
        action, arn = kwargs["ActionNames"][0], kwargs["ResourceArns"][0]
        table = arn.split(":table/", 1)[1].split("/", 1)[0]
        if (function, action, table) in self.extra:
            return {"EvaluationResults": [{"EvalActionName": action, "EvalDecision": "allowed"}]}
        return super().simulate_principal_policy(**kwargs)


class TestSweepControlSeedWrites:
    """Seeding writes through BatchWriteItem only: PutItem on each seed table is explicitly denied."""

    def test_the_grant_locals_hold_no_seed_put_item_and_a_batch_write_on_each_seed_table(
        self,
    ) -> None:
        grants = iam_roles.terraform_local_grants(iam_roles.SWEEP_GRANT_LOCALS["sweep-control"])
        for table in _SEED_TABLES:
            actions = {a for g in grants if g.table == table for a in g.actions}
            assert "dynamodb:PutItem" not in actions and "dynamodb:BatchWriteItem" in actions

    def test_each_seed_table_gets_an_own_table_denied_put_item_row_that_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results, _iam = _real_composition_run(monkeypatch)
        for table in _SEED_TABLES:
            row = _row(
                results, "sweep-control", f"own_table_denied:sweep-control:dynamodb:PutItem@{table}"
            )
            assert row.status == "pass", row
            batch = _row(
                results, "sweep-control", f"simulate:sweep-control:dynamodb:BatchWriteItem@{table}"
            )
            assert batch.status == "pass", batch

    @pytest.mark.parametrize("table", _SEED_TABLES)
    def test_a_regained_put_item_grant_reds_exactly_its_own_table_denied_row(
        self, monkeypatch: pytest.MonkeyPatch, table: str
    ) -> None:
        monkeypatch.setattr(iam_roles, "_load_matrix", _real_load_matrix)
        monkeypatch.setattr(
            iam_roles, "_terraform_intended_grants", _real_terraform_intended_grants
        )
        monkeypatch.setattr(
            iam_roles, "_terraform_index_only_grants", _real_terraform_index_only_grants
        )
        results = _run_with_arn_deciding_iam(
            _AllowingIam({("sweep-control", "dynamodb:PutItem", table)})
        )
        reddened = {
            r.check
            for r in results
            if r.check.startswith("own_table_denied:sweep-control:") and r.status != "pass"
        }
        assert reddened == {f"own_table_denied:sweep-control:dynamodb:PutItem@{table}"}

    def test_a_denied_batch_write_reds_its_own_simulate_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        intended = _real_intended_grants()
        narrowed = {
            **intended,
            "sweep-control": intended["sweep-control"] - {("notes", "dynamodb:BatchWriteItem")},
        }
        monkeypatch.setattr(iam_roles, "_load_matrix", _real_load_matrix)
        monkeypatch.setattr(
            iam_roles, "_terraform_intended_grants", _real_terraform_intended_grants
        )
        monkeypatch.setattr(
            iam_roles, "_terraform_index_only_grants", _real_terraform_index_only_grants
        )
        results = _run_with_arn_deciding_iam(
            _ScopeModellingIam(narrowed, iam_roles.conditioned_scopes())
        )
        row = _row(results, "sweep-control", "simulate:sweep-control:dynamodb:BatchWriteItem@notes")
        assert row.status == "fail"


class _GrantIam:
    """Decides DynamoDB simulates from grant objects as the live simulator was measured to:
    ForAllValues LeadingKeys, an extra condition failing when its variable is absent or another
    value, and the absent variable reported missing when the LeadingKeys condition held."""

    def __init__(self, grants: Sequence[iam_roles.LocalGrant]) -> None:
        self.grants = grants

    def simulate_principal_policy(self, **request: Any) -> dict[str, Any]:
        (action,) = request["ActionNames"]
        (arn,) = request["ResourceArns"]
        tail = arn.split(":table/", 1)[1]
        table, _, index = tail.partition("/index/")
        entries = request.get("ContextEntries", [])
        keys = [
            v
            for e in entries
            if e["ContextKeyName"] == "dynamodb:LeadingKeys"
            for v in e["ContextKeyValues"]
        ]
        context = {
            e["ContextKeyName"]: e["ContextKeyValues"][0]
            for e in entries
            if e["ContextKeyName"] != "dynamodb:LeadingKeys"
        }
        missing: list[str] = []
        decision = "implicitDeny"
        for grant in self.grants:
            covers = index in grant.index_names if index else grant.include_table_arn
            if grant.table != table or action not in grant.actions or not covers:
                continue
            if grant.key_values is not None and not all(
                any(
                    iam_roles.condition_matches(v, k, grant.operator or "")
                    for v in grant.key_values
                )
                for k in keys
            ):
                continue
            unset = [c.variable for c in grant.extra_conditions if c.variable not in context]
            missing += unset
            if unset or any(context[c.variable] not in c.values for c in grant.extra_conditions):
                continue
            decision = "allowed"
            missing = []
            break
        return {
            "EvaluationResults": [
                {
                    "EvalActionName": action,
                    "EvalDecision": decision,
                    "MissingContextValues": missing,
                }
            ]
        }


def _control_grants() -> list[iam_roles.LocalGrant]:
    return iam_roles.terraform_local_grants(iam_roles.SWEEP_GRANT_LOCALS["sweep-control"])


def _extra_rows(deployed: Sequence[iam_roles.LocalGrant]) -> dict[str, Any]:
    ctx = context_for_test(clients={"iam": _GrantIam(deployed)}, outputs={})
    rows = list(
        iam_roles.extra_condition_rows(
            ctx,
            "crm-agent-eval-sweep-control-role",
            "arn:aws:iam::123456789012:role/crm-agent-eval-sweep-control-role",
            "sweep-control",
            _control_grants(),
            lambda table: _TABLE_ARN.format(table=table),
        )
    )
    by_check = {r.check: r for r in rows}
    assert len(by_check) == len(rows)
    return by_check


def _count_tables() -> set[str]:
    return {g.table for g in _control_grants() if g.extra_conditions}


class TestDemoCountQuery:
    """sweep_control may Query `gsi_tenant` under `demo` with `Select = COUNT` only."""

    def test_the_reader_reads_the_count_condition_on_every_tenant_index_table(self) -> None:
        demo = [g for g in _control_grants() if g.extra_conditions]
        assert demo and {g.table for g in demo} == {
            g.table for g in _control_grants() if "gsi_tenant" in g.index_names
        }
        for grant in demo:
            assert grant.key_values == (DEMO_TENANT,) and grant.operator == "StringEquals"
            assert grant.extra_conditions == (
                iam_roles.ExtraCondition("StringEquals", "dynamodb:Select", ("COUNT",)),
            )
            assert grant.index_names == ("gsi_tenant",) and not grant.include_table_arn

    def test_every_row_passes_against_the_real_grants(self) -> None:
        rows = _extra_rows(_control_grants())
        assert rows and {c: r.detail for c, r in rows.items() if r.status != "pass"} == {}
        for table in _count_tables():
            where = f"sweep-control:dynamodb:Query@{table}/gsi_tenant"
            assert f"extra_condition_allowed:{where}[demo]" in rows
            assert (
                f"extra_condition_denied:{where}[demo,dynamodb:Select=ALL_PROJECTED_ATTRIBUTES]"
                in rows
            )
            assert f"extra_condition_absent_denied:{where}[demo,dynamodb:Select]" in rows
            sibling = f"extra_condition_sibling_allowed:{where}"
            assert any(c.startswith(f"{sibling}[eval-") for c in rows), rows

    def test_a_deployed_grant_without_the_count_condition_reds_its_denied_rows(self) -> None:
        deployed = [g._replace(extra_conditions=()) for g in _control_grants()]
        failing = {c for c, r in _extra_rows(deployed).items() if r.status != "pass"}
        assert failing and all(
            c.startswith(("extra_condition_denied:", "extra_condition_absent_denied:"))
            for c in failing
        )
        assert {c.split("@")[1].split("/")[0] for c in failing} == _count_tables()

    def test_a_deployed_policy_without_the_demo_grant_reds_the_allowed_and_absent_rows(
        self,
    ) -> None:
        """With no demo grant the denial is not the condition's, so `Select` is not reported
        missing and the absent row fails too; the wrong-value denials still hold."""
        deployed = [g for g in _control_grants() if not g.extra_conditions]
        failing = {c for c, r in _extra_rows(deployed).items() if r.status != "pass"}
        assert failing == {
            row
            for t in _count_tables()
            for row in (
                f"extra_condition_allowed:sweep-control:dynamodb:Query@{t}/gsi_tenant[demo]",
                f"extra_condition_absent_denied:sweep-control:dynamodb:Query@{t}/gsi_tenant"
                "[demo,dynamodb:Select]",
            )
        }

    def test_a_count_restriction_on_the_eval_grant_reds_the_sibling_rows(self) -> None:
        count = iam_roles.ExtraCondition("StringEquals", "dynamodb:Select", ("COUNT",))
        deployed = [
            g._replace(extra_conditions=(count,)) if "gsi_tenant" in g.index_names else g
            for g in _control_grants()
        ]
        failing = {c for c, r in _extra_rows(deployed).items() if r.status != "pass"}
        assert failing and all(c.startswith("extra_condition_sibling_allowed:") for c in failing)

    def test_a_denial_that_reports_no_missing_select_fails_the_absent_row(self) -> None:
        class _NoMissing(_GrantIam):
            def simulate_principal_policy(self, **request: Any) -> dict[str, Any]:
                answer = super().simulate_principal_policy(**request)
                answer["EvaluationResults"][0]["MissingContextValues"] = []
                return answer

        ctx = context_for_test(clients={"iam": _NoMissing(_control_grants())}, outputs={})
        rows = list(
            iam_roles.extra_condition_rows(
                ctx,
                "r",
                "arn:aws:iam::123456789012:role/r",
                "sweep-control",
                _control_grants(),
                lambda table: _TABLE_ARN.format(table=table),
            )
        )
        failing = {r.check for r in rows if r.status != "pass"}
        assert failing and all(c.startswith("extra_condition_absent_denied:") for c in failing)


class TestSelectConditionReader:
    """`select_condition` is read as a literal or `local.*` list, or raised, never dropped."""

    _LOCAL = (
        "locals {\n"
        '  probe_tables = [\n    "accounts", "notes",\n  ]\n'
        '  probe_select = ["COUNT"]\n'
        "  probe_grants = concat([\n"
        '    { table = "eval_runs", actions = ["dynamodb:GetItem"], index_names = [] },\n'
        '    { table = "reps", actions = ["dynamodb:Query"], index_names = ["gsi_tenant"], '
        'include_table_arn = false, leading_key_condition = { attribute = "dynamodb:LeadingKeys", '
        'operator = "StringEquals", values = ["demo"] }, select_condition = SELECT },\n'
        "    ],\n"
        "    [\n"
        "      for table in local.probe_tables : {\n"
        "        table                 = table\n"
        '        actions               = ["dynamodb:Query"]\n'
        '        index_names           = ["gsi_tenant"]\n'
        "        include_table_arn     = false\n"
        '        leading_key_condition = { attribute = "dynamodb:LeadingKeys", operator = "StringEquals", values = ["demo"] }\n'
        "        select_condition      = SELECT\n"
        "      }\n"
        "    ],\n"
        "  )\n"
        "}\n"
    )
    _COUNT = (iam_roles.ExtraCondition("StringEquals", "dynamodb:Select", ("COUNT",)),)

    def _grants(self, select: str) -> list[iam_roles.LocalGrant]:
        return iam_roles.terraform_local_grants(
            "probe_grants", self._LOCAL.replace("SELECT", select)
        )

    @pytest.mark.parametrize(
        "select", ["local.probe_select", '["COUNT"]'], ids=["local", "literal"]
    )
    def test_a_select_condition_is_read_on_inline_and_comprehension_objects(
        self, select: str
    ) -> None:
        grants = self._grants(select)
        assert [g.table for g in grants] == ["eval_runs", "reps", "accounts", "notes"]
        assert grants[0].extra_conditions == ()
        assert all(g.extra_conditions == self._COUNT for g in grants[1:])

    def test_an_empty_select_condition_is_no_condition(self) -> None:
        assert all(g.extra_conditions == () for g in self._grants("[]"))

    @pytest.mark.parametrize(
        "select",
        ['["EVERYTHING"]', "local.absent_list", 'try(local.x, ["COUNT"])', "[local.probe_select]"],
        ids=["unknown-value", "unresolved-local", "call", "nested-reference"],
    )
    def test_an_unreadable_select_condition_raises(self, select: str) -> None:
        with pytest.raises(iam_roles.UnreadableTerraformGrantsError):
            self._grants(select)

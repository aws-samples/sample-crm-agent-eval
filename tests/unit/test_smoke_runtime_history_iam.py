# SPDX-License-Identifier: MIT
"""Unit tests for `scripts/smoke/runtime_history_iam.py`, against fake IAM and DynamoDB clients.

The fake IAM client evaluates the intended Runtime history policy; each planted weakening of it
reds exactly the rows that guard that statement.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import botocore.session
import pytest
from scripts.smoke import runtime_history_iam as mod
from scripts.smoke.harness import CheckResult, context_for_test

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT}:role/crm-agent-eval-runtime-role"
_KEY_ARN = f"arn:aws:kms:{_REGION}:{_ACCOUNT}:key/fixture-dynamodb-cmk"
_TABLES = {
    name: f"crm-agent-eval-{name}"
    for name in ("reps", "accounts", "opportunities", "audit_records")
}


def _table_arn(physical: str) -> str:
    return f"arn:aws:dynamodb:{_REGION}:{_ACCOUNT}:table/{physical}"


_REPS_ARN = _table_arn(_TABLES["reps"])


def _context_values(context: list[dict[str, Any]] | None, key: str) -> list[str] | None:
    for entry in context or []:
        if entry["ContextKeyName"] == key:
            return list(entry["ContextKeyValues"])
    return None


class _PolicyIam:
    """Evaluates the intended policy; each flag drops one statement or condition."""

    def __init__(
        self,
        *,
        null_condition: bool = True,
        prefix_deny: bool = True,
        canary_deny: bool = True,
        not_action_deny: bool = True,
        other_tables_deny: bool = True,
        attributes_condition: bool = True,
        kms_table_condition: bool = True,
        kms_via_service: bool = True,
        empty_results_for: str | None = None,
    ) -> None:
        self.null_condition = null_condition
        self.prefix_deny = prefix_deny
        self.canary_deny = canary_deny
        self.not_action_deny = not_action_deny
        self.other_tables_deny = other_tables_deny
        self.attributes_condition = attributes_condition
        self.kms_table_condition = kms_table_condition
        self.kms_via_service = kms_via_service
        self.empty_results_for = empty_results_for
        self.calls: list[dict[str, Any]] = []

    def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        action = kwargs["ActionNames"][0]
        resource = kwargs["ResourceArns"][0]
        if self.empty_results_for is not None and self.empty_results_for == action:
            return {"EvaluationResults": []}
        decision = self._decide(action, resource, kwargs.get("ContextEntries"))
        return {"EvaluationResults": [{"EvalActionName": action, "EvalDecision": decision}]}

    def _decide(self, action: str, resource: str, context: list[dict[str, Any]] | None) -> str:
        if action.startswith("kms:"):
            return self._decide_kms(action, context)
        keys = _context_values(context, "dynamodb:LeadingKeys")
        on_reps = resource == _REPS_ARN or resource.startswith(f"{_REPS_ARN}/index/")
        if not on_reps:
            return "explicitDeny" if self.other_tables_deny else "implicitDeny"
        if action not in mod.GRANTED_ACTIONS or resource != _REPS_ARN:
            return "explicitDeny" if self.not_action_deny else "implicitDeny"
        if keys is not None:
            if self.prefix_deny and any(not k.startswith("history#") for k in keys):
                return "explicitDeny"
            if self.canary_deny and any(k.startswith("history#canary#") for k in keys):
                return "explicitDeny"
        if keys is None and self.null_condition:
            return "implicitDeny"
        if keys is not None and not all(k.startswith("history#") for k in keys):
            return "implicitDeny"
        attributes = _context_values(context, "dynamodb:Attributes")
        if (
            action == mod.PUT_ITEM
            and self.attributes_condition
            and attributes is not None
            and not set(attributes) <= set(mod.HISTORY_ITEM_ATTRIBUTES)
        ):
            return "implicitDeny"
        return "allowed"

    def _decide_kms(self, action: str, context: list[dict[str, Any]] | None) -> str:
        if action not in mod.KMS_ACTIONS:
            return "implicitDeny"
        via = _context_values(context, "kms:ViaService")
        table = _context_values(context, "kms:EncryptionContext:aws:dynamodb:tableName")
        if self.kms_via_service and via != [f"dynamodb.{_REGION}.amazonaws.com"]:
            return "implicitDeny"
        if self.kms_table_condition and table != [_TABLES["reps"]]:
            return "implicitDeny"
        return "allowed"


class _FakeDynamoDb:
    def __init__(self, *, ttl_status: str = "ENABLED", ttl_attribute: str = "expires_at") -> None:
        self.ttl_status = ttl_status
        self.ttl_attribute = ttl_attribute

    def describe_table(self, TableName: str) -> dict[str, Any]:  # noqa: N803 - boto3 kwarg
        table: dict[str, Any] = {"TableName": TableName, "TableArn": _table_arn(TableName)}
        if TableName == _TABLES["reps"]:
            table["SSEDescription"] = {"KMSMasterKeyArn": _KEY_ARN}
        return {"Table": table}

    def describe_time_to_live(self, TableName: str) -> dict[str, Any]:  # noqa: N803
        assert TableName == _TABLES["reps"]
        return {
            "TimeToLiveDescription": {
                "TimeToLiveStatus": self.ttl_status,
                "AttributeName": self.ttl_attribute,
            }
        }


def _run(
    iam: _PolicyIam | None = None,
    dynamodb: _FakeDynamoDb | None = None,
    outputs: dict[str, Any] | None = None,
) -> list[CheckResult]:
    ctx = context_for_test(
        clients={"iam": iam or _PolicyIam(), "dynamodb": dynamodb or _FakeDynamoDb()},
        outputs=outputs
        if outputs is not None
        else {"agentcore_runtime_role_arn": _ROLE_ARN, "dynamodb_table_names": _TABLES},
        region=_REGION,
    )
    return list(mod.checks(ctx))


def _failed(results: list[CheckResult]) -> set[str]:
    return {r.check for r in results if r.status == "fail"}


def test_the_intended_policy_passes_every_row_and_meets_the_derived_minimum() -> None:
    results = _run()
    assert not _failed(results), [(r.check, r.detail) for r in results if r.status == "fail"]
    assert all(r.status == "pass" for r in results)
    ctx = context_for_test(outputs={"dynamodb_table_names": _TABLES}, region=_REGION)
    assert len(results) == mod.expected_check_count(ctx).minimum


def test_every_simulation_targets_the_runtime_role_from_the_output() -> None:
    iam = _PolicyIam()
    _run(iam=iam)
    assert iam.calls
    assert {call["PolicySourceArn"] for call in iam.calls} == {_ROLE_ARN}


def test_each_foreign_key_row_carries_a_leading_keys_context() -> None:
    iam = _PolicyIam()
    _run(iam=iam)
    no_context = [c for c in iam.calls if not c.get("ContextEntries")]
    assert len(no_context) == 1 and no_context[0]["ActionNames"] == [mod.GET_ITEM]


_MUTATIONS: list[tuple[str, Callable[[], _PolicyIam], set[str]]] = [
    (
        "null_condition",
        lambda: _PolicyIam(null_condition=False),
        {"deny_without_leading_keys:dynamodb:GetItem"},
    ),
    (
        "prefix_deny",
        lambda: _PolicyIam(prefix_deny=False),
        {
            f"explicit_deny:{action}@{label}"
            for action in mod.GRANTED_ACTIONS
            for label in ("usersettings#", "conversation#", "rep_id")
        },
    ),
    (
        "canary_deny",
        lambda: _PolicyIam(canary_deny=False),
        {f"explicit_deny:{action}@history#canary#" for action in mod.GRANTED_ACTIONS},
    ),
    (
        "not_action_deny",
        lambda: _PolicyIam(not_action_deny=False),
        {f"explicit_deny:{action}@reps" for action in mod.DENIED_TABLE_ACTIONS}
        | {"explicit_deny:dynamodb:Query@reps/index"},
    ),
    (
        "other_tables_deny",
        lambda: _PolicyIam(other_tables_deny=False),
        {
            f"explicit_deny:dynamodb:GetItem@{name}"
            for name in ("accounts", "opportunities", "audit_records")
        },
    ),
    (
        "attributes_condition",
        lambda: _PolicyIam(attributes_condition=False),
        {"deny_tenant_attribute:dynamodb:PutItem"},
    ),
    (
        "kms_table_condition",
        lambda: _PolicyIam(kms_table_condition=False),
        {"deny:kms:Decrypt@other_table"},
    ),
    (
        "kms_via_service",
        lambda: _PolicyIam(kms_via_service=False),
        {"deny:kms:Decrypt@without_via_service"},
    ),
]


@pytest.mark.parametrize(
    ("name", "make_iam", "expected"), _MUTATIONS, ids=[m[0] for m in _MUTATIONS]
)
def test_dropping_one_policy_element_reds_exactly_its_rows(
    name: str, make_iam: Callable[[], _PolicyIam], expected: set[str]
) -> None:
    assert _failed(_run(iam=make_iam())) == expected, name


def test_a_denied_grant_reds_its_allow_rows() -> None:
    class _NoGrant(_PolicyIam):
        def _decide(self, action: str, resource: str, context: Any) -> str:
            decision = super()._decide(action, resource, context)
            return "implicitDeny" if decision == "allowed" else decision

    failed = _failed(_run(iam=_NoGrant()))
    assert {f"allow:{a}@history#" for a in mod.GRANTED_ACTIONS} <= failed
    assert {f"allow:{a}@reps" for a in mod.KMS_ACTIONS} <= failed


def test_an_implicit_deny_does_not_satisfy_an_explicit_deny_row() -> None:
    class _AllImplicit(_PolicyIam):
        def _decide(self, action: str, resource: str, context: Any) -> str:
            decision = super()._decide(action, resource, context)
            return "implicitDeny" if decision == "explicitDeny" else decision

    failed = _failed(_run(iam=_AllImplicit()))
    assert failed and all(check.startswith("explicit_deny:") for check in failed)


def test_an_empty_evaluation_is_a_defer_not_a_pass() -> None:
    results = _run(iam=_PolicyIam(empty_results_for="dynamodb:Scan"))
    row = next(r for r in results if r.check == "explicit_deny:dynamodb:Scan@reps")
    assert row.status == "defer"
    assert row.defer_reason == "simulate_returned_no_evaluation_result"


@pytest.mark.parametrize(
    ("status", "attribute", "expected"),
    [
        ("ENABLED", "expires_at", "pass"),
        ("ENABLING", "expires_at", "defer"),
        ("DISABLED", "expires_at", "fail"),
        ("ENABLED", "ttl", "fail"),
    ],
)
def test_the_ttl_row(status: str, attribute: str, expected: str) -> None:
    results = _run(dynamodb=_FakeDynamoDb(ttl_status=status, ttl_attribute=attribute))
    row = next(r for r in results if r.check == "ttl_enabled:expires_at")
    assert row.status == expected


@pytest.mark.parametrize(
    "outputs",
    [
        {},
        {"agentcore_runtime_role_arn": _ROLE_ARN},
        {"agentcore_runtime_role_arn": _ROLE_ARN, "dynamodb_table_names": {"reps": "x-reps"}},
    ],
    ids=["no_outputs", "no_table_names", "reps_only"],
)
def test_missing_targets_are_one_failing_row_not_a_crash(outputs: dict[str, Any]) -> None:
    results = _run(outputs=outputs)
    assert len(results) == 1 and results[0].status == "fail"


def test_a_table_without_a_cmk_is_a_failure() -> None:
    class _NoCmk(_FakeDynamoDb):
        def describe_table(self, TableName: str) -> dict[str, Any]:  # noqa: N803
            response = super().describe_table(TableName)
            response["Table"].pop("SSEDescription", None)
            return response

    results = _run(dynamodb=_NoCmk())
    assert [r.check for r in results] == ["history_table_cmk"]
    assert results[0].status == "fail"


def test_the_denied_actions_are_real_dynamodb_actions() -> None:
    operations = set(botocore.session.get_session().get_service_model("dynamodb").operation_names)
    iam_only = {"dynamodb:PartiQLSelect", "dynamodb:ConditionCheckItem"}
    for action in set(mod.DENIED_TABLE_ACTIONS) - iam_only:
        assert action.removeprefix("dynamodb:") in operations, action
    for action in mod.GRANTED_ACTIONS:
        assert action.removeprefix("dynamodb:") in operations, action
    assert {"dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan"} <= set(
        mod.DENIED_TABLE_ACTIONS
    )
    assert {"dynamodb:BatchWriteItem", "dynamodb:UpdateItem"} <= set(mod.DENIED_TABLE_ACTIONS)


def test_the_history_attributes_exclude_tenant() -> None:
    assert "tenant" not in mod.HISTORY_ITEM_ATTRIBUTES
    assert "expires_at" in mod.HISTORY_ITEM_ATTRIBUTES
    assert len(set(mod.HISTORY_ITEM_ATTRIBUTES)) == len(mod.HISTORY_ITEM_ATTRIBUTES)


def test_the_history_prefix_is_the_store_key_prefix() -> None:
    from crm.store.keys import history_key

    assert history_key("demo", "s").startswith(mod.HISTORY_KEY_PREFIX)

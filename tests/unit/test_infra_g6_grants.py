# SPDX-License-Identifier: MIT
"""Plan-time pins for two grants: eval_trigger's artefact read and the create handlers'
audit write.

Reuses `test_infra_inline_policy_budget`'s offline root plan, in which every policy renders.
"""

from __future__ import annotations

import fnmatch
import json
from typing import Any, Final

import pytest

from tests.unit import test_infra_inline_policy_budget as budget
from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)

ACCOUNT = budget.ACCOUNT
EVAL_TRIGGER_ROLE = budget.EVAL_TRIGGER_ROLE
_as_list = budget._as_list
_dynamodb_outputs = budget._dynamodb_outputs
_lambda_action_roles = budget._lambda_action_roles
_role_dynamodb_statements = budget._role_dynamodb_statements


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)
    work_dir = tmp_path_factory.mktemp("g6-grants")
    return plan_root(work_dir, budget._build(work_dir))


ARTIFACTS_POLICY: Final[str] = "aws_iam_role_policy.eval_trigger_artifacts"
ARTIFACTS_BUCKET_ARN: Final[str] = f"arn:aws:s3:::crm-agent-eval-eval-artifacts-{ACCOUNT}"
ARTIFACT_PREFIX: Final[str] = "sweeps"

#: The five create handlers whose direct create writes its audit record in the same transaction.
AUDITED_CREATES: Final[tuple[str, ...]] = (
    "create_account",
    "create_contact",
    "create_opportunity",
    "create_note",
    "log_activity",
)


def _allows(statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in statements if s["Effect"] == "Allow"]


def _artifacts_statements(plan: Plan) -> list[dict[str, Any]]:
    rendered = plan.after(ARTIFACTS_POLICY)
    assert rendered["role"] == EVAL_TRIGGER_ROLE
    statements = json.loads(rendered["policy"])["Statement"]
    assert isinstance(statements, list)
    return statements


def test_eval_trigger_reads_only_the_artifact_prefix(plan: Plan) -> None:
    statements = _artifacts_statements(plan)
    s3 = [s for s in statements if any(a.startswith("s3:") for a in _as_list(s["Action"]))]
    assert len(s3) == 1
    assert s3[0]["Effect"] == "Allow"
    assert _as_list(s3[0]["Action"]) == ["s3:GetObject"]
    assert _as_list(s3[0]["Resource"]) == [f"{ARTIFACTS_BUCKET_ARN}/{ARTIFACT_PREFIX}/*"]
    assert "Condition" not in s3[0]


def test_eval_trigger_decrypts_the_artifact_cmk_only_through_s3(plan: Plan) -> None:
    statements = _artifacts_statements(plan)
    kms = [s for s in statements if any(a.startswith("kms:") for a in _as_list(s["Action"]))]
    assert len(kms) == 1
    assert _as_list(kms[0]["Action"]) == ["kms:Decrypt"]
    (key,) = _as_list(kms[0]["Resource"])
    assert key.startswith("arn:aws:kms:") and ":key/" in key
    assert kms[0]["Condition"] == {"StringEquals": {"kms:ViaService": "s3.us-east-1.amazonaws.com"}}


def test_the_artifacts_policy_carries_nothing_else(plan: Plan) -> None:
    actions = sorted(a for s in _artifacts_statements(plan) for a in _as_list(s["Action"]))
    assert actions == ["kms:Decrypt", "s3:GetObject"]


@pytest.mark.parametrize(
    "resource",
    [
        f"{ARTIFACTS_BUCKET_ARN}",
        f"{ARTIFACTS_BUCKET_ARN}/other/run/key.json",
        f"arn:aws:s3:::some-other-bucket/{ARTIFACT_PREFIX}/run/key.json",
    ],
)
def test_no_eval_trigger_s3_grant_covers_another_bucket_or_prefix(
    plan: Plan, resource: str
) -> None:
    for statement in _allows(_artifacts_statements(plan)):
        assert not any(fnmatch.fnmatchcase(resource, r) for r in _as_list(statement["Resource"])), (
            statement
        )


def test_eval_trigger_carries_the_artifact_root_env(plan: Plan) -> None:
    """The env map references the eval_artifacts module's bucket name and prefix outputs."""
    call = plan.configuration["root_module"]["module_calls"]["lambda_action_eval_trigger"]
    references = call["expressions"]["environment_variables"]["references"]
    assert "module.eval_artifacts.bucket_name" in references
    assert "module.eval_artifacts.artifact_prefix" in references


def _audit_allows(plan: Plan, module: str, role: str) -> list[tuple[str, str]]:
    table = _dynamodb_outputs()["table_arns"]["audit_records"]
    return sorted(
        (action, resource)
        for s in _allows(_role_dynamodb_statements(plan, module, role))
        for action in _as_list(s["Action"])
        for resource in _as_list(s["Resource"])
        if resource == table or resource.startswith(f"{table}/")
    )


@pytest.mark.parametrize("tool", AUDITED_CREATES)
def test_each_create_handler_may_put_exactly_one_audit_record_action(plan: Plan, tool: str) -> None:
    roles = _lambda_action_roles(plan)
    module = f'module.lambda_action["{tool}"]'
    table = _dynamodb_outputs()["table_arns"]["audit_records"]
    assert _audit_allows(plan, module, roles[module]) == [("dynamodb:PutItem", table)]

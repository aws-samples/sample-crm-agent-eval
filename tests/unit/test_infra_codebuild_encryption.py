# SPDX-License-Identifier: MIT
"""Plan-time assertions on the agentcore CodeBuild project's CMK encryption.

Plans `infra/modules/agentcore` with `test_infra_agentcore`'s literal arguments. The project's
`encryption_key` is a KMS key ARN taken from `var.source_kms_key_arn`, and the CodeBuild role's KMS
grant is exactly `kms:Decrypt`, `kms:Encrypt`, `kms:GenerateDataKey*` and `kms:DescribeKey` on that key.
The role policy renders only at apply time, so the grant is read from the plan's `configuration`.
Skips when terraform or the cached providers are absent, unless `CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Final

import pytest

from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_module,
    require_terraform,
    terraform_unavailable_reason,
)
from tests.unit.test_infra_agentcore import _module_arguments

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ROOT_MAIN_TF: Final[Path] = REPO_ROOT / "infra" / "main.tf"

PROJECT: Final[str] = "module.subject.aws_codebuild_project.agent_image"
PROJECT_CONFIG: Final[str] = "aws_codebuild_project.agent_image"
PERMISSIONS_CONFIG: Final[str] = "data.aws_iam_policy_document.codebuild_permissions"
SOURCE_KEY_ARN: Final[str] = "arn:aws:kms:us-east-1:123456789012:key/fixture-source"
KEY_VARIABLE: Final[str] = "var.source_kms_key_arn"

KMS_KEY_ARN: Final[re.Pattern[str]] = re.compile(r"^arn:aws:kms:[a-z0-9-]+:\d{12}:key/[\w-]+$")

PROJECT_KEY_ACTIONS: Final[frozenset[str]] = frozenset(
    {"kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey*", "kms:DescribeKey"}
)


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)
    return plan_module(tmp_path_factory.mktemp("codebuild-plan"), "agentcore", _module_arguments())


def _module_resources(plan: Plan) -> dict[str, dict[str, Any]]:
    resources = plan.configuration["root_module"]["module_calls"]["subject"]["module"]["resources"]
    return {r["address"]: r for r in resources}


def _statements(permissions: dict[str, Any]) -> list[dict[str, Any]]:
    statements = permissions["expressions"]["statement"]
    return [statements] if isinstance(statements, dict) else list(statements)


def kms_grant_failures(permissions: dict[str, Any]) -> list[str]:
    """Why the configured CodeBuild role grant is not exactly the project-key action set."""
    failures: list[str] = []
    granted: set[str] = set()
    for statement in _statements(permissions):
        actions = set(statement.get("actions", {}).get("constant_value") or [])
        kms_actions = {a for a in actions if a.startswith("kms:") or a == "*"}
        if not kms_actions:
            continue
        sid = statement.get("sid", {}).get("constant_value")
        effect = statement.get("effect", {}).get("constant_value")
        if effect != "Allow":
            failures.append(f"{sid}: KMS statement effect {effect!r}")
        resources = statement.get("resources", {})
        if resources.get("references") != [KEY_VARIABLE] or "constant_value" in resources:
            failures.append(f"{sid}: KMS resources {resources!r}, expected only {KEY_VARIABLE}")
        granted |= kms_actions
    if granted != PROJECT_KEY_ACTIONS:
        failures.append(f"KMS actions {sorted(granted)}, expected {sorted(PROJECT_KEY_ACTIONS)}")
    return failures


def test_the_project_encryption_key_is_a_kms_key_arn(plan: Plan) -> None:
    encryption_key = plan.after(PROJECT)["encryption_key"]
    assert KMS_KEY_ARN.match(encryption_key), encryption_key
    assert encryption_key == SOURCE_KEY_ARN


def test_the_project_encryption_key_is_the_source_key_variable(plan: Plan) -> None:
    expression = _module_resources(plan)[PROJECT_CONFIG]["expressions"]["encryption_key"]
    assert expression.get("references") == [KEY_VARIABLE], expression


def test_the_role_grants_exactly_the_project_key_actions_on_that_key(plan: Plan) -> None:
    assert kms_grant_failures(_module_resources(plan)[PERMISSIONS_CONFIG]) == []


@pytest.mark.parametrize(
    ("description", "mutate"),
    [
        ("an action dropped", lambda s: s["actions"]["constant_value"].remove("kms:DescribeKey")),
        ("a wildcard action", lambda s: s["actions"]["constant_value"].append("kms:*")),
        ("an extra action", lambda s: s["actions"]["constant_value"].append("kms:ReEncrypt*")),
        ("a wildcard resource", lambda s: s.__setitem__("resources", {"constant_value": ["*"]})),
    ],
)
def test_a_mutated_grant_reds_the_grant_check(plan: Plan, description: str, mutate: Any) -> None:
    permissions = copy.deepcopy(_module_resources(plan)[PERMISSIONS_CONFIG])
    [statement] = [
        s
        for s in _statements(permissions)
        if any(a.startswith("kms:") for a in s["actions"]["constant_value"])
    ]
    mutate(statement)
    assert kms_grant_failures(permissions), description


def test_the_root_passes_a_rotating_cmk_as_the_project_key() -> None:
    """The root wires `aws_kms_key.eval_artifacts` (rotation on) into `source_kms_key_arn`."""
    text = ROOT_MAIN_TF.read_text(encoding="utf-8")
    agentcore = re.search(r'(?ms)^module "agentcore" \{\n.*?^\}', text)
    assert agentcore, 'no module "agentcore" block'
    assert re.search(
        r"(?m)^\s*source_kms_key_arn\s*=\s*aws_kms_key\.eval_artifacts\.arn\s*$", agentcore.group(0)
    )
    key = re.search(r'(?ms)^resource "aws_kms_key" "eval_artifacts" \{\n.*?^\}', text)
    assert key, 'no resource "aws_kms_key" "eval_artifacts"'
    assert re.search(r"(?m)^\s*enable_key_rotation\s*=\s*true\s*$", key.group(0))

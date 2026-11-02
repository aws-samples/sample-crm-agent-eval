# SPDX-License-Identifier: MIT
"""The Runtime role invokes exactly the action Lambdas some agent tool set uses.

* `local.runtime_uninvocable_tools` in `infra/main.tf` equals the toolspec tools whose `agents` set
  is empty, so a tool gaining or losing every agent reds this file until the grant follows.
* Planning the `agentcore_runtime_permissions` document with one stand-in function per toolspec
  tool renders `InvokeActionLambdas` over every agent tool and none of the agent-less ones.
* Control: a scratch copy of the document without the exclusion grants the agent-less tools.
Terraform-backed cells skip when terraform is absent, unless `CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest

from crm.toolspec.definitions import ALL_TOOLS
from tests.unit._terraform_fixture import plan_root, require_terraform, terraform_unavailable_reason
from tests.unit.test_infra_bedrock_grant import _CAPABILITIES_YAML, _top_level_blocks

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_MAIN_TF: Final[Path] = _REPO_ROOT / "infra" / "main.tf"
_ACCOUNT: Final[str] = "123456789012"
_REGION: Final[str] = "us-east-1"
_SID: Final[str] = "InvokeActionLambdas"
_EXCLUSION_LOCAL = re.compile(r"runtime_uninvocable_tools\s*=\s*toset\(\[([^\]]*)\]\)")
_GRANT_EXPRESSION: Final[str] = (
    "[for name, m in module.lambda_action : m.function_arn "
    "if !contains(local.runtime_uninvocable_tools, name)]"
)
_UNEXCLUDED_EXPRESSION: Final[str] = "[for name, m in module.lambda_action : m.function_arn]"

AGENT_LESS: Final[frozenset[str]] = frozenset(t.name for t in ALL_TOOLS if not t.agents)
AGENT_TOOLS: Final[frozenset[str]] = frozenset(t.name for t in ALL_TOOLS if t.agents)


def _function_arn(tool: str) -> str:
    return f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:crm-agent-eval-{tool}"


def _declared_exclusions() -> frozenset[str]:
    match = _EXCLUSION_LOCAL.search(_MAIN_TF.read_text(encoding="utf-8"))
    assert match is not None, "infra/main.tf no longer declares local.runtime_uninvocable_tools"
    return frozenset(re.findall(r'"([^"]+)"', match.group(1)))


def test_the_exclusion_list_is_the_toolspec_agent_less_set() -> None:
    assert _declared_exclusions() == AGENT_LESS, (
        f"main.tf-only {sorted(_declared_exclusions() - AGENT_LESS)}, "
        f"toolspec-only {sorted(AGENT_LESS - _declared_exclusions())}"
    )


def test_approve_reject_and_revert_are_agent_less() -> None:
    assert {"approve_proposal", "reject_proposal", "revert_audit_record"} <= AGENT_LESS


def _subject_hcl(*, excluded: bool) -> str:
    blocks = _top_level_blocks(_MAIN_TF.read_text(encoding="utf-8"))
    locals_blocks = [
        b for b in blocks if b.startswith("locals ") and "runtime_uninvocable_tools" in b
    ]
    documents = [
        b
        for b in blocks
        if b.startswith('data "aws_iam_policy_document" "agentcore_runtime_permissions"')
    ]
    assert len(locals_blocks) == 1 and len(documents) == 1
    document = documents[0]
    assert _GRANT_EXPRESSION in document, "the InvokeActionLambdas expression changed"
    if not excluded:
        document = document.replace(_GRANT_EXPRESSION, _UNEXCLUDED_EXPRESSION)
    for expression, literal in {
        "data.aws_caller_identity.current.account_id": f'"{_ACCOUNT}"',
        "var.aws_region": f'"{_REGION}"',
        "local.agent_ecr_repository_name": '"crm-agent-eval-agent"',
    }.items():
        document = document.replace(expression, literal)
    document = document.replace("module.lambda_action", "local.stand_in_lambda_action")
    stand_ins = ",\n".join(
        f'    {t.name} = {{ function_arn = "{_function_arn(t.name)}" }}' for t in ALL_TOOLS
    )
    return (
        f"{locals_blocks[0]}\n\nlocals {{\n  stand_in_lambda_action = {{\n{stand_ins}\n  }}\n}}\n\n"
        f"{document}\n"
        'resource "terraform_data" "runtime_permissions" {\n'
        "  input = data.aws_iam_policy_document.agentcore_runtime_permissions.json\n}\n"
    )


def _granted(tmp_path: Path, *, excluded: bool) -> set[str]:
    reason = terraform_unavailable_reason()
    if reason is not None and not require_terraform():
        pytest.skip(reason)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "model_capabilities.yaml").write_text(
        _CAPABILITIES_YAML.read_text(encoding="utf-8"), encoding="utf-8"
    )
    plan = plan_root(tmp_path / "infra", _subject_hcl(excluded=excluded))
    rendered: Any = plan.after("terraform_data.runtime_permissions")["input"]
    statements = {s["Sid"]: s for s in json.loads(rendered)["Statement"]}
    resources = statements[_SID]["Resource"]
    return {resources} if isinstance(resources, str) else set(resources)


def test_the_runtime_role_invokes_every_agent_tool_and_no_agent_less_tool(tmp_path: Path) -> None:
    granted = _granted(tmp_path, excluded=True)
    assert granted == {_function_arn(name) for name in AGENT_TOOLS}
    for name in ("approve_proposal", "reject_proposal", "revert_audit_record"):
        assert _function_arn(name) not in granted, name


def test_without_the_exclusion_the_agent_less_tools_are_granted(tmp_path: Path) -> None:
    granted = _granted(tmp_path, excluded=False)
    assert {_function_arn(name) for name in AGENT_LESS} <= granted

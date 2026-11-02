# SPDX-License-Identifier: MIT
"""Plan-time contract for the Runtime role's Bedrock grant against the capability matrix.

Plans the root's `bedrock_inference_profiles` locals block and the
`agentcore_runtime_permissions` policy document, read verbatim from `infra/main.tf`, beside a copy
of `config/model_capabilities.yaml` at the path the locals read. The rendered policy is captured
through a `terraform_data` resource and asserted:

* `BedrockInference` grants exactly the matrix's `inference_profile` ARNs plus `project/default`;
* `BedrockInferenceFoundationModels` grants exactly the region-less and any-region foundation-model
  ARN of each profile, bound by a `bedrock:InferenceProfileArn` condition naming exactly those
  profiles;
* no retired profile is granted;
* a model added to a scratch copy of the matrix grows both statements, and a duplicate profile is
  granted once;
* a profile that is not `global.<its family>.<model-id>` (regional, wildcard, wrong family)
  fails the plan's precondition.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from scripts.smoke.runtime_bedrock_iam import RETIRED_INFERENCE_PROFILES

from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    TerraformError,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_MAIN_TF: Final[Path] = _REPO_ROOT / "infra" / "main.tf"
_CAPABILITIES_YAML: Final[Path] = _REPO_ROOT / "config" / "model_capabilities.yaml"

ACCOUNT: Final[str] = "123456789012"
REGION: Final[str] = "us-east-1"

_PROFILES_SID: Final[str] = "BedrockInference"
_FOUNDATION_MODELS_SID: Final[str] = "BedrockInferenceFoundationModels"
_CONDITION_KEY: Final[str] = "bedrock:InferenceProfileArn"
_PROJECT_ARN: Final[str] = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:project/default"

#: Root expressions the fixture cannot evaluate offline, and their deployed shape.
_SUBSTITUTIONS: Final[dict[str, str]] = {
    "[for name, m in module.lambda_action : m.function_arn if !contains(local.runtime_uninvocable_tools, name)]": (
        f'["arn:aws:lambda:{REGION}:{ACCOUNT}:function:crm-agent-eval-smoke"]'
    ),
    "data.aws_caller_identity.current.account_id": f'"{ACCOUNT}"',
    "var.aws_region": f'"{REGION}"',
    "local.agent_ecr_repository_name": '"crm-agent-eval-agent"',
}

_CAPTURE_HCL: Final[str] = """
resource "terraform_data" "runtime_permissions" {
  input = data.aws_iam_policy_document.agentcore_runtime_permissions.json
}
"""

_BLOCK_START = re.compile(r"^(locals|data|resource|module|variable|output|check)\b[^\n]*\{\s*$")


def _top_level_blocks(text: str) -> list[str]:
    """Every column-0 block of `text` that closes on a column-0 `}`, each as its full source."""
    blocks: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if not _BLOCK_START.match(lines[index]):
            index += 1
            continue
        end = index + 1
        while lines[end] != "}":
            end += 1
        blocks.append("\n".join(lines[index : end + 1]))
        index = end + 1
    return blocks


def _subject_hcl() -> str:
    """The grant's locals block and policy document from `infra/main.tf`, made plannable offline."""
    blocks = _top_level_blocks(_MAIN_TF.read_text(encoding="utf-8"))
    locals_blocks = [
        b for b in blocks if b.startswith("locals ") and "bedrock_inference_profiles" in b
    ]
    documents = [
        b
        for b in blocks
        if b.startswith('data "aws_iam_policy_document" "agentcore_runtime_permissions"')
    ]
    assert len(locals_blocks) == 1, f"expected one locals block, found {len(locals_blocks)}"
    assert len(documents) == 1, f"expected one policy document, found {len(documents)}"
    document = documents[0]
    for expression, literal in _SUBSTITUTIONS.items():
        assert expression in document, f"{expression!r} is no longer in the policy document"
        document = document.replace(expression, literal)
    return f"{locals_blocks[0]}\n\n{document}\n{_CAPTURE_HCL}"


def _plan_policy(work_dir: Path, capabilities: str) -> dict[str, dict[str, Any]]:
    """Plan the subject beside `capabilities` and return the rendered statements by sid."""
    (work_dir / "config").mkdir(parents=True)
    (work_dir / "config" / "model_capabilities.yaml").write_text(capabilities, encoding="utf-8")
    plan = plan_root(work_dir / "infra", _subject_hcl())
    rendered = plan.after("terraform_data.runtime_permissions")["input"]
    assert isinstance(rendered, str), f"the policy document is unknown at plan time: {rendered!r}"
    statements = json.loads(rendered)["Statement"]
    return {str(s["Sid"]): s for s in statements}


def _as_set(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    assert isinstance(value, list), f"expected a string or list, got {value!r}"
    return {str(v) for v in value}


def _yaml_profiles(capabilities: str) -> set[str]:
    data = yaml.safe_load(capabilities)
    return {str(entry["inference_profile"]) for entry in data["models"].values()}


def _profile_arn(profile: str) -> str:
    return f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{profile}"


def _foundation_model_arns(profiles: set[str]) -> set[str]:
    models = {p.removeprefix("global.") for p in profiles}
    return {f"arn:aws:bedrock:::foundation-model/{m}" for m in models} | {
        f"arn:aws:bedrock:*::foundation-model/{m}" for m in models
    }


def _with_extra_models(capabilities: str, profiles: dict[str, str]) -> str:
    """`capabilities` with one extra model entry per `{model_id: inference_profile}`."""
    data = yaml.safe_load(capabilities)
    for model_id, profile in profiles.items():
        entry = dict(next(iter(data["models"].values())))
        entry["inference_profile"] = profile
        data["models"][model_id] = entry
    return str(yaml.safe_dump(data, sort_keys=False))


@pytest.fixture(scope="module")
def _terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)


@pytest.fixture(scope="module")
def statements(_terraform: None, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    work_dir = tmp_path_factory.mktemp("bedrock-grant")
    return _plan_policy(work_dir, _CAPABILITIES_YAML.read_text(encoding="utf-8"))


def _assert_grant_equals(statements: dict[str, Any], profiles: set[str]) -> None:
    profile_arns = {_profile_arn(p) for p in profiles}
    assert _as_set(statements[_PROFILES_SID]["Resource"]) == profile_arns | {_PROJECT_ARN}
    models = statements[_FOUNDATION_MODELS_SID]
    assert _as_set(models["Resource"]) == _foundation_model_arns(profiles)
    assert _as_set(models["Condition"]["StringEquals"][_CONDITION_KEY]) == profile_arns


def test_both_bedrock_statements_equal_the_capability_matrix(statements: dict[str, Any]) -> None:
    """Profiles, foundation models and the condition are exactly the matrix's profile set."""
    profiles = _yaml_profiles(_CAPABILITIES_YAML.read_text(encoding="utf-8"))
    assert profiles, "config/model_capabilities.yaml advertises no inference_profile"
    _assert_grant_equals(statements, profiles)


def test_every_granted_profile_is_global(statements: dict[str, Any]) -> None:
    """Every inference-profile ARN in the grant names a `global.` profile."""
    granted = _as_set(statements[_PROFILES_SID]["Resource"]) - {_PROJECT_ARN}
    assert granted
    assert all(arn.rsplit("/", 1)[1].startswith("global.") for arn in granted), sorted(granted)


def test_no_retired_profile_is_granted(statements: dict[str, Any]) -> None:
    """The retired profiles leave both statements."""
    assert RETIRED_INFERENCE_PROFILES
    rendered = json.dumps(statements)
    leaked = [p for p in RETIRED_INFERENCE_PROFILES if p.removeprefix("global.") in rendered]
    assert not leaked, f"retired profiles still granted: {leaked}"


def test_the_sid_contracts_are_unchanged(statements: dict[str, Any]) -> None:
    """`relay_iam` and `runtime_bedrock_iam` read these sids; both invoke actions stay granted."""
    for sid in (_PROFILES_SID, _FOUNDATION_MODELS_SID, "InvokeActionLambdas"):
        assert statements[sid]["Effect"] == "Allow", sid
    for sid in (_PROFILES_SID, _FOUNDATION_MODELS_SID):
        assert _as_set(statements[sid]["Action"]) == {
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
        }


def test_a_model_added_to_a_scratch_matrix_grows_the_grant(
    _terraform: None, tmp_path: Path
) -> None:
    """The grant is derived: a new matrix entry is granted, and a duplicate profile only once."""
    original = _CAPABILITIES_YAML.read_text(encoding="utf-8")
    added = "global.anthropic.claude-scratch-0"
    existing = sorted(_yaml_profiles(original))[0]
    mutated = _with_extra_models(
        original, {"anthropic.claude-scratch-0": added, "anthropic.claude-duplicate-0": existing}
    )
    statements = _plan_policy(tmp_path, mutated)
    _assert_grant_equals(statements, _yaml_profiles(original) | {added})
    resources = statements[_PROFILES_SID]["Resource"]
    assert len(resources) == len(set(resources)), resources


@pytest.mark.parametrize(
    "profile",
    [
        "us.anthropic.claude-scratch-0",
        "global.*",
        "global.anthropic.*",
        "global.openai.claude-scratch-0",
        "global.anthropic.",
    ],
    ids=["regional", "wildcard", "family-wildcard", "wrong-family", "no-model"],
)
def test_a_malformed_profile_fails_the_precondition(
    _terraform: None, tmp_path: Path, profile: str
) -> None:
    """A profile that is not `global.<its family>.<model-id>` stops the plan."""
    mutated = _with_extra_models(
        _CAPABILITIES_YAML.read_text(encoding="utf-8"), {"anthropic.claude-scratch-0": profile}
    )
    with pytest.raises(TerraformError, match=r"Resource precondition failed") as raised:
        _plan_policy(tmp_path, mutated)
    assert "must be global.<family>.<model-id>" in " ".join(str(raised.value).split())

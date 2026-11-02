# SPDX-License-Identifier: MIT
"""The agent ECR lifecycle policy selects the tags the stack actually pushes.

The tag side is parsed from `local.agent_image_tag` in `infra/main.tf`; the selection side is read
from the rendered `aws_ecr_lifecycle_policy.agent` in a real plan of `infra/modules/agentcore`. Skips
when terraform or the cached providers are absent, unless `CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_module,
    require_terraform,
    terraform_unavailable_reason,
)
from tests.unit.test_infra_agentcore import _module_arguments

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_MAIN_TF = REPO_ROOT / "infra" / "main.tf"

LIFECYCLE_ADDRESS = "module.subject.aws_ecr_lifecycle_policy.agent"

_AGENT_IMAGE_TAG = re.compile(r'^\s*agent_image_tag\s*=\s*"([^"$]*)\$\{', re.MULTILINE)
_MODULE_IMAGE_TAG = re.compile(r"^\s*image_tag\s*=\s*local\.agent_image_tag\s*$", re.MULTILINE)


def _image_tag_prefix() -> str:
    """The literal text before the first interpolation in `local.agent_image_tag`."""
    matches = _AGENT_IMAGE_TAG.findall(ROOT_MAIN_TF.read_text(encoding="utf-8"))
    assert len(matches) == 1, f"expected one agent_image_tag definition, found {matches!r}"
    prefix: str = matches[0]
    assert prefix, "agent_image_tag has no literal prefix before its first interpolation"
    return prefix


def _tagged_rules(policy: dict[str, Any]) -> list[dict[str, Any]]:
    rules = policy.get("rules")
    assert isinstance(rules, list) and rules, f"lifecycle policy has no rules: {policy!r}"
    return [r for r in rules if r.get("selection", {}).get("tagStatus") == "tagged"]


@pytest.fixture(scope="module")
def lifecycle_policy(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)
    plan: Plan = plan_module(
        tmp_path_factory.mktemp("ecr-lifecycle-plan"), "agentcore", _module_arguments()
    )
    rendered = plan.after(LIFECYCLE_ADDRESS).get("policy")
    assert isinstance(rendered, str), f"policy is not known at plan time: {rendered!r}"
    policy: dict[str, Any] = json.loads(rendered)
    return policy


def test_the_module_is_given_the_agent_image_tag() -> None:
    """`module.agentcore` receives `local.agent_image_tag` as the tag it pushes and runs."""
    assert _MODULE_IMAGE_TAG.search(ROOT_MAIN_TF.read_text(encoding="utf-8"))


def test_the_tagged_rule_selects_the_pushed_tag_prefix(lifecycle_policy: dict[str, Any]) -> None:
    """The one tagged-image rule's `tagPrefixList` is exactly the prefix of `agent_image_tag`."""
    tagged = _tagged_rules(lifecycle_policy)
    assert len(tagged) == 1, f"expected one tagged-image rule, found {tagged!r}"
    selection = tagged[0]["selection"]
    assert selection.get("tagPrefixList") == [_image_tag_prefix()], selection
    assert "tagPatternList" not in selection, selection


def test_the_tagged_rule_keeps_the_newest_images(lifecycle_policy: dict[str, Any]) -> None:
    """The tagged rule expires by count beyond a positive number, so the newest image is kept."""
    selection = _tagged_rules(lifecycle_policy)[0]["selection"]
    assert selection.get("countType") == "imageCountMoreThan", selection
    count = selection.get("countNumber")
    assert isinstance(count, int) and count >= 1, selection

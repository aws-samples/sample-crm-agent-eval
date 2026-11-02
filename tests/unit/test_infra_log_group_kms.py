# SPDX-License-Identifier: MIT
"""Every `aws_cloudwatch_log_group` under `infra/` sets a non-empty `kms_key_id`, never null.

Read from the Terraform source as text: each resource block is located by brace matching over a copy
with comments, heredoc bodies and string contents blanked (`scripts.hcl_text`). Whether a
`var.<name>` reference resolves to null is a plan property, asserted by the plan gate's
`log_group_kms` control.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from scripts import plan_fixtures
from scripts.hcl_text import blank_comments, blank_literals

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA_DIR = REPO_ROOT / "infra"

_LOG_GROUP = re.compile(r'(?m)^resource\s+"aws_cloudwatch_log_group"\s+"(?P<name>[^"]+)"\s*\{')
_KMS_KEY_ID = re.compile(r"(?m)^\s*kms_key_id\s*=\s*(?P<value>\S.*?)\s*$")


def _block_body(blanked: str, open_brace: int) -> str:
    """The text between the brace at `open_brace` and its matching close, from blanked text."""
    depth = 0
    for index in range(open_brace, len(blanked)):
        if blanked[index] == "{":
            depth += 1
        elif blanked[index] == "}":
            depth -= 1
            if depth == 0:
                return blanked[open_brace + 1 : index]
    raise AssertionError(f"unbalanced braces from offset {open_brace}")


def _top_level(body: str, source: str) -> str:
    """`source` with every character inside a nested block of `body` dropped.

    `body` is the blanked form of `source`, so braces inside strings do not count.
    """
    out: list[str] = []
    depth = 0
    for char, original in zip(body, source, strict=True):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif depth == 0:
            out.append(original)
    return "".join(out)


def log_groups(infra_dir: Path) -> dict[str, str | None]:
    """Map `<file>:<resource name>` to the log group's top-level `kms_key_id` expression, or `None`.

    The expression keeps its string literals; comments are blanked.
    """
    found: dict[str, str | None] = {}
    for path in sorted(infra_dir.rglob("*.tf")):
        if ".terraform" in path.relative_to(infra_dir).parts:
            continue
        original = path.read_text(encoding="utf-8")
        uncommented = blank_comments(original)
        blanked = blank_literals(uncommented)
        for match in _LOG_GROUP.finditer(uncommented):
            open_brace = match.end() - 1
            body = _block_body(blanked, open_brace)
            source = uncommented[open_brace + 1 : open_brace + 1 + len(body)]
            setting = _KMS_KEY_ID.search(_top_level(body, source))
            key = f"{path.relative_to(infra_dir)}:{match.group('name')}"
            found[key] = None if setting is None else setting.group("value")
    return found


def _may_be_unset(expression: str | None) -> bool:
    """Whether a `kms_key_id` expression is absent, an empty string, or has a `null` branch."""
    if expression is None or re.fullmatch(r'"\s*"', expression):
        return True
    return re.search(r"\bnull\b", blank_literals(expression)) is not None


def unencrypted(infra_dir: Path) -> list[str]:
    """The log groups under `infra_dir` whose `kms_key_id` is absent, empty, or may be `null`."""
    return sorted(key for key, value in log_groups(infra_dir).items() if _may_be_unset(value))


def _planned_log_group_blocks() -> set[str]:
    """`<module dir>:<name>` of every log-group block in the compliant plan's `configuration`."""
    document = plan_fixtures.build("compliant")
    found: set[str] = set()

    def walk(module: dict[str, Any], source: str) -> None:
        for resource in module.get("resources", []):
            if resource.get("type") == "aws_cloudwatch_log_group":
                found.add(f"{source}:{resource['name']}")
        for call in module.get("module_calls", {}).values():
            walk(call["module"], call["source"].removeprefix("./"))

    walk(document["configuration"]["root_module"], ".")
    return found


def test_the_scan_finds_every_log_group_the_plan_configuration_declares() -> None:
    """Every log-group block in the plan's parsed configuration is one the text scan found."""
    planned = _planned_log_group_blocks()
    scanned = {
        f"{Path(key.split(':')[0]).parent}:{key.split(':')[1]}" for key in log_groups(INFRA_DIR)
    }
    assert planned, "the plan configuration declares no aws_cloudwatch_log_group"
    assert planned <= scanned, f"declared in the plan but missed by the scan: {planned - scanned}"


def test_every_log_group_sets_kms_key_id() -> None:
    """No log group under `infra/` falls back to the service-default key."""
    missing = unencrypted(INFRA_DIR)
    assert missing == [], f"log groups without kms_key_id: {missing}"


def test_a_log_group_without_kms_key_id_is_reported(tmp_path: Path) -> None:
    """Removing `kms_key_id` from one log group makes the check name exactly that group."""
    relative = Path("modules/lambda_action/main.tf")
    copy = tmp_path / relative
    copy.parent.mkdir(parents=True)
    shutil.copyfile(INFRA_DIR / relative, copy)
    source = copy.read_text(encoding="utf-8")
    assert "kms_key_id        = var.environment_kms_key_arn\n" in source
    copy.write_text(source.replace("kms_key_id        = var.environment_kms_key_arn\n", ""))
    assert unencrypted(tmp_path) == ["modules/lambda_action/main.tf:this"]


def test_a_null_or_nested_kms_key_id_is_not_accepted(tmp_path: Path) -> None:
    """`kms_key_id = null`, or a `kms_key_id` only inside a nested block, counts as unset."""
    (tmp_path / "a.tf").write_text(
        'resource "aws_cloudwatch_log_group" "nulled" {\n'
        '  name       = "/x/nulled"\n'
        "  kms_key_id = null\n"
        "}\n\n"
        'resource "aws_cloudwatch_log_group" "nested" {\n'
        '  name = "/x/nested"\n'
        "  lifecycle {\n"
        "    kms_key_id = var.k\n"
        "  }\n"
        "}\n\n"
        'resource "aws_cloudwatch_log_group" "commented" {\n'
        '  name = "/x/commented"\n'
        "  # kms_key_id = var.k\n"
        '  description = "kms_key_id = var.k"\n'
        "}\n\n"
        'resource "aws_cloudwatch_log_group" "set" {\n'
        '  name       = "/x/set"\n'
        "  kms_key_id = var.k\n"
        "}\n",
        encoding="utf-8",
    )
    assert unencrypted(tmp_path) == ["a.tf:commented", "a.tf:nested", "a.tf:nulled"]


@pytest.mark.parametrize(
    "expression",
    ['""', '"  "', "(null)", "var.enabled ? var.k : null", "try(var.k, null)"],
    ids=["empty-string", "blank-string", "parenthesised-null", "conditional-null", "try-null"],
)
def test_an_empty_or_possibly_null_kms_key_id_is_reported(tmp_path: Path, expression: str) -> None:
    """An empty string, or an expression with a `null` branch, counts as unset."""
    (tmp_path / "a.tf").write_text(
        'resource "aws_cloudwatch_log_group" "subject" {\n'
        '  name       = "/x/subject"\n'
        f"  kms_key_id = {expression}\n"
        "}\n",
        encoding="utf-8",
    )
    assert unencrypted(tmp_path) == ["a.tf:subject"]


def test_a_string_mentioning_null_is_not_mistaken_for_a_null_branch(tmp_path: Path) -> None:
    """`null` inside a string literal is data, not a `null` expression."""
    (tmp_path / "a.tf").write_text(
        'resource "aws_cloudwatch_log_group" "subject" {\n'
        '  name       = "/x/subject"\n'
        '  kms_key_id = lookup(var.keys, "null", var.k)\n'
        "}\n",
        encoding="utf-8",
    )
    assert unencrypted(tmp_path) == []

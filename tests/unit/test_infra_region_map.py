# SPDX-License-Identifier: MIT
"""Root `aws_region` against `infra/regions.json`: the US-regex validation and the map precondition.

The variable block and the guard resource are planned from their committed text, with the map
written beside the fixture root where `${path.module}/regions.json` resolves.
Skips when terraform or the cached providers are absent, unless `CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
from scripts.check_plan_security_baseline import EXPECTED_RESOURCE_COUNTS
from scripts.hcl_text import blank_comments, blank_heredoc_bodies

from tests.unit._terraform_fixture import (
    REPO_ROOT,
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    TerraformError,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)

INFRA: Final = REPO_ROOT / "infra"
MAIN_TF: Final = INFRA / "main.tf"
VARIABLES_TF: Final = INFRA / "variables.tf"
REGION_MAP: Final = INFRA / "regions.json"
GUARD_HEADER: Final = 'resource "terraform_data" "chat_scope_agreement"'
GUARD_ADDRESS: Final = "terraform_data.chat_scope_agreement"
CHAT_SCOPE: Final = '"crm-supervisor/chat"'

ZONE_NOTE_FILES: Final = (
    INFRA / "variables.tf",
    INFRA / "modules" / "agentcore" / "variables.tf",
    INFRA / "modules" / "vpc" / "variables.tf",
)


def _skip_unless_terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)


def _block(text: str, header: str) -> str:
    """The one column-0 block whose first line starts with `header`, brace-matched."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith(header)]
    assert len(starts) == 1, f"expected one {header!r} block, got {len(starts)}"
    depth = 0
    for offset, line in enumerate(lines[starts[0] :], start=starts[0]):
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return "".join(lines[starts[0] : offset + 1])
    raise AssertionError(f"unterminated {header!r}")


def _real_map() -> dict[str, Any]:
    document = json.loads(REGION_MAP.read_text("utf-8"))
    assert isinstance(document, dict)
    return document


def _root() -> str:
    """The committed region variable and guard, outside refs as literals."""
    variable = _block(VARIABLES_TF.read_text("utf-8"), 'variable "aws_region"')
    guard = _block(MAIN_TF.read_text("utf-8"), GUARD_HEADER)
    reference = "module.cognito.chat_scope_identifier"
    assert reference in guard, f"{reference} no longer appears in {GUARD_HEADER}"
    guard = guard.replace(reference, "local.cognito_chat_scope_identifier")
    return (
        f"{variable}\nlocals {{\n"
        f"  chat_scope_identifier         = {CHAT_SCOPE}\n"
        f"  cognito_chat_scope_identifier = {CHAT_SCOPE}\n}}\n{guard}"
    )


def _plan(work_dir: Path, region: str, region_map: dict[str, Any]) -> Plan:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "regions.json").write_text(json.dumps(region_map), encoding="utf-8")
    (work_dir / "region.auto.tfvars").write_text(f'aws_region = "{region}"\n', encoding="utf-8")
    return plan_root(work_dir, _root())


def _without(region_map: dict[str, Any], region: str) -> dict[str, Any]:
    synthetic: dict[str, Any] = json.loads(json.dumps(region_map))
    del synthetic["regions"][region]
    return synthetic


@pytest.mark.parametrize("region", sorted(_real_map()["regions"]))
def test_every_map_region_plans(tmp_path: Path, region: str) -> None:
    _skip_unless_terraform()
    plan = _plan(tmp_path, region, _real_map())
    assert GUARD_ADDRESS in plan.resource_changes


def test_us_west_2_plans_the_same_addresses_as_us_east_1(tmp_path: Path) -> None:
    _skip_unless_terraform()
    east = _plan(tmp_path / "east", "us-east-1", _real_map())
    west = _plan(tmp_path / "west", "us-west-2", _real_map())
    assert set(west.resource_changes) == set(east.resource_changes)


@pytest.mark.parametrize("region", ["eu-west-1", "us-gov-west-1", "us-west-10"])
def test_a_non_us_region_fails_validation_even_when_mapped(tmp_path: Path, region: str) -> None:
    """The regex refuses on its own: the synthetic map lists the region, so the precondition passes."""
    _skip_unless_terraform()
    synthetic = json.loads(json.dumps(_real_map()))
    synthetic["regions"][region] = synthetic["regions"]["us-east-1"]
    with pytest.raises(TerraformError, match=r"Invalid value for variable"):
        _plan(tmp_path, region, synthetic)


def test_a_region_missing_from_the_map_fails_the_precondition(tmp_path: Path) -> None:
    _skip_unless_terraform()
    with pytest.raises(TerraformError, match=r"Resource precondition failed") as raised:
        _plan(tmp_path, "us-west-2", _without(_real_map(), "us-west-2"))
    assert "infra/regions.json" in str(raised.value)


def test_the_guard_is_the_only_map_reader_in_the_root() -> None:
    """No `locals` entry reads the map, so harnesses copying main.tf's locals need none."""
    guard = _block(MAIN_TF.read_text("utf-8"), GUARD_HEADER)
    assert 'file("${path.module}/regions.json")' in guard
    assert "var.aws_region" in guard
    reader = re.compile(r"file\([^)]*regions\.json")
    readers = {
        path.name: len(reader.findall(path.read_text("utf-8").replace(guard, "")))
        for path in INFRA.glob("*.tf")
    }
    assert readers == dict.fromkeys(readers, 0)


def test_the_root_declares_exactly_the_counted_terraform_data_guards() -> None:
    """A guard added as a new `terraform_data` address would move `EXPECTED_RESOURCE_COUNTS`."""
    declared = sum(
        len(re.findall(r'^resource "terraform_data" "', path.read_text("utf-8"), re.MULTILINE))
        for path in INFRA.glob("*.tf")
    )
    assert declared == EXPECTED_RESOURCE_COUNTS["terraform_data"].fixed == 3


def test_no_count_or_for_each_reads_the_region() -> None:
    """Resource addresses cannot vary by region when no `count`/`for_each` reads it."""
    offenders = []
    for path in sorted(INFRA.rglob("*.tf")):
        if ".terraform" in path.parts or "bootstrap_account" in path.parts:
            continue
        code = blank_comments(blank_heredoc_bodies(path.read_text("utf-8")))
        for line_number, line in enumerate(code.splitlines(), start=1):
            if re.match(r"\s*(count|for_each)\s*=", line) and re.search(
                r"aws_region|region_map|data\.aws_region", line
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_number}: {line.strip()}")
    assert offenders == []


@pytest.mark.parametrize("path", ZONE_NOTE_FILES, ids=lambda p: str(p.relative_to(INFRA)))
def test_zone_notes_point_at_the_map(path: Path) -> None:
    text = path.read_text("utf-8")
    assert re.search(r"use1-az[0-9]", text) is None
    assert "infra/regions.json" in text


def test_the_vpc_short_name_error_names_no_region() -> None:
    text = (INFRA / "modules" / "vpc" / "variables.tf").read_text("utf-8")
    assert "com.amazonaws.<region>.kms" in text
    assert "com.amazonaws.us-east-1" not in text


def test_the_tfvars_example_zone_note_points_at_the_map() -> None:
    text = (INFRA / "terraform.tfvars.example").read_text("utf-8")
    assert re.search(r"use1-az[0-9]", text) is None
    assert "infra/regions.json" in text
    assert "--region us-east-1" in text

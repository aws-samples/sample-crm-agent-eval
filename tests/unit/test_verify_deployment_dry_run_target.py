# SPDX-License-Identifier: MIT
"""Tests that `verify_deployment.sh --dry-run` refuses a region the outputs snapshot contradicts.

Profile and region resolve flag → `AWS_PROFILE`/`AWS_REGION` → default. The account is not checked
offline and the preview says so. These tests run the real script with the real interpreter.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Final

import pytest
from scripts import proc_run

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SCRIPT: Final[Path] = _REPO_ROOT / "scripts" / "verify_deployment.sh"

#: An ARN naming a region, with the documentation-placeholder account id (`check_no_account_id`).
_EAST_ARN: Final[str] = "arn:aws:lambda:us-east-1:123456789012:function:crm-agent-eval-agent-relay"


def _outputs(tmp_path: Path, *, arn: str | None) -> Path:
    """An outputs snapshot in the `terraform output -json` shape, optionally naming a region."""
    path = tmp_path / "tf-outputs.json"
    document: dict[str, object] = {"cloudfront_domain_name": {"value": "d1.cloudfront.net"}}
    if arn is not None:
        document["agent_relay_function_arn"] = {"value": arn}
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _dry_run(
    outputs: Path, *, region: str | None, profile: str | None, ambient: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run `--dry-run` with a controlled environment and no inherited AWS target."""
    argv = ["bash", str(_SCRIPT), "--dry-run", "--outputs", str(outputs)]
    if region:
        argv += ["--region", region]
    if profile:
        argv += ["--profile", profile]
    env = {k: v for k, v in os.environ.items() if k not in ("AWS_REGION", "AWS_PROFILE")}
    env.update(ambient)
    return proc_run.run(
        argv, cwd=str(_REPO_ROOT), capture_output=True, text=True, env=env, check=False
    )


@pytest.fixture(autouse=True)
def _requires_venv() -> None:
    if not (_REPO_ROOT / ".venv" / "bin" / "python3").exists():
        pytest.skip(".venv not provisioned; the real interpreter is required for this check")


def test_the_preview_refuses_when_the_ambient_region_is_not_the_outputs_region(
    tmp_path: Path,
) -> None:
    """An ambient `AWS_REGION=us-west-2` against a us-east-1 snapshot exits 2 naming both."""
    result = _dry_run(
        _outputs(tmp_path, arn=_EAST_ARN),
        region=None,
        profile=None,
        ambient={"AWS_REGION": "us-west-2", "AWS_PROFILE": "cde-sandbox"},
    )
    assert result.returncode == 2, result.stdout
    assert "REFUSED" in result.stderr
    assert "us-west-2" in result.stderr
    assert "us-east-1" in result.stderr


def test_the_refusal_prints_no_preview_line_at_all(tmp_path: Path) -> None:
    """A refused preview prints no `would run:` line."""
    result = _dry_run(
        _outputs(tmp_path, arn=_EAST_ARN),
        region=None,
        profile=None,
        ambient={"AWS_REGION": "us-west-2"},
    )
    assert "would run:" not in result.stdout
    assert "would run:" not in result.stderr


def test_the_refusal_names_where_the_wrong_value_came_from(tmp_path: Path) -> None:
    """The refusal names the environment variables the wrong values came from."""
    result = _dry_run(
        _outputs(tmp_path, arn=_EAST_ARN),
        region=None,
        profile=None,
        ambient={"AWS_REGION": "us-west-2", "AWS_PROFILE": "cde-sandbox"},
    )
    assert "from AWS_REGION=us-west-2" in result.stderr
    assert "from AWS_PROFILE=cde-sandbox" in result.stderr


def test_a_matching_region_still_previews_every_module(tmp_path: Path) -> None:
    """A matching region previews every module and exits 0."""
    result = _dry_run(
        _outputs(tmp_path, arn=_EAST_ARN),
        region="us-east-1",
        profile="default",
        ambient={},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("would run:") >= 20


def test_an_explicit_region_flag_overrides_a_wrong_ambient_value(tmp_path: Path) -> None:
    """An explicit `--region` flag overrides a wrong ambient `AWS_REGION`."""
    result = _dry_run(
        _outputs(tmp_path, arn=_EAST_ARN),
        region="us-east-1",
        profile="default",
        ambient={"AWS_REGION": "us-west-2", "AWS_PROFILE": "cde-sandbox"},
    )
    assert result.returncode == 0, result.stderr
    assert "would run:" in result.stdout


def test_a_snapshot_naming_no_region_yields_no_opinion(tmp_path: Path) -> None:
    """Outputs with no regional ARN are not a mismatch, as in `assert_region_matches_outputs`."""
    result = _dry_run(
        _outputs(tmp_path, arn=None),
        region="eu-west-1",
        profile="default",
        ambient={},
    )
    assert result.returncode == 0, result.stderr
    assert "would run:" in result.stdout


def test_the_accepted_preview_declares_the_account_half_unchecked(tmp_path: Path) -> None:
    """An accepted preview states that the account half is NOT checked."""
    result = _dry_run(
        _outputs(tmp_path, arn=_EAST_ARN), region="us-east-1", profile="default", ambient={}
    )
    assert "account half" in result.stdout
    assert "NOT checked" in result.stdout


def test_an_absent_snapshot_is_not_reported_as_a_target_conflict(tmp_path: Path) -> None:
    """An absent outputs snapshot is not reported as `REFUSED`."""
    result = _dry_run(
        tmp_path / "does-not-exist.json", region="us-west-2", profile="default", ambient={}
    )
    assert "REFUSED" not in result.stderr

# SPDX-License-Identifier: MIT
"""Tests for `scripts/refresh_outputs.sh`, run against a stub `terraform` with no AWS call."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest
from scripts import proc_run
from scripts.outputs_provenance import PROVENANCE_KEY

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Records its argv and environment, then answers `output -json` from STUB_TF_OUTPUT.
_TERRAFORM_STUB: Final[str] = """#!/usr/bin/env bash
set -euo pipefail
echo "$* AWS_PROFILE=${AWS_PROFILE:-} AWS_REGION=${AWS_REGION:-}" >> "${TF_CALL_LOG}"
if [[ "${STUB_TF_FAIL:-}" == "1" ]]; then
  echo "stub: state unreadable" >&2
  exit 1
fi
printf '%s' "${STUB_TF_OUTPUT}"
"""

_OUTPUTS: Final[dict[str, object]] = {
    "api_endpoint": {"value": "https://x", "type": "string", "sensitive": False},
    "admin_group_name": {"value": "admin", "type": "string", "sensitive": False},
}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy holding the script and its stamper, with a stub terraform first on PATH."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "infra").mkdir()
    for name in ("refresh_outputs.sh", "outputs_provenance.py", "proc_run.py"):
        shutil.copy2(_REPO_ROOT / "scripts" / name, root / "scripts" / name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "terraform").write_text(_TERRAFORM_STUB, encoding="utf-8")
    (bin_dir / "terraform").chmod(0o755)
    (bin_dir / "python3").symlink_to(sys.executable)
    return root


def _run(
    tree: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    base = {
        "PATH": f"{tree.parent / 'bin'}:/usr/bin:/bin",
        "HOME": str(tree.parent),
        "TF_CALL_LOG": str(tree.parent / "tf.log"),
        "STUB_TF_OUTPUT": json.dumps(_OUTPUTS),
    }
    return proc_run.run(
        ["bash", str(tree / "scripts" / "refresh_outputs.sh"), *args],
        cwd=tree,
        capture_output=True,
        text=True,
        env={**base, **(env or {})},
        check=False,
    )


def _snapshot(tree: Path) -> Path:
    return tree / "results" / "tf-outputs.json"


def test_a_refresh_records_the_outputs_with_a_refresh_stamp(tree: Path) -> None:
    result = _run(tree, "--profile", "p1", "--region", "r1")
    assert result.returncode == 0, result.stderr
    document = json.loads(_snapshot(tree).read_text(encoding="utf-8"))
    assert {k: v for k, v in document.items() if not k.startswith("_")} == _OUTPUTS
    how = document[PROVENANCE_KEY]["how"]
    assert "scripts/refresh_outputs.sh" in how and "no apply" in how
    assert not Path(f"{_snapshot(tree)}.partial").exists()
    assert "recorded 2 outputs" in result.stdout


def test_profile_and_region_resolve_flag_then_env_then_default(tree: Path) -> None:
    log = tree.parent / "tf.log"
    _run(tree, "--profile", "flagp", env={"AWS_PROFILE": "envp", "AWS_REGION": "envr"})
    _run(tree, env={"AWS_PROFILE": "envp"})
    _run(tree)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith("AWS_PROFILE=flagp AWS_REGION=envr")
    assert lines[1].endswith("AWS_PROFILE=envp AWS_REGION=us-east-1")
    assert lines[2].endswith("AWS_PROFILE=default AWS_REGION=us-east-1")
    assert all(line.startswith("output -no-color -json") for line in lines)


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"STUB_TF_FAIL": "1"}, "terraform output -json exited non-zero"),
        ({"STUB_TF_OUTPUT": "{}"}, "not a non-empty JSON object"),
        ({"STUB_TF_OUTPUT": "not json"}, "not a non-empty JSON object"),
    ],
)
def test_a_failed_refresh_leaves_the_previous_snapshot(
    tree: Path, env: dict[str, str], reason: str
) -> None:
    _snapshot(tree).parent.mkdir(parents=True)
    _snapshot(tree).write_text('{"previous": 1}', encoding="utf-8")
    result = _run(tree, env=env)
    assert result.returncode == 1
    assert reason in result.stderr
    assert _snapshot(tree).read_text(encoding="utf-8") == '{"previous": 1}'
    assert not Path(f"{_snapshot(tree)}.partial").exists()


def test_static_credentials_are_refused_before_terraform_runs(tree: Path) -> None:
    result = _run(tree, env={"AWS_ACCESS_KEY_ID": "AKIAEXAMPLE"})
    assert result.returncode == 2
    assert "Unset them" in result.stderr
    assert not (tree.parent / "tf.log").exists()


def test_an_unknown_argument_is_a_usage_error(tree: Path) -> None:
    result = _run(tree, "--apply")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr

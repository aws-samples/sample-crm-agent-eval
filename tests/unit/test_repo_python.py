# SPDX-License-Identifier: MIT
"""Tests for `scripts/repo_python.sh` and the scripts that resolve repository Python through it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest
from scripts import proc_run

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_HELPER: Final[Path] = _REPO_ROOT / "scripts" / "repo_python.sh"
_BASH: Final[str] = shutil.which("bash") or "/bin/bash"

#: Answers the helper's version probe with __VERSION__; otherwise records that it was executed and the
#: LC_ALL it received.
_PYTHON_SHIM: Final[str] = """#!/bin/sh
if [ "$1" = "-c" ] && case "$2" in *version_info*) true;; *) false;; esac; then
  echo "__VERSION__"
  exit 0
fi
echo "shim-ran $*"
echo "lc_all=${LC_ALL-<unset>}"
"""

#: Every caller that runs repository Python through the helper, as a path relative to the repo.
_CALLERS: Final[tuple[str, ...]] = (
    ".pre-commit-config.yaml",
    "githooks/pre-commit",
    "scripts/deploy.sh",
    "scripts/refresh_outputs.sh",
    "scripts/teardown.sh",
    "scripts/verify_fixtures.sh",
    "scripts/bootstrap_account.sh",
    "scripts/check_cost_withheld.sh",
    "scripts/package_lambdas.sh",
    "scripts/preflight_adopted_vpc.sh",
    "frontend/scripts/generate-types.mjs",
    "frontend/src/test/responseShapes.test.ts",
)


def _tool_bin(tmp_path: Path) -> Path:
    """A PATH directory holding only the external tools the scripts need, and no python3."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("bash", "env", "dirname", "basename", "mktemp", "rm", "mkdir", "cat"):
        found = shutil.which(name)
        assert found is not None, name
        (tools / name).symlink_to(found)
    return tools


def _shim(directory: Path, version: str = "${SHIM_VERSION}") -> Path:
    """A `python3` shim reporting ``version``; by default, whatever ``$SHIM_VERSION`` holds."""
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "python3"
    shim.write_text(_PYTHON_SHIM.replace("__VERSION__", version), encoding="utf-8")
    shim.chmod(0o755)
    return shim


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A repo root holding only the helper; no virtualenv."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(_HELPER, root / "scripts" / "repo_python.sh")
    return root


def _run_helper(
    root: Path, path: str, *args: str, version: str = "", extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        [_BASH, str(root / "scripts" / "repo_python.sh"), *args],
        cwd=root,
        capture_output=True,
        text=True,
        env={"PATH": path, "HOME": str(root.parent), "SHIM_VERSION": version, **(extra_env or {})},
        check=False,
    )


def test_the_project_virtualenv_wins_whatever_the_ambient_python3(sandbox: Path) -> None:
    venv_python = _shim(sandbox / ".venv" / "bin", version="3.14")
    ambient = _shim(sandbox.parent / "ambient")
    tools = _tool_bin(sandbox.parent)
    result = _run_helper(sandbox, f"{ambient.parent}:{tools}", "--print", version="3.13")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(venv_python)


def test_a_stale_virtualenv_below_the_floor_is_refused_naming_both_versions(sandbox: Path) -> None:
    venv_python = _shim(sandbox / ".venv" / "bin", version="3.13")
    ambient = _shim(sandbox.parent / "ambient")
    tools = _tool_bin(sandbox.parent)
    path = f"{ambient.parent}:{tools}"
    for args in (("--print",), ("scripts/x.py",)):
        result = _run_helper(sandbox, path, *args, version="3.14")
        assert result.returncode == 2, result.stdout
        assert result.stdout == ""
        assert "requires Python >= 3.14" in result.stderr
        assert f"the project virtualenv {venv_python} is 3.13" in result.stderr


def test_an_unreadable_virtualenv_version_is_refused(sandbox: Path) -> None:
    _shim(sandbox / ".venv" / "bin", version="garbage")
    tools = _tool_bin(sandbox.parent)
    result = _run_helper(sandbox, str(tools), "--print")
    assert result.returncode == 2
    assert "unreadable version 'garbage'" in result.stderr


@pytest.mark.parametrize(
    ("caller_env", "expected"),
    [({}, "lc_all=<unset>"), ({"LC_ALL": "en_US.UTF-8"}, "lc_all=en_US.UTF-8")],
    ids=["unset", "set"],
)
def test_the_interpreter_runs_under_the_callers_lc_all(
    sandbox: Path, caller_env: dict[str, str], expected: str
) -> None:
    _shim(sandbox / ".venv" / "bin", version="3.14")
    tools = _tool_bin(sandbox.parent)
    result = _run_helper(sandbox, str(tools), "scripts/x.py", extra_env=caller_env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["shim-ran scripts/x.py", expected]


def test_an_ambient_python3_at_the_floor_is_accepted_and_executed(sandbox: Path) -> None:
    ambient = _shim(sandbox.parent / "ambient")
    tools = _tool_bin(sandbox.parent)
    path = f"{ambient.parent}:{tools}"
    printed = _run_helper(sandbox, path, "--print", version="3.14")
    assert printed.returncode == 0, printed.stderr
    assert printed.stdout.strip() == str(ambient)
    executed = _run_helper(sandbox, path, "scripts/x.py", "--flag", version="3.14")
    assert executed.returncode == 0, executed.stderr
    assert executed.stdout.splitlines()[0] == "shim-ran scripts/x.py --flag"


def test_a_newer_ambient_python3_is_accepted(sandbox: Path) -> None:
    ambient = _shim(sandbox.parent / "ambient")
    tools = _tool_bin(sandbox.parent)
    result = _run_helper(sandbox, f"{ambient.parent}:{tools}", "--print", version="4.0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(ambient)


def test_an_ambient_python3_below_the_floor_is_refused_by_name(sandbox: Path) -> None:
    ambient = _shim(sandbox.parent / "ambient")
    tools = _tool_bin(sandbox.parent)
    result = _run_helper(sandbox, f"{ambient.parent}:{tools}", "scripts/x.py", version="3.13")
    assert result.returncode == 2
    assert "shim-ran" not in result.stdout
    assert "requires Python >= 3.14" in result.stderr
    assert f"the ambient {ambient} is 3.13" in result.stderr


def test_an_unreadable_ambient_version_is_refused(sandbox: Path) -> None:
    ambient = _shim(sandbox.parent / "ambient")
    tools = _tool_bin(sandbox.parent)
    result = _run_helper(sandbox, f"{ambient.parent}:{tools}", "--print", version="garbage")
    assert result.returncode == 2
    assert "unreadable version 'garbage'" in result.stderr


def test_no_python3_at_all_is_refused(sandbox: Path) -> None:
    tools = _tool_bin(sandbox.parent)
    result = _run_helper(sandbox, str(tools), "--print")
    assert result.returncode == 2
    assert "no python3 on PATH" in result.stderr


def test_no_arguments_is_a_usage_error(sandbox: Path) -> None:
    tools = _tool_bin(sandbox.parent)
    _shim(sandbox / ".venv" / "bin", version="3.14")
    result = _run_helper(sandbox, str(tools))
    assert result.returncode == 2
    assert "usage" in result.stderr


def test_the_floor_is_the_major_minor_of_python_version() -> None:
    floor = re.search(r'^readonly MIN_PYTHON="([^"]+)"$', _HELPER.read_text(), re.MULTILINE)
    assert floor is not None
    pinned = (_REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert floor.group(1) == ".".join(pinned.split(".")[:2])


@pytest.mark.parametrize("caller", _CALLERS)
def test_each_caller_resolves_repository_python_through_the_helper(caller: str) -> None:
    text = (_REPO_ROOT / caller).read_text(encoding="utf-8")
    assert "repo_python.sh" in text
    bare = re.compile(r'(^|[\s"(;&|`])python3\s+("?\$\{?\w+\}?/|scripts/|src/|tests/|-m\s)')
    offenders = [line for line in text.splitlines() if bare.search(line)]
    assert offenders == []
    assert not re.search(r'(PYTHON\w*)="python3"', text)
    assert ': "python3"' not in text


def _sandbox_with(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    for name in names:
        shutil.copy2(_REPO_ROOT / "scripts" / name, root / "scripts" / name)
    return root


def test_verify_fixtures_refuses_an_ambient_python3_below_the_floor(tmp_path: Path) -> None:
    root = _sandbox_with(tmp_path, "verify_fixtures.sh", "repo_python.sh")
    ambient = _shim(tmp_path / "ambient")
    tools = _tool_bin(tmp_path)
    result = proc_run.run(
        [_BASH, str(root / "scripts" / "verify_fixtures.sh")],
        cwd=root,
        capture_output=True,
        text=True,
        env={"PATH": f"{ambient.parent}:{tools}", "HOME": str(tmp_path), "SHIM_VERSION": "3.13"},
        check=False,
    )
    assert result.returncode != 0
    assert "shim-ran" not in result.stdout
    assert "requires Python >= 3.14" in result.stderr


def test_refresh_outputs_refuses_before_stamping_with_an_old_ambient_python3(
    tmp_path: Path,
) -> None:
    root = _sandbox_with(
        tmp_path, "refresh_outputs.sh", "repo_python.sh", "outputs_provenance.py", "proc_run.py"
    )
    (root / "infra").mkdir()
    ambient = _shim(tmp_path / "ambient")
    tools = _tool_bin(tmp_path)
    (tools / "mv").symlink_to(shutil.which("mv") or "/bin/mv")
    terraform = tools / "terraform"
    terraform.write_text('#!/bin/sh\nprintf \'{"a": {"value": 1}}\'\n', encoding="utf-8")
    terraform.chmod(0o755)
    result = proc_run.run(
        [_BASH, str(root / "scripts" / "refresh_outputs.sh"), "--profile", "p", "--region", "r"],
        cwd=root,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{ambient.parent}:{tools}",
            "HOME": str(tmp_path),
            "SHIM_VERSION": "3.13",
        },
        check=False,
    )
    assert result.returncode != 0
    assert "requires Python >= 3.14" in result.stderr
    assert not (root / "results" / "tf-outputs.json").exists()


def test_the_helper_is_executable_in_the_tree() -> None:
    assert os.access(_HELPER, os.X_OK)

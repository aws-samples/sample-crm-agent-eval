# SPDX-License-Identifier: MIT
"""`agent-source.zip`, the AgentCore image build context, carries no `src/relay/` tree.

Two subjects. The script's own staging `rsync` command runs against a scratch `src/` tree, so a
removed exclusion reds without a rebuild. The real built archive is read when one exists; it skips
when nothing has been built, unless `CRM_REQUIRE_RELAY_PACKAGING=1`, which turns either skip into a
failure.
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Final, NoReturn

import pytest
from scripts import proc_run

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO_ROOT / "scripts" / "package_lambdas.sh"
BUILT_ARCHIVE: Final = REPO_ROOT / "infra" / "build" / "lambda" / "agent-source.zip"
REQUIRE_ENV: Final = "CRM_REQUIRE_RELAY_PACKAGING"

#: The staging command: from the `rsync` after `agent_source_dir` is created up to its `src/` target.
_STAGING_RSYNC: Final = re.compile(
    r'mkdir -p "\$\{agent_source_dir\}"\n(rsync -a [^\n]*(?:\\\n[^\n]*)*"\$\{agent_source_dir\}/src/")'
)


def _skip_or_fail(message: str) -> NoReturn:
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"{message} ({REQUIRE_ENV}=1 forbids skipping)")
    pytest.skip(message)


def _staging_command() -> str:
    match = _STAGING_RSYNC.search(SCRIPT.read_text(encoding="utf-8"))
    assert match is not None, (
        "the agent-source staging rsync is gone or reshaped; re-point this test"
    )
    return match.group(1)


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_staging_rsync_drops_src_relay_and_keeps_the_python_packages(tmp_path: Path) -> None:
    """Only the top-level `relay/` is dropped; a nested directory named `relay` is still staged."""
    rsync = shutil.which("rsync")
    if rsync is None:
        _skip_or_fail("rsync is not on PATH, so the staging command cannot run")
    src = tmp_path / "src"
    _write(src / "relay" / "package.json", "{}")
    _write(src / "relay" / "node_modules" / "dep" / "index.js")
    _write(src / "relay" / "src" / "handler.ts")
    _write(src / "agents" / "supervisor.py")
    _write(src / "crm" / "relay" / "nested.py")
    _write(src / "crm" / "__pycache__" / "cached.pyc")
    stage = tmp_path / "stage"
    stage.mkdir()

    result = proc_run.run(
        ["bash", "-euo", "pipefail", "-c", _staging_command()],
        env={
            "SRC_DIR": str(src),
            "agent_source_dir": str(stage),
            "PATH": f"{Path(rsync).parent}:/bin:/usr/bin",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    staged = {p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()}
    assert staged == {"src/agents/supervisor.py", "src/crm/relay/nested.py"}, sorted(staged)


def test_the_built_agent_source_zip_has_no_relay_entries() -> None:
    """The last build's archive holds the Python packages and nothing under `src/relay/`."""
    if not BUILT_ARCHIVE.is_file():
        _skip_or_fail(f"no built {BUILT_ARCHIVE.name}; run scripts/package_lambdas.sh")
    with zipfile.ZipFile(BUILT_ARCHIVE) as archive:
        members = archive.namelist()
    relay = [m for m in members if m == "src/relay/" or m.startswith("src/relay/")]
    assert relay == [], relay[:5]
    assert "src/agents/supervisor.py" in members
    assert "Dockerfile" in members

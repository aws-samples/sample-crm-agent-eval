# SPDX-License-Identifier: MIT
"""Non-vacuity tests for `check_handler_validator_coverage`'s handler-path probe.

Deleting the `validate_arguments` call in `crm.handlers._common.run_handler` (in a copy of `src/`),
or rebinding it to a no-op, must be reported; the unmutated source reports nothing.
"""

from __future__ import annotations

import shutil
import sys
import textwrap
from pathlib import Path

from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_toolspec_parity.py"
COMMON_RELATIVE = Path("crm") / "handlers" / "_common.py"
ENFORCEMENT_CALL = "validate_arguments(tool, arguments)"

_RUNNER = """
import importlib.util, sys
sys.path.insert(0, {src!r})
import crm.handlers._common  # noqa: F401  - bind the source tree under test first
print(crm.handlers._common.__file__)
{mutation}
spec = importlib.util.spec_from_file_location("check_toolspec_parity", {script!r})
gate = importlib.util.module_from_spec(spec)
sys.modules["check_toolspec_parity"] = gate
spec.loader.exec_module(gate)
violations = gate.check_handler_validator_coverage()
print(len(violations))
for v in violations:
    print(v)
"""


#: Names excluded from the `src/` copy; the probe imports only first-party Python packages.
_EXCLUDED_FROM_COPY = (
    "node_modules",  # ~71 MB of relay npm dependencies; no Python reads them
    "build",  # compiled relay TypeScript, written and removed by the relay packaging tests
    "__pycache__",
    "*.egg-info",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
)


def _copy_src(tmp_path: Path) -> Path:
    """Copy `src/` into `tmp_path/src` without `_EXCLUDED_FROM_COPY` and return the copy."""
    src = tmp_path / "src"
    shutil.copytree(REPO_ROOT / "src", src, ignore=shutil.ignore_patterns(*_EXCLUDED_FROM_COPY))
    return src


def _run_gate(src: Path, *, mutation: str = "") -> tuple[int, list[str]]:
    """Run `check_handler_validator_coverage` in a child interpreter against `src`.

    Args:
        src: The source tree to place ahead of the repository's own on `sys.path`.
        mutation: Optional Python executed after `crm.handlers._common` is imported.

    Returns:
        `(violation_count, violation_messages)`.
    """
    program = _RUNNER.format(src=str(src), script=str(SCRIPT_PATH), mutation=mutation)
    result = proc_run.run(
        [sys.executable, "-c", textwrap.dedent(program)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"probe runner failed:\n{result.stdout}\n{result.stderr}"
    bound, *lines = result.stdout.strip().splitlines()
    assert Path(bound) == src / COMMON_RELATIVE, (
        f"the probe imported {bound}, not the tree under test"
    )
    return int(lines[0]), lines[1:]


def test_unmutated_source_reports_no_violations() -> None:
    """Control: without this the mutation tests could pass for an unrelated reason."""
    count, messages = _run_gate(REPO_ROOT / "src")
    assert count == 0, messages


def test_an_unmutated_copy_reports_no_violations(tmp_path: Path) -> None:
    """Control for the copy below: the bounded copy alone reports nothing."""
    count, messages = _run_gate(_copy_src(tmp_path))
    assert count == 0, messages


def test_removing_the_validate_arguments_call_site_fails_the_gate(tmp_path: Path) -> None:
    """Deleting the single enforcement statement from the source must be detected."""
    src = _copy_src(tmp_path)
    common = src / COMMON_RELATIVE
    text = common.read_text(encoding="utf-8")
    assert text.count(ENFORCEMENT_CALL) == 1, "the enforcement call site moved; update this test"
    common.write_text(text.replace(ENFORCEMENT_CALL, "None"), encoding="utf-8")

    count, messages = _run_gate(src)

    assert count > 0, "the gate reported nothing with the enforcement call site removed"
    assert any("is not reached on this handler's path" in m for m in messages), messages


def test_rebinding_the_validator_to_a_no_op_fails_the_gate() -> None:
    """Rebinding `validate_arguments` to a no-op is reported."""
    count, messages = _run_gate(
        REPO_ROOT / "src",
        mutation=(
            "crm.handlers._common.validate_arguments = lambda *a, **k: None"  # noqa: E501
        ),
    )

    assert count > 0, "the gate reported nothing with the validator rebound to a no-op"
    assert any("is not reached on this handler's path" in m for m in messages), messages

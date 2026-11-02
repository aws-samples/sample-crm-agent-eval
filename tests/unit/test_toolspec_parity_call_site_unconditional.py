# SPDX-License-Identifier: MIT
"""Non-vacuity tests for `check_validator_call_site_unconditional`.

Each mutation of the single `validate_arguments` call site (conditional guard, except block, second
site, deletion) must be reported; a control over the unmutated source reports nothing.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any

from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_toolspec_parity.py"
COMMON_RELATIVE = Path("crm") / "handlers" / "_common.py"
ENFORCEMENT_CALL = "        validate_arguments(tool, arguments)\n"

#: A conditional guard around the validator call, indented to replace the unconditional call.
CONDITIONAL_BYPASS = (
    '        if "__debug" not in arguments:\n            validate_arguments(tool, arguments)\n'
)


def _gate_module() -> Any:
    """Import `scripts/check_toolspec_parity.py` as a module without executing it as a script."""
    spec = importlib.util.spec_from_file_location("check_toolspec_parity_undertest", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sources() -> dict[Path, str]:
    """The real `src/` tree as `{path relative to src: text}`, the shape the check consumes."""
    return {
        path.relative_to(SRC_DIR): path.read_text(encoding="utf-8")
        for path in sorted(SRC_DIR.rglob("*.py"))
    }


def _mutate(replacement: str) -> dict[Path, str]:
    """Return the real sources with the enforcement statement replaced by `replacement`."""
    sources = _sources()
    text = sources[COMMON_RELATIVE]
    assert text.count(ENFORCEMENT_CALL) == 1, "the enforcement call site moved; update this test"
    sources[COMMON_RELATIVE] = text.replace(ENFORCEMENT_CALL, replacement)
    return sources


def test_the_unmutated_source_has_one_unconditional_call_site() -> None:
    """Control: without this every mutation test below could pass for an unrelated reason."""
    assert _gate_module().validator_call_site_violations(_sources()) == []


def test_a_conditional_guard_around_the_validator_fails_the_check() -> None:
    """`if "__debug" not in arguments:` around the single call site is reported."""
    violations = _gate_module().validator_call_site_violations(_mutate(CONDITIONAL_BYPASS))

    assert violations, "the check reported nothing with the validator behind a conditional guard"
    assert any("nested inside If" in message for message in violations), violations


def test_a_validator_call_moved_into_an_except_block_fails_the_check() -> None:
    """A validator call reachable only from an except block is reported."""
    moved = (
        "        pass\n"
        "    except ValueError:\n"
        "        validate_arguments(tool, arguments)\n"
        "        raise\n"
    )
    violations = _gate_module().validator_call_site_violations(_mutate(moved))

    assert violations, "the check reported nothing with the validator moved into an except block"
    assert any("reached through Try.handlers" in message for message in violations), violations


def test_a_second_call_site_elsewhere_in_src_fails_the_check() -> None:
    """A second validator call site elsewhere in `src/` is reported."""
    sources = _sources()
    sources[Path("crm") / "handlers" / "_extra_seam.py"] = (
        "def f(tool: object, arguments: object) -> None:\n    validate_arguments(tool, arguments)\n"
    )
    violations = _gate_module().validator_call_site_violations(sources)

    assert violations, "the check reported nothing with a second call site present"
    assert any("found 2" in message for message in violations), violations


def test_deleting_the_only_call_site_fails_the_check() -> None:
    """Zero validator call sites is reported."""
    violations = _gate_module().validator_call_site_violations(_mutate("        pass\n"))

    assert violations, "the check reported nothing with the only call site deleted"
    assert any("found 0" in message for message in violations), violations


_PROBE_RUNNER = """
import importlib.util, sys
sys.path.insert(0, {src!r})
import crm.handlers._common
print(crm.handlers._common.__file__)
spec = importlib.util.spec_from_file_location("check_toolspec_parity", {script!r})
gate = importlib.util.module_from_spec(spec)
sys.modules["check_toolspec_parity"] = gate
spec.loader.exec_module(gate)
print(len(gate.check_handler_validator_coverage()))
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


def _run_probe(src: Path) -> tuple[Path, str]:
    """Run the executed handler probe against `src`; return the bound `_common.py` and the count."""
    program = _PROBE_RUNNER.format(src=str(src), script=str(SCRIPT_PATH))
    result = proc_run.run(
        [sys.executable, "-c", textwrap.dedent(program)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    bound, count = result.stdout.strip().splitlines()
    return Path(bound), count


def test_the_executed_handler_probe_imports_an_unmutated_copy_and_reports_nothing(
    tmp_path: Path,
) -> None:
    """Control: the probe binds the copy's `crm` package, and the unmutated copy reports nothing."""
    src = _copy_src(tmp_path)
    bound, count = _run_probe(src)
    assert bound == src / COMMON_RELATIVE, bound
    assert count == "0"


def test_the_executed_handler_probe_is_blind_to_the_conditional_bypass(tmp_path: Path) -> None:
    """The executed handler probe reports no violation against the `__debug` conditional bypass."""
    src = _copy_src(tmp_path)
    common = src / COMMON_RELATIVE
    text = common.read_text(encoding="utf-8")
    assert text.count(ENFORCEMENT_CALL) == 1, "the enforcement call site moved; update this test"
    common.write_text(text.replace(ENFORCEMENT_CALL, CONDITIONAL_BYPASS), encoding="utf-8")

    bound, count = _run_probe(src)

    assert bound == common, bound
    assert count == "0", (
        "the executed handler probe now reports the conditional bypass; update this test and the "
        f"W-A1 rationale rather than deleting the structural check.\n{count}"
    )

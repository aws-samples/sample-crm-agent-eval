# SPDX-License-Identifier: MIT
"""Tests for `check_toolspec_parity.regenerate_frontend_types`: the frontend half's verdict does
not depend on whether `gen:types` ran before the gate.

The first class drives the function with a scripted generator in place of `npm`, so each verdict
is pinned to one generator behaviour. The second runs the real gate, with the real `npm run
gen:types`, in a scratch copy holding no generated file: the fresh-clone state.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from scripts import proc_run


@dataclass(frozen=True)
class _FakeCompleted[Out: (str, bytes)]:
    """Stand-in for a finished process: the attributes the code under test reads; nothing runs."""

    args: Sequence[str]
    returncode: int
    stdout: Out
    stderr: Out


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_toolspec_parity.py"
FRONTEND_NODE_MODULES = REPO_ROOT / "frontend" / "node_modules"
GENERATED_RELATIVE = Path("frontend") / "src" / "types" / "generated"


def _gate_module() -> Any:
    """Import `scripts/check_toolspec_parity.py` as a module without executing it as a script."""
    spec = importlib.util.spec_from_file_location("check_toolspec_parity_regen", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The gate module with its generated-file path redirected into ``tmp_path``."""
    module = _gate_module()
    monkeypatch.setattr(module, "GENERATED_TS", tmp_path / "generated" / "toolSchemas.ts")
    monkeypatch.setattr(module, "FRONTEND_DIR", tmp_path)
    return module


def _tool_schemas_text(names: list[str], *, suffix: str = "") -> str:
    """Generated-file text whose `ToolName` union names ``names``, in the generator's layout."""
    union = "\n".join(f'  | "{name}"' for name in names)
    return f"// header\nexport type ToolName =\n{union};\n{suffix}"


def _all_names(gate: Any) -> list[str]:
    return sorted(tool.name for tool in gate.ALL_TOOLS)


def _script_generator(
    gate: Any, monkeypatch: pytest.MonkeyPatch, outputs: Iterator[str | None], returncode: int = 0
) -> list[int]:
    """Replace `npm run gen:types` with one writing the next of ``outputs`` per call.

    A `None` output writes nothing. Returns the list the call count is appended to.
    """
    calls: list[int] = []

    def run(*_args: Any, **_kwargs: Any) -> _FakeCompleted[str]:
        calls.append(1)
        content = next(outputs)
        if content is not None:
            gate.GENERATED_TS.parent.mkdir(parents=True, exist_ok=True)
            staged = gate.GENERATED_TS.with_suffix(".tmp")
            staged.write_text(content, encoding="utf-8")
            staged.replace(gate.GENERATED_TS)
        return _FakeCompleted(args=[], returncode=returncode, stdout="", stderr="")

    monkeypatch.setattr(gate, "proc_run", SimpleNamespace(run=run))
    return calls


def _repeat(content: str) -> Iterator[str | None]:
    while True:
        yield content


class TestRegenerationVerdicts:
    def test_absent_file_with_a_stable_generator_passes_on_the_first_run(
        self, gate: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fresh-clone state: no generated file yet must not fail the gate."""
        calls = _script_generator(gate, monkeypatch, _repeat(_tool_schemas_text(_all_names(gate))))

        assert gate.regenerate_frontend_types(skip=False) == []
        assert len(calls) == 2

    def test_absent_file_with_a_non_reproducible_generator_fails(
        self, gate: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        names = _all_names(gate)
        outputs = iter([_tool_schemas_text(names), _tool_schemas_text(names, suffix="// drift\n")])
        _script_generator(gate, monkeypatch, outputs)

        violations = gate.regenerate_frontend_types(skip=False)
        assert len(violations) == 1
        assert "not reproducible" in violations[0]

    def test_present_file_matching_the_regeneration_passes(
        self, gate: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content = _tool_schemas_text(_all_names(gate))
        gate.GENERATED_TS.parent.mkdir(parents=True)
        gate.GENERATED_TS.write_text(content, encoding="utf-8")
        calls = _script_generator(gate, monkeypatch, _repeat(content))

        assert gate.regenerate_frontend_types(skip=False) == []
        assert len(calls) == 1

    def test_present_stale_file_fails_as_diverged(
        self, gate: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        names = _all_names(gate)
        gate.GENERATED_TS.parent.mkdir(parents=True)
        gate.GENERATED_TS.write_text(_tool_schemas_text(names[1:]), encoding="utf-8")
        _script_generator(gate, monkeypatch, _repeat(_tool_schemas_text(names)))

        violations = gate.regenerate_frontend_types(skip=False)
        assert len(violations) == 1
        assert "diverged" in violations[0]

    @pytest.mark.parametrize("pre_existing", [False, True])
    def test_a_tool_name_union_missing_a_tool_fails_even_when_stable(
        self, gate: Any, monkeypatch: pytest.MonkeyPatch, pre_existing: bool
    ) -> None:
        """A generator that reproducibly drops a tool fails against `ALL_TOOLS` itself."""
        names = _all_names(gate)
        content = _tool_schemas_text(names[:-1])
        if pre_existing:
            gate.GENERATED_TS.parent.mkdir(parents=True)
            gate.GENERATED_TS.write_text(content, encoding="utf-8")
        _script_generator(gate, monkeypatch, _repeat(content))

        violations = gate.regenerate_frontend_types(skip=False)
        assert len(violations) == 1
        assert names[-1] in violations[0]
        assert "ToolName" in violations[0]

    def test_a_file_with_no_tool_name_union_fails(
        self, gate: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _script_generator(gate, monkeypatch, _repeat("// header only\n"))

        violations = gate.regenerate_frontend_types(skip=False)
        assert violations == [
            f"{gate.GENERATED_TS} carries no `export type ToolName =` union of string literals"
        ]

    def test_present_file_not_replaced_by_an_exit_zero_generator_fails(
        self, gate: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A generator that exits 0 but leaves a stale file in place must not self-compare."""
        gate.GENERATED_TS.parent.mkdir(parents=True)
        gate.GENERATED_TS.write_text(_tool_schemas_text(_all_names(gate)), encoding="utf-8")
        _script_generator(gate, monkeypatch, _repeat_none())

        violations = gate.regenerate_frontend_types(skip=False)
        assert len(violations) == 1
        assert "did not replace" in violations[0]

    @pytest.mark.parametrize(
        ("outputs", "returncode", "expected"),
        [
            (lambda: _repeat("irrelevant"), 1, "failed to run"),
            (lambda: _repeat_none(), 0, "did not write"),
        ],
    )
    def test_a_generator_that_fails_or_writes_nothing_fails_the_gate(
        self,
        gate: Any,
        monkeypatch: pytest.MonkeyPatch,
        outputs: Callable[[], Iterator[str | None]],
        returncode: int,
        expected: str,
    ) -> None:
        _script_generator(gate, monkeypatch, outputs(), returncode=returncode)

        violations = gate.regenerate_frontend_types(skip=False)
        assert len(violations) == 1
        assert expected in violations[0]


def _repeat_none() -> Iterator[str | None]:
    while True:
        yield None


def _tracked_copy(dest: Path) -> Path:
    """Materialise the repository's tracked files (`git ls-files`, working-tree content) at `dest`.

    Untracked and ignored files are never copied; tracked symlinks stay symlinks.
    """
    listing = proc_run.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"], capture_output=True, check=True
    )
    for raw in listing.stdout.split(b"\0"):
        if not raw:
            continue
        relative = os.fsdecode(raw)
        source = REPO_ROOT / relative
        target = dest / relative
        if not source.is_symlink() and not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(source.readlink())
        else:
            shutil.copy2(source, target)
    return dest


def _scratch_copy(tmp_path: Path) -> Path:
    """Copy the repository's tracked files into ``tmp_path``, dependency trees symlinked."""
    dest = _tracked_copy(tmp_path / "repo_copy")
    (dest / ".venv").symlink_to(REPO_ROOT / ".venv")
    (dest / "frontend" / "node_modules").symlink_to(FRONTEND_NODE_MODULES)
    return dest


def _run_full_gate(root: Path) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        [sys.executable, str(root / "scripts" / "check_toolspec_parity.py")],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(
    not FRONTEND_NODE_MODULES.exists(),
    reason="frontend/node_modules not installed in this environment",
)
class TestFullGateInScratchCopy:
    def test_passes_with_node_modules_installed_and_no_generated_file(self, tmp_path: Path) -> None:
        """The fresh-clone state (no generated types), exercised with the real `npm run gen:types`.

        The second run takes the present-file path over the file the first run wrote.
        """
        root = _scratch_copy(tmp_path)
        shutil.rmtree(root / GENERATED_RELATIVE, ignore_errors=True)

        first = _run_full_gate(root)
        assert first.returncode == 0, first.stdout + first.stderr
        assert (root / GENERATED_RELATIVE / "toolSchemas.ts").is_file()

        second = _run_full_gate(root)
        assert second.returncode == 0, second.stdout + second.stderr

    def test_stale_generated_file_fails(self, tmp_path: Path) -> None:
        root = _scratch_copy(tmp_path)
        generated = root / GENERATED_RELATIVE / "toolSchemas.ts"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text("// stale\n", encoding="utf-8")

        result = _run_full_gate(root)

        assert result.returncode == 1
        assert "diverged" in result.stderr

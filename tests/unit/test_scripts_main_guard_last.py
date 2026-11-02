# SPDX-License-Identifier: MIT
"""Every `scripts/` and tracked `src/` module's `if __name__ == "__main__":` block is last.

Under `python -m`, the guard calls `main()` the moment it executes, so anything defined below it
does not exist yet: a NameError on the deployed run, while every import-based unit test passes.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from scripts import proc_run

REPO = Path(__file__).resolve().parents[2]


def _is_main_guard(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    )


def _statements_after_main_guard(source: str) -> list[int]:
    body = ast.parse(source).body
    guards = [i for i, node in enumerate(body) if _is_main_guard(node)]
    return [node.lineno for node in body[guards[0] + 1 :]] if guards else []


def _tracked_src_modules(repo: Path) -> list[Path]:
    listed = proc_run.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--", "src/*.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted({repo / name for name in listed.split("\0") if name})


def _modules_with_a_main_guard(repo: Path = REPO) -> list[Path]:
    candidates = sorted((repo / "scripts").rglob("*.py")) + _tracked_src_modules(repo)
    return [
        path
        for path in candidates
        if any(_is_main_guard(n) for n in ast.parse(path.read_text()).body)
    ]


def test_the_scan_finds_the_smoke_modules() -> None:
    names = {p.name for p in _modules_with_a_main_guard()}
    assert {"integration_proposal_chain.py", "agents.py", "eval_sfn.py"} <= names


def test_the_scan_finds_the_src_modules() -> None:
    names = {str(p.relative_to(REPO)) for p in _modules_with_a_main_guard()}
    assert {"src/eval/harness.py", "src/agents/event_schema.py"} <= names


def test_a_late_definition_in_a_tracked_src_module_is_detected(tmp_path: Path) -> None:
    module = tmp_path / "src" / "pkg" / "late.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        'def main():\n    return 0\n\nif __name__ == "__main__":\n    main()\n\nX = 1\n'
    )
    proc_run.run(["git", "init", "-q", str(tmp_path)], check=True)
    proc_run.run(["git", "-C", str(tmp_path), "add", "src/pkg/late.py"], check=True)
    assert _modules_with_a_main_guard(tmp_path) == [module]
    assert _statements_after_main_guard(module.read_text()) == [7]


@pytest.mark.parametrize(
    "path", _modules_with_a_main_guard(), ids=lambda p: str(p.relative_to(REPO))
)
def test_nothing_follows_the_main_guard(path: Path) -> None:
    assert _statements_after_main_guard(path.read_text()) == []


def test_a_definition_after_the_guard_is_detected() -> None:
    source = 'def main():\n    return 0\n\nif __name__ == "__main__":\n    main()\n\ndef late():\n    pass\n'
    assert _statements_after_main_guard(source) == [7]

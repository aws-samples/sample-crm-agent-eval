# SPDX-License-Identifier: MIT
"""AST guards on `eval.harness.run_sweep`'s seed/teardown ordering, and on `src/` -> `scripts/`.

* No module under `src/` imports `scripts`, which is not in any Lambda archive.
* `_read_demo_baseline` runs before the first write (`record_run_started`), nothing that can raise
  sits between that write and the `try`, the unit tenants are seeded as the `try` body's first
  statement, and the `finally` computes the delta checks before `teardown_unit_tenants`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
HARNESS = SRC_DIR / "eval" / "harness.py"


def _run_sweep() -> ast.FunctionDef:
    tree = ast.parse(HARNESS.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_sweep":
            return node
    raise AssertionError("eval.harness.run_sweep not found")


def _index_of_call(body: list[ast.stmt], name: str) -> int:
    """Index of the top-level statement in `body` that calls `name`, asserting there is exactly one."""
    found = [
        i
        for i, stmt in enumerate(body)
        for call in ast.walk(stmt)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == name
    ]
    assert len(found) == 1, f"expected exactly one top-level call to {name}, found at {found}"
    return found[0]


def _try_index(body: list[ast.stmt]) -> int:
    found = [i for i, stmt in enumerate(body) if isinstance(stmt, ast.Try)]
    assert len(found) == 1, f"expected exactly one top-level try in run_sweep, found {len(found)}"
    return found[0]


class TestSeedAndTeardownOrdering:
    def test_the_demo_baseline_is_read_before_anything_is_written(self) -> None:
        """`_read_demo_baseline` is called before `record_run_started` and `seed_unit_tenants`."""
        body = _run_sweep().body
        baseline = _index_of_call(body, "_read_demo_baseline")
        assert baseline < _index_of_call(body, "record_run_started")
        assert baseline < _index_of_call(body, "seed_unit_tenants")

    def test_nothing_that_can_raise_sits_between_the_first_write_and_the_try(self) -> None:
        """Only literal local initialisations sit between `record_run_started` and the `try`."""
        body = _run_sweep().body
        between = body[_index_of_call(body, "record_run_started") + 1 : _try_index(body)]
        for stmt in between:
            assert isinstance(stmt, ast.Assign | ast.AnnAssign), (
                f"{ast.unparse(stmt)!r} sits between record_run_started and the try, so its "
                "failure would skip the teardown finally entirely"
            )
            value = stmt.value
            assert value is None or isinstance(
                value, ast.Constant | ast.List | ast.Dict | ast.Tuple
            ), (
                f"{ast.unparse(stmt)!r} evaluates an expression between record_run_started and the "
                "try; only a literal initialisation is safe there"
            )

    def test_the_unit_tenants_are_seeded_as_the_first_statement_of_the_try(self) -> None:
        """A failed seed of a later unit still reaches the `finally` that tears the earlier down."""
        body = _run_sweep().body
        block = body[_try_index(body)]
        assert isinstance(block, ast.Try)
        first = block.body[0]
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
        assert isinstance(first.value.func, ast.Name)
        assert first.value.func.id == "seed_unit_tenants"

    def test_the_try_has_a_finally_that_tears_the_tenant_down(self) -> None:
        body = _run_sweep().body
        block = body[_try_index(body)]
        assert isinstance(block, ast.Try)
        assert block.finalbody, "run_sweep's try has no finally, so teardown is not guaranteed"
        calls = [
            call.func.id
            for stmt in block.finalbody
            for call in ast.walk(stmt)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        ]
        assert "teardown_unit_tenants" in calls

    def test_the_fr93_checks_are_computed_before_teardown_inside_the_finally(self) -> None:
        """Inside the `finally`, `_fr93_delta_checks` is called before `teardown_unit_tenants`."""
        body = _run_sweep().body
        block = body[_try_index(body)]
        assert isinstance(block, ast.Try)
        order = [
            call.func.id
            for stmt in block.finalbody
            for call in ast.walk(stmt)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        ]
        assert "_fr93_delta_checks" in order and "teardown_unit_tenants" in order
        assert order.index("_fr93_delta_checks") < order.index("teardown_unit_tenants")


class TestSrcNeverImportsScripts:
    """No module under `src/` imports from `scripts/`, which is in no Lambda archive."""

    @pytest.mark.parametrize(
        "path",
        sorted(SRC_DIR.rglob("*.py")),
        ids=lambda p: str(p.relative_to(SRC_DIR)),
    )
    def test_no_module_under_src_imports_scripts(self, path: Path) -> None:
        offenders: list[str] = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                offenders += [a.name for a in node.names if a.name.split(".")[0] == "scripts"]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                module = node.module or ""
                if module.split(".")[0] == "scripts":
                    offenders.append(module)
        assert offenders == [], (
            f"{path.relative_to(REPO_ROOT)} imports {offenders} — `scripts/` is not in any Lambda "
            "archive, so this resolves locally and raises ModuleNotFoundError once deployed"
        )

    def test_the_guard_would_catch_the_defect_it_was_written_for(self) -> None:
        """The detection catches both a function-local and a module-scope `scripts` import."""
        planted = ast.parse(
            "def f():\n"
            "    from scripts.smoke.harness import CheckResult\n"
            "    return CheckResult\n"
            "import scripts.smoke.demo_tenant_guard\n"
        )
        offenders: list[str] = []
        for node in ast.walk(planted):
            if isinstance(node, ast.Import):
                offenders += [a.name for a in node.names if a.name.split(".")[0] == "scripts"]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                module = node.module or ""
                if module.split(".")[0] == "scripts":
                    offenders.append(module)
        assert sorted(offenders) == [
            "scripts.smoke.demo_tenant_guard",
            "scripts.smoke.harness",
        ]

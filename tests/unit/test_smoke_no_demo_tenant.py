# SPDX-License-Identifier: MIT
"""Cross-module guard: no `scripts/smoke/*.py` module hardcodes the `demo` tenant literal.

Walks the AST of every smoke module for a dict literal binding `"tenant": "demo"`.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SMOKE_DIR = Path(__file__).resolve().parents[2] / "scripts" / "smoke"

#: Smoke modules the guard does not scan.
_EXEMPT_MODULES = frozenset({"harness.py", "__init__.py"})


def _find_hardcoded_demo_tenant_assignments(tree: ast.AST) -> list[str]:
    """Return a description of every `"tenant": "demo"` dict-literal binding found in `tree`."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "tenant"
                and isinstance(value, ast.Constant)
                and value.value == "demo"
            ):
                violations.append(f"line {node.lineno}: dict literal binds 'tenant': 'demo'")
    return violations


def _smoke_modules() -> list[Path]:
    return sorted(p for p in _SMOKE_DIR.glob("*.py") if p.name not in _EXEMPT_MODULES)


def test_no_smoke_module_hardcodes_the_demo_tenant_in_a_dict_literal() -> None:
    """Every module under `scripts/smoke/` must be free of a `"tenant": "demo"` literal binding."""
    offenders: dict[str, list[str]] = {}
    for module_path in _smoke_modules():
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        violations = _find_hardcoded_demo_tenant_assignments(tree)
        if violations:
            offenders[module_path.name] = violations

    assert offenders == {}, (
        f"the following scripts/smoke/*.py modules hardcode a 'tenant': 'demo' dict literal, "
        f"violating the prohibition on smoke scripts ever writing the demo tenant: {offenders!r}"
    )


def test_this_guard_is_non_vacuous_against_the_pre_fix_hardcoded_literal() -> None:
    """The walker reports a violation for an `_identity_envelope` body that hardcodes `demo`."""
    pre_fix_source = """
def _identity_envelope(*, full_visibility: bool, book=()) -> dict:
    return {
        "rep_id": "smoke-rep",
        "book": list(book),
        "full_visibility": full_visibility,
        "principal_type": "user",
        "session_id": "smoke-session",
        "source": "gateway",
        "tenant": "demo",
    }
"""
    tree = ast.parse(pre_fix_source)
    violations = _find_hardcoded_demo_tenant_assignments(tree)
    assert violations, (
        "the guard failed to detect the exact pre-fix hardcoded 'tenant': 'demo' shape -- "
        "it would be vacuous against the real defect it exists to catch"
    )


def test_lambda_actions_module_itself_is_currently_clean() -> None:
    """`lambda_actions.py` contains no `"tenant": "demo"` dict literal."""
    module_path = _SMOKE_DIR / "lambda_actions.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    violations = _find_hardcoded_demo_tenant_assignments(tree)
    assert violations == [], f"lambda_actions.py: {violations!r}"

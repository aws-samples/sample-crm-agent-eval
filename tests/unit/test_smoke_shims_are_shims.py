# SPDX-License-Identifier: MIT
"""`scripts/smoke/{harness,demo_tenant_guard}.py` re-export `src/` and never re-define it.

Each re-exported name must be identical (`is`) to its `src/` definition, and every name the tree
imports from a shim must resolve on it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from scripts.smoke import demo_tenant_guard, harness

from eval import tenant_guard, verification

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIMS = {
    "scripts.smoke.harness": (harness, verification),
    "scripts.smoke.demo_tenant_guard": (demo_tenant_guard, tenant_guard),
}

#: Names that must resolve to the `src/` definition itself, per shim module.
RE_EXPORTED = {
    "scripts.smoke.harness": (
        "CheckResult",
        "Provenance",
        "ResourceClassResult",
        "Status",
        "DEFER",
        "UNKNOWN",
        "DEFAULT_PROFILE",
        "DEFAULT_REGION",
        "resolve_profile",
        "resolve_region",
        "git_commit",
        "git_tree_dirty",
        "utc_now_iso",
    ),
    "scripts.smoke.demo_tenant_guard": (
        "DEMO_TENANT",
        "WATCHED_TABLES",
        "TenantRowReader",
        "TenantSnapshot",
        "demo_tenant_untouched_check",
        "read_tenant_snapshot",
        "store_row_reader",
        "writes_only_under_smoke_tenant_check",
    ),
}


def _module_path(dotted: str) -> Path:
    return REPO_ROOT / (dotted.replace(".", "/") + ".py")


def _imported_names(dotted: str) -> set[str]:
    """Every name the tree imports `from <dotted> import ...`, discovered rather than listed."""
    pattern = re.compile(
        r"from " + re.escape(dotted) + r" import\s+(\(([^)]*)\)|([^\n#]+))",
    )
    names: set[str] = set()
    for path in [
        *(REPO_ROOT / "scripts").rglob("*.py"),
        *(REPO_ROOT / "src").rglob("*.py"),
        *(REPO_ROOT / "tests").rglob("*.py"),
    ]:
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            block = match.group(2) or match.group(3) or ""
            for raw in block.split(","):
                name = raw.strip().split(" as ")[0].strip()
                if name and name.isidentifier():
                    names.add(name)
    return names


@pytest.mark.parametrize("dotted", sorted(SHIMS))
def test_every_re_exported_name_is_the_src_definition_itself(dotted: str) -> None:
    """Each re-exported name is the `src/` object itself (`is`), not a wrapper or a copy."""
    shim, source = SHIMS[dotted]
    for name in RE_EXPORTED[dotted]:
        assert hasattr(shim, name), f"{dotted} no longer re-exports {name!r}"
        assert getattr(shim, name) is getattr(source, name), (
            f"{dotted}.{name} is not {source.__name__}.{name} — a second definition has appeared, "
            "which is exactly the drift the single-reader property forbids"
        )


@pytest.mark.parametrize("dotted", sorted(SHIMS))
def test_the_shim_defines_no_class_or_function_of_its_own_for_a_re_exported_name(
    dotted: str,
) -> None:
    tree = ast.parse(_module_path(dotted).read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    collisions = sorted(defined & set(RE_EXPORTED[dotted]))
    assert collisions == [], f"{dotted} defines its own {collisions} instead of re-exporting"


def test_the_demo_tenant_guard_shim_is_nothing_but_re_exports() -> None:
    """The demo-tenant-guard shim contains only imports, `__all__` and expression statements."""
    tree = ast.parse(_module_path("scripts.smoke.demo_tenant_guard").read_text(encoding="utf-8"))
    for node in tree.body:
        assert isinstance(node, ast.Import | ast.ImportFrom | ast.Assign | ast.Expr), (
            f"unexpected statement in a pure re-export module: {ast.unparse(node)!r}"
        )
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            assert targets == ["__all__"], f"unexpected assignment: {ast.unparse(node)!r}"


@pytest.mark.parametrize("dotted", sorted(SHIMS))
def test_every_name_the_tree_imports_from_the_shim_resolves(dotted: str) -> None:
    """Every name the tree imports from the shim is provided by the shim."""
    shim, _ = SHIMS[dotted]
    imported = _imported_names(dotted)
    assert imported, f"no consumer imports from {dotted}; the discovery pattern has drifted"
    missing = sorted(name for name in imported if not hasattr(shim, name))
    assert missing == [], f"{dotted} does not provide {missing}, which the tree imports from it"

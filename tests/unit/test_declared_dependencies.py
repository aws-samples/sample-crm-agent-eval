# SPDX-License-Identifier: MIT
"""Every third-party package first-party `src/` imports must be a declared direct dependency.

An imported package present only as a transitive pin in `requirements.txt`, and absent from
`pyproject.toml`'s `dependencies`, fails this test.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
#: Every first-party tree whose imports need an installed package: product, gate and test code.
FIRST_PARTY_ROOTS = (SRC_ROOT, REPO_ROOT / "scripts", REPO_ROOT / "tests")
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: Distribution name per imported top-level module, where the two differ. Only packages this
#: project actually imports need an entry.
_MODULE_TO_DISTRIBUTION = {
    "yaml": "pyyaml",
    "jwt": "pyjwt",
    "strands": "strands-agents",
    "strands_tools": "strands-agents-tools",
    "dateutil": "python-dateutil",
}

#: First-party top-level packages, which are never third-party dependencies. `scripts` is this
#: repository's own script directory, imported by `src/eval/harness.py`.
_FIRST_PARTY = frozenset(
    {"crm", "agents", "gateway", "providers", "eval", "fixtures", "scripts", "tests"}
)


def _declared_distributions() -> frozenset[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dependencies = data["project"]["dependencies"]
    names = set()
    for entry in dependencies:
        name = entry.split("==")[0].split("[")[0].strip()
        names.add(name.lower().replace("_", "-"))
    return frozenset(names)


def _imported_top_level_modules(*roots: Path) -> dict[str, set[str]]:
    """Map each top-level module imported under ``roots`` (default ``src/``) to its importers."""
    imports: dict[str, set[str]] = {}
    for path in sorted(p for root in roots or (SRC_ROOT,) for p in root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = str(path.relative_to(REPO_ROOT))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.setdefault(alias.name.split(".")[0], set()).add(relative)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.setdefault(node.module.split(".")[0], set()).add(relative)
    return imports


def _third_party_imports() -> dict[str, set[str]]:
    return {
        module: sites
        for module, sites in _imported_top_level_modules().items()
        if module not in _FIRST_PARTY and module not in sys.stdlib_module_names
    }


def test_every_third_party_import_under_src_is_a_declared_direct_dependency() -> None:
    declared = _declared_distributions()
    undeclared = {
        module: sorted(sites)
        for module, sites in sorted(_third_party_imports().items())
        if _MODULE_TO_DISTRIBUTION.get(module, module).lower().replace("_", "-") not in declared
    }
    assert undeclared == {}, (
        "first-party source imports package(s) absent from pyproject.toml's dependencies; "
        f"declare each with an exact pin: {undeclared}"
    )


def test_httpx_is_not_a_direct_dependency_while_no_first_party_module_imports_it() -> None:
    """``httpx`` is neither imported first-party nor declared, while ``httpx2`` is imported."""
    imports = _imported_top_level_modules(*FIRST_PARTY_ROOTS)
    assert "httpx2" in imports, "the scan must see the httpx family to be meaningful"
    assert sorted(imports.get("httpx", set())) == []
    assert "httpx" not in _declared_distributions()


def test_the_scan_is_non_vacuous() -> None:
    """The scan finds third-party imports, including ``boto3``."""
    third_party = _third_party_imports()
    assert len(third_party) >= 3, third_party
    assert "boto3" in third_party


@pytest.mark.parametrize("entry", sorted(_declared_distributions()))
def test_every_declared_dependency_name_is_normalised(entry: str) -> None:
    """Every declared dependency name is lower-case with no underscore."""
    assert entry == entry.lower()
    assert "_" not in entry

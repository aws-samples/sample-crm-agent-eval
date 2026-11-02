#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Assert every first-party import resolves inside the **tracked** tree, not just on this laptop.

Materialises tracked content via ``scripts/verify_fresh_clone.sh --materialise-only`` (``git archive``,
which cannot see an untracked file) and resolves every first-party ``import`` in the materialised
``scripts/``, ``src/`` and ``tests/`` trees against that tree alone, using only the standard library.

Exit codes: 0 all first-party imports resolve; 1 at least one does not; 2 usage/environment error
(materialisation failed, ``git`` unavailable); 3 VERIFIED NOTHING (no Python file was examined;
rendered ``[SKIP]`` by ``run_gates.py``).

Boundaries:

* **Python imports only.** Untracked TypeScript modules are not covered.
* **An untracked test file is not detected**, since nothing imports a test module.
* **It reports on a ref, by default ``HEAD``**, not the working tree. If both an importer and its
  target are uncommitted, nothing is reported.

Usage: python3 scripts/check_tracked_tree_self_contained.py [--ref REF] [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import shutil
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import proc_run  # noqa: E402

FRESH_CLONE_SCRIPT: Final[Path] = REPO_ROOT / "scripts" / "verify_fresh_clone.sh"

EXIT_VERIFIED_NOTHING: Final[int] = 3

#: Directories in the materialised tree whose Python files are walked.
WALKED_TREES: Final[tuple[str, ...]] = ("scripts", "src", "tests")

#: Import root for `scripts/`, which has no `__init__.py`; the `src/` roots are derived from the tree.
_SCRIPTS_ROOT: Final[str] = "scripts"


class SelfContainmentError(RuntimeError):
    """Materialisation or environment failure -- never a finding about the tree's contents."""


def first_party_roots(tree: Path) -> dict[str, Path]:
    """Map each first-party import root to the directory it resolves against, derived from `tree`.

    `src/<pkg>` for every package directory under `src/`, plus `scripts`.
    """
    roots: dict[str, Path] = {}
    src = tree / "src"
    if src.is_dir():
        for entry in sorted(src.iterdir()):
            if entry.is_dir() and not entry.name.startswith((".", "_")):
                roots[entry.name] = src
    if (tree / _SCRIPTS_ROOT).is_dir():
        roots[_SCRIPTS_ROOT] = tree
    return roots


def _module_exists(dotted: str, roots: dict[str, Path]) -> bool:
    """Whether `dotted` resolves to a module or package **within the materialised tree**."""
    head, _, _ = dotted.partition(".")
    base = roots.get(head)
    if base is None:
        return True  # third-party or stdlib: not this gate's subject.
    relative = Path(*dotted.split("."))
    if (base / relative).is_dir() or (base / relative).with_suffix(".py").is_file():
        return True
    # `from crm.errors import X` where `X` is a name, not a submodule: the parent must resolve.
    parent = relative.parent
    return parent != Path() and (
        (base / parent).is_dir() or (base / parent).with_suffix(".py").is_file()
    )


def imported_modules(source: str) -> list[str]:
    """Every dotted module name `source` imports, absolute imports only.

    Relative imports are skipped.
    """
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def materialise(ref: str) -> Path:
    """Materialise tracked content for `ref` and return the scratch path. Caller removes it."""
    if not FRESH_CLONE_SCRIPT.is_file():
        raise SelfContainmentError(f"{FRESH_CLONE_SCRIPT} not found; cannot materialise a tree")
    if shutil.which("bash") is None:
        raise SelfContainmentError("bash unavailable; cannot materialise a tree")
    completed = proc_run.run(
        ["bash", str(FRESH_CLONE_SCRIPT), "--ref", ref, "--materialise-only"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SelfContainmentError(
            f"verify_fresh_clone.sh exited {completed.returncode}: {completed.stderr.strip()[-500:]}"
        )
    # The script prints progress to stderr and the scratch path, alone, on the last stdout line.
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise SelfContainmentError("verify_fresh_clone.sh printed no scratch path")
    scratch = Path(lines[-1].strip())
    if not scratch.is_dir():
        raise SelfContainmentError(f"materialised path is not a directory: {scratch}")
    return scratch


def unresolved_imports(tree: Path) -> tuple[list[str], int]:
    """Return `(findings, files_examined)` for first-party imports that do not resolve in `tree`."""
    roots = first_party_roots(tree)
    findings: list[str] = []
    examined = 0
    for walked in WALKED_TREES:
        base = tree / walked
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                findings.append(f"{path.relative_to(tree)}: unreadable in the tracked tree ({exc})")
                continue
            try:
                modules = imported_modules(source)
            except SyntaxError as exc:
                findings.append(
                    f"{path.relative_to(tree)}: does not parse in the tracked tree ({exc})"
                )
                continue
            examined += 1
            for dotted in modules:
                if not _module_exists(dotted, roots):
                    findings.append(
                        f"{path.relative_to(tree)}: imports {dotted!r}, which does not exist in the "
                        "tracked tree. It resolves on a working tree that carries it untracked, which "
                        "is how three Layer 4/5 smoke modules stayed green while existing on one "
                        "machine only. Commit it, or drop the import."
                    )
    return findings, examined


def main(argv: list[str] | None = None) -> int:
    """Entry point: materialise, resolve, report, and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="Tree-ish to materialise (default: HEAD).")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    try:
        scratch = materialise(args.ref)
    except SelfContainmentError as exc:
        print(f"check_tracked_tree_self_contained: {exc}", file=sys.stderr)
        return 2
    try:
        findings, examined = unresolved_imports(scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if args.json:
        print(
            json.dumps(
                {"ref": args.ref, "files_examined": examined, "findings": findings}, indent=2
            )
        )
    if findings:
        if not args.json:
            print("check_tracked_tree_self_contained: FAIL", file=sys.stderr)
            for finding in findings:
                print(f"  {finding}", file=sys.stderr)
        return 1
    # An empty walk verified nothing.
    if not examined:
        if not args.json:
            print(
                "check_tracked_tree_self_contained: SKIP -- no Python file was examined in the "
                f"materialised tree for ref {args.ref!r}, so this run is not evidence that the "
                "tracked tree is self-contained. Exit 3 rather than 0.",
                file=sys.stderr,
            )
        return EXIT_VERIFIED_NOTHING
    if not args.json:
        print(
            f"check_tracked_tree_self_contained: OK ({examined} tracked Python file(s) examined; "
            "every first-party import resolves inside the tracked tree)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: every child process started from `scripts/` or `tests/` goes through `scripts/proc_run.py`.

Reads every tracked `*.py` under `scripts/` and `tests/` (`git ls-files`) by AST and reports:

- a call of `subprocess.<run|Popen|call|check_call|check_output>`, including through a module
  attribute such as `gate.subprocess.run(...)`;
- one of those launchers bound to a name (`launch = subprocess.run`);
- `getattr(subprocess, ...)`;
- `from subprocess import` of any of those names, or `*`;
- `import subprocess as <alias>`.

`scripts/proc_run.py` is the only exempt file. Other `subprocess` names, such as
`CompletedProcess`, `PIPE`, `DEVNULL` and `TimeoutExpired`, are allowed.

Usage: `python3 scripts/check_subprocess_funnel.py [--repo-root DIR]`

Exit codes: 0 pass; 1 a forbidden form was found, or a file in scope does not parse; 2 usage or
environment error (not a git work tree, git unavailable); 3 SKIP: no tracked `*.py` in scope, so
nothing was verified. `scripts/run_gates.py` renders 3 as `[SKIP]`.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import proc_run  # noqa: E402

#: Exit status for "ran, verified nothing". Must equal `scripts/run_gates.py`'s
#: `GATE_SKIP_EXIT_STATUS`.
SKIP_EXIT_STATUS = 3

#: Repository-relative directories whose tracked `*.py` files are in scope.
SCOPE_DIRS: tuple[str, ...] = ("scripts", "tests")

#: The one file allowed to call `subprocess` directly.
EXEMPT: frozenset[str] = frozenset({"scripts/proc_run.py"})

#: `subprocess` functions that start a child process.
LAUNCHERS: frozenset[str] = frozenset({"run", "Popen", "call", "check_call", "check_output"})


class FunnelError(Exception):
    """The scope could not be listed."""


def _is_subprocess_module(node: ast.expr) -> bool:
    """Whether `node` names the `subprocess` module: `subprocess` or `<anything>.subprocess`."""
    if isinstance(node, ast.Name):
        return node.id == "subprocess"
    return isinstance(node, ast.Attribute) and node.attr == "subprocess"


def find_violations(source: str, path: str) -> list[str]:
    """Every forbidden subprocess form in `source`, as `path:line: message` strings.

    Raises `SyntaxError` when `source` does not parse.
    """
    violations: list[str] = []
    tree = ast.parse(source, filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in LAUNCHERS
                and _is_subprocess_module(func.value)
            ):
                violations.append(
                    f"{path}:{node.lineno}: `{ast.unparse(func)}(...)` starts a process outside "
                    "the funnel; call `proc_run.run` / `proc_run.popen` instead"
                )
            elif (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and node.args
                and _is_subprocess_module(node.args[0])
            ):
                violations.append(
                    f"{path}:{node.lineno}: `{ast.unparse(node)}` looks up a subprocess launcher "
                    "outside the funnel; call `proc_run.run` / `proc_run.popen` instead"
                )
        elif (
            isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
            and isinstance(node.value, ast.Attribute)
            and node.value.attr in LAUNCHERS
            and _is_subprocess_module(node.value.value)
        ):
            violations.append(
                f"{path}:{node.lineno}: `{ast.unparse(node.value)}` is bound to a name outside the "
                "funnel; call `proc_run.run` / `proc_run.popen` instead"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            names = sorted(a.name for a in node.names if a.name in LAUNCHERS or a.name == "*")
            if names:
                violations.append(
                    f"{path}:{node.lineno}: `from subprocess import {', '.join(names)}` imports a "
                    "launcher; call `proc_run.run` / `proc_run.popen` instead"
                )
        elif isinstance(node, ast.Import):
            violations.extend(
                f"{path}:{node.lineno}: `import subprocess as {alias.asname}` hides launches "
                "from this gate; import it under its own name"
                for alias in node.names
                if alias.name == "subprocess" and alias.asname is not None
            )
    return violations


def tracked_scope(repo_root: Path) -> list[str]:
    """The tracked `*.py` paths under :data:`SCOPE_DIRS`, repository-relative, in git's order.

    Raises :class:`FunnelError` when `repo_root` is not a git work tree or git cannot run.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_INDEX_FILE")}
    try:
        listed = proc_run.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", "--", *SCOPE_DIRS],
            capture_output=True,
            check=False,
            env=env,
        )
    except OSError as exc:
        raise FunnelError(f"could not run git: {exc}") from exc
    if listed.returncode != 0:
        stderr = listed.stderr.decode("utf-8", errors="replace").strip()
        raise FunnelError(f"git ls-files exited {listed.returncode}: {stderr[:400]}")
    entries = (os.fsdecode(raw) for raw in listed.stdout.split(b"\0") if raw)
    return [entry for entry in entries if entry.endswith(".py")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="The git work tree to check (default: this repository).",
    )
    args = parser.parse_args(argv)
    repo_root: Path = args.repo_root

    try:
        scope = tracked_scope(repo_root)
    except FunnelError as exc:
        print(f"check_subprocess_funnel: ERROR — {exc}", file=sys.stderr)
        return 2
    if not scope:
        print(
            f"[SKIP] check_subprocess_funnel: no tracked *.py under {', '.join(SCOPE_DIRS)} in "
            f"{repo_root}. Nothing was verified."
        )
        return SKIP_EXIT_STATUS

    violations: list[str] = []
    checked = 0
    for relative in scope:
        if relative in EXEMPT:
            continue
        path = repo_root / relative
        try:
            violations += find_violations(path.read_text(encoding="utf-8"), relative)
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            violations.append(f"{relative}: cannot be read as Python ({exc}), so it is unverified")
        checked += 1

    if violations:
        print("check_subprocess_funnel: VIOLATION(S) FOUND:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        print("check_subprocess_funnel: FAILED", file=sys.stderr)
        return 1
    print(
        f"check_subprocess_funnel: OK — {checked} tracked file(s) under {', '.join(SCOPE_DIRS)} "
        f"start no process outside {', '.join(sorted(EXEMPT))}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

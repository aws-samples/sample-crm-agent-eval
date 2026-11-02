# SPDX-License-Identifier: MIT
"""No writer records a notes audit with a before-snapshot, so a notes revert is delete-only.

`scripts/check_handler_grant_coverage.py` declares revert_audit_record's PutItem on notes
unreachable on that basis. Every `build_audit_record(...)` call under `src/` is classified by its
`target_table` argument:

* a string literal or module-level string constant: it may be `"notes"` only with a literal `{}`
  `before_snapshot`;
* `context.target_table` (the proposal paths): the proposal target-table allowlist, `TABLES` in
  `crm.toolspec.definitions`, must exclude notes;
* a function parameter: the call must pass a literal `{}` `before_snapshot` (the create path).

Calls in `def` and `async def` bodies are classified; a call outside every function fails. Any
other form fails, so a new writer has to be classified here before it can ship.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final, TypeGuard

from crm.toolspec.definitions import TABLES as PROPOSAL_TARGET_TABLES

_SRC: Final[Path] = Path(__file__).resolve().parents[2] / "src"
_NOTES: Final[str] = "notes"


def _module_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value.value
    return constants


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_empty_dict(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Dict) and not node.keys


def _is_audit_call(node: ast.AST) -> TypeGuard[ast.Call]:
    if not isinstance(node, ast.Call):
        return False
    callee = node.func
    name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", "")
    return name == "build_audit_record"


def _calls_with_scope(tree: ast.Module) -> list[tuple[ast.Call, set[str] | None]]:
    """Each `build_audit_record` call with its innermost enclosing function's parameters.

    The parameter set is ``None`` for a call outside every function (module or class scope).
    """
    found: list[tuple[ast.Call, set[str] | None]] = []

    def visit(node: ast.AST, params: set[str] | None) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            params = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
        if _is_audit_call(node):
            found.append((node, params))
        for child in ast.iter_child_nodes(node):
            visit(child, params)

    visit(tree, None)
    return found


def _violations(where_prefix: str, source: str) -> list[str]:
    tree = ast.parse(source)
    constants = _module_constants(tree)
    unclassified: list[str] = []
    for call, params in _calls_with_scope(tree):
        where = f"{where_prefix}:{call.lineno}"
        if params is None:
            unclassified.append(f"{where}: a build_audit_record call outside any function")
            continue
        target = _keyword(call, "target_table")
        before = _keyword(call, "before_snapshot")
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            table: str | None = target.value
        elif isinstance(target, ast.Name) and target.id in constants:
            table = constants[target.id]
        elif isinstance(target, ast.Attribute) and ast.unparse(target) == "context.target_table":
            continue
        elif isinstance(target, ast.Name) and target.id in params:
            if not _is_empty_dict(before):
                unclassified.append(
                    f"{where}: parameter target_table with a non-empty before_snapshot"
                )
            continue
        else:
            unclassified.append(
                f"{where}: unclassified target_table {ast.unparse(target) if target else None}"
            )
            continue
        if table == _NOTES and not _is_empty_dict(before):
            unclassified.append(f"{where}: a notes audit with a before_snapshot")
    return unclassified


def _src_modules() -> list[tuple[Path, str]]:
    return [(path, path.read_text(encoding="utf-8")) for path in sorted(_SRC.rglob("*.py"))]


def test_the_scan_sees_the_known_writers() -> None:
    files = {path.name for path, source in _src_modules() if _calls_with_scope(ast.parse(source))}
    assert {"_common.py", "approve_proposal.py", "rebase_proposal.py", "log_activity.py"} <= files


def test_no_writer_records_a_notes_audit_with_a_before_snapshot() -> None:
    assert _NOTES not in PROPOSAL_TARGET_TABLES
    unclassified = [
        violation
        for path, source in _src_modules()
        for violation in _violations(str(path.relative_to(_SRC)), source)
    ]
    assert not unclassified, unclassified


def test_a_notes_audit_inside_an_async_function_is_flagged() -> None:
    source = (
        "async def write():\n"
        "    build_audit_record(target_table='notes', before_snapshot={'a': 1})\n"
    )
    assert _violations("probe.py", source) == ["probe.py:2: a notes audit with a before_snapshot"]


def test_a_module_scope_audit_call_is_flagged() -> None:
    source = "build_audit_record(target_table='accounts', before_snapshot={})\n"
    assert _violations("probe.py", source) == [
        "probe.py:1: a build_audit_record call outside any function"
    ]

# SPDX-License-Identifier: MIT
"""Static read-only gate over the residue scan: every AWS operation is a read-only literal.

Walks `scripts/residue/*.py` and `scripts/scan_residue.py` (files not present are skipped). Every
operation passed to `call`/`paginate`/`get_paginator` must be a string literal matching
`READ_ONLY_OPERATION`, and no other attribute call or `getattr` is made on a client. The bodies of
`core.call` and `core.paginate` are the only exempt sites.
"""

from __future__ import annotations

import ast
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from scripts.residue.core import READ_ONLY_OPERATION

ROOT = Path(__file__).resolve().parents[2]
RESIDUE_DIR = ROOT / "scripts" / "residue"
CLI = ROOT / "scripts" / "scan_residue.py"
EXEMPT_FUNCTIONS = frozenset({"call", "paginate"})
SINKS = frozenset({"call", "paginate"})


def scanned_files(residue_dir: Path = RESIDUE_DIR, cli: Path = CLI) -> list[Path]:
    """The gate's subject: every residue module plus the CLI, when present."""
    files = sorted(residue_dir.glob("*.py"))
    if cli.exists():
        files.append(cli)
    return files


@dataclass
class Report:
    """Violations found plus the count of literal sink calls seen (the gate's positive signal)."""

    violations: list[str] = field(default_factory=list)
    sink_calls: int = 0


def _callee_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_sink(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id in SINKS
    return (
        isinstance(func, ast.Attribute)
        and func.attr in SINKS
        and isinstance(func.value, ast.Name)
        and func.value.id == "core"
    )


def _is_client_name(name: str) -> bool:
    return name.endswith("client")


def _is_client_source(value: ast.expr) -> bool:
    if not isinstance(value, ast.Call):
        return False
    callee = _callee_name(value.func)
    return callee.endswith("factory") or (
        isinstance(value.func, ast.Attribute) and value.func.attr == "client"
    )


def _is_paginator_source(value: ast.expr) -> bool:
    return isinstance(value, ast.Call) and _callee_name(value.func) == "get_paginator"


def _target_names(targets: list[ast.expr]) -> list[str]:
    return [t.id for t in targets if isinstance(t, ast.Name)]


class _Checker(ast.NodeVisitor):
    def __init__(self, path: Path, report: Report) -> None:
        self.path = path
        self.report = report
        self.is_core = path.name == "core.py" and path.parent.name == "residue"
        self.function_stack: list[str] = []
        self.clients: set[str] = set()
        self.paginators: set[str] = set()

    def _flag(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", 0)
        self.report.violations.append(f"{self.path.name}:{line}: {message}")

    def _exempt(self) -> bool:
        return (
            self.is_core
            and bool(self.function_stack)
            and self.function_stack[-1] in EXEMPT_FUNCTIONS
        )

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if _is_client_name(arg.arg):
                self.clients.add(arg.arg)
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def _bind(self, names: list[str], value: ast.expr | None) -> None:
        if value is None:
            return
        for name in names:
            if _is_client_source(value) or _is_client_name(name):
                self.clients.add(name)
            if _is_paginator_source(value):
                self.paginators.add(name)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._bind(_target_names(node.targets), node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._bind(_target_names([node.target]), node.value)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._bind([node.target.id], node.value)
        self.generic_visit(node)

    def _check_operation_literal(
        self, node: ast.Call, operation: ast.expr | None, sink: str
    ) -> None:
        if not (isinstance(operation, ast.Constant) and isinstance(operation.value, str)):
            self._flag(node, f"{sink}: operation is not a string literal")
            return
        if not READ_ONLY_OPERATION.fullmatch(operation.value):
            self._flag(node, f"{sink}: non-read-only operation {operation.value!r}")
            return
        self.report.sink_calls += 1

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if self._exempt():
            self.generic_visit(node)
            return
        if _is_sink(func):
            operation = node.args[2] if len(node.args) > 2 else None
            for keyword in node.keywords:
                if keyword.arg == "operation":
                    operation = keyword.value
            self._check_operation_literal(node, operation, _callee_name(func))
        elif isinstance(func, ast.Name) and func.id == "getattr" and node.args:
            receiver = node.args[0]
            if (
                isinstance(receiver, ast.Name) and receiver.id in self.clients
            ) or _is_client_source(receiver):
                self._flag(node, "getattr on a client")
        elif isinstance(func, ast.Attribute):
            receiver = func.value
            on_client = (
                isinstance(receiver, ast.Name) and receiver.id in self.clients
            ) or _is_client_source(receiver)
            on_paginator = (
                isinstance(receiver, ast.Name) and receiver.id in self.paginators
            ) or _is_paginator_source(receiver)
            if on_client:
                if func.attr == "get_paginator":
                    self._check_operation_literal(
                        node, node.args[0] if node.args else None, "get_paginator"
                    )
                else:
                    self._flag(node, f"client attribute call .{func.attr}()")
            elif on_paginator and func.attr != "paginate":
                self._flag(node, f"paginator attribute call .{func.attr}()")
        self.generic_visit(node)


def check(paths: list[Path]) -> Report:
    """Run the gate over ``paths`` and return its report."""
    report = Report()
    for path in paths:
        _Checker(path, report).visit(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
    return report


# --- the gate ------------------------------------------------------------------------------


def test_gate_subject_includes_core() -> None:
    names = {p.name for p in scanned_files()}
    assert "core.py" in names


def test_residue_scan_is_read_only() -> None:
    report = check(scanned_files())
    assert report.violations == []


# --- controls (scratch copies only) --------------------------------------------------------


def _scratch_tree(tmp_path: Path) -> tuple[Path, Path]:
    residue = tmp_path / "residue"
    shutil.copytree(RESIDUE_DIR, residue, ignore=shutil.ignore_patterns("__pycache__"))
    cli = tmp_path / "scan_residue.py"
    if CLI.exists():
        shutil.copy2(CLI, cli)
    return residue, cli


def test_control_unmutated_scratch_copy_passes(tmp_path: Path) -> None:
    residue, cli = _scratch_tree(tmp_path)
    copy_report = check(scanned_files(residue, cli))
    assert copy_report.violations == []
    assert copy_report.sink_calls == check(scanned_files()).sink_calls


VALID_SINKS = """
def ok(factory, config):
    iam = factory("iam", "us-east-1")
    paginator = iam.get_paginator("list_roles")
    pages = paginator.paginate()
    core.call(iam, "iam", "get_role", RoleName="x")
    return paginate(iam, "iam", "list_roles", "Roles"), call(iam, "iam", operation="list_policies")
"""


def test_control_valid_sinks_counted_not_flagged(tmp_path: Path) -> None:
    residue, cli = _scratch_tree(tmp_path)
    (residue / "zz_valid.py").write_text(VALID_SINKS, encoding="utf-8")
    report = check(scanned_files(residue, cli))
    assert report.violations == []
    assert report.sink_calls == check(scanned_files()).sink_calls + 4


MUTATIONS = {
    "delete_literal_via_call": 'def bad(client):\n    return call(client, "s3", "delete_bucket", Bucket="x")\n',
    "delete_literal_via_core_paginate": (
        'def bad(client):\n    return core.paginate(client, "s3", "delete_objects", "Deleted")\n'
    ),
    "keyword_operation": 'def bad(client):\n    return call(client, "s3", operation="put_object")\n',
    "non_literal_operation": 'def bad(client, op):\n    return call(client, "s3", op)\n',
    "direct_client_call": 'def bad(client):\n    return client.delete_bucket(Bucket="x")\n',
    "direct_read_call_bypassing_helper": "def bad(client):\n    return client.list_buckets()\n",
    "factory_bound_client": 'def bad(factory, config):\n    s3 = factory("s3", "r")\n    s3.delete_bucket(Bucket="x")\n',
    "chained_factory_call": 'def bad(factory, config):\n    factory("s3", "r").delete_bucket(Bucket="x")\n',
    "session_client": 'def bad(session):\n    kms = session.client("kms")\n    kms.schedule_key_deletion(KeyId="k")\n',
    "getattr_on_client": 'def bad(client):\n    return getattr(client, "delete_bucket")()\n',
    "mutating_paginator": 'def bad(client):\n    return client.get_paginator("delete_objects")\n',
    "paginator_other_method": 'def bad(client):\n    p = client.get_paginator("list_roles")\n    p.build_full_result()\n',
}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_control_mutated_scratch_copy_reds_the_gate(tmp_path: Path, mutation: str) -> None:
    residue, cli = _scratch_tree(tmp_path)
    target = residue / "core.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n\n" + MUTATIONS[mutation], encoding="utf-8"
    )
    report = check(scanned_files(residue, cli))
    assert len(report.violations) == 1, report.violations


def test_control_exemption_is_bound_to_core_helpers(tmp_path: Path) -> None:
    """The getattr in core.call is exempt only inside core.py's call/paginate."""
    residue, cli = _scratch_tree(tmp_path)
    (residue / "zz_copy.py").write_text(
        "def call(client, family, operation, **kwargs):\n    return getattr(client, operation)(**kwargs)\n",
        encoding="utf-8",
    )
    report = check(scanned_files(residue, cli))
    assert len(report.violations) == 1
    assert "getattr on a client" in report.violations[0]


def test_control_cli_file_is_in_subject(tmp_path: Path) -> None:
    residue, _ = _scratch_tree(tmp_path)
    cli = tmp_path / "scan_residue_scratch.py"
    cli.write_text(MUTATIONS["delete_literal_via_call"], encoding="utf-8")
    assert len(check(scanned_files(residue, cli)).violations) == 1

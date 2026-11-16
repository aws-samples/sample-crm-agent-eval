# SPDX-License-Identifier: MIT
"""Guard: no smoke detail formats the text of an exception that can be an AWS error.

Inside an `except` handler whose types include `ClientError`, `BotoCoreError` (or any
`botocore.exceptions` class), `Exception` or `BaseException`, the bound exception name must not be
interpolated by an f-string, `str.format`, `%` formatting, `str()` or `repr()`. Passing it to
`aws_error_code`, `aws_error_detail`, `exception_detail` or `error_code`, or reading `type(exc)`,
is allowed. Import aliases, module-level tuples of exception types, and module-local subclasses
are resolved.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SMOKE_DIR: Final[Path] = REPO_ROOT / "scripts" / "smoke"

BROAD_BUILTINS: Final[frozenset[str]] = frozenset({"Exception", "BaseException"})
BOTOCORE_EXCEPTIONS_MODULE: Final[str] = "botocore.exceptions"
SAFE_CALLS: Final[frozenset[str]] = frozenset(
    {"aws_error_code", "aws_error_detail", "exception_detail", "error_code"}
)


@dataclass
class _ModuleNames:
    """Names in one module that denote AWS-capable exception types, and tuple aliases."""

    aws_types: set[str] = field(default_factory=set)
    exceptions_module_aliases: set[str] = field(default_factory=set)
    botocore_aliases: set[str] = field(default_factory=set)
    tuples: dict[str, ast.expr] = field(default_factory=dict)


@dataclass(frozen=True)
class Violation:
    """One handler that formats a broad or AWS exception's text."""

    path: str
    line: int
    name: str
    construct: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.construct} interpolates `{self.name}`"


def _collect_names(tree: ast.Module) -> _ModuleNames:
    names = _ModuleNames()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == BOTOCORE_EXCEPTIONS_MODULE:
            names.aws_types.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module == "botocore":
            for alias in node.names:
                if alias.name == "exceptions":
                    names.exceptions_module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == BOTOCORE_EXCEPTIONS_MODULE and alias.asname:
                    names.exceptions_module_aliases.add(alias.asname)
                elif alias.name.split(".")[0] == "botocore":
                    names.botocore_aliases.add(alias.asname or "botocore")
    for statement in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign):
            target, value = statement.target, statement.value
        if isinstance(target, ast.Name) and isinstance(value, ast.Tuple):
            names.tuples[target.id] = value
    changed = True
    while changed:
        changed = False
        for statement in tree.body:
            if (
                isinstance(statement, ast.ClassDef)
                and statement.name not in names.aws_types
                and any(_is_aws_type(base, names, ()) for base in statement.bases)
            ):
                names.aws_types.add(statement.name)
                changed = True
    return names


def _is_broad_builtin(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id in BROAD_BUILTINS


def _is_aws_type(node: ast.expr, names: _ModuleNames, seen: tuple[str, ...]) -> bool:
    """Whether `node` names a botocore exception (directly, via alias, or a local subclass)."""
    if isinstance(node, ast.Name):
        if node.id in names.aws_types:
            return True
        if node.id in names.tuples and node.id not in seen:
            return _catches_aws_types(names.tuples[node.id], names, (*seen, node.id))
        return False
    if isinstance(node, ast.Attribute):
        owner = node.value
        if isinstance(owner, ast.Name) and owner.id in names.exceptions_module_aliases:
            return True
        return (
            isinstance(owner, ast.Attribute)
            and owner.attr == "exceptions"
            and isinstance(owner.value, ast.Name)
            and owner.value.id in names.botocore_aliases
        )
    return False


def _catches_aws_types(node: ast.expr, names: _ModuleNames, seen: tuple[str, ...] = ()) -> bool:
    if isinstance(node, ast.Tuple):
        return any(_catches_aws_types(element, names, seen) for element in node.elts)
    if _is_broad_builtin(node):
        return True
    if isinstance(node, ast.Attribute) and node.attr in BROAD_BUILTINS:
        return isinstance(node.value, ast.Name) and node.value.id == "builtins"
    return _is_aws_type(node, names, seen)


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _mentions(node: ast.AST, name: str) -> bool:
    """Whether `node` reads `name` outside a safe helper call or a `type(name)` expression."""
    if isinstance(node, ast.Call) and (
        _call_name(node) in SAFE_CALLS
        or (isinstance(node.func, ast.Name) and node.func.id == "type")
    ):
        return False
    if isinstance(node, ast.Name):
        return node.id == name
    return any(_mentions(child, name) for child in ast.iter_child_nodes(node))


def _formatting_violations(handler: ast.ExceptHandler, path: str) -> list[Violation]:
    name = handler.name
    assert name is not None
    found: list[Violation] = []

    def flag(node: ast.AST, construct: str) -> None:
        found.append(Violation(path, getattr(node, "lineno", handler.lineno), name, construct))

    for statement in handler.body:
        for node in ast.walk(statement):
            if isinstance(node, ast.FormattedValue) and _mentions(node.value, name):
                flag(node, "f-string")
            elif isinstance(node, ast.Call):
                callee = _call_name(node)
                arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
                if callee in ("str", "repr") and isinstance(node.func, ast.Name):
                    if any(_mentions(argument, name) for argument in arguments):
                        flag(node, f"{callee}()")
                elif (
                    callee == "format"
                    and isinstance(node.func, ast.Attribute)
                    and any(_mentions(argument, name) for argument in arguments)
                ):
                    flag(node, ".format()")
            elif (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Mod)
                and isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str)
                and _mentions(node.right, name)
            ):
                flag(node, "% formatting")
    return found


def scan_source(source: str, path: str) -> tuple[list[Violation], int]:
    """Return the violations in `source` and the number of AWS-capable named handlers examined."""
    tree = ast.parse(source)
    names = _collect_names(tree)
    violations: list[Violation] = []
    examined = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ExceptHandler)
            and node.name
            and node.type is not None
            and _catches_aws_types(node.type, names)
        ):
            examined += 1
            violations.extend(_formatting_violations(node, path))
    return violations, examined


def _smoke_modules() -> list[Path]:
    return sorted(SMOKE_DIR.glob("*.py"))


def test_no_smoke_module_formats_the_text_of_an_aws_capable_exception() -> None:
    """Every handler that can catch an AWS error reports it through the message-free helpers."""
    modules = _smoke_modules()
    assert (SMOKE_DIR / "harness.py") in modules, f"the scan read no smoke tree at {SMOKE_DIR}"
    violations: list[Violation] = []
    examined = 0
    for module in modules:
        found, count = scan_source(
            module.read_text(encoding="utf-8"), str(module.relative_to(REPO_ROOT))
        )
        violations.extend(found)
        examined += count
    assert examined > 0, "no AWS-capable handler was examined, so the guard verified nothing"
    assert not violations, "\n".join(str(v) for v in violations)


def _violations(source: str) -> list[Violation]:
    return scan_source(source, "<control>")[0]


@pytest.mark.parametrize(
    "source",
    [
        "try:\n    pass\nexcept Exception as exc:\n    detail = f'failed: {exc}'\n",
        "try:\n    pass\nexcept BaseException as exc:\n    detail = str(exc)\n",
        (
            "from botocore.exceptions import ClientError as CE\n"
            "try:\n    pass\nexcept CE as e:\n    detail = f'{e!r}'\n"
        ),
        (
            "import botocore.exceptions as bex\n"
            "try:\n    pass\nexcept bex.ClientError as e:\n    detail = repr(e)\n"
        ),
        (
            "import botocore\n"
            "try:\n    pass\nexcept botocore.exceptions.BotoCoreError as e:\n"
            "    detail = 'failed: {}'.format(e)\n"
        ),
        (
            "from botocore.exceptions import ClientError\n"
            "_AWS = (ClientError, OSError)\n"
            "try:\n    pass\nexcept _AWS as exc:\n    detail = 'failed: %s' % exc\n"
        ),
        (
            "try:\n    pass\nexcept (OSError, Exception) as exc:\n"
            "    raise RuntimeError(f'could not read: {exc}') from exc\n"
        ),
        (
            "from botocore.exceptions import ClientError\n"
            "try:\n    pass\nexcept ClientError as exc:\n"
            "    detail = f\"{exc.response['Error']['Message']}\"\n"
        ),
        (
            "from botocore.exceptions import ClientError\n"
            "class Denied(ClientError):\n    pass\n"
            "try:\n    pass\nexcept Denied as exc:\n    detail = f'{exc}'\n"
        ),
    ],
    ids=[
        "fstring-exception",
        "str-baseexception",
        "repr-conversion-aliased-clienterror",
        "repr-module-alias",
        "format-dotted-botocore",
        "percent-tuple-alias",
        "raise-fstring-mixed-tuple",
        "fstring-message-attribute",
        "local-subclass",
    ],
)
def test_a_formatted_aws_capable_exception_is_flagged(source: str) -> None:
    assert len(_violations(source)) == 1, source


@pytest.mark.parametrize(
    "source",
    [
        "try:\n    pass\nexcept OSError as exc:\n    detail = f'read failed: {exc}'\n",
        "try:\n    pass\nexcept (ValueError, KeyError) as exc:\n    detail = str(exc)\n",
        (
            "class LocalError(Exception):\n    pass\n"
            "try:\n    pass\nexcept LocalError as exc:\n    detail = f'{exc}'\n"
        ),
        (
            "try:\n    pass\nexcept Exception as exc:\n"
            "    detail = f'{aws_error_detail(\"sts:GetCallerIdentity\", exc)}'\n"
        ),
        "try:\n    pass\nexcept Exception as exc:\n    detail = exception_detail(exc)\n",
        "try:\n    pass\nexcept Exception as exc:\n    detail = f'{type(exc).__name__}'\n",
        (
            "try:\n    pass\nexcept Exception as exc:\n"
            "    detail = f'{harness.aws_error_code(exc.__cause__ or exc)}'\n"
        ),
        "try:\n    pass\nexcept Exception:\n    detail = 'failed'\n",
    ],
    ids=[
        "narrow-oserror",
        "narrow-tuple",
        "local-exception-subclass",
        "aws-error-detail",
        "exception-detail",
        "type-name",
        "attribute-helper-on-cause",
        "unbound",
    ],
)
def test_a_narrow_or_helper_reported_exception_is_allowed(source: str) -> None:
    assert _violations(source) == [], source

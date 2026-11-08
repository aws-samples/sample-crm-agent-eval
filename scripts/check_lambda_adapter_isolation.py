#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: no Lambda handler can import a model adapter, so no Lambda zip needs the vendor SDKs.

Usage:
    python3 scripts/check_lambda_adapter_isolation.py [--repo-root DIR]

`providers.anthropic_adapter` and `providers.openai_adapter` import the vendor SDKs at module scope,
and no Lambda zip vendors those SDKs. `scripts/check_lambda_zip_imports.py`'s closure phase accepts
the unvendored imports on the strength of this gate, which has two cells:

1. ``import_time`` -- no deployed Python Lambda handler reaches either adapter through the imports it
   executes. The walk starts from every import in the handler module, function bodies included, then
   follows only imports that execute at import time: module bodies, including nested
   ``if``/``try``/``with``/class bodies, and the ``__init__`` of every package on the way, but not
   function bodies. An ``if TYPE_CHECKING:`` body is never followed. The handler set is derived from
   every ``handler = "..."`` string in the root composition's ``infra/*.tf`` files.
2. ``stub_literal`` -- ``eval.trigger`` cannot take the lazy path to the adapters (``eval.harness``
   -> ``eval.live`` -> ``providers.factory``) in Lambda. Anywhere in the module, function bodies
   included: it takes from ``eval.harness`` only the names in :data:`HARNESS_ALLOWED_NAMES`, and only
   by ``from eval.harness import``; it never binds ``eval.harness`` itself or reaches an
   ``eval.harness`` attribute outside that set; and it references no module in
   :data:`FORBIDDEN_MODULES` by import, dotted name or string literal. Also
   ``submit_sweep`` exists, and no other function or lambda in the module declares a ``mode``,
   ``live`` or ``substrate`` parameter. ``submit_sweep``'s ``mode`` only selects the state machine's
   input.

``tests/stub/test_stub_sweep.py`` asserts the runtime half: neither ``anthropic`` nor ``openai`` is in
``sys.modules`` after a stub sweep.

Bounds: a dynamic import with a computed name, and a harness attribute reached through ``getattr`` or
an object assigned at runtime, are not seen; only the root ``infra/*.tf`` files are read.

Exit codes: 0 pass; 1 violation; 2 usage or environment error.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ADAPTER_MODULES = frozenset({"providers.anthropic_adapter", "providers.openai_adapter"})
FIRST_PARTY_PACKAGES = frozenset({"crm", "gateway", "agents", "providers", "eval", "fixtures"})
COMPOSITION = Path("infra")
TRIGGER_MODULE = Path("src") / "eval" / "trigger.py"
HARNESS_MODULE = "eval.harness"
#: The only `eval.harness` names `eval.trigger` may import; none runs a sweep.
HARNESS_ALLOWED_NAMES = frozenset(
    {
        # Type annotations on `submit_sweep` and the result wire shape.
        "EvalMode",
        "SweepResult",
        # `sweep_shape()`: constants and a JSON load of the golden task file.
        "DEFAULT_ARM_SPECS",
        "_DEFAULT_GOLDEN_TASKS_PATH",
        "_load_golden_tasks",
    }
)
#: Modules on the lazy path to the adapters; `eval.trigger` may not reference them at all.
FORBIDDEN_MODULES = frozenset({"eval.live", "providers.factory", "eval.sweep_units"})
FORBIDDEN_PARAMETERS = frozenset({"mode", "live", "substrate"})
#: Module-level functions allowed a `FORBIDDEN_PARAMETERS` parameter; each must exist.
MODE_PARAMETER_FUNCTIONS = frozenset({"submit_sweep"})
#: Handler modules the composition already names whose source a later task adds: not walked until
#: they exist, and a violation once they exist while still listed.
PENDING_HANDLER_MODULES: frozenset[str] = frozenset()

_HANDLER_PATTERN = re.compile(
    r'handler\s*=\s*"((?:' + "|".join(sorted(FIRST_PARTY_PACKAGES)) + r')\.[^"]+)"'
)


def derived_handlers(repo_root: Path) -> list[str]:
    """Every first-party Python handler string the composition declares, deduplicated and sorted."""
    found: set[str] = set()
    for path in sorted((repo_root / COMPOSITION).glob("*.tf")):
        found.update(_HANDLER_PATTERN.findall(path.read_text(encoding="utf-8")))
    return sorted(found)


def _module_path(src: Path, module: str) -> Path | None:
    base = src / module.replace(".", "/")
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _import_time_nodes(
    body: list[ast.stmt], *, function_bodies: bool = False
) -> list[ast.Import | ast.ImportFrom]:
    """Import statements in `body` that execute when the module is imported.

    With `function_bodies`, imports inside function and lambda bodies are included too: every import
    the module can execute, which is what a handler module's own invocation path needs.
    """
    found: list[ast.Import | ast.ImportFrom] = []
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        if not function_bodies and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            continue
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            pending.extend(node.orelse)
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
            continue
        pending.extend(ast.iter_child_nodes(node))
    return found


def _targets(node: ast.Import | ast.ImportFrom, package: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level:
        parts = package.split(".") if package else []
        base = ".".join(parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts)
        absolute = f"{base}.{node.module}" if node.module else base
    else:
        absolute = node.module or ""
    if not absolute:
        return []
    return [absolute] + [f"{absolute}.{a.name}" for a in node.names if a.name != "*"]


def _with_parent_packages(module: str) -> list[str]:
    parts = module.split(".")
    return [".".join(parts[:depth]) for depth in range(1, len(parts) + 1)]


def import_time_path(
    src: Path, handler_module: str, *, seed_function_bodies: bool = False
) -> list[str] | None:
    """The import-time chain from `handler_module` to an adapter, or ``None`` when there is none.

    Importing a dotted module first executes every package ``__init__`` above it, so those are
    starting points too. With `seed_function_bodies`, `handler_module`'s own function-body imports
    are followed as well, and every other module is still read at module scope only.
    """
    starts = _with_parent_packages(handler_module)
    parent: dict[str, str | None] = dict.fromkeys(starts)
    pending = list(starts)
    while pending:
        module = pending.pop()
        if module in ADAPTER_MODULES:
            chain = [module]
            step = parent[module]
            while step is not None:
                chain.append(step)
                step = parent[step]
            return list(reversed(chain))
        path = _module_path(src, module)
        if path is None or module.split(".")[0] not in FIRST_PARTY_PACKAGES:
            continue
        package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        seeding = seed_function_bodies and module == handler_module
        for node in _import_time_nodes(tree.body, function_bodies=seeding):
            for target in _targets(node, package):
                for step_module in _with_parent_packages(target):
                    if step_module not in parent:
                        parent[step_module] = module
                        pending.append(step_module)
    return None


def check_import_time(repo_root: Path) -> tuple[list[str], int]:
    """Cell ``import_time``: violations, and how many handlers were walked."""
    src = repo_root / "src"
    handlers = derived_handlers(repo_root)
    if not handlers:
        return ([f"no first-party Python handler string found in {COMPOSITION}"], 0)
    violations: list[str] = []
    for handler in handlers:
        module = handler.rsplit(".", 1)[0]
        if module in PENDING_HANDLER_MODULES:
            if _module_path(src, module) is not None:
                violations.append(
                    f"handler module {module!r} now exists but is still in PENDING_HANDLER_MODULES, "
                    "so it is not walked: remove it from that set"
                )
            continue
        if _module_path(src, module) is None:
            violations.append(f"handler {handler!r} names module {module!r}, absent from src/")
            continue
        chain = import_time_path(src, module, seed_function_bodies=True)
        if chain is not None:
            violations.append(
                f"handler {handler!r} reaches an adapter on its executed path: {' -> '.join(chain)}"
            )
    return (violations, len(handlers))


def _within(module: str, roots: frozenset[str] | set[str]) -> str | None:
    """The member of `roots` that `module` is or lies under, if any."""
    return next(
        (root for root in sorted(roots) if module == root or module.startswith(root + ".")), None
    )


def _dotted(node: ast.expr) -> str | None:
    """`a.b.c` for a chain of attributes on a name, else `None`."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def _import_from_module(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    base = package.split(".")[: len(package.split(".")) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])])


def _harness_and_forbidden_references(tree: ast.Module, package: str) -> list[tuple[int, str]]:
    """`(line, reason)` for every reference in `tree` outside the harness allowlist."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = _import_from_module(node, package)
            for alias in node.names:
                target = f"{module}.{alias.name}"
                if (
                    forbidden := _within(module, FORBIDDEN_MODULES)
                    or _within(target, FORBIDDEN_MODULES)
                ) is not None:
                    found.append((node.lineno, f"imports from {forbidden}"))
                elif module == HARNESS_MODULE and alias.name not in HARNESS_ALLOWED_NAMES:
                    found.append((node.lineno, f"imports {alias.name} from {HARNESS_MODULE}"))
                elif target == HARNESS_MODULE:
                    found.append((node.lineno, f"binds the {HARNESS_MODULE} module itself"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (forbidden := _within(alias.name, FORBIDDEN_MODULES)) is not None:
                    found.append((node.lineno, f"imports {forbidden}"))
                elif _within(alias.name, {HARNESS_MODULE}) is not None:
                    found.append((node.lineno, f"binds the {HARNESS_MODULE} module itself"))
        elif isinstance(node, ast.Attribute) and (dotted := _dotted(node)) is not None:
            harness_attribute = dotted.removeprefix(HARNESS_MODULE + ".")
            if _within(dotted, FORBIDDEN_MODULES) is not None or (
                harness_attribute != dotted and harness_attribute not in HARNESS_ALLOWED_NAMES
            ):
                found.append((node.lineno, f"reaches {dotted}"))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (forbidden := _within(node.value, FORBIDDEN_MODULES)) is not None
        ):
            found.append((node.lineno, f"names {forbidden} in a string literal"))
    return sorted(set(found))


def check_stub_literal(repo_root: Path) -> list[str]:
    """Cell ``stub_literal``: ``eval.trigger`` starts state-machine sweeps and runs none itself."""
    path = repo_root / TRIGGER_MODULE
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = [
        f"{TRIGGER_MODULE}:{line} {reason}, outside the harness allowlist"
        for line, reason in _harness_and_forbidden_references(tree, "eval")
    ]
    exempt = {
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in MODE_PARAMETER_FUNCTIONS
    }
    for missing in sorted(MODE_PARAMETER_FUNCTIONS - {node.name for node in exempt}):
        violations.append(f"{TRIGGER_MODULE} defines no {missing}()")
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) or (
            function in exempt
        ):
            continue
        name = "<lambda>" if isinstance(function, ast.Lambda) else function.name
        arguments = function.args
        declared = [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            *([arguments.vararg] if arguments.vararg else []),
            *([arguments.kwarg] if arguments.kwarg else []),
        ]
        for argument in declared:
            if argument.arg in FORBIDDEN_PARAMETERS:
                violations.append(
                    f"{TRIGGER_MODULE}:{function.lineno} {name}() declares a {argument.arg!r} "
                    "parameter, so a caller could select a provider mode"
                )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    repo_root: Path = args.repo_root.resolve()
    try:
        import_time, handler_count = check_import_time(repo_root)
        stub_literal = check_stub_literal(repo_root)
    except (OSError, SyntaxError) as exc:
        print(f"check_lambda_adapter_isolation: cannot read the tree: {exc}", file=sys.stderr)
        return 2

    for cell, violations in (("import_time", import_time), ("stub_literal", stub_literal)):
        print(f"[{'FAIL' if violations else 'pass'}] {cell}")
        for violation in violations:
            print(f"         {violation}")
    print("  note: the runtime cell is tests/stub/test_stub_sweep.py, run by make test-stub")
    if import_time or stub_literal:
        print(f"FAIL: {len(import_time) + len(stub_literal)} violation(s)")
        return 1
    print(
        f"OK: {handler_count} Lambda handler(s) from {COMPOSITION} reach neither adapter on their "
        f"executed path, and {TRIGGER_MODULE} runs no in-process sweep"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

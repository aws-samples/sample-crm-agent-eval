#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""AST-based gate proving every logging call site under `src/` cannot emit a secret or payload.

Parses every `.py` module under `src/` and finds every attribute call named after a
`logging.Logger` method (`info`, `debug`, `warning`, `error`, `critical`, `exception`, `log`).
Each call site is classified as:

  * **redacted** — the logged value is a call to `redact()` (`crm.metering.redact`), or a name
    whose every store in its own scope is redacted and that no nested scope stores to (see
    `_bound_names`).
  * **literal-safe** — the logged value is a dict literal, or `json.dumps(<dict literal>)`, whose
    every key is in `_SAFE_LITERAL_KEYS`.
  * **unresolved** — anything else.

The whole call is classified, not only the message: every extra positional must be a literal
constant, and every keyword must be in `_ALLOWED_LOG_KEYWORDS` and hold a literal constant. For
`logger.log(level, msg)` the message is the second positional. The text and traceback that
`logger.exception(...)` appends are outside the scope of this gate.

The run prints the resolved and unresolved counts and exits 1 if any call site is unresolved.

Usage: `python3 scripts/check_log_redaction.py [--src-root src]`
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: stdlib `logging.Logger` method names treated as a "log call" for this gate.
_LOG_METHODS = frozenset({"info", "debug", "warning", "error", "critical", "exception", "log"})

#: Key names that may appear in a literal dict passed to a log call.
_SAFE_LITERAL_KEYS = frozenset(
    {
        "event",
        "reason",
        "ts",
        "rep_id",
        # Only ever the literal "user" or "service" (`crm.authz.PrincipalType`).
        "principal_type",
        "session_id",
        "correlation_id",
        "turn_id",
        "invocation_id",
    }
)

#: Callee names (bare or attribute) whose return value is treated as already redacted.
_REDACTING_CALLEES = frozenset({"redact"})


#: Keyword arguments a log call may carry, each of which must hold a literal constant.
_ALLOWED_LOG_KEYWORDS = frozenset({"exc_info", "stack_info", "stacklevel"})

#: Method names that may be called on a bound name without counting as a store to it.
_NON_STORING_METHODS = frozenset({"get", "keys", "values", "items", "copy"})

_ScopeNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
_Scope = ast.Module | _ScopeNode


@dataclass(frozen=True, slots=True)
class CallSite:
    """One logging call site found under `src/`."""

    path: Path
    line: int
    classification: str  # "redacted" | "literal-safe" | "unresolved"
    detail: str


def _is_logger_call(node: ast.Call) -> str | None:
    """Return the logging method name if `node` calls a `.<method>()` matching `_LOG_METHODS`."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS:
        return func.attr
    return None


def _expr_is_redacted(node: ast.expr, redact_bound_names: frozenset[str]) -> bool:
    """Return whether `node` is a redaction call or a name listed in `redact_bound_names`.

    Recognises a direct `redact(...)` call, a call to an attribute named `redact`, or a bare name in
    `redact_bound_names`.
    """
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in _REDACTING_CALLEES:
            return True
        if isinstance(func, ast.Attribute) and func.attr in _REDACTING_CALLEES:
            return True
    return isinstance(node, ast.Name) and node.id in redact_bound_names


def _dict_literal_keys(node: ast.expr) -> list[str] | None:
    """Return the literal string keys of `node` if it is a `dict` literal, else `None`.

    Only a plain `{...}` literal with constant string keys is recognised.
    """
    if not isinstance(node, ast.Dict):
        return None
    keys: list[str] = []
    for key in node.keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return None
        keys.append(key.value)
    return keys


def _json_dumps_arg(node: ast.expr) -> ast.expr | None:
    """Return the first positional argument if `node` is a call to `json.dumps(...)`."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    is_json_dumps = (isinstance(func, ast.Attribute) and func.attr == "dumps") or (
        isinstance(func, ast.Name) and func.id == "dumps"
    )
    if not is_json_dumps or not node.args:
        return None
    return node.args[0]


def _classify_call(
    call: ast.Call, redact_bound_names: frozenset[str], method: str = "info"
) -> tuple[str, str]:
    """Classify one logging call — the whole call, not only its first argument.

    A call resolves safe only when the message argument (the second positional for ``log``) is
    redacted or literal-safe, every remaining positional is a literal constant, and every keyword is
    in :data:`_ALLOWED_LOG_KEYWORDS` and holds a literal constant.

    Args:
        call: The logging call node.
        redact_bound_names: Names safe to log in the scope enclosing `call`.
        method: The resolved logger method name, which decides where the message argument sits.

    Returns:
        A ``(classification, detail)`` pair. ``classification`` is one of ``"redacted"``,
        ``"literal-safe"``, or ``"unresolved"``.
    """
    message_index = 1 if method == "log" else 0
    if len(call.args) <= message_index:
        return (
            "unresolved",
            f"{method}() call has no message argument at position {message_index} to inspect",
        )

    for keyword in call.keywords:
        if keyword.arg is None:
            return "unresolved", "log call passes **kwargs, whose contents cannot be resolved"
        if keyword.arg not in _ALLOWED_LOG_KEYWORDS:
            return (
                "unresolved",
                f"log call passes keyword {keyword.arg!r}, which is not in the allowed set "
                f"{sorted(_ALLOWED_LOG_KEYWORDS)} — its contents reach the emitted record",
            )
        if not isinstance(keyword.value, ast.Constant):
            return (
                "unresolved",
                f"log call passes keyword {keyword.arg!r} with a non-literal value",
            )

    extra_positionals = call.args[message_index + 1 :]
    non_literal = [
        argument for argument in extra_positionals if not isinstance(argument, ast.Constant)
    ]
    if non_literal:
        return (
            "unresolved",
            f"log call passes {len(non_literal)} non-literal interpolation argument(s) after the "
            f"message, which the formatter splices into the emitted line",
        )

    logged_expr = call.args[message_index]

    if _expr_is_redacted(logged_expr, redact_bound_names):
        return "redacted", "argument is a call to redact() or a name bound from one"

    dumps_arg = _json_dumps_arg(logged_expr)
    target = dumps_arg if dumps_arg is not None else logged_expr

    if _expr_is_redacted(target, redact_bound_names):
        return "redacted", "json.dumps() argument is a call to redact() or a bound name"

    keys = _dict_literal_keys(target)
    if keys is not None:
        unsafe_keys = [key for key in keys if key not in _SAFE_LITERAL_KEYS]
        if unsafe_keys:
            return (
                "unresolved",
                f"dict literal contains key(s) not in the safe allowlist: {unsafe_keys!r}",
            )
        return "literal-safe", f"dict literal, all keys allowlisted: {keys!r}"

    return (
        "unresolved",
        f"logged expression is neither a redact() call nor a recognised dict literal "
        f"(got {ast.dump(target)[:80]}...)",
    )


def _scope_roots(scope: _Scope) -> list[ast.AST]:
    """Return the top-level nodes evaluated inside `scope` itself, parameters included."""
    if isinstance(scope, ast.Module | ast.ClassDef):
        return list(scope.body)
    arguments = scope.args
    params: list[ast.AST] = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    params.extend(arg for arg in (arguments.vararg, arguments.kwarg) if arg is not None)
    return [*params, *scope.body]


def _enclosing_parts(node: _ScopeNode) -> list[ast.AST]:
    """Return the parts of a nested def or class that are evaluated in the enclosing scope."""
    parts: list[ast.AST] = [*node.decorator_list, *node.type_params]
    if isinstance(node, ast.ClassDef):
        return [*parts, *node.bases, *node.keywords]
    defaults = [default for default in node.args.kw_defaults if default is not None]
    returns = [node.returns] if node.returns is not None else []
    return [*parts, *node.args.defaults, *defaults, *returns]


def _own_nodes(scope: _Scope) -> Iterator[ast.AST]:
    """Yield every node evaluated in `scope`: nested def/class nodes, but not their bodies.

    Lambdas and comprehensions are folded into the enclosing scope.
    """
    stack = _scope_roots(scope)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _ScopeNode):
            stack.extend(_enclosing_parts(node))
        else:
            stack.extend(ast.iter_child_nodes(node))


def _store_root(node: ast.expr) -> str | None:
    """Return the name at the base of a `name[...]` / `name.attr` / `name(...)` / `*name` chain."""
    while isinstance(node, ast.Subscript | ast.Attribute | ast.Starred | ast.Call):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


@dataclass
class _ScopeStores:
    """Every store to every name within one scope, and the names the scope binds locally."""

    assigned: dict[str, list[ast.expr]] = field(default_factory=dict)
    updates: dict[str, list[ast.Call]] = field(default_factory=dict)
    disqualified: set[str] = field(default_factory=set)
    local: set[str] = field(default_factory=set)
    declared: set[str] = field(default_factory=set)

    def escaping(self) -> set[str]:
        """Return the names stored to in this scope that resolve to an enclosing scope."""
        touched = {*self.assigned, *self.updates, *self.disqualified}
        return touched - (self.local - self.declared)


def _collect_stores(scope: _Scope, module_disqualified: frozenset[str]) -> _ScopeStores:
    """Record each store in `scope` as a plain assignment, an `.update()`, or a disqualifier."""
    stores = _ScopeStores(disqualified=set(module_disqualified))
    plain_targets: set[int] = set()

    def disqualify(name: str | None) -> None:
        if name is not None:
            stores.disqualified.add(name)

    def bind(name: str | None) -> None:
        if name is not None:
            stores.local.add(name)
            disqualify(name)

    for node in _own_nodes(scope):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    plain_targets.add(id(target))
                    if node.value is not None:
                        stores.assigned.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.Name):
            if not isinstance(node.ctx, ast.Load):
                stores.local.add(node.id)
                if id(node) not in plain_targets:
                    disqualify(node.id)
        elif isinstance(node, ast.Subscript | ast.Attribute):
            if not isinstance(node.ctx, ast.Load):
                disqualify(_store_root(node.value))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            if isinstance(receiver, ast.Name) and node.func.attr == "update":
                stores.updates.setdefault(receiver.id, []).append(node)
            elif node.func.attr not in _NON_STORING_METHODS:
                disqualify(_store_root(receiver))
        elif isinstance(node, ast.Global | ast.Nonlocal):
            stores.declared.update(node.names)
        elif isinstance(node, ast.arg):
            bind(node.arg)
        elif isinstance(node, ast.alias):
            bind(node.asname or node.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
            bind(node.name)
        elif isinstance(node, ast.MatchMapping):
            bind(node.rest)
        elif isinstance(node, _ScopeNode):
            bind(node.name)
    return stores


def _bound_names(stores: _ScopeStores) -> frozenset[str]:
    """Return the names in one scope that are safe to log by name.

    A name is safe only if it has at least one plain assignment (`=` or annotated `=`) in the
    scope, no other store or binding of it there (for example augmented assignment, a subscript or
    attribute store, unpacking, a parameter, a loop/`with`/`except` target, an import, `del`, or a
    method call other than `update` and `_NON_STORING_METHODS`, including one reached through a
    subscript or call on the name), no such store or `.update()` of it in a nested def or class that
    does not bind it locally, no `global`/`nonlocal` of it anywhere in the module, and either:

    1. every assignment is a `redact(...)` call and there is no `.update()` call; or
    2. every assignment is a `redact(...)` call or a dict literal whose keys are all in
       `_SAFE_LITERAL_KEYS`, every `.update()` call passes exactly one positional argument that
       is a `redact(...)` call or a rule-1 name and no keyword or `**` argument, and there is at
       least one `.update()` call if any assignment is a dict literal.

    Mutation through another reference or inside a callee is not tracked.
    """
    candidates = {
        name: values for name, values in stores.assigned.items() if name not in stores.disqualified
    }
    direct = frozenset(
        name
        for name, values in candidates.items()
        if name not in stores.updates
        and all(_expr_is_redacted(value, frozenset()) for value in values)
    )
    bound = set(direct)
    for name, values in candidates.items():
        if name in direct:
            continue
        literals = [value for value in values if not _expr_is_redacted(value, frozenset())]
        if any(
            (keys := _dict_literal_keys(value)) is None
            or not all(key in _SAFE_LITERAL_KEYS for key in keys)
            for value in literals
        ):
            continue
        updates = stores.updates.get(name, [])
        if literals and not updates:
            continue
        if all(
            len(update.args) == 1
            and not update.keywords
            and _expr_is_redacted(update.args[0], direct)
            for update in updates
        ):
            bound.add(name)
    return frozenset(bound)


def scan_file(path: Path) -> list[CallSite]:
    """Return every logging call site found in `path`, ordered by position."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [
            CallSite(
                path=path,
                line=exc.lineno or 0,
                classification="unresolved",
                detail=f"SyntaxError: {exc}",
            )
        ]

    module_disqualified = frozenset(
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Global | ast.Nonlocal)
        for name in node.names
    )
    scopes: list[_Scope] = [tree]
    scopes.extend(node for node in ast.walk(tree) if isinstance(node, _ScopeNode))

    scope_stores = {id(scope): _collect_stores(scope, module_disqualified) for scope in scopes}
    for scope in scopes:
        stores = scope_stores[id(scope)]
        for nested in ast.walk(scope):
            if nested is not scope and isinstance(nested, _ScopeNode):
                stores.disqualified |= scope_stores[id(nested)].escaping()

    located: list[tuple[int, int, CallSite]] = []
    for scope in scopes:
        bound = _bound_names(scope_stores[id(scope)])
        for node in _own_nodes(scope):
            if not isinstance(node, ast.Call):
                continue
            method = _is_logger_call(node)
            if method is None:
                continue
            classification, detail = _classify_call(node, bound, method)
            site = CallSite(
                path=path, line=node.lineno, classification=classification, detail=detail
            )
            located.append((node.lineno, node.col_offset, site))
    return [site for _, _, site in sorted(located, key=lambda entry: entry[:2])]


def scan_tree(src_root: Path) -> list[CallSite]:
    """Return every logging call site found under every `.py` file in `src_root`."""
    sites: list[CallSite] = []
    for path in sorted(src_root.rglob("*.py")):
        sites.extend(scan_file(path))
    return sites


def main(argv: list[str] | None = None) -> int:
    """Entry point: scan, report, and fail if any call site is unresolved."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src-root",
        default=str(_REPO_ROOT / "src"),
        help="Root directory to scan for logging call sites (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    src_root = Path(args.src_root).resolve()

    sites = scan_tree(src_root)
    total = len(sites)
    unresolved = [site for site in sites if site.classification == "unresolved"]
    resolved = total - len(unresolved)

    print(f"check_log_redaction: {total} logging call site(s) found under {src_root}")
    print(
        f"check_log_redaction: {resolved}/{total} provably cannot emit a token, secret, or full record payload"
    )

    for site in sites:
        rel = (
            site.path.relative_to(_REPO_ROOT) if site.path.is_relative_to(_REPO_ROOT) else site.path
        )
        print(f"  [{site.classification}] {rel}:{site.line} — {site.detail}")

    if unresolved:
        print(
            f"check_log_redaction: FAILED — {len(unresolved)} call site(s) not provably safe",
            file=sys.stderr,
        )
        return 1

    print("check_log_redaction: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

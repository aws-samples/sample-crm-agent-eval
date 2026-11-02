#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Coverage gate: every path that persists or applies a proposal `after_payload` must pass it
through `crm.domain.allowlists.validate_proposal_payload` first.

It reuses `check_handler_grant_coverage`'s AST module index over `src/crm`, `src/gateway` and
`src/eval` and adds a payload-flow pass over it. For every first-party function it derives an
**obligation** to validate from four independent triggers:

1. `TAINTED_PERSIST` — a value derived from an `after_payload` (a parameter of that name, a
   `[<expr folding to "after_payload">]` subscript, or a `.get(<expr folding to "after_payload">)`)
   reaches a persistence call in `PERSIST_SINKS`. The key expression is constant-folded, so
   `.get("after" + "_payload")` is the same root as `.get("after_payload")`.
2. `APPLY_CALL` — a call to `_apply_operation`, the function that merges a payload into the target
   record, whether or not the argument is taint-traceable.
3. `PROPOSAL_ITEM` — a mapping literal carrying an `"after_payload"` key with a non-`None` value in
   a function that also persists, i.e. a stored proposal row that will carry a payload.
4. `MERGE_PERSIST` — a **mapping merge** reaches a persistence sink. Data read out of one mapping
   and written into another is a merge, as is a dict built from more than one source mapping: a
   dict display with 2+ unpacks, or one unpack plus an explicit key drawn from a second mapping; the
   `|` operator and `|=`; `dict(record, **overrides)`; any `dict(...)` whose argument draws on two or
   more source mappings; a `.update(...)` call; a subscript assignment whose value is read out of a
   second mapping; a loop writing `target[<loop variable>]` while iterating another mapping; a dict
   comprehension that iterates one mapping and reads a second; and a call to a first-party helper
   that itself returns any of these, resolved to a fixpoint. Merged data is tracked through
   tuple/list assignment targets as well as bare names.

   A bare `dict(record)` copy, `dict(sorted(record.items()))` and a single unpack stamped with
   computed fields (`{**opportunity, "last_touch": ...}`) are not merges. A merge performed by a
   third-party callable that is none of the above (`operator.or_(record, intended)`) is not
   recognised.

   This trigger also covers the direct `update_*` write path, whose guard is
   `validate_update_fields`.

An obligated function satisfies the gate only with a validator call (`validate_proposal_payload`,
or `validate_update_fields` for a `MERGE_PERSIST` obligation on the direct update path) that is
(a) imported from `crm.domain.allowlists`, directly or through the `crm.domain` package's
re-export of it (resolved through the index); (b) a **top-level statement of that function's
body**, not nested in an `if`/`try`/loop; (c) positioned **before** the first obligation; and
(d) passed the payload — a payload-derived expression, or, for `MERGE_PERSIST`, one of the mappings
the merge unpacks.

`EXPECTED_OBLIGATIONS` pins the functions this analysis must find, and `DECLARED_UNGUARDED_SINKS`
enumerates individually declared exceptions.

Usage: `python3 scripts/check_payload_validation_coverage.py [--src PATH]`

Exit codes: 0 pass; 1 coverage findings; 2 usage/environment error.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The single authoritative validator, and the module it must come from.
VALIDATOR_NAME: Final[str] = "validate_proposal_payload"
VALIDATOR_MODULE: Final[str] = "crm.domain.allowlists"

#: The direct-update-path validator in the same module. It satisfies a `MERGE_PERSIST` obligation
#: (an `update_*` handler merging caller-supplied `fields` into a record), never a proposal
#: obligation, whose payload set is wider.
UPDATE_VALIDATOR_NAME: Final[str] = "validate_update_fields"

#: Every name a guard may resolve to, both declared in `VALIDATOR_MODULE`.
VALIDATOR_NAMES: Final[frozenset[str]] = frozenset({VALIDATOR_NAME, UPDATE_VALIDATOR_NAME})

#: Package whose `__init__` re-exports the validators. A guard bound through it is accepted only
#: when the package itself imports the name from `VALIDATOR_MODULE`.
VALIDATOR_REEXPORT_MODULE: Final[str] = "crm.domain"

#: The mapping key a proposal payload travels under.
PAYLOAD_KEY: Final[str] = "after_payload"

#: Calls that put a value into persisted state. `transact_write` covers the raw transact-item path
#: `crm.proposals.approve_proposal` builds; `put_operation` is the first-party item builder.
PERSIST_SINKS: Final[frozenset[str]] = frozenset(
    {"put_item", "put_idempotent", "transact_write", "put_operation"}
)

#: Calls that *apply* a payload to a record. Obligation trigger 2 fires on these regardless of
#: whether the payload argument is taint-traceable at the call site.
APPLY_SINKS: Final[frozenset[str]] = frozenset({"_apply_operation"})

#: How many `**` unpacks (or source mappings) make a merge for trigger 4.
MERGE_UNPACK_THRESHOLD: Final[int] = 2


@dataclass(frozen=True)
class UnguardedSink:
    """One obligation deliberately left unguarded, with the reason it is safe.

    Each entry names one module, one function and one trigger. `check_stale_declarations` fails an
    entry that no longer corresponds to a real obligation.
    """

    module: str
    function: str
    trigger: str
    reason: str


#: Currently empty: every payload-persisting path in first-party source validates first.
DECLARED_UNGUARDED_SINKS: Final[tuple[UnguardedSink, ...]] = ()

#: The obligations this analysis must derive from real source; a missing one is an
#: `ANALYSIS BLIND` finding.
EXPECTED_OBLIGATIONS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("crm.proposals", "submit_proposal"),
        ("crm.proposals", "approve_proposal"),
        ("crm.handlers.update_account", "_update_account"),
        ("crm.handlers.update_contact", "_update_contact"),
        ("crm.handlers.update_opportunity", "_update_opportunity"),
        ("crm.handlers.update_activity", "_update_activity"),
    }
)


@dataclass(frozen=True)
class Obligation:
    """One reason a function must validate a payload, and the line the requirement starts at."""

    module: str
    function: str
    trigger: str
    lineno: int
    detail: str
    #: For a `MERGE_PERSIST` obligation, the names of the mappings the merge unpacks. A guard
    #: naming any of them satisfies it; an empty set means only a payload-derived argument does.
    merged_operands: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Guard:
    """A validator call that may satisfy an obligation."""

    lineno: int
    unconditional: bool
    payload_argument: bool
    name: str = VALIDATOR_NAME
    #: Bare identifiers appearing in the call's arguments, used to match a `MERGE_PERSIST`
    #: obligation's unpacked operands.
    argument_names: frozenset[str] = frozenset()


@dataclass
class Coverage:
    """Result of the payload-flow pass over first-party source."""

    obligations: list[Obligation]
    guards: dict[tuple[str, str], list[Guard]]
    #: `(module, function)` pairs whose validator binding is not `crm.domain.allowlists`.
    misbound: list[str]


def _load_index_builder() -> Callable[[Path], dict[str, Any]]:
    """Return `check_handler_grant_coverage.build_index`, this repository's one AST module index.

    Loaded by path because `scripts/` is not an importable package.
    """
    path = REPO_ROOT / "scripts" / "check_handler_grant_coverage.py"
    spec = importlib.util.spec_from_file_location("check_handler_grant_coverage", path)
    if spec is None or spec.loader is None:
        raise OSError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_handler_grant_coverage"] = module
    spec.loader.exec_module(module)
    builder: Callable[[Path], dict[str, Any]] = module.build_index
    return builder


def _const_str(node: ast.AST | None) -> str | None:
    """Fold `node` to a string constant when it is one statically, else return `None`.

    Handles a plain literal, a `+` concatenation of foldable operands, and an f-string whose parts
    are all constants.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _const_str(node.left)
        right = _const_str(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            folded = _const_str(value)
            if folded is None:
                return None
            parts.append(folded)
        return "".join(parts)
    return None


def _is_payload_root(node: ast.AST, parameters: frozenset[str]) -> bool:
    """Whether `node` introduces a proposal payload into the enclosing function."""
    if isinstance(node, ast.Name) and node.id in parameters:
        return True
    if isinstance(node, ast.Subscript) and _const_str(node.slice) == PAYLOAD_KEY:
        return True
    return bool(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and _const_str(node.args[0]) == PAYLOAD_KEY
    )


def _payload_parameters(func: ast.FunctionDef) -> frozenset[str]:
    args = func.args
    named = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    return frozenset({arg.arg for arg in named if arg.arg == PAYLOAD_KEY})


def _is_tainted(expr: ast.AST, parameters: frozenset[str], tainted: frozenset[str]) -> bool:
    """Whether `expr` carries payload-derived data."""
    for node in ast.walk(expr):
        if _is_payload_root(node, parameters):
            return True
        if isinstance(node, ast.Name) and node.id in tainted:
            return True
    return False


def _tainted_names(func: ast.FunctionDef, parameters: frozenset[str]) -> frozenset[str]:
    """Names bound to payload-derived values, to a fixpoint over the function's assignments."""
    tainted: set[str] = set(parameters)
    for _pass in range(4):
        grown = False
        for node in ast.walk(func):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            if node.value is None or not _is_tainted(node.value, parameters, frozenset(tainted)):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in tainted:
                    tainted.add(target.id)
                    grown = True
        if not grown:
            break
    return frozenset(tainted)


def _is_mapping_merge_dict(node: ast.Dict) -> bool:
    """Whether a dict display merges two mappings, in either of the two shapes that count.

    Two or more `**` unpacks (`{**record, **caller_supplied}`), or one unpack plus an explicit key
    whose value reads out of a different name (`{**record, "name": intended["name"]}`). A single
    unpack with only computed values is not a merge.
    """
    unpacks = [value for key, value in zip(node.keys, node.values, strict=True) if key is None]
    if len(unpacks) >= MERGE_UNPACK_THRESHOLD:
        return True
    if not unpacks:
        return False
    unpacked_names = {
        inner.id for value in unpacks for inner in ast.walk(value) if isinstance(inner, ast.Name)
    }
    for key, value in zip(node.keys, node.values, strict=True):
        if key is None:
            continue
        for inner in ast.walk(value):
            if (
                isinstance(inner, ast.Subscript)
                and isinstance(inner.value, ast.Name)
                and inner.value.id not in unpacked_names
            ):
                return True
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "get"
                and isinstance(inner.func.value, ast.Name)
                and inner.func.value.id not in unpacked_names
            ):
                return True
    return False


def _is_mapping_merge_binop(node: ast.BinOp) -> bool:
    """Whether `node` is a `|` mapping merge (PEP 584) rather than a bitwise-integer `|`.

    A numeric literal on either side rules the expression out; anything else counts.
    """
    if not isinstance(node.op, ast.BitOr):
        return False
    return not any(
        isinstance(side, ast.Constant) and isinstance(side.value, int | float)
        for side in (node.left, node.right)
    )


#: Methods whose receiver is being read *as a mapping*, so the receiver is a data source rather
#: than a callee: `record.items()` reads `record`.
_MAPPING_READ_METHODS: Final[frozenset[str]] = frozenset(
    {"get", "items", "keys", "values", "pop", "copy"}
)


def _source_names(node: ast.AST) -> frozenset[str]:
    """Identifiers `node` reads as *data*, excluding the names it merely calls.

    `chain(record.items(), intended.items())` reads `record` and `intended`; `chain` is a callee and
    not a source. A receiver read through a mapping method stays a source.
    """
    callee_ids: set[int] = set()
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        func = inner.func
        if isinstance(func, ast.Attribute) and func.attr in _MAPPING_READ_METHODS:
            continue
        callee_ids.update(id(name) for name in ast.walk(func))
    return frozenset(
        inner.id
        for inner in ast.walk(node)
        if isinstance(inner, ast.Name) and id(inner) not in callee_ids
    )


def _is_mapping_merge_call(node: ast.Call) -> bool:
    """Whether `node` is a `dict(...)` call that merges two mappings rather than copying one.

    `dict(record, **overrides)`, `dict(**a, **b)`, and any `dict(...)` built from an expression
    drawing on more than one source mapping (`dict(chain(a.items(), b.items()))`,
    `dict(ChainMap(b, a))`) are merges. `dict(record)` and `dict(sorted(record.items()))` are copies;
    a following `.update()` is seen by :func:`_mutating_merge_targets`.
    """
    if not (isinstance(node.func, ast.Name) and node.func.id == "dict"):
        return False
    unpacks = sum(1 for kw in node.keywords if kw.arg is None)
    if node.args and node.keywords:
        return True
    if unpacks >= MERGE_UNPACK_THRESHOLD:
        return True
    sources: set[str] = set()
    for argument in node.args:
        if isinstance(argument, ast.Name):
            continue
        sources |= _source_names(argument)
    return len(sources) >= MERGE_UNPACK_THRESHOLD


def _iterated_mapping_name(node: ast.expr) -> str | None:
    """Return the mapping name behind `X.items()`/`X.keys()`/`X.values()`/`X`, else `None`."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr in {"items", "keys", "values"} and isinstance(node.func.value, ast.Name):
            return node.func.value.id
        return None
    if isinstance(node, ast.Name):
        return node.id
    return None


def _dotted_name(node: ast.expr) -> str | None:
    """Return `a`, `a.b`, `a.b.c` for a Name/Attribute chain, else `None`.

    Used to name a mapping held on an attribute, such as `holder.data`.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _referenced_names(expr: ast.AST) -> frozenset[str]:
    """Every bare identifier and dotted path `expr` mentions, used to match merge bindings."""
    names: set[str] = set()
    for inner in ast.walk(expr):
        if isinstance(inner, ast.Name | ast.Attribute):
            dotted = _dotted_name(inner)
            if dotted is not None:
                names.add(dotted)
    return frozenset(names)


def _mapping_reads(node: ast.AST) -> frozenset[str]:
    """Names read as mappings inside `node` -- `X[...]`, `X.get(...)`, `X.items()`.

    `X` may be a dotted path (`holder.data[...]`).
    """
    names: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Subscript):
            base = _dotted_name(inner.value)
            if base is not None:
                names.add(base)
        elif (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr in {"get", "items", "keys", "values", "pop"}
        ):
            base = _dotted_name(inner.func.value)
            if base is not None:
                names.add(base)
    return frozenset(names)


def _is_mapping_merge_comprehension(node: ast.DictComp) -> bool:
    """Whether a dict comprehension merges the mapping it iterates with a second one.

    For example `{k: intended.get(k, v) for k, v in record.items()}`.
    """
    iterated = {
        name
        for generator in node.generators
        if (name := _iterated_mapping_name(generator.iter)) is not None
    }
    if not iterated:
        return False
    read = _mapping_reads(node.key) | _mapping_reads(node.value)
    return bool(read - iterated)


def _operand_names(node: ast.AST) -> frozenset[str]:
    """Every bare identifier appearing anywhere in a merge expression."""
    return frozenset(inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name))


def _merge_expressions(
    func: ast.FunctionDef, merge_producers: frozenset[str] = frozenset()
) -> list[ast.expr]:
    """Return every expression in `func` that evaluates to a merged mapping.

    Covers the dict-display forms (:func:`_is_mapping_merge_dict`), the `|` operator
    (:func:`_is_mapping_merge_binop`), the `dict(...)` constructor (:func:`_is_mapping_merge_call`),
    a dict comprehension, and a call to a first-party function in `merge_producers`.
    """

    def is_merge(node: ast.AST) -> bool:
        if isinstance(node, ast.Dict):
            return _is_mapping_merge_dict(node)
        if isinstance(node, ast.DictComp):
            return _is_mapping_merge_comprehension(node)
        if isinstance(node, ast.BinOp):
            return _is_mapping_merge_binop(node)
        if isinstance(node, ast.Call):
            return _is_mapping_merge_call(node) or _callee_name(node) in merge_producers
        return False

    return [node for node in ast.walk(func) if isinstance(node, ast.expr) and is_merge(node)]


def _mutating_merge_targets(func: ast.FunctionDef) -> dict[str, tuple[int, frozenset[str]]]:
    """Return names mutated into a merged mapping in place, as `name -> (lineno, operand names)`.

    Covers `merged.update(intended)`, `merged |= intended`, a loop writing `target[<loop variable>]`
    from another mapping, and a subscript assignment whose value is read out of another mapping.
    """
    targets_map: dict[str, tuple[int, frozenset[str]]] = {}

    def record(name: str, lineno: int, operands: frozenset[str]) -> None:
        existing = targets_map.get(name)
        if existing is None:
            targets_map[name] = (lineno, operands)
        else:
            targets_map[name] = (min(existing[0], lineno), existing[1] | operands)

    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Name)
        ):
            operands = frozenset({node.func.value.id})
            for argument in [*node.args, *(kw.value for kw in node.keywords)]:
                operands |= _operand_names(argument)
            record(node.func.value.id, node.lineno, operands)
        elif (
            isinstance(node, ast.AugAssign)
            and isinstance(node.op, ast.BitOr)
            and isinstance(node.target, ast.Name)
        ):
            record(node.target.id, node.lineno, _operand_names(node.value) | {node.target.id})
        elif isinstance(node, ast.For):
            # A loop writing `target[<loop variable>]` while iterating another mapping. A fixed-key
            # write inside an unrelated loop does not count.
            sources = set(_mapping_reads(node.iter)) | _source_names(node.iter)
            if not sources:
                continue
            loop_names = {
                inner.id for inner in ast.walk(node.target) if isinstance(inner, ast.Name)
            }
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Assign):
                    continue
                for target in inner.targets:
                    if not (
                        isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                    ):
                        continue
                    key_names = {n.id for n in ast.walk(target.slice) if isinstance(n, ast.Name)}
                    remaining = sources - {target.value.id}
                    if key_names & loop_names and remaining:
                        record(
                            target.value.id,
                            node.lineno,
                            frozenset({target.value.id})
                            | frozenset(remaining)
                            | _operand_names(inner.value),
                        )
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            # `merged["stage"] = intended["stage"]`: a value read out of another mapping. A computed
            # or literal value does not count.
            targets = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
            if node.value is None:
                continue
            for target in targets:
                if not isinstance(target, ast.Subscript):
                    continue
                container = _dotted_name(target.value)
                if container is None:
                    continue
                read = _mapping_reads(node.value) - {container}
                if not read:
                    continue
                record(
                    container,
                    node.lineno,
                    frozenset({container}) | read | _operand_names(node.value),
                )
    return targets_map


def find_merge_producers(index: dict[str, Any]) -> frozenset[str]:
    """Return the names of first-party functions that return a merged mapping.

    Iterated to a fixpoint, so a function returning another producer's result is a producer too.
    """
    producers: set[str] = set()
    for _pass in range(8):
        grown = False
        known = frozenset(producers)
        for module_name in sorted(index):
            for func_name, func in index[module_name].functions.items():
                if func_name in producers:
                    continue
                merges = _merge_expressions(func, known)
                if not merges:
                    continue
                merge_ids = frozenset(id(node) for node in merges)
                derived = _merge_tainted_names(func, merges)
                for node in ast.walk(func):
                    if not isinstance(node, ast.Return) or node.value is None:
                        continue
                    if _contains_node(node.value, merge_ids) or any(
                        isinstance(inner, ast.Name) and inner.id in derived
                        for inner in ast.walk(node.value)
                    ):
                        producers.add(func_name)
                        grown = True
                        break
        if not grown:
            break
    return frozenset(producers)


def _contains_node(expr: ast.AST, wanted: frozenset[int]) -> bool:
    return any(id(node) in wanted for node in ast.walk(expr))


def _merge_bindings(
    func: ast.FunctionDef,
    merges: list[ast.expr],
    mutating_targets: dict[str, tuple[int, frozenset[str]]],
) -> dict[str, tuple[int, frozenset[str]]]:
    """Map each name holding merged data to `(earliest merge lineno, operand names)`.

    A fixpoint over the function's assignments, seeded with the names merged in place by
    `.update()`, `|=`, or subscript writes.
    """
    wanted = {id(node): node for node in merges}
    bindings: dict[str, tuple[int, frozenset[str]]] = dict(mutating_targets)
    for _pass in range(4):
        grown = False
        for node in ast.walk(func):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            if node.value is None:
                continue
            contained: list[ast.expr] = [
                inner
                for inner in ast.walk(node.value)
                if id(inner) in wanted and isinstance(inner, ast.expr)
            ]
            inherited = [
                bindings[name] for name in _referenced_names(node.value) if name in bindings
            ]
            if not contained and not inherited:
                continue
            lineno = min([inner.lineno for inner in contained] + [line for line, _ops in inherited])
            operands = frozenset().union(
                frozenset(),
                *([_operand_names(inner) for inner in contained] + [ops for _l, ops in inherited]),
            )
            bound_names = [
                inner.id
                for target in targets
                for inner in ast.walk(target)
                if isinstance(inner, ast.Name)
            ]
            for name in bound_names:
                existing = bindings.get(name)
                merged = (
                    (lineno, operands)
                    if existing is None
                    else (min(existing[0], lineno), existing[1] | operands)
                )
                if existing != merged:
                    bindings[name] = merged
                    grown = True
        if not grown:
            break
    return bindings


def _merge_tainted_names(
    func: ast.FunctionDef,
    merges: list[ast.expr],
    mutating_targets: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Names bound to a merged mapping. Thin wrapper over :func:`_merge_bindings`."""
    seeded = {name: (0, frozenset({name})) for name in mutating_targets}
    return frozenset(_merge_bindings(func, merges, seeded))


def _callee_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _obligations_for(
    module_name: str, func: ast.FunctionDef, merge_producers: frozenset[str] = frozenset()
) -> tuple[list[Obligation], list[Guard]]:
    """Derive one function's obligations and the guards it offers against them."""
    parameters = _payload_parameters(func)
    tainted = _tainted_names(func, parameters)
    merges = _merge_expressions(func, merge_producers)
    mutating_targets = _mutating_merge_targets(func)
    merge_ids = frozenset(id(node) for node in merges)
    merge_bindings = _merge_bindings(func, merges, mutating_targets)
    obligations: list[Obligation] = []
    persist_sites: list[ast.Call] = []
    payload_dicts: list[ast.Dict] = []

    for node in ast.walk(func):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == PAYLOAD_KEY
                    and not (isinstance(value, ast.Constant) and value.value is None)
                ):
                    payload_dicts.append(node)
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node)
        if name is None:
            continue
        if name in PERSIST_SINKS:
            persist_sites.append(node)
            arguments = [*node.args, *(kw.value for kw in node.keywords)]
            if any(_is_tainted(argument, parameters, tainted) for argument in arguments):
                obligations.append(
                    Obligation(
                        module_name,
                        func.name,
                        "TAINTED_PERSIST",
                        node.lineno,
                        f"payload-derived value reaches {name}()",
                    )
                )
            reaching: list[tuple[int, frozenset[str]]] = [
                (inner.lineno, _operand_names(inner))
                for argument in arguments
                for inner in ast.walk(argument)
                if id(inner) in merge_ids and isinstance(inner, ast.expr)
            ]
            reaching += [
                merge_bindings[name]
                for argument in arguments
                for name in _referenced_names(argument)
                if name in merge_bindings
            ]
            if reaching:
                operands = frozenset().union(frozenset(), *(ops for _l, ops in reaching))
                obligations.append(
                    Obligation(
                        module_name,
                        func.name,
                        "MERGE_PERSIST",
                        min([line for line, _ops in reaching] + [node.lineno]),
                        f"a mapping merge over {sorted(operands)} reaches {name}()",
                        merged_operands=operands,
                    )
                )
        if name in APPLY_SINKS:
            obligations.append(
                Obligation(
                    module_name,
                    func.name,
                    "APPLY_CALL",
                    node.lineno,
                    f"{name}() merges a payload into the target record",
                )
            )

    if persist_sites and payload_dicts:
        first_dict = min(payload_dicts, key=lambda node: node.lineno)
        obligations.append(
            Obligation(
                module_name,
                func.name,
                "PROPOSAL_ITEM",
                min(first_dict.lineno, min(site.lineno for site in persist_sites)),
                f"a persisted mapping carries a non-None {PAYLOAD_KEY!r} key",
            )
        )

    guards = [
        guard
        for statement in func.body
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
        for guard in _guard_for(statement.value, parameters, tainted)
    ]
    nested = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and _callee_name(node) in VALIDATOR_NAMES
    ]
    for call in nested:
        if all(guard.lineno != call.lineno for guard in guards):
            arguments = [*call.args, *(kw.value for kw in call.keywords)]
            guards.append(
                Guard(
                    lineno=call.lineno,
                    unconditional=False,
                    payload_argument=any(
                        _is_tainted(argument, parameters, tainted) for argument in arguments
                    ),
                    name=_callee_name(call) or VALIDATOR_NAME,
                    argument_names=_argument_names(arguments),
                )
            )
    return obligations, guards


def _argument_names(arguments: list[ast.expr]) -> frozenset[str]:
    """Return every bare identifier appearing in a validator call's arguments."""
    return frozenset(
        node.id
        for argument in arguments
        for node in ast.walk(argument)
        if isinstance(node, ast.Name)
    )


def _guard_for(call: ast.Call, parameters: frozenset[str], tainted: frozenset[str]) -> list[Guard]:
    name = _callee_name(call)
    if name not in VALIDATOR_NAMES:
        return []
    arguments = [*call.args, *(kw.value for kw in call.keywords)]
    return [
        Guard(
            lineno=call.lineno,
            unconditional=True,
            payload_argument=any(
                _is_tainted(argument, parameters, tainted) for argument in arguments
            ),
            name=name or VALIDATOR_NAME,
            argument_names=_argument_names(arguments),
        )
    ]


def analyse(src_root: Path | None = None) -> Coverage:
    """Run the payload-flow pass over every first-party module and return its coverage."""
    build_index = _load_index_builder()
    index = build_index(src_root if src_root is not None else REPO_ROOT / "src")
    merge_producers = find_merge_producers(index)
    obligations: list[Obligation] = []
    guards: dict[tuple[str, str], list[Guard]] = {}
    misbound: list[str] = []
    for module_name in sorted(index):
        module = index[module_name]
        for func_name in sorted(module.functions):
            func = module.functions[func_name]
            found, offered = _obligations_for(module_name, func, merge_producers)
            if found:
                obligations.extend(found)
            if offered:
                guards[(module_name, func_name)] = offered
                for guard in offered:
                    problem = _binding_problem(index, module, guard.name)
                    if problem is not None:
                        misbound.append(f"{module_name}.{func_name} {problem}")
    return Coverage(obligations=obligations, guards=guards, misbound=misbound)


def _binding_problem(index: dict[str, Any], module: Any, validator: str) -> str | None:
    """Return why `validator` is not bound to the authoritative module in `module`, or `None`.

    Accepts a direct import from `VALIDATOR_MODULE`, or an import from `VALIDATOR_REEXPORT_MODULE`
    when that package's own index entry shows it importing the same name from `VALIDATOR_MODULE`.
    Aliasing the name to something else in `crm/domain/__init__.py` is a finding.
    """
    binding = module.imports.get(validator)
    if binding is None:
        return (
            f"calls {validator} but the name is not imported from {VALIDATOR_MODULE} at all "
            f"(no import binding — a module-local definition shadowing the validator)"
        )
    if binding[0] == VALIDATOR_MODULE and binding[1] == validator:
        return None
    if binding[0] == VALIDATOR_MODULE:
        return (
            f"calls {validator} but the name is aliased onto {binding[1]!r} from "
            f"{VALIDATOR_MODULE}, which is a different function"
        )
    if binding[0] == VALIDATOR_REEXPORT_MODULE:
        package = index.get(VALIDATOR_REEXPORT_MODULE)
        if package is None:
            return (
                f"calls {validator} through {VALIDATOR_REEXPORT_MODULE}, which this gate could not "
                f"find in the module index to resolve"
            )
        reexport = package.imports.get(binding[1])
        if reexport is not None and reexport == (VALIDATOR_MODULE, validator):
            return None
        return (
            f"calls {validator} through {VALIDATOR_REEXPORT_MODULE}, which does not re-export it "
            f"from {VALIDATOR_MODULE} (resolved to {reexport!r})"
        )
    return (
        f"calls {validator} but the name is not imported from {VALIDATOR_MODULE} "
        f"(bound to {binding!r})"
    )


def findings_for(coverage: Coverage) -> list[str]:
    """Return every coverage finding, in reporting order."""
    findings: list[str] = list(coverage.misbound)
    declared = {(d.module, d.function, d.trigger) for d in DECLARED_UNGUARDED_SINKS}

    for obligation in sorted(
        coverage.obligations, key=lambda o: (o.module, o.function, o.lineno, o.trigger)
    ):
        key = (obligation.module, obligation.function, obligation.trigger)
        if key in declared:
            continue
        guards = coverage.guards.get((obligation.module, obligation.function), [])
        qualifying = [
            guard
            for guard in guards
            if guard.unconditional
            and guard.lineno < obligation.lineno
            and _guard_covers(guard, obligation)
        ]
        if qualifying:
            continue
        findings.append(
            f"UNVALIDATED PAYLOAD [{obligation.trigger}] {obligation.module}."
            f"{obligation.function}:{obligation.lineno}: {obligation.detail}, with no "
            f"unconditional {VALIDATOR_NAME}() call on the payload before it "
            f"(guards seen: {[(g.name, g.lineno, g.unconditional, g.payload_argument) for g in guards]})"
        )

    obligated = {(o.module, o.function) for o in coverage.obligations}
    for module_name, func_name in sorted(EXPECTED_OBLIGATIONS - obligated):
        findings.append(
            f"ANALYSIS BLIND [{module_name}.{func_name}] is pinned in EXPECTED_OBLIGATIONS but this "
            f"pass derived no payload obligation for it; the payload flow it must guard has become "
            f"invisible to the gate"
        )
    findings.extend(check_stale_declarations(coverage))
    return findings


def _guard_covers(guard: Guard, obligation: Obligation) -> bool:
    """Whether `guard` validates the data `obligation` is about.

    A proposal-path obligation is satisfied only by `validate_proposal_payload` on a payload-derived
    expression. A `MERGE_PERSIST` obligation is additionally satisfied by either validator called on
    one of the mappings the merge unpacks (as `validate_update_fields(table, fields)` covers a
    `{**before, **fields}` merge).
    """
    if obligation.trigger != "MERGE_PERSIST":
        return guard.name == VALIDATOR_NAME and guard.payload_argument
    return guard.payload_argument or bool(obligation.merged_operands & guard.argument_names)


def check_stale_declarations(coverage: Coverage) -> list[str]:
    """Fail a `DECLARED_UNGUARDED_SINKS` entry with no corresponding real obligation."""
    real = {(o.module, o.function, o.trigger) for o in coverage.obligations}
    return [
        f"STALE UNGUARDED DECLARATION {declaration.module}.{declaration.function}/"
        f"{declaration.trigger} is declared unguarded but no such obligation exists; "
        f"delete the declaration"
        for declaration in DECLARED_UNGUARDED_SINKS
        if (declaration.module, declaration.function, declaration.trigger) not in real
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=None, help="Source root to analyse.")
    args = parser.parse_args(argv)

    try:
        coverage = analyse(args.src)
    except (OSError, SyntaxError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    findings = findings_for(coverage)
    if findings:
        for finding in findings:
            print(finding)
        print(f"\nFAILED: {len(findings)} payload-validation coverage finding(s)")
        return 1

    print(
        f"OK: {len(coverage.obligations)} payload-persisting obligation(s) across "
        f"{len({(o.module, o.function) for o in coverage.obligations})} function(s), each guarded "
        f"beforehand by an unconditional call to its allowlist validator "
        f"({VALIDATOR_NAME} for a proposal payload, {UPDATE_VALIDATOR_NAME} for a direct update)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

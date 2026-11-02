#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gate: no `PageContext` variant may carry retrieved rows.

`PageContext` describes what the user is looking at (route, entity type, filter/sort/page); it never
carries the rows the browser retrieved, so the agent re-executes the described query through its
own scoped tools. Three readings of `crm.toolspec.page_context` are checked and their findings
unioned:

1. **The rendered JSON Schema** (`render_json_schema`) contains no array whose items admit an
   object.
2. **The variant dataclass annotations**, read from source by AST, contain no `list`/`tuple`/
   `Sequence`/`set` of a non-scalar, including fields the schema generator cannot render.
3. **`page_context.array_of_record_paths`**, the module's own predicate, when it exposes one.

Usage: `python3 scripts/check_page_context_no_rows.py [--module-path PATH]`

Exit codes: 0 pass; 1 an array-of-record field was found; 2 usage/environment error;
3 SKIP — `crm.toolspec.page_context` is absent or unimportable, so nothing was verified.
`scripts/run_gates.py` renders 3 as `[SKIP]`.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Exit status for "ran, verified nothing". Must equal `scripts/run_gates.py`'s
#: `GATE_SKIP_EXIT_STATUS`; a test pins the two together.
SKIP_EXIT_STATUS = 3

DEFAULT_MODULE_PATH = REPO_ROOT / "src" / "crm" / "toolspec" / "page_context.py"

#: JSON Schema scalar type names. An array whose items are drawn only from these carries no record.
_SCALAR_JSON_TYPES = frozenset({"string", "integer", "number", "boolean", "null"})

#: Annotation origins that denote a sequence in the variant dataclasses. A field annotated as one of
#: these over anything but a scalar is a row carrier.
_SEQUENCE_ORIGINS = frozenset({"list", "tuple", "Sequence", "set", "frozenset", "Iterable"})

#: Python scalar annotation names permitted inside a sequence.
_SCALAR_ANNOTATIONS = frozenset({"str", "int", "float", "bool", "None"})


def find_schema_violations(schema: object, path: str = "$") -> list[str]:
    """Walk a JSON Schema and report every array whose items admit an object.

    Args:
        schema: A JSON-Schema node — the whole document on the first call.
        path: JSON-pointer-ish location of `schema`, used in violation messages.

    Returns:
        One message per offending array, naming its location and why it offends. Empty when the
        schema carries no array-of-record anywhere.
    """
    violations: list[str] = []
    if isinstance(schema, list):
        for index, member in enumerate(schema):
            violations += find_schema_violations(member, f"{path}[{index}]")
        return violations
    if not isinstance(schema, dict):
        return violations

    declared = schema.get("type")
    types = {declared} if isinstance(declared, str) else set(declared or ())
    if "array" in types:
        items = schema.get("items")
        if not _items_are_scalar_only(items):
            violations.append(
                f"{path}: array field whose items admit an object "
                f"({_describe_items(items)}). A PageContext variant may describe a query but "
                "never carry its rows: serialising retrieved records would authorize the "
                "agent's data by the browser's read rather than by crm.authz's predicate."
            )

    for key, value in schema.items():
        if key in {"type", "const", "enum", "required", "title", "description"}:
            continue
        violations += find_schema_violations(value, f"{path}.{key}")
    return violations


def _items_are_scalar_only(items: object) -> bool:
    """Return whether an `items` schema admits scalars and nothing else."""
    if not isinstance(items, dict):
        # Absent, `true`, or a tuple form: an unconstrained item admits an object.
        return False
    if "properties" in items or "oneOf" in items or "anyOf" in items or "$ref" in items:
        return False
    declared = items.get("type")
    types = {declared} if isinstance(declared, str) else set(declared or ())
    return bool(types) and types <= _SCALAR_JSON_TYPES


def _describe_items(items: object) -> str:
    if isinstance(items, dict):
        return f"items={items.get('title') or items.get('type') or sorted(items)}"
    return f"items={items!r}"


def find_annotation_violations(module_source: str) -> list[str]:
    """Report every sequence-of-non-scalar annotation on a class in `module_source`.

    Reads by AST, so a field whose type the schema generator cannot render is still reported.

    Args:
        module_source: Full text of `page_context.py`.

    Returns:
        One message per offending annotated field, naming the class, field, and annotation.
    """
    violations: list[str] = []
    tree = ast.parse(module_source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or statement.annotation is None:
                continue
            target = statement.target
            name = target.id if isinstance(target, ast.Name) else ast.unparse(target)
            offence = _sequence_of_non_scalar(statement.annotation)
            if offence is not None:
                violations.append(
                    f"{node.name}.{name}: annotation `{ast.unparse(statement.annotation)}` is a "
                    f"sequence of {offence}, i.e. an array-of-record field. A PageContext variant "
                    "describes a query; it never carries the query's rows."
                )
    return violations


def _sequence_of_non_scalar(annotation: ast.expr) -> str | None:
    """Return a description of the non-scalar element type, or `None` if this is not a violation."""
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        for side in (annotation.left, annotation.right):
            offence = _sequence_of_non_scalar(side)
            if offence is not None:
                return offence
        return None
    if not isinstance(annotation, ast.Subscript):
        return None
    origin = _annotation_name(annotation.value)
    if origin not in _SEQUENCE_ORIGINS:
        # Not a sequence itself, but it may parameterise one (e.g. `Mapping[str, list[Record]]`).
        return _nested_sequence_offence(annotation.slice)
    element = annotation.slice
    if isinstance(element, ast.Tuple):
        # `tuple[X, ...]` is homogeneous; any other tuple form is a fixed-arity record in itself.
        members = [m for m in element.elts if not isinstance(m, ast.Constant) or m.value is not ...]
        if len(members) != 1:
            return "multiple element types"
        element = members[0]
    if _is_scalar_annotation(element):
        return None
    return f"`{ast.unparse(element)}`"


def _nested_sequence_offence(node: ast.expr) -> str | None:
    if isinstance(node, ast.Tuple):
        for member in node.elts:
            offence = _sequence_of_non_scalar(member)
            if offence is not None:
                return offence
        return None
    return _sequence_of_non_scalar(node)


def _is_scalar_annotation(node: ast.expr) -> bool:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_scalar_annotation(node.left) and _is_scalar_annotation(node.right)
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    return _annotation_name(node) in _SCALAR_ANNOTATIONS


def _annotation_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _load_schema() -> tuple[dict[str, Any], Callable[[object], list[str]] | None]:
    """Import the module, render its schema, and return it with the module's own predicate.

    The predicate is `crm.toolspec.page_context.array_of_record_paths`, or `None` when the module
    does not define it.

    Raises:
        ImportError: The module is unavailable.
    """
    src = str(REPO_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from crm.toolspec import page_context

    predicate = getattr(page_context, "array_of_record_paths", None)
    return page_context.render_json_schema(), predicate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--module-path",
        type=Path,
        default=DEFAULT_MODULE_PATH,
        help="Path to page_context.py (default: src/crm/toolspec/page_context.py).",
    )
    args = parser.parse_args(argv)

    module_path: Path = args.module_path
    if not module_path.is_file():
        print(
            f"[SKIP] check_page_context_no_rows: {module_path} does not exist, so the "
            "PageContext schema artefact is unavailable. Nothing was verified."
        )
        return SKIP_EXIT_STATUS

    try:
        schema, owner_predicate = _load_schema()
    except ImportError as exc:
        print(
            f"[SKIP] check_page_context_no_rows: cannot import crm.toolspec.page_context ({exc}), "
            "so the schema artefact is unavailable. Nothing was verified."
        )
        return SKIP_EXIT_STATUS
    except Exception as exc:  # noqa: BLE001 — a generator that raises is a failure, not a skip.
        print(
            "check_page_context_no_rows: VIOLATION — rendering the PageContext schema raised "
            f"{type(exc).__name__}: {exc}. A field whose type the generator cannot render is an "
            "unreviewed shape, not a pass.",
            file=sys.stderr,
        )
        return 1

    violations = find_schema_violations(schema)
    violations += find_annotation_violations(module_path.read_text(encoding="utf-8"))
    if owner_predicate is not None:
        violations += [
            f"{path}: reported by crm.toolspec.page_context.array_of_record_paths as an "
            "array-of-record field"
            for path in owner_predicate(schema)
        ]

    if violations:
        print(
            "check_page_context_no_rows: VIOLATION — PageContext carries retrieved rows:",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        print("check_page_context_no_rows: FAILED", file=sys.stderr)
        return 1

    variants = schema.get("oneOf", [])
    readings = "this gate's schema walk, its annotation walk"
    readings += (
        ", and crm.toolspec.page_context's own array_of_record_paths predicate"
        if owner_predicate is not None
        else " (the schema module exposes no array_of_record_paths predicate to cross-check against)"
    )
    print(
        f"check_page_context_no_rows: OK — {len(variants)} variant(s) checked "
        f"({', '.join(str(v.get('title', '?')) for v in variants)}); no array-of-record field per "
        f"{readings}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

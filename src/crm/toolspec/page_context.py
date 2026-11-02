# SPDX-License-Identifier: MIT
"""The closed ``PageContext`` union the assistant panel describes the user's current view with.

A ``PageContext`` describes what the user is looking at and never carries retrieved rows: no member
of the union has a field of array-of-record type. Four variants, discriminated by a class-level
``kind``:

* ``entity`` — a single-record view: ``route``, ``entity_type``, ``entity_id``.
* ``list`` — a filtered collection view: ``route``, ``entity_type``, and a :class:`QueryDescriptor`.
* ``board`` — the pipeline board: ``route`` and a :class:`QueryDescriptor`.
* ``opaque`` — a view the agent's tools cannot express, with a mandatory ``reason``.

:func:`render_json_schema` derives the JSON Schema from the dataclass annotations, and
:func:`array_of_record_paths` is the predicate ``scripts/check_page_context_no_rows.py`` applies to
it. Run as ``python3 -m crm.toolspec.page_context`` to print the schema.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import sys
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Final, Literal, Union, get_args, get_origin, get_type_hints

from crm.errors import ValidationFailed

#: Entity vocabulary the ``entity`` and ``list`` variants accept; the same set as
#: :data:`crm.toolspec.definitions.TABLES`, which imports this module.
EntityType = Literal["accounts", "contacts", "opportunities", "activities"]

ENTITY_TYPES: Final[tuple[str, ...]] = ("accounts", "contacts", "opportunities", "activities")

#: Sort direction, matching the two values the list pages emit.
SortDirection = Literal["asc", "desc"]

#: The only value types a filter may carry: scalars.
FilterValue = str | int | float | bool

#: Where :func:`write_schema` puts the generated schema, relative to the repository root.
SCHEMA_PATH: Final[Path] = Path("build") / "schema" / "page_context.json"

#: Schema version carried in the artefact as ``x-schema-version``.
SCHEMA_VERSION: Final[str] = "1"

#: Bounds on the client-supplied free strings, in characters.
ROUTE_MAX_LENGTH: Final[int] = 256
REASON_MAX_LENGTH: Final[int] = 300
ENTITY_ID_MAX_LENGTH: Final[int] = 128

#: A route is an absolute path of letters, digits, ``/``, ``_`` and ``-``; no query or fragment.
_ROUTE_PATTERN: Final[re.Pattern[str]] = re.compile(r"/[A-Za-z0-9/_\-]*")

#: A record id as the store issues them (``<kind>-<tenant>-<suffix>``, ``<prefix>-<uuid>``).
_ENTITY_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")

#: Bounds on a :class:`QueryDescriptor`: filter count, field-name, filter-value and cursor lengths.
FILTER_MAX_COUNT: Final[int] = 16
FIELD_NAME_MAX_LENGTH: Final[int] = 64
FILTER_VALUE_MAX_LENGTH: Final[int] = 256
CURSOR_MAX_LENGTH: Final[int] = 2048

#: A filter key or sort field: a lower-case snake-case attribute name.
_FIELD_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]*")

#: A pagination cursor: URL-safe base64, as :func:`crm.search.encode_cursor` emits.
_CURSOR_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_\-]*=*")

#: Characters a string filter value may not carry: C0/C1 controls and the Unicode line separators.
_FORBIDDEN_VALUE_CHARACTERS: Final[re.Pattern[str]] = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u2028\u2029]"
)

#: Longest rendering of a caller value a ``detail`` string carries.
_DETAIL_VALUE_MAX_LENGTH: Final[int] = 64


@dataclass(frozen=True, slots=True)
class SortDescriptor:
    """The sort a view has applied, as a field name and a direction."""

    field: str
    direction: SortDirection = "asc"

    def __post_init__(self) -> None:
        """Reject a sort naming no field, or a field that is not a bounded attribute name."""
        if not self.field:
            raise ValueError("SortDescriptor requires a non-empty field")
        if not _is_field_name(self.field):
            raise ValueError(_FIELD_NAME_RULE.format(what="sort field"))


@dataclass(frozen=True, slots=True)
class PageDescriptor:
    """The pagination position a view is at: an opaque cursor and a page size."""

    cursor: str | None = None
    limit: int | None = None

    def __post_init__(self) -> None:
        """Reject a non-positive page size, or a cursor that is not bounded URL-safe base64."""
        if self.limit is not None and self.limit < 1:
            raise ValueError("PageDescriptor limit must be positive")
        if self.cursor is not None and (
            not isinstance(self.cursor, str)
            or len(self.cursor) > CURSOR_MAX_LENGTH
            or _CURSOR_PATTERN.fullmatch(self.cursor) is None
        ):
            raise ValueError(
                f"cursor must be at most {CURSOR_MAX_LENGTH} characters of URL-safe base64"
            )


@dataclass(frozen=True, slots=True)
class QueryDescriptor:
    """A description of the query a view ran — never the query's results.

    ``filters`` maps a field name to a scalar. The agent re-executes the described query through
    its own scoped tools.
    """

    filters: Mapping[str, FilterValue] = field(default_factory=dict)
    sort: SortDescriptor | None = None
    page: PageDescriptor | None = None

    def __post_init__(self) -> None:
        """Reject too many filters, a key that is not an attribute name, or an unbounded value."""
        if len(self.filters) > FILTER_MAX_COUNT:
            raise ValueError(f"query carries more than {FILTER_MAX_COUNT} filters")
        for key, value in self.filters.items():
            if not _is_field_name(key):
                raise ValueError(_FIELD_NAME_RULE.format(what="filter key"))
            _require_filter_value(value)


@dataclass(frozen=True, slots=True)
class EntityPageContext:
    """A view of one record, identified by type and id."""

    kind: ClassVar[Literal["entity"]] = "entity"

    route: str
    entity_type: EntityType
    entity_id: str

    def __post_init__(self) -> None:
        """Reject a context that identifies no concrete record."""
        _require_route(self.route)
        _require_entity_type(self.entity_type)
        if not self.entity_id:
            raise ValueError("EntityPageContext requires a non-empty entity_id")
        if (
            not isinstance(self.entity_id, str)
            or len(self.entity_id) > ENTITY_ID_MAX_LENGTH
            or _ENTITY_ID_PATTERN.fullmatch(self.entity_id) is None
        ):
            raise ValueError(
                f"entity_id must be at most {ENTITY_ID_MAX_LENGTH} characters of letters, digits, "
                "'_' and '-', starting with a letter or digit"
            )


@dataclass(frozen=True, slots=True)
class ListPageContext:
    """A filtered collection view of one entity type."""

    kind: ClassVar[Literal["list"]] = "list"

    route: str
    entity_type: EntityType
    query: QueryDescriptor = field(default_factory=QueryDescriptor)

    def __post_init__(self) -> None:
        """Reject a context naming no route or an unrecognised entity type."""
        _require_route(self.route)
        _require_entity_type(self.entity_type)


@dataclass(frozen=True, slots=True)
class BoardPageContext:
    """The pipeline board view, which spans entity types and so names none."""

    kind: ClassVar[Literal["board"]] = "board"

    route: str
    query: QueryDescriptor = field(default_factory=QueryDescriptor)

    def __post_init__(self) -> None:
        """Reject a context naming no route."""
        _require_route(self.route)


@dataclass(frozen=True, slots=True)
class OpaquePageContext:
    """A view the agent's tool surface cannot express; ``reason`` is mandatory and non-empty."""

    kind: ClassVar[Literal["opaque"]] = "opaque"

    route: str
    reason: str

    def __post_init__(self) -> None:
        """Reject an opaque context that does not say why it is opaque."""
        _require_route(self.route)
        if not self.reason:
            raise ValueError("OpaquePageContext requires a non-empty reason")
        if not isinstance(self.reason, str) or len(self.reason) > REASON_MAX_LENGTH:
            raise ValueError(f"reason must be a string of at most {REASON_MAX_LENGTH} characters")


PageContext = EntityPageContext | ListPageContext | BoardPageContext | OpaquePageContext

#: Every variant, in schema order; :func:`render_json_schema` and :func:`parse_page_context` use it.
PAGE_CONTEXT_VARIANTS: Final[tuple[type[Any], ...]] = (
    EntityPageContext,
    ListPageContext,
    BoardPageContext,
    OpaquePageContext,
)

BY_KIND: Final[Mapping[str, type[Any]]] = {
    variant.kind: variant for variant in PAGE_CONTEXT_VARIANTS
}


def _require_route(route: str) -> None:
    """Raise unless ``route`` is a bounded absolute path. Every variant carries one.

    The error text names the rule, never the offending value.
    """
    if not route:
        raise ValueError("a PageContext requires a non-empty route")
    if (
        not isinstance(route, str)
        or len(route) > ROUTE_MAX_LENGTH
        or _ROUTE_PATTERN.fullmatch(route) is None
    ):
        raise ValueError(
            f"route must be at most {ROUTE_MAX_LENGTH} characters, start with '/', and contain "
            "only letters, digits, '/', '_' and '-'"
        )


def _require_entity_type(entity_type: str) -> None:
    """Raise unless ``entity_type`` is a member of :data:`ENTITY_TYPES`."""
    if entity_type not in ENTITY_TYPES:
        raise ValueError(
            f"entity_type must be one of {list(ENTITY_TYPES)}, got {_clip(entity_type)}"
        )


_FIELD_NAME_RULE: Final[str] = (
    f"{{what}} must be at most {FIELD_NAME_MAX_LENGTH} characters of lower-case letters, digits "
    "and '_', starting with a letter"
)


def _is_field_name(name: object) -> bool:
    """Whether ``name`` is a bounded snake-case attribute name."""
    return (
        isinstance(name, str)
        and len(name) <= FIELD_NAME_MAX_LENGTH
        and _FIELD_NAME_PATTERN.fullmatch(name) is not None
    )


def _require_filter_value(value: object) -> None:
    """Raise unless ``value`` is a bounded, control-free string, a finite number, or a bool."""
    if isinstance(value, str):
        if len(value) > FILTER_VALUE_MAX_LENGTH or _FORBIDDEN_VALUE_CHARACTERS.search(value):
            raise ValueError(
                f"a string filter value must be at most {FILTER_VALUE_MAX_LENGTH} characters "
                "with no control or line-separator character"
            )
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("a numeric filter value must be finite")


def _clip(value: object) -> str:
    """``repr(value)``, cut to :data:`_DETAIL_VALUE_MAX_LENGTH` characters for a ``detail`` string."""
    rendered = repr(value)
    if len(rendered) <= _DETAIL_VALUE_MAX_LENGTH:
        return rendered
    return rendered[: _DETAIL_VALUE_MAX_LENGTH - 1] + "…"


# --------------------------------------------------------------------------------------
# Schema generation — derived from the dataclasses, never transcribed
# --------------------------------------------------------------------------------------

_SCALAR_SCHEMAS: Final[Mapping[Any, dict[str, Any]]] = {
    str: {"type": "string"},
    bool: {"type": "boolean"},
    int: {"type": "integer"},
    float: {"type": "number"},
}


def _is_optional(annotation: Any) -> tuple[bool, Any]:
    """Split ``X | None`` into ``(True, X)``, leaving anything else as ``(False, annotation)``."""
    if get_origin(annotation) not in (Union, types.UnionType):
        return False, annotation
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    if len(args) == len(get_args(annotation)):
        return False, annotation
    if len(args) == 1:
        return True, args[0]
    return True, Union[tuple(args)]  # noqa: UP007 — reconstructing a runtime union


def _schema_for(annotation: Any) -> dict[str, Any]:
    """Render one field annotation as JSON Schema.

    Raises:
        TypeError: ``annotation`` is a form this generator does not map.
    """
    optional, inner = _is_optional(annotation)
    rendered = _schema_for_required(inner)
    if optional:
        # Optional is expressed by adding "null" to the type list, not by `oneOf`.
        declared = rendered.get("type")
        if isinstance(declared, str):
            rendered = {**rendered, "type": [declared, "null"]}
        elif isinstance(declared, list):
            rendered = {**rendered, "type": [*declared, "null"]}
    return rendered


def _schema_for_required(annotation: Any) -> dict[str, Any]:
    """Render a non-optional annotation. See :func:`_schema_for`."""
    if annotation in _SCALAR_SCHEMAS:
        return dict(_SCALAR_SCHEMAS[annotation])

    origin = get_origin(annotation)

    if origin is Literal:
        values = list(get_args(annotation))
        return {"type": "string", "enum": values}

    if origin in (Union, types.UnionType):
        member_types: list[str] = []
        for member in get_args(annotation):
            member_schema = _schema_for_required(member)
            member_type = member_schema.get("type")
            if not isinstance(member_type, str):
                raise TypeError(f"unsupported union member in PageContext schema: {member!r}")
            if member_type not in member_types:
                member_types.append(member_type)
        return {"type": member_types}

    if origin in (Mapping, dict):
        key_type, value_type = get_args(annotation)
        if key_type is not str:
            raise TypeError(f"PageContext mappings must be keyed by str, got {key_type!r}")
        return {"type": "object", "additionalProperties": _schema_for_required(value_type)}

    if origin in (list, tuple, set, frozenset):
        (item_type,) = get_args(annotation)[:1] or (Any,)
        return {"type": "array", "items": _schema_for_required(item_type)}

    if dataclasses.is_dataclass(annotation) and isinstance(annotation, type):
        return _object_schema(annotation)

    raise TypeError(
        f"{annotation!r} has no PageContext schema mapping. Add one deliberately — an unmapped "
        "annotation is not silently omitted, because the no-rows gate can only inspect what the "
        "artefact actually declares."
    )


def _object_schema(cls: type[Any]) -> dict[str, Any]:
    """Render a dataclass as a closed object schema.

    The class-level ``kind`` discriminator is not a dataclass field; it is added as a ``const``.
    """
    hints = get_type_hints(cls, include_extras=False)
    properties: dict[str, Any] = {}
    required: list[str] = []

    discriminator = getattr(cls, "kind", None)
    if isinstance(discriminator, str):
        properties["kind"] = {"type": "string", "const": discriminator}
        required.append("kind")

    for member in dataclasses.fields(cls):
        properties[member.name] = _schema_for(hints[member.name])
        has_default = (
            member.default is not dataclasses.MISSING
            or member.default_factory is not dataclasses.MISSING
        )
        if not has_default:
            required.append(member.name)

    return {
        "type": "object",
        "title": cls.__name__,
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def render_json_schema() -> dict[str, Any]:
    """Return the ``PageContext`` JSON Schema, derived from :data:`PAGE_CONTEXT_VARIANTS`.

    The union is a ``oneOf`` over the variant object schemas, each carrying its ``kind`` as a
    ``const``.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "PageContext",
        "description": "What the user is looking at, as a descriptor. Carries no retrieved rows.",
        "x-schema-version": SCHEMA_VERSION,
        "oneOf": [_object_schema(variant) for variant in PAGE_CONTEXT_VARIANTS],
    }


def write_schema(root: Path | None = None) -> Path:
    """Write :func:`render_json_schema` to :data:`SCHEMA_PATH` under ``root`` and return the path.

    Args:
        root: Directory the relative :data:`SCHEMA_PATH` is resolved against. Defaults to the
            current working directory.
    """
    destination = (root or Path.cwd()) / SCHEMA_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(render_json_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return destination


# --------------------------------------------------------------------------------------
# The no-rows predicate the gate applies
# --------------------------------------------------------------------------------------


def array_of_record_paths(schema: Any, *, _path: str = "$") -> list[str]:
    """Return every location in ``schema`` where an array could carry a record.

    An array is row-carrying when its ``items`` schema is absent, of object type, untyped, or
    declares ``properties`` or ``$ref``.

    Args:
        schema: A JSON Schema fragment, or the whole artefact.
        _path: Internal accumulator naming the location being inspected.

    Returns:
        JSON-path-ish strings, one per offending array, in traversal order. Empty means the schema
        holds no field a retrieved row could be assigned to.
    """
    violations: list[str] = []
    if isinstance(schema, list):
        for index, member in enumerate(schema):
            violations += array_of_record_paths(member, _path=f"{_path}[{index}]")
        return violations
    if not isinstance(schema, dict):
        return violations

    declared = schema.get("type")
    declared_types = declared if isinstance(declared, list) else [declared]
    if "array" in declared_types:
        items = schema.get("items")
        if _admits_record(items):
            violations.append(_path)

    for keyword in (
        "properties",
        "additionalProperties",
        "items",
        "oneOf",
        "anyOf",
        "allOf",
        "$defs",
        "definitions",
    ):
        nested = schema.get(keyword)
        if isinstance(nested, dict) and keyword in ("properties", "$defs", "definitions"):
            for name, subschema in nested.items():
                violations += array_of_record_paths(subschema, _path=f"{_path}.{name}")
        elif nested is not None:
            violations += array_of_record_paths(nested, _path=f"{_path}.{keyword}")
    return violations


def _admits_record(items: Any) -> bool:
    """Return whether an array's ``items`` schema could hold an object; absent ``items`` does."""
    if not isinstance(items, dict):
        return True
    declared = items.get("type")
    declared_types = declared if isinstance(declared, list) else [declared]
    if "object" in declared_types:
        return True
    if "properties" in items or "$ref" in items:
        return True
    return declared is None


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def parse_page_context(raw: object) -> PageContext:
    """Decode an untrusted mapping into a :data:`PageContext`.

    Unknown keys are rejected rather than ignored.

    Args:
        raw: The caller-supplied value, of unproven shape.

    Returns:
        The matching variant instance.

    Raises:
        ValidationFailed: ``raw`` is not a mapping, names no recognised ``kind``, omits a required
            field, carries an unknown key, or carries a filter value that is not a scalar.
    """
    if not isinstance(raw, dict):
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context",
            detail=f"page_context must be an object, got {type(raw).__name__}",
        )

    kind = raw.get("kind")
    variant = BY_KIND.get(kind) if isinstance(kind, str) else None
    if variant is None:
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context.kind",
            detail=f"kind={_clip(kind)} is not one of {sorted(BY_KIND)}",
        )

    field_names = {member.name for member in dataclasses.fields(variant)}
    unknown = sorted(set(raw) - field_names - {"kind"})
    if unknown:
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context",
            detail=(
                f"kind={kind!r} carries {len(unknown)} undeclared key(s), first {_clip(unknown[0])}. "
                "PageContext is closed; a row collection cannot be attached to it."
            ),
        )

    kwargs: dict[str, Any] = {}
    for member in dataclasses.fields(variant):
        if member.name not in raw:
            continue
        value = raw[member.name]
        kwargs[member.name] = (
            _parse_query(value) if member.name == "query" else _parse_scalar(member.name, value)
        )

    try:
        return typing.cast("PageContext", variant(**kwargs))
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context",
            detail=f"kind={kind!r} rejected: {type(exc).__name__}: {exc}",
        ) from exc


def _parse_scalar(name: str, value: object) -> object:
    """Return ``value`` unchanged if it is a scalar or ``None``, else raise ``ValidationFailed``."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValidationFailed(
        "The page context was not valid.",
        source=f"page_context.{name}",
        detail=(
            f"{name} must be a scalar, got {type(value).__name__} — PageContext carries no "
            "collections"
        ),
    )


def _parse_query(raw: object) -> QueryDescriptor:
    """Decode a :class:`QueryDescriptor`, rejecting any non-scalar filter value.

    Raises:
        ValidationFailed: ``raw`` is not a mapping, carries an unknown key, or holds a non-scalar
            filter value, or its sort or page is invalid.
    """
    if not isinstance(raw, dict):
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context.query",
            detail=f"query must be an object, got {type(raw).__name__}",
        )
    unknown = sorted(set(raw) - {"filters", "sort", "page"})
    if unknown:
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context.query",
            detail=f"query carries {len(unknown)} undeclared key(s), first {_clip(unknown[0])}",
        )

    filters: dict[str, FilterValue] = {}
    raw_filters = raw.get("filters", {})
    if not isinstance(raw_filters, dict):
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context.query.filters",
            detail=f"filters must be an object, got {type(raw_filters).__name__}",
        )
    for key, value in raw_filters.items():
        if not isinstance(value, (str, int, float, bool)):
            raise ValidationFailed(
                "The page context was not valid.",
                source="page_context.query.filters",
                detail=(
                    f"filter {_clip(key)} is {type(value).__name__}; only scalars are permitted, so "
                    "a row collection cannot travel as a filter value"
                ),
            )
        filters[str(key)] = value

    try:
        return QueryDescriptor(
            filters=filters,
            sort=_parse_sort(raw.get("sort")),
            page=_parse_page(raw.get("page")),
        )
    except ValueError as exc:
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context.query",
            detail=f"query rejected: {exc}",
        ) from exc


def _parse_sort(raw: object) -> SortDescriptor | None:
    """Decode an optional :class:`SortDescriptor`."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context.query.sort",
            detail=f"sort must be an object, got {type(raw).__name__}",
        )
    unknown = sorted(set(raw) - {"field", "direction"})
    if unknown:
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context.query.sort",
            detail=f"sort carries {len(unknown)} undeclared key(s), first {_clip(unknown[0])}",
        )
    direction = raw.get("direction", "asc")
    if direction not in get_args(SortDirection):
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context.query.sort",
            detail=f"direction={_clip(direction)} is not one of {list(get_args(SortDirection))}",
        )
    sort_field = raw.get("field")
    if not isinstance(sort_field, str) or not sort_field:
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context.query.sort",
            detail="sort requires a non-empty string field",
        )
    return SortDescriptor(field=sort_field, direction=typing.cast("SortDirection", direction))


def _parse_page(raw: object) -> PageDescriptor | None:
    """Decode an optional :class:`PageDescriptor`."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context.query.page",
            detail=f"page must be an object, got {type(raw).__name__}",
        )
    unknown = sorted(set(raw) - {"cursor", "limit"})
    if unknown:
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context.query.page",
            detail=f"page carries {len(unknown)} undeclared key(s), first {_clip(unknown[0])}",
        )
    cursor = raw.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context.query.page",
            detail=f"cursor must be a string, got {type(cursor).__name__}",
        )
    limit = raw.get("limit")
    # `bool` is a subclass of `int`, so a `True` limit would otherwise be accepted as 1.
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context.query.page",
            detail=f"limit must be an integer, got {type(limit).__name__}",
        )
    try:
        return PageDescriptor(cursor=cursor, limit=limit)
    except ValueError as exc:
        raise ValidationFailed(
            "The page context was not valid.",
            source="page_context.query.page",
            detail=str(exc),
        ) from exc


def to_dict(context: PageContext) -> dict[str, Any]:
    """Render a :data:`PageContext` back to its wire mapping, discriminator included.

    The result round-trips through :func:`parse_page_context`.
    """
    payload: dict[str, Any] = {"kind": context.kind}
    for member in dataclasses.fields(context):
        value = getattr(context, member.name)
        payload[member.name] = _unwrap(value)
    return payload


def _unwrap(value: Any) -> Any:
    """Recursively convert nested descriptors and mappings to plain JSON-able values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            member.name: _unwrap(getattr(value, member.name))
            for member in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _unwrap(inner) for key, inner in value.items()}
    return value


def render_schema_json() -> str:
    """Return the schema as the exact JSON text :func:`write_schema` persists."""
    return json.dumps(render_json_schema(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    """Write the rendered schema to stdout."""
    sys.stdout.write(render_schema_json())
    return 0


__all__ = [
    "BY_KIND",
    "ENTITY_TYPES",
    "PAGE_CONTEXT_VARIANTS",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "CURSOR_MAX_LENGTH",
    "ENTITY_ID_MAX_LENGTH",
    "FIELD_NAME_MAX_LENGTH",
    "FILTER_MAX_COUNT",
    "FILTER_VALUE_MAX_LENGTH",
    "REASON_MAX_LENGTH",
    "ROUTE_MAX_LENGTH",
    "BoardPageContext",
    "EntityPageContext",
    "EntityType",
    "ListPageContext",
    "OpaquePageContext",
    "PageContext",
    "PageDescriptor",
    "QueryDescriptor",
    "SortDescriptor",
    "array_of_record_paths",
    "parse_page_context",
    "render_json_schema",
    "render_schema_json",
    "to_dict",
    "write_schema",
]


if __name__ == "__main__":
    raise SystemExit(main())

# SPDX-License-Identifier: MIT
"""Tests for the ``PageContext`` closed union: its variants, parsing, and the no-rows property.

:func:`test_no_member_is_an_array_of_record_type` asserts no member can hold a retrieved row;
:func:`test_array_of_record_paths_detects_an_injected_row_collection` is its positive control.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, get_args

import pytest

from crm.errors import ValidationFailed
from crm.toolspec import page_context as pc
from crm.toolspec.definitions import TABLES

# --------------------------------------------------------------------------------------
# The union cannot carry rows
# --------------------------------------------------------------------------------------


def test_no_member_is_an_array_of_record_type() -> None:
    """No variant, and nothing nested in one, declares an array that could hold a record."""
    assert pc.array_of_record_paths(pc.render_json_schema()) == []


def test_the_schema_declares_no_array_at_all() -> None:
    """Nothing in `PageContext` is a collection, checked independently of the no-rows predicate."""
    arrays = _collect_declared_types(pc.render_json_schema())
    assert "array" not in arrays, f"PageContext declared an array type: {sorted(arrays)}"


def _collect_declared_types(schema: Any) -> set[str]:
    """Return every value appearing under a ``type`` keyword anywhere in ``schema``."""
    found: set[str] = set()
    if isinstance(schema, list):
        for member in schema:
            found |= _collect_declared_types(member)
        return found
    if not isinstance(schema, dict):
        return found
    declared = schema.get("type")
    if isinstance(declared, str):
        found.add(declared)
    elif isinstance(declared, list):
        found |= {member for member in declared if isinstance(member, str)}
    for key, value in schema.items():
        if key == "type":
            continue
        found |= _collect_declared_types(value)
    return found


@dataclass(frozen=True, slots=True)
class _Row:
    """A record shape, used only to prove the no-rows predicate fires on a real violation."""

    account_id: str
    name: str


@dataclass(frozen=True, slots=True)
class _RowCarryingContext:
    """A non-compliant variant carrying a collection of records."""

    kind: ClassVar[Literal["rows"]] = "rows"

    route: str
    items: list[_Row] = field(default_factory=list)


def test_array_of_record_paths_detects_an_injected_row_collection() -> None:
    """Verify the verifier: a variant carrying `items: [Record]` is reported, not passed."""
    offending = pc._object_schema(_RowCarryingContext)

    violations = pc.array_of_record_paths(offending)

    assert violations, "an array-of-record field was not detected"
    assert any("items" in path for path in violations), violations


@pytest.mark.parametrize(
    "items",
    [
        pytest.param({"type": "object"}, id="explicit-object"),
        pytest.param({"properties": {"id": {"type": "string"}}}, id="object-by-properties"),
        pytest.param({"$ref": "#/$defs/Row"}, id="reference"),
        pytest.param({}, id="unconstrained-items"),
        pytest.param(None, id="items-absent"),
        pytest.param({"type": ["object", "null"]}, id="nullable-object"),
    ],
)
def test_array_of_record_paths_treats_permissive_items_as_row_carrying(items: Any) -> None:
    """An `items` schema that admits an object is reported, including when `items` is absent."""
    schema: dict[str, Any] = {"type": "array"}
    if items is not None:
        schema["items"] = items

    assert pc.array_of_record_paths(schema) == ["$"]


@pytest.mark.parametrize(
    "items",
    [
        pytest.param({"type": "string"}, id="string"),
        pytest.param({"type": "integer"}, id="integer"),
        pytest.param({"type": ["string", "null"]}, id="nullable-string"),
    ],
)
def test_array_of_scalars_is_not_reported(items: dict[str, Any]) -> None:
    """The predicate targets rows, not arrays as such — a scalar array is not a row collection."""
    assert pc.array_of_record_paths({"type": "array", "items": items}) == []


def test_a_row_collection_nested_deep_inside_a_variant_is_still_found() -> None:
    """The walk reaches nested objects, so burying the array does not evade the check."""
    schema = {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "object",
                        "properties": {"rows": {"type": "array", "items": {"type": "object"}}},
                    }
                },
            }
        ]
    }

    assert pc.array_of_record_paths(schema) == ["$.oneOf[0].query.rows"]


def test_parse_rejects_an_undeclared_key_rather_than_ignoring_it() -> None:
    """The runtime half of the same guarantee: a `rows` array attached to the payload is refused."""
    with pytest.raises(ValidationFailed) as raised:
        pc.parse_page_context(
            {
                "kind": "list",
                "route": "/opportunities",
                "entity_type": "opportunities",
                "rows": [{"opportunity_id": "opp-1"}],
            }
        )

    assert raised.value.detail is not None
    assert "rows" in raised.value.detail


@pytest.mark.parametrize(
    "value",
    [
        pytest.param([{"opportunity_id": "opp-1"}], id="list-of-records"),
        pytest.param({"opportunity_id": "opp-1"}, id="record"),
        pytest.param(["a", "b"], id="list-of-scalars"),
    ],
)
def test_parse_rejects_a_non_scalar_filter_value(value: object) -> None:
    """A filter value is a scalar. A collection there would be a row collection in disguise."""
    with pytest.raises(ValidationFailed):
        pc.parse_page_context(
            {
                "kind": "list",
                "route": "/opportunities",
                "entity_type": "opportunities",
                "query": {"filters": {"stage": value}},
            }
        )


def test_parse_rejects_a_collection_at_a_scalar_field() -> None:
    """`entity_id` takes a scalar; a list there is rejected rather than coerced."""
    with pytest.raises(ValidationFailed):
        pc.parse_page_context(
            {
                "kind": "entity",
                "route": "/accounts/acct-1",
                "entity_type": "accounts",
                "entity_id": ["acct-1", "acct-2"],
            }
        )


# --------------------------------------------------------------------------------------
# The union is closed, and its variants are the four the design names
# --------------------------------------------------------------------------------------


def test_the_union_has_exactly_the_four_declared_variants() -> None:
    """`entity`, `list`, `board`, `opaque` — no more, no fewer."""
    assert [variant.kind for variant in pc.PAGE_CONTEXT_VARIANTS] == [
        "entity",
        "list",
        "board",
        "opaque",
    ]


def test_every_variant_appears_in_the_schema_with_its_kind_as_a_const() -> None:
    """The schema is derived from the variant tuple, so a new variant cannot be omitted from it."""
    schema = pc.render_json_schema()

    kinds = [member["properties"]["kind"]["const"] for member in schema["oneOf"]]

    assert kinds == [variant.kind for variant in pc.PAGE_CONTEXT_VARIANTS]


def test_every_variant_schema_is_closed() -> None:
    """`additionalProperties: false` on every variant — the schema-level form of the closed union."""
    for member in pc.render_json_schema()["oneOf"]:
        assert member["additionalProperties"] is False, member["title"]


def test_the_schema_declares_every_dataclass_field() -> None:
    """A field added to a variant reaches the artefact, which is what the gate can then inspect."""
    by_title = {member["title"]: member for member in pc.render_json_schema()["oneOf"]}

    for variant in pc.PAGE_CONTEXT_VARIANTS:
        declared = set(by_title[variant.__name__]["properties"])
        expected = {member.name for member in dataclasses.fields(variant)} | {"kind"}
        assert declared == expected, variant.__name__


def test_kind_is_a_class_level_discriminator_not_a_settable_field() -> None:
    """`kind` is a `ClassVar`, so no caller can construct a variant claiming another's kind."""
    for variant in pc.PAGE_CONTEXT_VARIANTS:
        assert "kind" not in {member.name for member in dataclasses.fields(variant)}


def test_entity_vocabulary_matches_the_toolspec_table_vocabulary() -> None:
    """`EntityType` and `ENTITY_TYPES` name exactly the toolspec's `TABLES`."""
    assert set(get_args(pc.EntityType)) == set(TABLES)
    assert set(pc.ENTITY_TYPES) == set(TABLES)


# --------------------------------------------------------------------------------------
# The opaque variant
# --------------------------------------------------------------------------------------


def test_the_opaque_variant_exists_and_carries_a_reason() -> None:
    """An inexpressible view is represented by the opaque variant, which carries a reason."""
    context = pc.OpaquePageContext(route="/pipeline", reason="client-only sort by health score")

    assert context.kind == "opaque"
    assert context.reason == "client-only sort by health score"


def test_an_opaque_context_without_a_reason_is_refused() -> None:
    """An opaque context with an empty reason is refused."""
    with pytest.raises(ValueError, match="non-empty reason"):
        pc.OpaquePageContext(route="/pipeline", reason="")


def test_reason_is_required_by_the_schema_too() -> None:
    """The requirement is in the artefact, not only in `__post_init__`."""
    opaque = next(
        member
        for member in pc.render_json_schema()["oneOf"]
        if member["title"] == "OpaquePageContext"
    )

    assert "reason" in opaque["required"]


# --------------------------------------------------------------------------------------
# Construction and parsing
# --------------------------------------------------------------------------------------


def test_every_variant_requires_a_route() -> None:
    """No variant may describe a view without naming where the user is."""
    with pytest.raises(ValueError, match="non-empty route"):
        pc.EntityPageContext(route="", entity_type="accounts", entity_id="acct-1")
    with pytest.raises(ValueError, match="non-empty route"):
        pc.ListPageContext(route="", entity_type="accounts")
    with pytest.raises(ValueError, match="non-empty route"):
        pc.BoardPageContext(route="")
    with pytest.raises(ValueError, match="non-empty route"):
        pc.OpaquePageContext(route="", reason="why")


@pytest.mark.parametrize("kind", ["entity", "list"])
def test_an_unrecognised_entity_type_is_refused(kind: str) -> None:
    """An `entity_type` outside the vocabulary is a `ValidationFailed` naming the field."""
    payload: dict[str, Any] = {"kind": kind, "route": "/x", "entity_type": "reps"}
    if kind == "entity":
        payload["entity_id"] = "rep-1"

    with pytest.raises(ValidationFailed) as raised:
        pc.parse_page_context(payload)

    assert raised.value.detail is not None
    assert "entity_type" in raised.value.detail


@pytest.mark.parametrize("entity_type", ["reps", "audit_records", "", "ACCOUNTS"])
def test_entity_type_is_checked_at_construction_too(entity_type: str) -> None:
    """Enforced on the type, so every construction path is covered — not just the parser."""
    with pytest.raises(ValueError, match="entity_type must be one of"):
        pc.EntityPageContext(route="/x", entity_type=entity_type, entity_id="x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="entity_type must be one of"):
        pc.ListPageContext(route="/x", entity_type=entity_type)  # type: ignore[arg-type]


def test_an_entity_context_requires_a_concrete_entity_id() -> None:
    with pytest.raises(ValueError, match="non-empty entity_id"):
        pc.EntityPageContext(route="/accounts/", entity_type="accounts", entity_id="")


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "kind": "entity",
                "route": "/accounts/acct-1",
                "entity_type": "accounts",
                "entity_id": "acct-1",
            },
            id="entity",
        ),
        pytest.param(
            {
                "kind": "list",
                "route": "/opportunities",
                "entity_type": "opportunities",
                "query": {
                    "filters": {"stage": "Discovery", "include_closed": False, "min_value": 1000},
                    "sort": {"field": "close_date", "direction": "desc"},
                    "page": {"cursor": "c-1", "limit": 25},
                },
            },
            id="list",
        ),
        pytest.param(
            {"kind": "board", "route": "/pipeline", "query": {"filters": {}}},
            id="board",
        ),
        pytest.param(
            {"kind": "opaque", "route": "/pipeline", "reason": "client-only sort"},
            id="opaque",
        ),
    ],
)
def test_parse_round_trips_through_to_dict(payload: dict[str, Any]) -> None:
    """`to_dict(parse(x))` re-parses to the same value, which is the frontend's wire contract."""
    parsed = pc.parse_page_context(payload)

    rendered = pc.to_dict(parsed)

    assert pc.parse_page_context(rendered) == parsed
    assert rendered["kind"] == payload["kind"]
    assert json.loads(json.dumps(rendered)) == rendered


def test_parse_defaults_an_absent_query_to_an_empty_descriptor() -> None:
    """A list view with no filters applied is a real view, not a malformed context."""
    parsed = pc.parse_page_context(
        {"kind": "list", "route": "/accounts", "entity_type": "accounts"}
    )

    assert isinstance(parsed, pc.ListPageContext)
    assert parsed.query == pc.QueryDescriptor()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"route": "/x"}, id="no-kind"),
        pytest.param({"kind": "rows", "route": "/x"}, id="unrecognised-kind"),
        pytest.param({"kind": 4, "route": "/x"}, id="non-string-kind"),
    ],
)
def test_parse_rejects_an_unrecognised_kind(payload: dict[str, Any]) -> None:
    """The union is closed at parse time as well as at type-check time."""
    with pytest.raises(ValidationFailed):
        pc.parse_page_context(payload)


@pytest.mark.parametrize(
    "payload", [pytest.param([], id="list"), pytest.param("entity", id="string")]
)
def test_parse_rejects_a_non_mapping(payload: object) -> None:
    with pytest.raises(ValidationFailed):
        pc.parse_page_context(payload)


def test_parse_rejects_a_missing_required_field() -> None:
    """An `entity` context with no `entity_id` is refused rather than defaulted."""
    with pytest.raises(ValidationFailed):
        pc.parse_page_context(
            {"kind": "entity", "route": "/accounts/acct-1", "entity_type": "accounts"}
        )


#: A value no fixed message contains, so its absence from an error proves nothing was echoed.
_MARKER = "zqxmarker"


def _parse_failure(payload: dict[str, Any]) -> ValidationFailed:
    """Parse ``payload``, which must be refused, and return the raised error."""
    with pytest.raises(ValidationFailed) as raised:
        pc.parse_page_context(payload)
    return raised.value


def _assert_not_echoed(error: ValidationFailed, value: str) -> None:
    """Neither the wire form nor the logged detail of ``error`` carries ``value``."""
    for text in (
        str(error),
        error.detail or "",
        error.source or "",
        json.dumps(error.to_api_response()),
    ):
        assert value not in text


@pytest.mark.parametrize(
    "route",
    [
        "/",
        "/accounts",
        "/accounts/acct-demo-0000",
        "/opportunities/opp-1/activities",
        "/evals/run_1",
        "/" + "a" * (pc.ROUTE_MAX_LENGTH - 1),
    ],
)
def test_a_route_within_the_bound_and_charset_is_accepted(route: str) -> None:
    """Every SPA path shape, and a route exactly at the bound, parses."""
    context = pc.parse_page_context({"kind": "board", "route": route})
    assert context.route == route


@pytest.mark.parametrize(
    "route",
    [
        pytest.param("/" + "a" * pc.ROUTE_MAX_LENGTH, id="one-over-the-bound"),
        pytest.param("/" + "a" * 100_000, id="far-over-the-bound"),
        pytest.param(f"{_MARKER}/accounts", id="relative"),
        pytest.param(f"/accounts?{_MARKER}=1", id="query-string"),
        pytest.param(f"/accounts#{_MARKER}", id="fragment"),
        pytest.param(f"/a {_MARKER}", id="space"),
        pytest.param(f"/a\n{_MARKER}", id="newline"),
        pytest.param(f"/{_MARKER}\n", id="trailing-newline"),
        pytest.param(f"/{_MARKER}.html", id="dot"),
        pytest.param(f"/%2e{_MARKER}", id="percent-encoding"),
        pytest.param(f"/caf\u00e9{_MARKER}", id="non-ascii"),
    ],
)
def test_a_route_outside_the_bound_or_charset_is_refused_without_echo(route: str) -> None:
    """Over-length or off-charset routes are `ValidationFailed` with a fixed, value-free message."""
    error = _parse_failure({"kind": "board", "route": route})

    assert error.detail is not None
    assert "route must be at most" in error.detail
    _assert_not_echoed(error, _MARKER if _MARKER in route else route)


def test_a_non_string_route_is_refused() -> None:
    """A scalar that is not a string cannot stand in for a route."""
    _parse_failure({"kind": "board", "route": 7})


def test_a_reason_at_the_bound_is_accepted() -> None:
    """A `reason` of exactly the bound parses; the SPA's own reasons are far shorter."""
    reason = "r" * pc.REASON_MAX_LENGTH
    context = pc.parse_page_context({"kind": "opaque", "route": "/pipeline", "reason": reason})
    assert isinstance(context, pc.OpaquePageContext)
    assert context.reason == reason


@pytest.mark.parametrize("length", [pc.REASON_MAX_LENGTH + 1, 100_000])
def test_a_reason_over_the_bound_is_refused_without_echo(length: int) -> None:
    """An over-long `reason` is refused with a fixed message that carries none of it."""
    reason = _MARKER + "r" * (length - len(_MARKER))
    error = _parse_failure({"kind": "opaque", "route": "/pipeline", "reason": reason})

    assert error.detail is not None
    assert "reason must be a string of at most" in error.detail
    _assert_not_echoed(error, _MARKER)


def test_a_non_string_reason_is_refused() -> None:
    """A number is not a reason."""
    _parse_failure({"kind": "opaque", "route": "/pipeline", "reason": 3})


@pytest.mark.parametrize(
    "entity_id",
    [
        "acct-1",
        "acct-demo-0000",
        "act-eval-abc-defect-pd01",
        "opp-0b6f8c1e-6a52-4d0e-9c47-6f2ad0c1b8e3",
        "a" * pc.ENTITY_ID_MAX_LENGTH,
    ],
)
def test_an_entity_id_in_the_store_key_shape_is_accepted(entity_id: str) -> None:
    """Fixture-generated, handler-generated and bound-length ids all parse."""
    context = pc.parse_page_context(
        {
            "kind": "entity",
            "route": "/accounts/x",
            "entity_type": "accounts",
            "entity_id": entity_id,
        }
    )
    assert isinstance(context, pc.EntityPageContext)
    assert context.entity_id == entity_id


@pytest.mark.parametrize(
    "entity_id",
    [
        pytest.param("a" * (pc.ENTITY_ID_MAX_LENGTH + 1), id="one-over-the-bound"),
        pytest.param(f"acct#{_MARKER}", id="key-separator"),
        pytest.param(f"-{_MARKER}", id="leading-dash"),
        pytest.param(f"acct/{_MARKER}", id="slash"),
        pytest.param(f"acct {_MARKER}", id="space"),
        pytest.param(f"{_MARKER}\n", id="trailing-newline"),
        pytest.param(f"{_MARKER}'; drop", id="quote"),
    ],
)
def test_an_entity_id_outside_the_store_key_shape_is_refused_without_echo(entity_id: str) -> None:
    """An id the store could not have issued is refused with a fixed, value-free message."""
    error = _parse_failure(
        {
            "kind": "entity",
            "route": "/accounts/x",
            "entity_type": "accounts",
            "entity_id": entity_id,
        }
    )

    assert error.detail is not None
    assert "entity_id must be at most" in error.detail
    _assert_not_echoed(error, _MARKER if _MARKER in entity_id else entity_id)


def _list_payload(**query: Any) -> dict[str, Any]:
    return {"kind": "list", "route": "/accounts", "entity_type": "accounts", "query": query}


def test_a_query_at_every_bound_is_accepted() -> None:
    """The maximum filter count, field-name, value and cursor lengths all parse."""
    filters: dict[str, Any] = {
        f"f{n}": "v" * pc.FILTER_VALUE_MAX_LENGTH for n in range(pc.FILTER_MAX_COUNT)
    }
    filters["f0"] = "O'Brien & Co — Zürich"
    payload = _list_payload(
        filters=filters,
        sort={"field": "a" * pc.FIELD_NAME_MAX_LENGTH, "direction": "desc"},
        page={"cursor": "eyJrIjoidiJ9" + "A" * (pc.CURSOR_MAX_LENGTH - 14) + "==", "limit": 25},
    )
    context = pc.parse_page_context(payload)
    assert isinstance(context, pc.ListPageContext)
    assert len(context.query.filters) == pc.FILTER_MAX_COUNT


@pytest.mark.parametrize(
    ("query", "rule"),
    [
        pytest.param(
            {"filters": {f"f{n}": 1 for n in range(pc.FILTER_MAX_COUNT + 1)}},
            "more than",
            id="too-many-filters",
        ),
        pytest.param(
            {"filters": {"a" * (pc.FIELD_NAME_MAX_LENGTH + 1): 1}}, "filter key", id="long-key"
        ),
        pytest.param({"filters": {f"{_MARKER} x": 1}}, "filter key", id="key-space"),
        pytest.param({"filters": {f"{_MARKER}\nignore": 1}}, "filter key", id="key-newline"),
        pytest.param({"filters": {f"Upper{_MARKER}": 1}}, "filter key", id="key-upper"),
        pytest.param(
            {"filters": {"query": _MARKER + "v" * pc.FILTER_VALUE_MAX_LENGTH}},
            "string filter value",
            id="long-value",
        ),
        pytest.param(
            {"filters": {"query": f"{_MARKER}\nignore prior"}},
            "string filter value",
            id="value-newline",
        ),
        pytest.param(
            {"filters": {"query": f"{_MARKER}\u2028x"}},
            "string filter value",
            id="value-line-separator",
        ),
        pytest.param(
            {"filters": {"query": f"{_MARKER}\x00"}}, "string filter value", id="value-nul"
        ),
        pytest.param({"filters": {"min_value": float("inf")}}, "finite", id="value-infinite"),
        pytest.param({"filters": {"min_value": float("nan")}}, "finite", id="value-nan"),
        pytest.param(
            {"sort": {"field": f"{_MARKER}; drop", "direction": "asc"}},
            "sort field",
            id="sort-charset",
        ),
        pytest.param(
            {"sort": {"field": "a" * (pc.FIELD_NAME_MAX_LENGTH + 1)}}, "sort field", id="sort-long"
        ),
        pytest.param({"page": {"cursor": f"{_MARKER}!*"}}, "cursor must be", id="cursor-charset"),
        pytest.param(
            {"page": {"cursor": "A" * (pc.CURSOR_MAX_LENGTH + 1)}},
            "cursor must be",
            id="cursor-long",
        ),
    ],
)
def test_a_query_outside_its_bounds_is_refused_without_echo(
    query: dict[str, Any], rule: str
) -> None:
    """Every over-bound or off-charset query part is a `ValidationFailed` with a fixed message."""
    error = _parse_failure(_list_payload(**query))

    assert error.detail is not None
    assert rule in error.detail
    _assert_not_echoed(error, _MARKER)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"kind": "x" * 10_000}, id="kind"),
        pytest.param({"kind": "board", "route": "/a", "x" * 10_000: 1}, id="undeclared-key"),
        pytest.param({"kind": "board", "route": "/a", "query": {"x" * 10_000: 1}}, id="query-key"),
        pytest.param(
            {"kind": "board", "route": "/a", "query": {"filters": {"x" * 10_000: [1]}}},
            id="filter-key",
        ),
        pytest.param(
            {
                "kind": "board",
                "route": "/a",
                "query": {"sort": {"field": "a", "direction": "x" * 10_000}},
            },
            id="direction",
        ),
        pytest.param(
            {"kind": "entity", "route": "/a", "entity_type": "x" * 10_000, "entity_id": "a"},
            id="entity-type",
        ),
    ],
)
def test_a_detail_string_carries_a_bounded_caller_value(payload: dict[str, Any]) -> None:
    """A rejected caller value appears in the logged detail at most clipped, never whole."""
    error = _parse_failure(payload)

    assert error.detail is not None
    assert "x" * 100 not in error.detail
    assert len(error.detail) < 400


def test_the_query_bounds_hold_on_direct_construction_too() -> None:
    """Enforced on the descriptor types, so no construction path skips them."""
    with pytest.raises(ValueError, match="filter key"):
        pc.QueryDescriptor(filters={"Bad Key": 1})
    with pytest.raises(ValueError, match="sort field"):
        pc.SortDescriptor(field="a b")
    with pytest.raises(ValueError, match="cursor must be"):
        pc.PageDescriptor(cursor="a b")


def test_the_bounds_hold_on_direct_construction_too() -> None:
    """Enforced on the types, so no construction path skips them."""
    with pytest.raises(ValueError, match="route must be at most"):
        pc.BoardPageContext(route="/a?b")
    with pytest.raises(ValueError, match="reason must be a string of at most"):
        pc.OpaquePageContext(route="/a", reason="r" * (pc.REASON_MAX_LENGTH + 1))
    with pytest.raises(ValueError, match="entity_id must be at most"):
        pc.EntityPageContext(route="/a", entity_type="accounts", entity_id="a#b")


def test_parse_rejects_an_empty_route() -> None:
    """`__post_init__`'s `ValueError` becomes a taxonomy member, never escapes as a `ValueError`."""
    with pytest.raises(ValidationFailed):
        pc.parse_page_context({"kind": "board", "route": ""})


@pytest.mark.parametrize(
    "sort",
    [
        pytest.param({"field": "close_date", "direction": "sideways"}, id="bad-direction"),
        pytest.param({"field": ""}, id="empty-field"),
        pytest.param({"direction": "asc"}, id="no-field"),
        pytest.param({"field": "x", "rows": []}, id="undeclared-key"),
        pytest.param("close_date", id="not-an-object"),
    ],
)
def test_parse_rejects_a_malformed_sort(sort: object) -> None:
    with pytest.raises(ValidationFailed):
        pc.parse_page_context({"kind": "board", "route": "/pipeline", "query": {"sort": sort}})


@pytest.mark.parametrize(
    "page",
    [
        pytest.param({"limit": 0}, id="zero-limit"),
        pytest.param({"limit": -5}, id="negative-limit"),
        pytest.param({"limit": True}, id="bool-limit"),
        pytest.param({"limit": "25"}, id="string-limit"),
        pytest.param({"cursor": 7}, id="non-string-cursor"),
        pytest.param({"rows": []}, id="undeclared-key"),
        pytest.param([], id="not-an-object"),
    ],
)
def test_parse_rejects_a_malformed_page(page: object) -> None:
    """`bool` is an `int` subclass, so `limit: true` must be refused explicitly."""
    with pytest.raises(ValidationFailed):
        pc.parse_page_context({"kind": "board", "route": "/pipeline", "query": {"page": page}})


def test_parse_rejects_a_query_that_is_not_an_object() -> None:
    with pytest.raises(ValidationFailed):
        pc.parse_page_context({"kind": "board", "route": "/pipeline", "query": []})


def test_parse_rejects_an_undeclared_query_key() -> None:
    with pytest.raises(ValidationFailed):
        pc.parse_page_context(
            {"kind": "board", "route": "/pipeline", "query": {"filters": {}, "rows": []}}
        )


def test_parse_rejects_filters_that_are_not_an_object() -> None:
    with pytest.raises(ValidationFailed):
        pc.parse_page_context(
            {"kind": "board", "route": "/pipeline", "query": {"filters": [{"stage": "x"}]}}
        )


# --------------------------------------------------------------------------------------
# Schema generation is derived, and fails loudly rather than silently omitting a field
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _UnmappedAnnotation:
    """A variant carrying a type the generator has no mapping for."""

    route: str
    when: complex = 0j


def test_schema_generation_raises_on_an_unmapped_annotation() -> None:
    """A field the generator cannot render raises rather than being left out of the artefact."""
    with pytest.raises(TypeError, match="no PageContext schema mapping"):
        pc._object_schema(_UnmappedAnnotation)


def test_write_schema_emits_parseable_json_at_the_declared_path(tmp_path: Any) -> None:
    """The gate reads a file; this is the file it reads."""
    written = pc.write_schema(root=tmp_path)

    assert written == tmp_path / pc.SCHEMA_PATH
    assert json.loads(written.read_text(encoding="utf-8")) == pc.render_json_schema()


def test_write_schema_is_deterministic(tmp_path: Any) -> None:
    """Two renders are byte-identical, so a parity gate diffing the artefact cannot flap."""
    first = pc.write_schema(root=tmp_path).read_text(encoding="utf-8")
    second = pc.write_schema(root=tmp_path).read_text(encoding="utf-8")

    assert first == second


def test_the_artefact_carries_its_schema_version() -> None:
    """The rendered schema carries `x-schema-version` equal to `SCHEMA_VERSION`."""
    assert pc.render_json_schema()["x-schema-version"] == pc.SCHEMA_VERSION


def test_optional_fields_admit_null_in_the_schema() -> None:
    """`cursor`/`limit`/`sort`/`page` are genuinely optional, and the artefact says so."""
    schema = pc._object_schema(pc.QueryDescriptor)

    assert "null" in schema["properties"]["sort"]["type"]
    assert "null" in schema["properties"]["page"]["type"]
    assert schema["required"] == []


def test_filter_values_are_scalars_in_the_schema() -> None:
    """The artefact declares the scalar restriction, not just the parser."""
    filters = pc._object_schema(pc.QueryDescriptor)["properties"]["filters"]

    assert filters["type"] == "object"
    assert set(filters["additionalProperties"]["type"]) == {
        "string",
        "integer",
        "number",
        "boolean",
    }

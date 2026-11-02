# SPDX-License-Identifier: MIT
"""Tests for the typed-array properties of the SSE frame schema (schema 3.0.0)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal, TypedDict, get_type_hints

import pytest
from jsonschema import Draft202012Validator

from agents import event_schema
from agents.event_schema import SCHEMA_VERSION, build_event_schema
from agents.events import AgentEvent, CostLine, ResolvedTierInfo
from agents.runtime_entrypoint import format_sse_line

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RELAY_TYPES = _REPO_ROOT / "src" / "relay" / "src" / "generated" / "events.ts"

_ARRAY_PROPERTIES: dict[str, type[Any]] = {
    "cost_lines": CostLine,
    "resolved_tiers": ResolvedTierInfo,
}


@pytest.fixture(name="schema")
def _schema() -> dict[str, Any]:
    return build_event_schema()


def _frame(**accounting: object) -> dict[str, Any]:
    event = AgentEvent(
        kind="completed",
        text="done",
        input_tokens=3,
        output_tokens=2,
        **accounting,  # type: ignore[arg-type]
    )
    parsed: dict[str, Any] = json.loads(format_sse_line(event).removeprefix("data: "))
    return parsed


_COST_LINE: CostLine = {
    "tier": "tool",
    "role": "research",
    "model_id": "m",
    "input_tokens": 3,
    "output_tokens": 2,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "cost_usd": None,
}
_RESOLVED: ResolvedTierInfo = {
    "tier": "tool",
    "family": "anthropic",
    "model_id": "m",
    "effort": None,
}


def test_schema_version_is_3_0_0() -> None:
    """The frame contract is at 3.0.0."""
    assert SCHEMA_VERSION == "3.0.0"


@pytest.mark.parametrize("name", sorted(_ARRAY_PROPERTIES))
def test_array_property_is_a_nullable_array_of_the_typed_dict(
    name: str, schema: dict[str, Any]
) -> None:
    """Each cost/tier array is ``array | null`` of a closed object derived from its ``TypedDict``."""
    typed_dict = _ARRAY_PROPERTIES[name]
    prop = schema["properties"][name]
    assert prop["type"] == ["array", "null"]
    items = prop["items"]
    assert items["type"] == "object"
    assert items["title"] == typed_dict.__name__
    assert items["additionalProperties"] is False
    hints = get_type_hints(typed_dict)
    assert items["required"] == list(hints)
    assert list(items["properties"]) == list(hints)


def test_element_enums_come_from_the_literal_annotations(schema: dict[str, Any]) -> None:
    """``tier`` and ``family`` render as enums of their ``Literal`` members, not as strings."""
    cost = schema["properties"]["cost_lines"]["items"]["properties"]
    resolved = schema["properties"]["resolved_tiers"]["items"]["properties"]
    assert cost["tier"] == {"enum": ["supervisor", "subagent", "tool"]}
    assert resolved["family"] == {"enum": ["anthropic", "openai"]}
    assert cost["cost_usd"] == {"type": ["number", "null"]}
    assert "temperature" not in resolved


def test_scalar_cost_fields(schema: dict[str, Any]) -> None:
    """``cost_usd`` is a nullable number and ``cost_withheld`` a nullable boolean."""
    assert schema["properties"]["cost_usd"] == {"type": ["number", "null"]}
    assert schema["properties"]["cost_withheld"] == {"type": ["boolean", "null"]}


def test_real_frame_with_cost_lines_validates(schema: dict[str, Any]) -> None:
    """A ``completed`` frame the Runtime can emit with every cost/tier field validates."""
    frame = _frame(
        cost_lines=(_COST_LINE,),
        cost_usd=None,
        cost_withheld=True,
        resolved_tiers=(_RESOLVED,),
    )
    Draft202012Validator(schema).validate(frame)


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("cost_lines", lambda line: {**line, "rows": []}),
        ("cost_lines", lambda line: {**line, "tier": "orchestrator"}),
        ("cost_lines", lambda line: {k: v for k, v in line.items() if k != "model_id"}),
        ("cost_lines", lambda line: {**line, "input_tokens": "3"}),
        ("resolved_tiers", lambda tier: {**tier, "family": "gemini"}),
        ("resolved_tiers", lambda tier: {**tier, "effort": 3}),
        ("resolved_tiers", lambda tier: {**tier, "temperature": None}),
    ],
)
def test_malformed_element_is_rejected(name: str, mutate: Any, schema: dict[str, Any]) -> None:
    """An element with an extra key, a missing key, or an off-contract value is invalid."""
    frame = _frame(cost_lines=(_COST_LINE,), resolved_tiers=(_RESOLVED,))
    frame[name] = [mutate(frame[name][0])]
    assert not Draft202012Validator(schema).is_valid(frame)


def test_non_array_value_is_rejected(schema: dict[str, Any]) -> None:
    """A bare object where an array is declared is invalid."""
    frame = _frame(cost_lines=(_COST_LINE,))
    frame["cost_lines"] = _COST_LINE
    assert not Draft202012Validator(schema).is_valid(frame)


class _Element(TypedDict):
    tier: Literal["a", "b"]
    weight: float


def test_property_schema_maps_tuple_of_typed_dict() -> None:
    """``tuple[TD, ...] | None`` renders as a nullable typed array; the mapping is generic."""
    rendered = event_schema._property_schema(tuple[_Element, ...] | None)
    assert rendered == {
        "type": ["array", "null"],
        "items": {
            "type": "object",
            "title": "_Element",
            "additionalProperties": False,
            "properties": {"tier": {"enum": ["a", "b"]}, "weight": {"type": "number"}},
            "required": ["tier", "weight"],
        },
    }


@pytest.mark.parametrize(
    "hint",
    [
        tuple[_Element, int],
        tuple[_Element, ...] | int,
        tuple[_Element, ...] | _Element,
        tuple[bytes, ...],
    ],
    ids=["fixed-tuple", "array-or-scalar", "two-structured", "unmapped-element"],
)
def test_property_schema_refuses_unrepresentable_annotations(hint: object) -> None:
    """Fixed tuples, mixed unions and unmapped element types raise rather than degrade."""
    with pytest.raises(TypeError):
        event_schema._property_schema(hint)


def _interface_fields(text: str, name: str) -> set[str]:
    match = re.search(rf"export interface {name} \{{(?P<body>[^}}]*)\}}", text)
    assert match is not None, f"no interface {name}"
    return set(re.findall(r"readonly (\w+):", match.group("body")))


def test_relay_consumer_declares_typed_element_interfaces() -> None:
    """The committed relay types declare each element interface with the schema's fields."""
    text = _RELAY_TYPES.read_text(encoding="utf-8")
    assert f'SCHEMA_VERSION = "{SCHEMA_VERSION}"' in text
    for name, typed_dict in _ARRAY_PROPERTIES.items():
        assert _interface_fields(text, typed_dict.__name__) == set(get_type_hints(typed_dict))
        assert f"readonly {name}: ReadonlyArray<{typed_dict.__name__}> | null;" in text

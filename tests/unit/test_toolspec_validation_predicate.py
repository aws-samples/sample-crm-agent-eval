# SPDX-License-Identifier: MIT
"""`arguments_conform` accepts and rejects exactly what `validate_arguments` does, for every tool.

Each tool's cases are derived from its own `input_schema`: a minimal valid instance, the empty
object, each required property removed, an undeclared key, and a type, enum, length or bound
violation per declared property.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from crm.errors import ValidationFailed
from crm.toolspec.definitions import ALL_TOOLS, ToolDefinition
from crm.toolspec.validation import CORRECTION_HINT, arguments_conform, validate_arguments


def _valid_value(schema: Mapping[str, Any]) -> Any:
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "string":
        return "x" * max(1, int(schema.get("minLength", 1)))
    if kind == "integer":
        return int(schema.get("minimum", 1))
    if kind == "number":
        return float(schema.get("minimum", 1))
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "object":
        return _minimal_instance(schema)
    return "x"


def _minimal_instance(schema: Mapping[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties", {})
    return {name: _valid_value(properties[name]) for name in schema.get("required", [])}


def _invalid_values(schema: Mapping[str, Any]) -> Iterator[Any]:
    kind = schema.get("type")
    yield {"wrong": "type"} if kind != "object" else "wrong-type"
    if "enum" in schema:
        yield "__not_in_enum__"
    if "minLength" in schema and int(schema["minLength"]) > 0:
        yield ""
    if "minimum" in schema:
        yield schema["minimum"] - 1
    if "maximum" in schema:
        yield schema["maximum"] + 1


def _cases(tool: ToolDefinition) -> Iterator[tuple[str, dict[str, Any]]]:
    schema = tool.input_schema
    base = _minimal_instance(schema)
    yield "minimal", base
    yield "empty", {}
    for name in schema.get("required", []):
        missing = dict(base)
        missing.pop(name)
        yield f"missing:{name}", missing
    yield "undeclared", {**base, "__undeclared__": 1}
    for name, prop in schema.get("properties", {}).items():
        valid = {**base, name: _valid_value(prop)}
        yield f"valid:{name}", valid
        for index, bad in enumerate(_invalid_values(prop)):
            yield f"invalid:{name}:{index}", {**base, name: copy.deepcopy(bad)}


def _validate_accepts(tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
    try:
        validate_arguments(tool, arguments)
    except ValidationFailed:
        return False
    return True


_ALL_CASES = [
    pytest.param(tool, arguments, id=f"{tool.name}-{label}")
    for tool in ALL_TOOLS
    for label, arguments in _cases(tool)
]


@pytest.mark.parametrize(("tool", "arguments"), _ALL_CASES)
def test_predicate_agrees_with_validate_arguments(
    tool: ToolDefinition, arguments: dict[str, Any]
) -> None:
    assert arguments_conform(tool.name, arguments) is _validate_accepts(tool, arguments)


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=[tool.name for tool in ALL_TOOLS])
def test_every_tool_has_both_an_accepted_and_a_rejected_case(tool: ToolDefinition) -> None:
    """The agreement cells discriminate: each tool contributes at least one of each verdict."""
    verdicts = {arguments_conform(tool.name, arguments) for _, arguments in _cases(tool)}
    assert verdicts == {True, False}


def test_an_unknown_tool_name_does_not_conform() -> None:
    assert arguments_conform("no_such_tool", {}) is False


def test_predicate_never_raises_on_a_violation() -> None:
    tool = ALL_TOOLS[0]
    assert arguments_conform(tool.name, {"__undeclared__": object()}) is False


#: A marker no schema contains; planted as every caller-controlled string in the cells below.
_PLANTED = "PLANTED-caller-value"


def _planted_cases(tool: ToolDefinition) -> Iterator[tuple[str, dict[str, Any]]]:
    """Rejected instances whose every caller-controlled string is the planted marker."""
    base = _minimal_instance(tool.input_schema)
    yield "undeclared", {**base, _PLANTED: _PLANTED}
    for name, prop in tool.input_schema.get("properties", {}).items():
        wrong: Any = {_PLANTED: _PLANTED} if prop.get("type") != "object" else _PLANTED
        yield f"wrong:{name}", {**base, name: wrong}
        if "enum" in prop:
            yield f"enum:{name}", {**base, name: _PLANTED}


_PLANTED_CASES = [
    pytest.param(tool, arguments, id=f"{tool.name}-{label}")
    for tool in ALL_TOOLS
    for label, arguments in _planted_cases(tool)
]


@pytest.mark.parametrize(("tool", "arguments"), _PLANTED_CASES)
def test_no_rejection_message_or_source_echoes_a_caller_value(
    tool: ToolDefinition, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ValidationFailed) as raised:
        validate_arguments(tool, arguments)
    error = raised.value
    assert _PLANTED not in error.message
    assert _PLANTED not in (error.source or "")
    assert _PLANTED not in (error.detail or "")
    assert error.message.endswith(CORRECTION_HINT)
    assert error.source in set(tool.input_schema.get("properties", {})) | {"arguments"}

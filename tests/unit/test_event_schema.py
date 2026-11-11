# SPDX-License-Identifier: MIT
"""Tests for the SSE frame JSON Schema artefact.

Every enum and property name in the schema is derived from :mod:`agents.events` and
:func:`agents.runtime_entrypoint.event_payload`; expected values are recovered by parsing
``src/agents/events.py`` itself.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from scripts import proc_run

from agents.event_schema import (
    ARTEFACT_RELPATH,
    SCHEMA_VERSION,
    build_event_schema,
    render_schema_json,
)
from agents.events import (
    EVENT_KINDS,
    TOOL_RESULT_BYTE_CEILING,
    TRUNCATION_MARKER,
    AgentEvent,
    budget_exhausted_event,
    truncate_tool_output,
)
from agents.runtime_entrypoint import event_payload, format_sse_line

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EVENTS_SOURCE = _REPO_ROOT / "src" / "agents" / "events.py"
_SCHEMA_SOURCE = _REPO_ROOT / "src" / "agents" / "event_schema.py"


def _module_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _assigned_value(tree: ast.Module, name: str) -> ast.expr:
    """Return the value assigned to a module-level ``name``, annotated or not."""
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name and node.value is not None:
                return node.value
        elif isinstance(node, ast.Assign):
            for assign_target in node.targets:
                if isinstance(assign_target, ast.Name) and assign_target.id == name:
                    return node.value
    raise AssertionError(f"{name} is no longer assigned at module level in {_EVENTS_SOURCE}")


def _string_literals(node: ast.AST) -> list[str]:
    return [
        element.value
        for element in ast.walk(node)
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    ]


def _declared_kinds_from_source(name: str) -> list[str]:
    """Recover a ``Literal[...]`` alias's members from ``events.py``'s source text."""
    value = _assigned_value(_module_ast(_EVENTS_SOURCE), name)
    subscript = value
    assert isinstance(subscript, ast.Subscript), f"{name} is no longer a Literal[...] alias"
    return _string_literals(subscript.slice)


@pytest.fixture(name="schema")
def _schema() -> dict[str, object]:
    return build_event_schema()


# --------------------------------------------------------------------------------------
# The enums are derived from events.py, not transcribed
# --------------------------------------------------------------------------------------


def test_kind_enum_matches_the_event_kinds_declared_in_events_py(schema: dict[str, object]) -> None:
    """``kind``'s enum is exactly ``EventKind``'s members as they appear in ``events.py``."""
    declared = _declared_kinds_from_source("EventKind")
    assert declared, "EventKind declares no members"
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["kind"] == {"enum": declared}


def test_event_kinds_tuple_and_event_kind_literal_cannot_drift() -> None:
    """``EVENT_KINDS`` and the ``EventKind`` alias in ``events.py`` list the same kinds."""
    tuple_members = _string_literals(_assigned_value(_module_ast(_EVENTS_SOURCE), "EVENT_KINDS"))
    assert tuple_members == _declared_kinds_from_source("EventKind")
    assert tuple_members == list(EVENT_KINDS)


def test_error_kind_enum_matches_events_py_and_stays_nullable(schema: dict[str, object]) -> None:
    """``error_kind``'s enum is ``ErrorKind``'s declared members plus ``null``."""
    declared = _declared_kinds_from_source("ErrorKind")
    assert declared, "ErrorKind declares no members"
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["error_kind"] == {"enum": [*declared, None]}


def test_schema_module_names_no_event_kind_of_its_own() -> None:
    """``event_schema.py`` contains no event-kind or error-kind string literal."""
    every_kind = set(_declared_kinds_from_source("EventKind")) | set(
        _declared_kinds_from_source("ErrorKind")
    )
    transcribed = sorted(every_kind.intersection(_string_literals(_module_ast(_SCHEMA_SOURCE))))
    assert transcribed == []


# --------------------------------------------------------------------------------------
# The property set is the wire payload's key set
# --------------------------------------------------------------------------------------


def test_properties_and_required_match_the_wire_payload(schema: dict[str, object]) -> None:
    """Every key ``event_payload`` emits is a required property, and nothing else is a property."""
    wire_keys = list(event_payload(budget_exhausted_event(partial_work="w", error_message="m")))
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert list(properties) == wire_keys
    assert schema["required"] == wire_keys
    assert schema["additionalProperties"] is False


def test_schema_carries_its_version(schema: dict[str, object]) -> None:
    """The artefact carries ``SCHEMA_VERSION`` and a matching ``$id``."""
    assert schema["schema_version"] == SCHEMA_VERSION
    assert SCHEMA_VERSION
    assert schema["$id"] == f"urn:crm-agent-eval:sse-event:{SCHEMA_VERSION}"


def test_framing_is_recovered_from_format_sse_line(schema: dict[str, object]) -> None:
    """The prefix and delimiter in the artefact are the ones ``format_sse_line`` actually emits."""
    frame = format_sse_line(budget_exhausted_event(partial_work="w", error_message="m"))
    prefix = schema["x-frame-prefix"]
    delimiter = schema["x-frame-delimiter"]
    assert isinstance(prefix, str) and isinstance(delimiter, str)
    assert frame.startswith(prefix)
    assert frame.endswith(delimiter)
    assert delimiter == "\n\n"


def test_truncation_constants_come_from_events_module(schema: dict[str, object]) -> None:
    """Consumers read the truncation marker from the artefact instead of copying the literal."""
    constants = schema["x-constants"]
    assert constants == {
        "truncation_marker": TRUNCATION_MARKER,
        "tool_result_byte_ceiling": TOOL_RESULT_BYTE_CEILING,
    }


# --------------------------------------------------------------------------------------
# The schema accepts every real frame and rejects what the contract forbids
# --------------------------------------------------------------------------------------


def _one_event_per_kind() -> list[AgentEvent]:
    """One real event for each declared kind, so no kind goes unvalidated."""
    truncated_items, truncated = truncate_tool_output(
        [{"id": f"rec-{index}", "pad": "x" * 512} for index in range(200)]
    )
    assert truncated, "the truncation fixture no longer truncates"
    events = [
        AgentEvent(kind="tool_call", tool_name="list_accounts", tool_input={"limit": 5}),
        AgentEvent(kind="tool_result", tool_output={"items": truncated_items}, truncated=True),
        AgentEvent(
            kind="proposal", proposal_id="p-1", proposal_summary={"before": {}, "after": {}}
        ),
        AgentEvent(kind="usage", input_tokens=11, output_tokens=7),
        AgentEvent(kind="completed", text="done", input_tokens=11, output_tokens=7),
        budget_exhausted_event(
            partial_work="one write committed", error_message="turn cap reached"
        ),
        AgentEvent(kind="text_delta", text="Hel", event_id="ans-1", partial=True),
        AgentEvent(kind="heartbeat"),
        AgentEvent(
            kind="usage",
            input_tokens=11,
            output_tokens=7,
            cost_lines=(
                {
                    "tier": "supervisor",
                    "role": "supervisor",
                    "model_id": "m",
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "long_context_calls": 0,
                    "cost_usd": 0.001,
                },
            ),
            cost_usd=0.001,
            cost_withheld=False,
            resolved_tiers=(
                {
                    "tier": "supervisor",
                    "family": "anthropic",
                    "model_id": "m",
                    "effort": "low",
                },
            ),
        ),
    ]
    assert {event.kind for event in events} == set(EVENT_KINDS)
    return events


@pytest.mark.parametrize("event", _one_event_per_kind(), ids=lambda event: str(event.kind))
def test_every_real_event_validates_against_the_schema(
    event: AgentEvent, schema: dict[str, object]
) -> None:
    """A frame the Runtime can actually emit is valid under the artefact, for every kind."""
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads(format_sse_line(event).removeprefix("data: ")))


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("kind", "delegation"),
        ("kind", "completion"),
        ("kind", "budget_exhausted"),
        ("error_kind", "rate_limited"),
    ],
)
def test_schema_rejects_a_kind_outside_the_closed_set(
    field_name: str, value: str, schema: dict[str, object]
) -> None:
    """Kinds outside ``EventKind`` and an invented ``ErrorKind`` are invalid."""
    frame = json.loads(
        format_sse_line(budget_exhausted_event(partial_work="w", error_message="m")).removeprefix(
            "data: "
        )
    )
    frame[field_name] = value
    validator = Draft202012Validator(schema)
    assert not validator.is_valid(frame)


def test_schema_rejects_an_unknown_field(schema: dict[str, object]) -> None:
    """A field outside the wire shape is invalid, so a consumer's generated type stays exact."""
    frame = json.loads(
        format_sse_line(budget_exhausted_event(partial_work="w", error_message="m")).removeprefix(
            "data: "
        )
    )
    frame["rows"] = [{"id": "acc-1"}]
    assert not Draft202012Validator(schema).is_valid(frame)


# --------------------------------------------------------------------------------------
# Rendering: deterministic, and what the module entry point emits
# --------------------------------------------------------------------------------------


def test_render_is_deterministic_and_parses_back_to_the_schema() -> None:
    """Two renders are byte-identical, so a parity gate can diff the artefact."""
    first = render_schema_json()
    assert first == render_schema_json()
    assert first.endswith("\n")
    assert json.loads(first) == build_event_schema()


def test_module_entry_point_emits_the_artefact() -> None:
    """``python -m agents.event_schema`` writes exactly the rendered artefact to stdout."""
    completed = proc_run.run(
        [sys.executable, "-m", "agents.event_schema"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONPATH": str(_REPO_ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert completed.stdout == render_schema_json()


def test_artefact_relpath_is_an_untracked_build_output() -> None:
    """The conventional artefact path sits under the gitignored ``build/`` tree."""
    assert ARTEFACT_RELPATH.startswith("build/")
    assert ARTEFACT_RELPATH.endswith(".json")

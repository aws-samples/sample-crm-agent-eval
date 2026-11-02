# SPDX-License-Identifier: MIT
"""The SSE frame contract as a JSON Schema artefact.

One schema covers both hops of the stream (Runtime -> relay, relay -> browser). It is derived from
:mod:`agents.events` and :func:`agents.runtime_entrypoint.event_payload`.

The relay's and the frontend's TypeScript types are both generated from this artefact and are
never hand-edited; ``scripts/check_sse_schema_parity.py`` regenerates and diffs them.

Regenerate the artefact from the repository root::

    .venv/bin/python3 -m agents.event_schema > build/schema/agent_events.json

Consumers surface a frame whose ``kind`` falls outside the enum rather than dropping it.
"""

from __future__ import annotations

import json
import sys
from dataclasses import fields
from types import UnionType
from typing import (
    Any,
    Final,
    Literal,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

from agents.events import (
    TOOL_RESULT_BYTE_CEILING,
    TRUNCATION_MARKER,
    AgentEvent,
    budget_exhausted_event,
)
from agents.runtime_entrypoint import event_payload, format_sse_line

#: Version of the frame contract itself, asserted by the relay at its hop.
SCHEMA_VERSION: Final[str] = "3.0.0"

#: Build-output path for the rendered artefact, relative to the repository root.
ARTEFACT_RELPATH: Final[str] = "build/schema/agent_events.json"

_JSON_SCALARS: Final[dict[type, str]] = {
    bool: "boolean",
    int: "integer",
    float: "number",
    str: "string",
    dict: "object",
}

#: Event used to recover the SSE framing and the wire key set from
#: :mod:`agents.runtime_entrypoint`.
_PROBE_EVENT: Final[AgentEvent] = budget_exhausted_event(
    partial_work="schema probe", error_message="schema probe"
)


def build_event_schema() -> dict[str, Any]:
    """Render the SSE frame contract as a JSON Schema (2020-12) object.

    Returns:
        A JSON-serialisable schema describing the single JSON object one ``data:`` frame carries.
        Property names, nullability, and every enum are derived from :class:`agents.events.
        AgentEvent`'s annotations and from :func:`agents.runtime_entrypoint.event_payload`'s key
        set; the framing affixes come from :func:`agents.runtime_entrypoint.format_sse_line`.

    Raises:
        TypeError: When a field's annotation has no JSON Schema representation.
        RuntimeError: When the wire payload and the dataclass fields disagree, or when the SSE
            framing cannot be recovered from ``format_sse_line``.
    """
    hints = get_type_hints(AgentEvent)
    names = [f.name for f in fields(AgentEvent)]
    wire_keys = list(event_payload(_PROBE_EVENT))
    if names != wire_keys:
        raise RuntimeError(
            f"event_payload keys {wire_keys} do not match AgentEvent fields {names}; "
            "the wire shape and the dataclass have drifted"
        )

    prefix, delimiter = _frame_affixes()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:crm-agent-eval:sse-event:{SCHEMA_VERSION}",
        "schema_version": SCHEMA_VERSION,
        "title": "AgentEventFrame",
        "description": (
            "One agent event, carried as exactly one JSON object on one SSE data frame. Every "
            "field is present on every frame; the fields a kind does not use are null."
        ),
        "type": "object",
        "additionalProperties": False,
        "properties": {name: _property_schema(hints[name]) for name in names},
        "required": names,
        "x-frame-prefix": prefix,
        "x-frame-delimiter": delimiter,
        "x-constants": {
            "truncation_marker": TRUNCATION_MARKER,
            "tool_result_byte_ceiling": TOOL_RESULT_BYTE_CEILING,
        },
    }


def render_schema_json() -> str:
    """Return the artefact's exact text: sorted keys, two-space indent, trailing newline."""
    return json.dumps(build_event_schema(), indent=2, sort_keys=True) + "\n"


def _frame_affixes() -> tuple[str, str]:
    """Recover the ``data: `` prefix and the frame delimiter from :func:`format_sse_line`."""
    body = json.dumps(event_payload(_PROBE_EVENT))
    prefix, found, delimiter = format_sse_line(_PROBE_EVENT).partition(body)
    if not found or not prefix or not delimiter:
        raise RuntimeError("format_sse_line no longer frames one JSON object per data line")
    return prefix, delimiter


def _property_schema(hint: object) -> dict[str, Any]:
    """Map one annotation to a JSON Schema property.

    Handles scalars, ``Literal`` enums, ``TypedDict`` objects and homogeneous ``tuple[X, ...]``
    arrays, each optionally ``| None``.
    """
    members = get_args(hint) if get_origin(hint) in (Union, UnionType) else (hint,)
    nullable = type(None) in members
    literals: list[str] = []
    types: list[str] = []
    structured: dict[str, Any] | None = None
    for member in members:
        if member is type(None):
            continue
        if get_origin(member) is Literal:
            literals.extend(str(value) for value in get_args(member))
        elif get_origin(member) is tuple or is_typeddict(member):
            if structured is not None:
                raise TypeError(f"annotation {hint!r} unions more than one structured type")
            structured = (
                _array_schema(member) if get_origin(member) is tuple else _object_schema(member)
            )
        else:
            origin = get_origin(member) or member
            json_type = _JSON_SCALARS.get(origin)
            if json_type is None:
                raise TypeError(f"no JSON Schema mapping for annotation member {member!r}")
            types.append(json_type)

    if structured is not None:
        if literals or types:
            raise TypeError(f"annotation {hint!r} mixes a structured type with another type")
        if nullable:
            structured["type"] = [structured["type"], "null"]
        return structured

    if literals:
        if types:
            raise TypeError(f"annotation {hint!r} mixes a Literal with a plain type")
        enum: list[str | None] = [*literals]
        if nullable:
            enum.append(None)
        return {"enum": enum}

    if nullable:
        types.append("null")
    return {"type": types[0] if len(types) == 1 else types}


def _array_schema(hint: object) -> dict[str, Any]:
    """Map a homogeneous ``tuple[X, ...]`` annotation to a JSON Schema array of ``X``."""
    args = get_args(hint)
    if len(args) != 2 or args[1] is not Ellipsis:
        raise TypeError(f"only homogeneous tuple[X, ...] annotations map to arrays, not {hint!r}")
    return {"type": "array", "items": _property_schema(args[0])}


def _object_schema(typed_dict: Any) -> dict[str, Any]:
    """Map a ``TypedDict`` to a closed JSON Schema object titled with its class name."""
    hints = get_type_hints(typed_dict)
    return {
        "type": "object",
        "title": typed_dict.__name__,
        "additionalProperties": False,
        "properties": {name: _property_schema(hint) for name, hint in hints.items()},
        "required": list(hints),
    }


def main() -> int:
    """Write the rendered artefact to stdout."""
    sys.stdout.write(render_schema_json())
    return 0


__all__ = [
    "ARTEFACT_RELPATH",
    "SCHEMA_VERSION",
    "build_event_schema",
    "render_schema_json",
]


if __name__ == "__main__":
    raise SystemExit(main())

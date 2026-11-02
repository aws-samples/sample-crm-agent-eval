# SPDX-License-Identifier: MIT
"""The prompt bundle: every model-facing text and tool definition, and its fingerprint.

:func:`prompt_bundle` gathers what the models are sent: the answer style, the agent prompts, the
subagent tool schemas, the page-context briefing templates, the tool-actor directive, every
toolspec tool, the prompt-caching mechanisms, and the texts and shapes code returns to a model as a
tool result (validation messages, the error ``next_step`` per class, the tool-actor errors, the
delegation result shape and its default messages). :func:`prompt_bundle_sha256` is the digest the
eval records in run provenance. Each member is read from its source module at call time; members
built inline by code are captured by rendering one fixed sample through the code that builds them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
from typing import Any, Final, cast

from agents import agent_prompts, answer_style, subagent_runner, supervisor, tool_actor, turn_driver
from agents.registry import RegisteredTool
from agents.subagents import ALL_SUBAGENT_SPECS
from crm import errors
from crm.toolspec import definitions, validation
from crm.toolspec.page_context import (
    BoardPageContext,
    EntityPageContext,
    ListPageContext,
    OpaquePageContext,
    PageContext,
    PageDescriptor,
    QueryDescriptor,
    SortDescriptor,
)
from providers import prompt_caching

#: The bundle layout version; bump it when a member is added, removed or reshaped.
PROMPT_BUNDLE_SCHEMA: Final[int] = 2

#: One fixed descriptor per briefing branch, so the rendered briefings stand for their templates.
_BRIEFING_SAMPLES: Final[tuple[tuple[str, PageContext], ...]] = (
    ("entity", EntityPageContext(route="/route", entity_type="accounts", entity_id="entity-id")),
    (
        "list",
        ListPageContext(
            route="/route",
            entity_type="accounts",
            query=QueryDescriptor(
                filters={"field": "value"},
                sort=SortDescriptor(field="field", direction="asc"),
                page=PageDescriptor(cursor="cursor", limit=1),
            ),
        ),
    ),
    ("board", BoardPageContext(route="/route")),
    ("opaque", OpaquePageContext(route="/route", reason="reason")),
)


#: A schema exercising each branch of the validation message builder, and one violating instance
#: per branch.
_VALIDATION_SAMPLE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["a", "b"]},
        "count": {"type": "integer"},
        "label": {"type": "string", "minLength": 2},
        "code": {"type": "string", "pattern": "^x$"},
    },
    "required": ["kind"],
}
_VALIDATION_SAMPLES: Final[tuple[tuple[str, dict[str, object]], ...]] = (
    ("required", {}),
    ("additionalProperties", {"kind": "a", "extra": 1}),
    ("enum", {"kind": "c"}),
    ("type", {"kind": "a", "count": "x"}),
    ("bound", {"kind": "a", "label": "z"}),
    ("other", {"kind": "a", "code": "y"}),
)

#: The tool name the rendered samples carry; distinct from every toolspec tool.
_SAMPLE_TOOL_NAME: Final[str] = "prompt_bundle_sample"


def _validation_messages() -> dict[str, str]:
    tool = dataclasses.replace(
        min(definitions.ALL_TOOLS, key=lambda definition: definition.name),
        name=_SAMPLE_TOOL_NAME,
        input_schema=_VALIDATION_SAMPLE_SCHEMA,
    )
    messages: dict[str, str] = {}
    for case, arguments in _VALIDATION_SAMPLES:
        error = validation._first_error(tool, arguments)
        if error is None:
            raise AssertionError(f"validation sample {case!r} violates nothing")
        messages[case] = validation._safe_message(tool, error)
    return messages


def _error_next_steps() -> dict[str, dict[str, str]]:
    return {
        name: {"code": member.code, "next_step": member.next_step}
        for name, member in sorted(inspect.getmembers(errors, inspect.isclass))
        if issubclass(member, errors.CrmError) and member.__module__ == errors.__name__
    }


def _sample_tool() -> RegisteredTool:
    definition = min(definitions.ALL_TOOLS, key=lambda candidate: candidate.name)
    return RegisteredTool._from_definition(definition)


def _tool_actor_errors() -> dict[str, object]:
    def no_dispatch(_tool: RegisteredTool, _arguments: dict[str, object]) -> dict[str, Any]:
        raise AssertionError("a tool-actor error sample must not dispatch")

    tool = _sample_tool()
    rendered: dict[str, object] = {}
    for case, saw_error, tool_input in (("model_error", True, {}), ("no_tool_call", False, None)):
        output, _latency = tool_actor._resolve_output(
            tool, tool_input, saw_error=saw_error, dispatch=no_dispatch, clock=None
        )
        rendered[case] = output
    return rendered


def _delegation_outputs() -> dict[str, object]:
    driver = subagent_runner._SubagentDriver(
        spec=ALL_SUBAGENT_SPECS[0],
        resolved=cast(Any, None),
        provider=cast(Any, None),
        tool_actor=cast(Any, None),
    )
    driver._record_outcome(
        _SAMPLE_TOOL_NAME, supervisor.SubagentOutcome(kind="data", data={"items": [{"id": "id"}]})
    )
    data = driver._aggregate_outcome()
    outcomes = {
        "data": data,
        "no_match": supervisor.SubagentOutcome(kind="no_match"),
        "tool_failure": supervisor.SubagentOutcome(kind="tool_failure", error_message="message"),
        "denied": supervisor.SubagentOutcome(
            kind="denied", missing_scope="scope", denial_message="message"
        ),
    }
    return {
        name: turn_driver._outcome_to_tool_output(outcome) for name, outcome in outcomes.items()
    }


def prompt_bundle() -> dict[str, object]:
    """Return the current prompt bundle as a JSON-safe mapping."""
    return {
        "schema": PROMPT_BUNDLE_SCHEMA,
        "answer_style": answer_style.load_answer_style_prompt(),
        "agent_prompts": dict(agent_prompts.load_agent_prompts()),
        "subagent_tools": list(supervisor.subagent_tool_schemas()),
        "page_briefing_templates": {
            name: supervisor.page_context_briefing(context) for name, context in _BRIEFING_SAMPLES
        },
        "tool_actor_directive": tool_actor._ACTOR_DIRECTIVE,
        "tools": [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in sorted(definitions.ALL_TOOLS, key=lambda tool: tool.name)
        ],
        "prompt_caching": dict(prompt_caching.CACHING),
        "validation_messages": _validation_messages(),
        "error_next_steps": _error_next_steps(),
        "tool_actor_errors": _tool_actor_errors(),
        "delegation_outputs": _delegation_outputs(),
    }


def prompt_bundle_sha256() -> str:
    """Return the SHA-256 hex digest of :func:`prompt_bundle`'s canonical JSON."""
    canonical = json.dumps(
        prompt_bundle(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["PROMPT_BUNDLE_SCHEMA", "prompt_bundle", "prompt_bundle_sha256"]

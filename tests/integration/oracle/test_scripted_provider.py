# SPDX-License-Identifier: MIT
"""Tests for the oracle contract and :class:`ScriptedProvider`.

No DynamoDB: role inference, per-role cursors, event shapes, id composition, exhaustion, and one
turn through the real :func:`agents.turn_driver.drive_turn` with an in-memory dispatch. The last
section asserts the oracle is unreachable from ``src/`` and from the harness CLI.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agents.budget import EVAL_BUDGET
from agents.registry import RegisteredTool, tool_registry_for
from agents.subagents import ALL_SUBAGENT_SPECS, SUBAGENT_SPECS_BY_ROLE
from agents.supervisor import subagent_tool_schemas
from agents.tool_actor import _ACTOR_DIRECTIVE
from agents.turn_driver import TurnRecords, drive_turn, session_turn_tuning
from eval.harness import build_arg_parser
from eval.task_ids import compose_ids_in_text
from providers.base import ProviderEvent, ResolvedInvocation, Tuning
from providers.capabilities import load_capability_matrix
from tests.integration.oracle._contract import (
    ROLES,
    SCRIPTS_REGISTRY_ATTR,
    Answer,
    Call,
    OracleScript,
)
from tests.integration.oracle.scripted_provider import (
    TOOL_TIER_ROLE,
    ScriptedProvider,
    ScriptExhausted,
    UnknownToolSurfaceError,
    infer_role,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MATRIX = load_capability_matrix()
_INVOCATION = ResolvedInvocation(model=_MATRIX.model_for("anthropic", "supervisor"), effort=None)
_TENANT = "eval-oracletest"


def _schema(tool: RegisteredTool) -> dict[str, object]:
    return {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}


def _supervisor_tools() -> list[dict[str, object]]:
    return [_schema(tool) for tool in tool_registry_for("supervisor")] + list(
        subagent_tool_schemas()
    )


def _subagent_tools(role: str) -> list[dict[str, object]]:
    return [_schema(tool) for tool in SUBAGENT_SPECS_BY_ROLE[role].tools]


def _actor_messages(intent: str) -> list[dict[str, object]]:
    return [{"role": "user", "content": f"{_ACTOR_DIRECTIVE}\n\n{intent}"}]


def _collect(
    provider: ScriptedProvider,
    tools: list[dict[str, object]],
    messages: list[dict[str, object]] | None = None,
) -> list[ProviderEvent]:
    async def _run() -> list[ProviderEvent]:
        stream: AsyncIterator[ProviderEvent] = provider.stream(
            _INVOCATION, messages if messages is not None else [], tools
        )
        return [event async for event in stream]

    return asyncio.run(_run())


# ---------------------------------------------------------------------------------- contract


def test_the_contract_names_the_supervisor_and_the_four_subagents() -> None:
    assert ("supervisor", *(spec.role for spec in ALL_SUBAGENT_SPECS)) == ROLES
    assert SCRIPTS_REGISTRY_ATTR == "SCRIPTS"


def test_steps_are_frozen_values() -> None:
    call = Call(role="research", tool="get_account", args={"account_id": "acct-0001"})
    assert call == Call(role="research", tool="get_account", args={"account_id": "acct-0001"})
    with pytest.raises(AttributeError):
        call.role = "mutation"  # type: ignore[misc]


# ------------------------------------------------------------------------------ role inference


def test_subagent_tool_names_infer_the_supervisor() -> None:
    assert infer_role(_supervisor_tools()) == "supervisor"


@pytest.mark.parametrize("role", [spec.role for spec in ALL_SUBAGENT_SPECS])
def test_each_subagent_surface_infers_its_role(role: str) -> None:
    assert infer_role(_subagent_tools(role)) == role


def test_a_single_tool_under_the_actor_directive_infers_the_tool_tier() -> None:
    tools = [_schema(SUBAGENT_SPECS_BY_ROLE["research"].tools[0])]
    assert infer_role(tools, _actor_messages("{}")) == TOOL_TIER_ROLE


@pytest.mark.parametrize(
    "tools",
    [
        [],
        [{"name": "get_account"}],
        [{"name": "get_account"}, {"name": "not_a_tool"}],
        [{"name": name} for name in ("get_account", "get_contact")],
    ],
    ids=["empty", "single-without-directive", "unknown-name", "partial-surface"],
)
def test_an_unknown_toolset_raises(tools: list[dict[str, object]]) -> None:
    with pytest.raises(UnknownToolSurfaceError):
        infer_role(tools)


def test_an_unknown_toolset_raises_from_stream() -> None:
    provider = ScriptedProvider((Answer(role="supervisor", text="x"),), _TENANT)
    with pytest.raises(UnknownToolSurfaceError):
        _collect(provider, [{"name": "nope"}])


def test_a_step_naming_an_unknown_role_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        ScriptedProvider((Answer(role="planner", text="x"),), _TENANT)


# ---------------------------------------------------------------------------- event shapes


def test_a_call_emits_tool_call_usage_and_completed_awaiting_tools() -> None:
    provider = ScriptedProvider(
        (Call(role="research", tool="get_account", args={"account_id": "acct-0002"}),), _TENANT
    )
    events = _collect(provider, _subagent_tools("research"))
    assert [event.type for event in events] == ["tool_call", "usage", "completed"]
    assert events[0].tool_name == "get_account"
    assert events[0].tool_id
    assert events[1].input_tokens and events[1].output_tokens
    assert events[2].awaiting_tools is True


def test_an_answer_emits_text_delta_usage_and_completed() -> None:
    provider = ScriptedProvider((Answer(role="supervisor", text="Done."),), _TENANT)
    events = _collect(provider, _supervisor_tools())
    assert [event.type for event in events] == ["text_delta", "usage", "completed"]
    assert events[0].text == "Done."
    assert events[2].awaiting_tools is False


def test_tool_ids_are_distinct_across_calls() -> None:
    provider = ScriptedProvider(
        (
            Call(role="research", tool="get_account", args={"account_id": "acct-0001"}),
            Call(role="research", tool="get_contact", args={"contact_id": "cont-0001"}),
        ),
        _TENANT,
    )
    first = _collect(provider, _subagent_tools("research"))[0]
    second = _collect(provider, _subagent_tools("research"))[0]
    assert first.tool_id != second.tool_id


# ----------------------------------------------------------------------------- id composition


def test_ids_in_every_string_of_args_and_text_are_composed() -> None:
    provider = ScriptedProvider(
        (
            Call(
                role="mutation",
                tool="submit_proposal",
                args={
                    "target_id": "opp-0000",
                    "after": {"primary_contact_id": "cont-0001", "value": 180000},
                    "ids": ["acct-0000", "note-0003"],
                },
            ),
            Answer(role="supervisor", text="Proposed cont-0001 for opp-0000."),
        ),
        _TENANT,
    )
    call = _collect(provider, _subagent_tools("mutation"))[0]

    def compose(text: str) -> str:
        return compose_ids_in_text(text, tenant=_TENANT)

    assert call.tool_input == {
        "target_id": compose("opp-0000"),
        "after": {"primary_contact_id": compose("cont-0001"), "value": 180000},
        "ids": [compose("acct-0000"), compose("note-0003")],
    }
    assert compose("opp-0000") != "opp-0000"
    answer = _collect(provider, _supervisor_tools())[0]
    assert answer.text == f"Proposed {compose('cont-0001')} for {compose('opp-0000')}."


def test_the_tool_tier_echoes_the_draft_arguments_and_consumes_no_step() -> None:
    provider = ScriptedProvider((Answer(role="supervisor", text="x"),), _TENANT)
    tool = SUBAGENT_SPECS_BY_ROLE["research"].tools[0]
    events = _collect(
        provider, [_schema(tool)], _actor_messages('{"account_id": "acct-eval-x-0002"}')
    )
    assert [event.type for event in events] == ["tool_call", "usage", "completed"]
    assert events[0].tool_name == tool.name
    assert events[0].tool_input == {"account_id": "acct-eval-x-0002"}
    assert events[2].awaiting_tools is False
    assert provider.remaining() == {"supervisor": 1}


def test_a_non_json_actor_intent_formulates_empty_arguments() -> None:
    provider = ScriptedProvider((), _TENANT)
    tool = SUBAGENT_SPECS_BY_ROLE["research"].tools[0]
    events = _collect(provider, [_schema(tool)], _actor_messages("find acme"))
    assert events[0].tool_input == {}


# -------------------------------------------------------------------- cursors and exhaustion


def test_each_role_keeps_its_own_cursor() -> None:
    provider = ScriptedProvider(
        (
            Call(role="supervisor", tool="research", args={"intent": "look up acct-0001"}),
            Call(role="research", tool="get_account", args={"account_id": "acct-0001"}),
            Answer(role="research", text="found"),
            Answer(role="supervisor", text="Here it is."),
        ),
        _TENANT,
    )
    assert _collect(provider, _supervisor_tools())[0].tool_name == "research"
    assert _collect(provider, _subagent_tools("research"))[0].tool_name == "get_account"
    assert provider.remaining() == {"supervisor": 1, "research": 1}
    assert _collect(provider, _subagent_tools("research"))[0].text == "found"
    assert _collect(provider, _supervisor_tools())[0].text == "Here it is."
    assert provider.remaining() == {}
    provider.assert_fully_consumed()


def test_running_past_a_roles_steps_raises_script_exhausted() -> None:
    provider = ScriptedProvider((Answer(role="supervisor", text="x"),), _TENANT)
    _collect(provider, _supervisor_tools())
    with pytest.raises(ScriptExhausted, match="supervisor"):
        _collect(provider, _supervisor_tools())


def test_a_role_with_no_steps_raises_script_exhausted() -> None:
    provider = ScriptedProvider((Answer(role="supervisor", text="x"),), _TENANT)
    with pytest.raises(ScriptExhausted, match="research"):
        _collect(provider, _subagent_tools("research"))


def test_an_unconsumed_step_fails_the_consumption_check() -> None:
    provider = ScriptedProvider(
        (Answer(role="research", text="x"), Answer(role="supervisor", text="y")), _TENANT
    )
    _collect(provider, _supervisor_tools())
    with pytest.raises(ScriptExhausted, match="unconsumed"):
        provider.assert_fully_consumed()


def test_an_overrun_fails_the_consumption_check_even_if_swallowed() -> None:
    provider = ScriptedProvider((), _TENANT)
    with pytest.raises(ScriptExhausted):
        _collect(provider, _supervisor_tools())
    with pytest.raises(ScriptExhausted, match="overran"):
        provider.assert_fully_consumed()


# ------------------------------------------------------------------ through the turn driver


def test_a_scripted_delegation_drives_the_real_turn_driver_end_to_end() -> None:
    script: OracleScript = (
        Call(role="supervisor", tool="research", args={"intent": "Look up account acct-0002."}),
        Call(role="research", tool="get_account", args={"account_id": "acct-0002"}),
        Answer(role="research", text="Account acct-0002 found."),
        Answer(role="supervisor", text="Account acct-0002 is Acme."),
    )
    provider = ScriptedProvider(script, _TENANT)
    dispatched: list[tuple[str, dict[str, object]]] = []

    def _dispatch(tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        dispatched.append((tool.name, tool_input))
        return {"statusCode": 200, "body": {"account_id": tool_input["account_id"]}}

    records = TurnRecords()
    texts: list[str] = []

    async def _run() -> None:
        async for event in drive_turn(
            prompt="Tell me about acct-0002",
            session_id="oracle-session",
            providers=lambda _family: provider,
            matrix=_MATRIX,
            tuning=session_turn_tuning(Tuning(source="test.oracle", family="anthropic")),
            budget=EVAL_BUDGET,
            dispatch=_dispatch,
            records=records,
        ):
            if event.kind == "text_delta" and event.text:
                texts.append(event.text)

    asyncio.run(_run())
    composed = compose_ids_in_text("acct-0002", tenant=_TENANT)
    assert dispatched == [("get_account", {"account_id": composed})]
    assert "".join(texts) == f"Account {composed} is Acme."
    assert sorted({invocation.tier for invocation in records.invocations}) == [
        "subagent",
        "supervisor",
        "tool",
    ]
    provider.assert_fully_consumed()


# ------------------------------------------------------------------------- unreachability


_FORBIDDEN_REFERENCES = ("ScriptedProvider", "integration.oracle", "scripted_provider")


def _references(path: Path) -> list[str]:
    """Every identifier, attribute, import name and string constant in ``path`` that names the oracle."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        tokens: list[str] = []
        if isinstance(node, ast.Name):
            tokens.append(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.append(node.attr)
        elif isinstance(node, ast.alias):
            tokens.append(node.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            tokens.append(node.module)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            tokens.append(node.value)
        found.extend(
            token for token in tokens if any(word in token for word in _FORBIDDEN_REFERENCES)
        )
    return found


def test_the_reference_scan_sees_the_oracle_in_its_own_package() -> None:
    assert _references(Path(__file__)), "positive control: this file names ScriptedProvider"


def test_no_src_module_references_the_oracle() -> None:
    sources = sorted((_REPO_ROOT / "src").rglob("*.py"))
    assert len(sources) > 50
    offenders = {
        str(path.relative_to(_REPO_ROOT)): refs for path in sources if (refs := _references(path))
    }
    assert offenders == {}


def test_no_harness_cli_option_references_the_oracle() -> None:
    parser = build_arg_parser()
    texts: list[str] = []
    for action in parser._actions:
        texts.extend(action.option_strings)
        texts.extend(str(value) for value in (action.dest, action.help, action.default))
        if action.choices is not None:
            texts.extend(str(choice) for choice in action.choices)
    assert any(text.startswith("--") for text in texts)
    lowered = " ".join(texts).lower()
    assert "oracle" not in lowered
    assert "scripted" not in lowered
    assert isinstance(parser, argparse.ArgumentParser)

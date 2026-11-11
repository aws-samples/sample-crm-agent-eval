# SPDX-License-Identifier: MIT
"""Tests for the closed agent event stream, turn budgets, and generated tool registry.

Covers the closed event set, budget exhaustion as a terminal reported state, record-boundary
truncation of oversized tool results, and generation of the registry from `crm.toolspec`.
"""

from __future__ import annotations

import dataclasses

import pytest

from agents.budget import DEFAULT_MAX_TOKENS, EVAL_MAX_TOOL_CALLS, TurnBudget
from agents.events import (
    EVENT_KINDS,
    TOOL_RESULT_BYTE_CEILING,
    TRUNCATION_MARKER,
    AgentEvent,
    budget_exhausted_event,
    truncate_tool_output,
)
from agents.registry import REGISTERED_TOOL_NAMES, RegisteredTool, full_registry, tool_registry_for
from crm.toolspec.definitions import ALL_TOOLS, tools_for_agent

# --------------------------------------------------------------------------------------
# The event set is closed and exhaustive
# --------------------------------------------------------------------------------------


def test_event_kinds_is_exactly_the_closed_set() -> None:
    """The closed set is tool call, tool result, proposal, usage, completion, error, text_delta,
    heartbeat."""
    assert set(EVENT_KINDS) == {
        "tool_call",
        "tool_result",
        "proposal",
        "usage",
        "completed",
        "error",
        "text_delta",
        "heartbeat",
    }
    assert len(EVENT_KINDS) == len(set(EVENT_KINDS))


def test_an_event_kind_outside_the_closed_set_cannot_be_constructed() -> None:
    """`cancelled` is not an event kind and cannot be constructed."""
    with pytest.raises(ValueError, match="unrecognised event kind"):
        AgentEvent(kind="cancelled")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kind",
    [
        "made_up_kind",
        "tool_call_v2",
        "",
        "TOOL_CALL",
    ],
)
def test_every_fabricated_kind_outside_the_set_is_rejected(kind: str) -> None:
    """Sweeps several plausible-looking but non-member kinds, none of which may construct."""
    with pytest.raises(ValueError, match="unrecognised event kind"):
        AgentEvent(kind=kind)  # type: ignore[arg-type]


def test_tool_call_event_requires_its_fields_and_forbids_others() -> None:
    event = AgentEvent(kind="tool_call", tool_name="get_account", tool_input={"account_id": "a1"})
    assert event.tool_name == "get_account"
    with pytest.raises(ValueError, match="requires tool_name"):
        AgentEvent(kind="tool_call", tool_input={"account_id": "a1"})
    with pytest.raises(ValueError, match="requires tool_input"):
        AgentEvent(kind="tool_call", tool_name="get_account")


def test_tool_result_event_requires_tool_output() -> None:
    event = AgentEvent(kind="tool_result", tool_output={"ok": True})
    assert event.tool_output == {"ok": True}
    with pytest.raises(ValueError, match="requires tool_output"):
        AgentEvent(kind="tool_result")


def test_tool_call_fields_forbidden_outside_tool_call_kind() -> None:
    with pytest.raises(ValueError, match="must not be set"):
        AgentEvent(kind="usage", tool_name="get_account", input_tokens=1, output_tokens=1)


def test_proposal_event_requires_id_and_summary() -> None:
    event = AgentEvent(
        kind="proposal", proposal_id="prop-1", proposal_summary={"before": {}, "after": {}}
    )
    assert event.proposal_id == "prop-1"
    with pytest.raises(ValueError, match="requires proposal_id"):
        AgentEvent(kind="proposal", proposal_summary={"before": {}, "after": {}})
    with pytest.raises(ValueError, match="requires proposal_summary"):
        AgentEvent(kind="proposal", proposal_id="prop-1")


def test_usage_event_requires_input_and_output_tokens() -> None:
    event = AgentEvent(kind="usage", input_tokens=10, output_tokens=5)
    assert event.input_tokens == 10
    with pytest.raises(ValueError, match="requires input_tokens"):
        AgentEvent(kind="usage", output_tokens=5)
    with pytest.raises(ValueError, match="requires output_tokens"):
        AgentEvent(kind="usage", input_tokens=10)


def test_completed_event_requires_text_and_may_carry_usage() -> None:
    event = AgentEvent(kind="completed", text="done", input_tokens=1, output_tokens=1)
    assert event.text == "done"
    with pytest.raises(ValueError, match="requires text"):
        AgentEvent(kind="completed")


def test_token_fields_forbidden_outside_usage_and_completed() -> None:
    with pytest.raises(ValueError, match="must not be set"):
        AgentEvent(kind="tool_result", tool_output={"a": 1}, input_tokens=1)


# --------------------------------------------------------------------------------------
# text_delta: the incrementally streamed answer kind
# --------------------------------------------------------------------------------------


def test_text_delta_event_requires_text_and_event_id() -> None:
    event = AgentEvent(kind="text_delta", text="Hel", event_id="ans-1", partial=True)
    assert event.text == "Hel"
    assert event.event_id == "ans-1"
    assert event.partial is True
    with pytest.raises(ValueError, match="requires text"):
        AgentEvent(kind="text_delta", event_id="ans-1", partial=True)
    with pytest.raises(ValueError, match="requires event_id"):
        AgentEvent(kind="text_delta", text="Hel", partial=True)


def test_a_produced_text_delta_is_always_partial() -> None:
    """Every emitted ``text_delta`` carries ``partial=True``."""
    event = AgentEvent(kind="text_delta", text="lo.", event_id="ans-1", partial=True)
    assert event.partial is True
    assert event.event_id == "ans-1"


def test_event_id_forbidden_outside_text_delta() -> None:
    with pytest.raises(ValueError, match="must not be set"):
        AgentEvent(kind="completed", text="done", event_id="ans-1")


def test_partial_flag_forbidden_outside_text_delta() -> None:
    with pytest.raises(ValueError, match="partial may only be set"):
        AgentEvent(kind="completed", text="done", partial=True)


def test_text_forbidden_outside_completed_and_text_delta() -> None:
    with pytest.raises(ValueError, match="must not be set"):
        AgentEvent(kind="usage", input_tokens=1, output_tokens=1, text="stray")


def test_error_event_requires_error_kind_and_message() -> None:
    event = AgentEvent(kind="error", error_kind="tool_failure", error_message="boom")
    assert event.error_kind == "tool_failure"
    with pytest.raises(ValueError, match="requires error_kind"):
        AgentEvent(kind="error", error_message="boom")
    with pytest.raises(ValueError, match="requires error_message"):
        AgentEvent(kind="error", error_kind="tool_failure")


def test_partial_work_forbidden_outside_budget_exhausted_error() -> None:
    with pytest.raises(ValueError, match="must not be set"):
        AgentEvent(
            kind="error",
            error_kind="tool_failure",
            error_message="boom",
            partial_work="did something",
        )
    with pytest.raises(ValueError, match="must not be set"):
        AgentEvent(kind="completed", text="done", partial_work="did something")


def test_truncated_flag_forbidden_outside_tool_result() -> None:
    with pytest.raises(ValueError, match="truncated may only be set"):
        AgentEvent(kind="completed", text="done", truncated=True)


def test_agent_event_is_frozen() -> None:
    """An event's kind cannot be mutated after construction."""
    event = AgentEvent(kind="usage", input_tokens=1, output_tokens=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.kind = "completed"  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# Budget exhaustion is a terminal reported state
# --------------------------------------------------------------------------------------


def test_default_budgets_are_positive() -> None:
    assert EVAL_MAX_TOOL_CALLS > 0
    assert DEFAULT_MAX_TOKENS > 0


def test_budget_rejects_non_positive_limits() -> None:
    with pytest.raises(ValueError, match="max_tool_calls must be positive"):
        TurnBudget(max_tool_calls=0)
    with pytest.raises(ValueError, match="max_tokens must be positive"):
        TurnBudget(max_tokens=0)


def test_budget_is_not_exhausted_before_any_consumption() -> None:
    budget = TurnBudget(max_tool_calls=2, max_tokens=100)
    assert not budget.is_exhausted()


def test_budget_exhausts_on_tool_call_ceiling() -> None:
    budget = TurnBudget(max_tool_calls=2, max_tokens=1_000_000)
    budget.record_tool_call("updated opportunity opp_1")
    assert not budget.is_exhausted()
    budget.record_tool_call("updated opportunity opp_2")
    assert budget.is_exhausted()


def test_budget_exhausts_on_token_ceiling() -> None:
    budget = TurnBudget(max_tool_calls=1_000, max_tokens=50)
    budget.record_tokens(49)
    assert not budget.is_exhausted()
    budget.record_tokens(1)
    assert budget.is_exhausted()


def test_record_tokens_rejects_negative_counts() -> None:
    budget = TurnBudget()
    with pytest.raises(ValueError, match="must not be negative"):
        budget.record_tokens(-1)


def test_exhausted_event_refuses_to_report_early() -> None:
    """Exhaustion is reported only once the budget is actually reached."""
    budget = TurnBudget(max_tool_calls=5, max_tokens=1_000_000)
    with pytest.raises(RuntimeError, match="before the budget was actually exhausted"):
        budget.exhausted_event()


def test_exhausted_event_is_terminal_error_naming_partial_work() -> None:
    """Exhaustion is a terminal reported state naming partial work."""
    budget = TurnBudget(max_tool_calls=2, max_tokens=1_000_000)
    budget.record_tool_call("updated opportunity opp_1 stage to Negotiation")
    budget.record_tool_call("logged activity act_1")
    assert budget.is_exhausted()

    event = budget.exhausted_event()

    assert event.kind == "error"
    assert event.error_kind == "budget_exhausted"
    assert event.partial_work is not None
    assert "opp_1" in event.partial_work
    assert "act_1" in event.partial_work
    assert event.error_message is not None


def test_exhausted_event_names_no_work_explicitly_when_nothing_completed() -> None:
    """Zero completed actions are still named in the exhausted state."""
    budget = TurnBudget(max_tool_calls=1_000, max_tokens=1)
    budget.record_tokens(1)
    assert budget.is_exhausted()

    event = budget.exhausted_event()

    assert event.partial_work == "no tool calls completed"


def test_budget_exhausted_event_helper_builds_the_same_shape_directly() -> None:
    event = budget_exhausted_event(partial_work="updated opp_1", error_message="ran out")
    assert event.kind == "error"
    assert event.error_kind == "budget_exhausted"
    assert event.partial_work == "updated opp_1"


# --------------------------------------------------------------------------------------
# Oversized tool results truncate at a record boundary with a marker
# --------------------------------------------------------------------------------------


def test_truncate_tool_output_keeps_everything_under_the_ceiling() -> None:
    items: list[dict[str, object]] = [{"id": str(i)} for i in range(5)]
    kept, truncated = truncate_tool_output(items, byte_ceiling=TOOL_RESULT_BYTE_CEILING)
    assert kept == items
    assert truncated is False


def test_truncate_tool_output_cuts_at_a_record_boundary_never_mid_record() -> None:
    """Every kept item is bit-for-bit one of the originals."""
    items: list[dict[str, object]] = [{"id": str(i), "payload": "x" * 50} for i in range(20)]
    kept, truncated = truncate_tool_output(items, byte_ceiling=300)
    assert truncated is True
    *records, marker = kept
    assert marker == {TRUNCATION_MARKER: True}
    for record in records:
        assert record in items


def test_truncate_tool_output_marker_present_even_reading_only_the_list() -> None:
    """Truncation is detectable from the returned list alone, without the paired bool."""
    items: list[dict[str, object]] = [{"id": str(i), "payload": "x" * 100} for i in range(10)]
    kept, truncated = truncate_tool_output(items, byte_ceiling=250)
    assert truncated is True
    assert any(record.get(TRUNCATION_MARKER) for record in kept)


def test_truncate_tool_output_empty_list_is_never_falsely_marked_truncated() -> None:
    kept, truncated = truncate_tool_output([], byte_ceiling=TOOL_RESULT_BYTE_CEILING)
    assert kept == []
    assert truncated is False


def test_truncate_tool_output_single_oversized_item_still_yields_a_marker() -> None:
    """A single item exceeding the ceiling truncates to zero kept records plus the marker."""
    items: list[dict[str, object]] = [{"id": "huge", "payload": "x" * 1000}]
    kept, truncated = truncate_tool_output(items, byte_ceiling=10)
    assert truncated is True
    assert kept == [{TRUNCATION_MARKER: True}]


def test_tool_result_event_can_declare_truncation() -> None:
    source_items: list[dict[str, object]] = [{"id": str(i), "payload": "x" * 50} for i in range(20)]
    kept, truncated = truncate_tool_output(source_items, byte_ceiling=300)
    tool_output: dict[str, object] = {"items": kept}
    event = AgentEvent(kind="tool_result", tool_output=tool_output, truncated=truncated)
    assert event.truncated is True
    items = tool_output["items"]
    assert isinstance(items, list)
    assert any(item.get(TRUNCATION_MARKER) for item in items)


# --------------------------------------------------------------------------------------
# The tool registry is generated from crm.toolspec.definitions
# --------------------------------------------------------------------------------------


def test_all_tools_names_are_unique() -> None:
    """Every ALL_TOOLS name is distinct, so the name-keyed registry loses none."""
    names = [tool.name for tool in ALL_TOOLS]
    assert names and len(set(names)) == len(names)


def test_full_registry_has_exactly_one_entry_per_source_definition() -> None:
    """The registry's size and key set equal ALL_TOOLS'."""
    registry = full_registry()
    assert len(registry) == len(ALL_TOOLS)
    assert set(registry) == {tool.name for tool in ALL_TOOLS}


def test_every_registered_tool_matches_its_source_definition_field_for_field() -> None:
    registry = full_registry()
    for definition in ALL_TOOLS:
        registered = registry[definition.name]
        assert registered.name == definition.name
        assert registered.description == definition.description
        assert registered.input_schema == definition.input_schema
        assert registered.handler == definition.handler
        assert registered.mutates == definition.mutates


def test_registered_tool_names_matches_all_tools_exactly() -> None:
    assert {tool.name for tool in ALL_TOOLS} == REGISTERED_TOOL_NAMES


@pytest.mark.parametrize("role", ["supervisor", "research", "mutation", "analytics", "composer"])
def test_tool_registry_for_role_matches_toolspec_tools_for_agent(role: str) -> None:
    """Each role's registry is exactly the role's ALL_TOOLS subset, in the same order."""
    registered = tool_registry_for(role)
    expected = tools_for_agent(role)
    assert [tool.name for tool in registered] == [definition.name for definition in expected]


def test_tool_registry_for_unknown_role_is_empty_not_an_error() -> None:
    """An unknown role gets an empty registry, matching `tools_for_agent`."""
    assert tool_registry_for("nonexistent-role") == ()


def test_a_hand_added_tool_absent_from_all_tools_cannot_appear_in_the_registry() -> None:
    """A RegisteredTool constructed outside ALL_TOOLS does not appear in the registry."""
    registry = full_registry()
    fabricated = RegisteredTool(
        name="hand_written_tool",
        description="not in ALL_TOOLS",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler="crm.handlers.nonexistent.handler",
        mutates=False,
    )
    assert fabricated.name not in registry
    assert "hand_written_tool" not in REGISTERED_TOOL_NAMES


def test_registry_size_tracks_all_tools_when_a_definition_is_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patching ALL_TOOLS changes the derived registry's size identically."""
    import agents.registry as registry_module
    from crm.toolspec.definitions import ToolDefinition

    extra = ToolDefinition(
        name="get_account_v2_test_only",
        display_label="Get account (v2 test)",
        description="distinct extra definition for registry-derivation test",
        input_schema={
            "type": "object",
            "properties": {"account_id": {"type": "string", "minLength": 1}},
            "required": ["account_id"],
            "additionalProperties": False,
        },
        risk="read",
        scope_arg="account_id",
        handler="crm.handlers.get_account.handler",
        agents=frozenset({"research"}),
    )
    patched_tools = (*ALL_TOOLS, extra)
    monkeypatch.setattr(registry_module, "ALL_TOOLS", patched_tools)

    patched_registry = registry_module.full_registry()

    assert len(patched_registry) == len(patched_tools)


def test_a_refused_request_ends_the_stream_with_the_budget_exhausted_terminal() -> None:
    """A request refused for its prompt size is reported like any other exhausted dimension."""
    budget = TurnBudget(max_tool_calls=EVAL_MAX_TOOL_CALLS)
    budget.record_tool_call("called get_account")
    budget.refuse_request(
        estimated_prompt_tokens=95_001, limit=95_000, tier="tool", role="research"
    )
    event = budget.exhausted_event()
    assert event.kind == "error"
    assert event.error_kind == "budget_exhausted"
    assert event.partial_work == "called get_account"
    assert "request_prompt_limit: estimated 95001 > 95000" in (event.error_message or "")

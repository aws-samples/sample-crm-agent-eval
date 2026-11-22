# SPDX-License-Identifier: MIT
"""Tests for the cost/tier fields and the ``heartbeat`` kind on :class:`agents.events.AgentEvent`."""

from __future__ import annotations

import json

import pytest

from agents.events import (
    EVENT_KINDS,
    AgentEvent,
    CostLine,
    ResolvedTierInfo,
    budget_exhausted_event,
    strip_control_chars,
)
from agents.runtime_entrypoint import event_payload, format_sse_line
from agents.supervisor import describe_truncation

_COST_LINE: CostLine = {
    "tier": "subagent",
    "role": "research",
    "model_id": "subagent-model",
    "input_tokens": 120,
    "output_tokens": 40,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "long_context_calls": 0,
    "cost_usd": 0.00031,
}

_RESOLVED_TIER: ResolvedTierInfo = {
    "tier": "supervisor",
    "family": "openai",
    "model_id": "openai.gpt-5",
    "effort": "low",
}

_ACCOUNTING: dict[str, object] = {
    "cost_lines": (_COST_LINE,),
    "cost_usd": 0.00031,
    "cost_withheld": False,
    "resolved_tiers": (_RESOLVED_TIER,),
}


def _non_accounted_events() -> list[tuple[str, dict[str, object]]]:
    """Minimal valid field sets for every kind that must not carry the accounting fields."""
    return [
        ("tool_call", {"tool_name": "list_accounts", "tool_input": {}}),
        ("tool_result", {"tool_output": {"items": []}}),
        ("proposal", {"proposal_id": "p-1", "proposal_summary": {}}),
        ("error", {"error_kind": "tool_failure", "error_message": "m"}),
        ("text_delta", {"text": "x", "event_id": "e-1", "partial": True}),
        ("heartbeat", {}),
        ("replay_unavailable", {}),
    ]


def test_heartbeat_is_a_declared_kind_and_carries_no_fields() -> None:
    """A ``heartbeat`` event constructs with no fields and renders every other field null/false."""
    assert "heartbeat" in EVENT_KINDS
    payload = event_payload(AgentEvent(kind="heartbeat"))
    assert payload["kind"] == "heartbeat"
    assert {key for key, value in payload.items() if value not in (None, False)} == {"kind"}


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("text", "x"),
        ("input_tokens", 1),
        ("tool_name", "t"),
        ("error_message", "m"),
        ("cost_usd", 0.1),
    ],
)
def test_heartbeat_rejects_any_field(field_name: str, value: object) -> None:
    """A heartbeat carrying any kind-specific field is rejected."""
    with pytest.raises(ValueError):
        AgentEvent(kind="heartbeat", **{field_name: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["usage", "completed"])
def test_usage_and_completed_carry_cost_lines_and_resolved_tiers(kind: str) -> None:
    """Both accounted kinds accept the cost/tier fields and render them as JSON arrays of objects."""
    extra: dict[str, object] = {"text": "done"} if kind == "completed" else {}
    event = AgentEvent(
        kind=kind,  # type: ignore[arg-type]
        input_tokens=120,
        output_tokens=40,
        **extra,  # type: ignore[arg-type]
        **_ACCOUNTING,  # type: ignore[arg-type]
    )
    wire = json.loads(format_sse_line(event).removeprefix("data: "))
    assert wire["cost_lines"] == [_COST_LINE]
    assert wire["resolved_tiers"] == [_RESOLVED_TIER]
    assert wire["cost_usd"] == pytest.approx(0.00031)
    assert wire["cost_withheld"] is False


def test_withheld_turn_renders_null_cost_and_true_flag() -> None:
    """A provisional-pricing turn carries ``cost_usd`` null and ``cost_withheld`` true."""
    withheld_line: CostLine = {**_COST_LINE, "long_context_calls": 2, "cost_usd": None}
    event = AgentEvent(
        kind="completed",
        text="done",
        input_tokens=1,
        output_tokens=1,
        cost_lines=(withheld_line,),
        cost_usd=None,
        cost_withheld=True,
    )
    payload = event_payload(event)
    assert payload["cost_usd"] is None
    assert payload["cost_withheld"] is True
    assert payload["cost_lines"] == [withheld_line]


def test_accounting_fields_default_to_null_on_the_wire() -> None:
    """A ``usage`` event without the cost/tier fields renders each of them as null."""
    payload = event_payload(AgentEvent(kind="usage", input_tokens=1, output_tokens=1))
    for name in _ACCOUNTING:
        assert payload[name] is None


@pytest.mark.parametrize(("kind", "fields"), _non_accounted_events())
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("cost_lines", (_COST_LINE,)),
        ("cost_lines", ()),
        ("cost_usd", 0.0),
        ("cost_withheld", False),
        ("resolved_tiers", (_RESOLVED_TIER,)),
        ("resolved_tiers", ()),
    ],
)
def test_accounting_fields_are_forbidden_outside_usage_and_completed(
    kind: str, fields: dict[str, object], field_name: str, value: object
) -> None:
    """Every non-accounted kind rejects each cost/tier field, including its falsy values."""
    AgentEvent(kind=kind, **fields)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=field_name):
        AgentEvent(kind=kind, **fields, **{field_name: value})  # type: ignore[arg-type]


def _accounted(**fields: object) -> AgentEvent:
    return AgentEvent(kind="usage", input_tokens=1, output_tokens=1, **fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "cost",
    [float("nan"), float("inf"), -0.01, True, "0.1"],
    ids=["nan", "inf", "neg", "bool", "str"],
)
def test_turn_cost_must_be_finite_and_non_negative(cost: object) -> None:
    """``cost_usd`` rejects NaN, infinity, a negative figure and a non-number."""
    with pytest.raises(ValueError, match="cost_usd"):
        _accounted(cost_usd=cost)


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -1.0], ids=["nan", "inf", "neg"])
def test_line_cost_must_be_finite_and_non_negative(cost: float) -> None:
    """Each line's ``cost_usd`` follows the same rule."""
    with pytest.raises(ValueError, match=r"cost_lines\.cost_usd"):
        _accounted(cost_lines=({**_COST_LINE, "cost_usd": cost},))


def test_withheld_turn_carries_no_figure() -> None:
    """``cost_withheld`` true forbids both the turn figure and any line figure."""
    with pytest.raises(ValueError, match="cost_usd must be None"):
        _accounted(cost_withheld=True, cost_usd=0.1)
    with pytest.raises(ValueError, match=r"cost_lines\.cost_usd must be None"):
        _accounted(cost_withheld=True, cost_lines=(_COST_LINE,))
    _accounted(cost_withheld=True, cost_lines=({**_COST_LINE, "cost_usd": None},))


@pytest.mark.parametrize(
    "line",
    [
        {**_COST_LINE, "tier": "orchestrator"},
        {**_COST_LINE, "rows": []},
        {k: v for k, v in _COST_LINE.items() if k != "role"},
        {**_COST_LINE, "input_tokens": -1},
        {**_COST_LINE, "output_tokens": 1.5},
        {**_COST_LINE, "cache_read_tokens": True},
        {**_COST_LINE, "model_id": ""},
        {k: v for k, v in _COST_LINE.items() if k != "long_context_calls"},
        {**_COST_LINE, "long_context_calls": -1},
        {**_COST_LINE, "long_context_calls": True},
        {**_COST_LINE, "long_context_calls": None},
        "not a mapping",
    ],
    ids=[
        "tier",
        "extra-key",
        "missing-key",
        "neg-count",
        "float-count",
        "bool-count",
        "blank-model",
        "missing-long-context-calls",
        "neg-long-context-calls",
        "bool-long-context-calls",
        "null-long-context-calls",
        "str",
    ],
)
def test_cost_lines_have_closed_keys_known_tiers_and_counts(line: object) -> None:
    """A cost line with an unknown tier, open key set or bad count is rejected."""
    with pytest.raises(ValueError, match="cost_lines"):
        _accounted(cost_lines=(line,))


@pytest.mark.parametrize(
    "tier",
    [
        {**_RESOLVED_TIER, "tier": "bogus"},
        {**_RESOLVED_TIER, "family": "gemini"},
        {**_RESOLVED_TIER, "extra": 1},
        {**_RESOLVED_TIER, "temperature": None},
        {**_RESOLVED_TIER, "effort": 3},
        {**_RESOLVED_TIER, "model_id": ""},
    ],
    ids=["tier", "family", "extra-key", "retired-temperature", "effort-type", "blank-model"],
)
def test_resolved_tiers_have_closed_keys_and_known_values(tier: object) -> None:
    """A resolved tier with an unknown tier/family, open key set or bad value is rejected."""
    with pytest.raises(ValueError, match="resolved_tiers"):
        _accounted(resolved_tiers=(tier,))


def test_cost_withheld_must_be_boolean() -> None:
    """A truthy non-boolean flag is rejected rather than read as withheld."""
    with pytest.raises(ValueError, match="cost_withheld"):
        _accounted(cost_withheld=1)


def test_sse_line_refuses_non_finite_json() -> None:
    """``format_sse_line`` serialises with ``allow_nan=False``, so no frame can carry ``NaN``."""
    event = _accounted()
    object.__setattr__(event, "cost_usd", float("nan"))
    with pytest.raises(ValueError):
        format_sse_line(event)


def test_replay_unavailable_is_a_declared_kind_and_carries_no_fields() -> None:
    """A ``replay_unavailable`` event renders every field but ``kind`` null or false."""
    assert "replay_unavailable" in EVENT_KINDS
    payload = event_payload(AgentEvent(kind="replay_unavailable"))
    assert payload["kind"] == "replay_unavailable"
    assert {key for key, value in payload.items() if value not in (None, False)} == {"kind"}


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("text", "x"),
        ("event_id", "e-1"),
        ("partial", True),
        ("truncated", True),
        ("input_tokens", 1),
        ("tool_name", "t"),
        ("tool_output", {"items": []}),
        ("proposal_id", "p-1"),
        ("error_kind", "tool_failure"),
        ("error_message", "m"),
        ("partial_work", "w"),
        ("cost_usd", 0.1),
        ("cost_withheld", False),
        ("resolved_tiers", (_RESOLVED_TIER,)),
    ],
)
def test_replay_unavailable_rejects_any_field(field_name: str, value: object) -> None:
    """A ``replay_unavailable`` event carrying any kind-specific field is rejected."""
    with pytest.raises(ValueError):
        AgentEvent(kind="replay_unavailable", **{field_name: value})  # type: ignore[arg-type]


_C0_EXCEPT_TAB_NEWLINE = "".join(chr(code) for code in range(0x20) if code not in (0x09, 0x0A))


def test_strip_control_chars_removes_c0_and_del_but_keeps_tab_and_newline() -> None:
    """Every C0 character except tab and newline, plus DEL, is removed; nothing else changes."""
    raw = f"a{_C0_EXCEPT_TAB_NEWLINE}b\x7fc\td\ne\u0080f\u00e9"
    assert strip_control_chars(raw) == "abc\td\ne\u0080f\u00e9"


def test_strip_control_chars_leaves_clean_text_unchanged() -> None:
    clean = "Pipeline is $1,200\tfor Q3.\nNext line — ok."
    assert strip_control_chars(clean) == clean


@pytest.mark.parametrize(
    ("kind", "fields", "field_name"),
    [
        ("completed", {}, "text"),
        ("text_delta", {"event_id": "e-1", "partial": True}, "text"),
        ("error", {"error_kind": "tool_failure"}, "error_message"),
    ],
)
def test_text_bearing_events_strip_control_chars(
    kind: str, fields: dict[str, object], field_name: str
) -> None:
    """Each text-bearing field is stored, and rendered on the wire, without control characters."""
    event = AgentEvent(
        kind=kind,  # type: ignore[arg-type]
        **{field_name: "ok\x1b[31m\x00\ttab\nline\x7f"},  # type: ignore[arg-type]
        **fields,  # type: ignore[arg-type]
    )
    assert getattr(event, field_name) == "ok[31m\ttab\nline"
    wire = json.loads(format_sse_line(event).removeprefix("data: "))
    assert wire[field_name] == "ok[31m\ttab\nline"


def test_budget_exhausted_event_strips_control_chars_from_both_text_fields() -> None:
    event = budget_exhausted_event(partial_work="one\x07 write", error_message="cap\x08 reached")
    assert event.partial_work == "one write"
    assert event.error_message == "cap reached"


_DROPPED_NOTICE = "The tool result was too large to include in full and was truncated."


def test_a_byte_cut_list_page_with_a_cursor_says_more_records_follow() -> None:
    page: dict[str, object] = {
        "body": {"activities": [{"activity_id": "a"}], "truncated": True, "next_cursor": "c"}
    }
    notice = describe_truncation(page, event_truncated=False)
    assert notice is not None
    assert "more records follow" in notice and "next_cursor" in notice
    assert "truncated" not in notice and "too large" not in notice


def test_a_cursor_continuable_search_page_does_not_claim_the_search_stopped_early() -> None:
    page: dict[str, object] = {
        "body": {"items": [], "truncated": True, "examined_count": 2000, "next_cursor": "c"}
    }
    notice = describe_truncation(page, event_truncated=False)
    assert notice is not None
    assert "2000" in notice and "more records follow" in notice
    assert "stopped early" not in notice and "may be included" not in notice


@pytest.mark.parametrize("next_cursor", [None, ""])
def test_a_flagged_page_without_a_cursor_keeps_the_incomplete_notices(
    next_cursor: str | None,
) -> None:
    listing: dict[str, object] = {
        "body": {"activities": [], "truncated": True, "next_cursor": next_cursor}
    }
    search: dict[str, object] = {
        "body": {"items": [], "truncated": True, "examined_count": 7, "next_cursor": None}
    }
    assert describe_truncation(listing, event_truncated=False) == _DROPPED_NOTICE
    notice = describe_truncation(search, event_truncated=False)
    assert notice is not None and notice.startswith("Search stopped early after examining 7")


def test_a_page_trimmed_by_the_byte_ceiling_is_described_as_truncated_even_with_a_cursor() -> None:
    page: dict[str, object] = {"body": {"activities": [], "truncated": True, "next_cursor": "c"}}
    assert describe_truncation(page, event_truncated=True) == _DROPPED_NOTICE


def test_an_unflagged_page_with_a_cursor_has_no_notice() -> None:
    page: dict[str, object] = {"body": {"activities": [{"activity_id": "a"}], "next_cursor": "c"}}
    assert describe_truncation(page, event_truncated=False) is None

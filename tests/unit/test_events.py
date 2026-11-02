# SPDX-License-Identifier: MIT
"""Tests for the cost/tier fields and the ``heartbeat`` kind on :class:`agents.events.AgentEvent`."""

from __future__ import annotations

import json

import pytest

from agents.events import EVENT_KINDS, AgentEvent, CostLine, ResolvedTierInfo
from agents.runtime_entrypoint import event_payload, format_sse_line

_COST_LINE: CostLine = {
    "tier": "subagent",
    "role": "research",
    "model_id": "subagent-model",
    "input_tokens": 120,
    "output_tokens": 40,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
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
    withheld_line: CostLine = {**_COST_LINE, "cost_usd": None}
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

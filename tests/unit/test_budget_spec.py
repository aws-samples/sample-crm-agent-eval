# SPDX-License-Identifier: MIT
"""Tests for :class:`agents.budget.TurnBudgetSpec` cap validation."""

from __future__ import annotations

import math

import pytest

from agents.budget import CHAT_BUDGET, EVAL_BUDGET, TurnBudget, TurnBudgetSpec


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("max_tool_calls", 0),
        ("max_tool_calls", -1),
        ("max_tool_calls", True),
        ("max_tool_calls", 2.5),
        ("max_tokens", 0),
        ("max_tokens", -10),
        ("max_wall_clock_seconds", 0.0),
        ("max_wall_clock_seconds", -1.0),
        ("max_wall_clock_seconds", math.nan),
        ("max_wall_clock_seconds", math.inf),
    ],
)
def test_spec_rejects_a_cap_that_is_not_finite_and_positive(field_name: str, value: object) -> None:
    """Zero, negative, boolean, fractional-count and non-finite caps are refused at construction."""
    caps: dict[str, object] = {"max_tool_calls": 5, "max_wall_clock_seconds": 10.0}
    caps[field_name] = value
    with pytest.raises(ValueError, match=field_name):
        TurnBudgetSpec(**caps)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_budget_rejects_a_non_finite_wall_clock(value: float) -> None:
    """``TurnBudget`` applies the same rule, so a NaN deadline can never be built directly."""
    with pytest.raises(ValueError, match="max_wall_clock_seconds"):
        TurnBudget(max_wall_clock_seconds=value)


@pytest.mark.parametrize("spec", [CHAT_BUDGET, EVAL_BUDGET], ids=["chat", "eval"])
def test_from_spec_carries_every_cap(spec: TurnBudgetSpec) -> None:
    """A budget built from a spec has exactly the spec's caps."""
    budget = TurnBudget.from_spec(spec, clock=lambda: 0.0)
    assert (budget.max_tool_calls, budget.max_tokens, budget.max_wall_clock_seconds) == (
        spec.max_tool_calls,
        spec.max_tokens,
        spec.max_wall_clock_seconds,
    )

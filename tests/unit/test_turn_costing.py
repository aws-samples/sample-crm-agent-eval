# SPDX-License-Identifier: MIT
"""Tests for :mod:`agents.turn_costing`: chat cost lines priced by ``crm.metering``."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from agents.events import CostLine
from agents.tier_attribution import CacheCounts, InvocationAttribution, TokenCounts
from agents.turn_costing import DEFAULT_PRICING_PATH, cost_lines, is_cost_withheld, turn_cost
from crm.metering import TokenUsage, cost_for_usage
from providers.capabilities import load_capability_matrix

_MATRIX = load_capability_matrix()
OPUS = _MATRIX.model_for("anthropic", "supervisor").model_id
HAIKU = _MATRIX.model_for("anthropic", "tool").model_id
LUNA = _MATRIX.model_for("openai", "tool").model_id


def _inv(
    tier: str, role: str, model_id: str, tokens_in: int, tokens_out: int, cache_read: int = 0
) -> InvocationAttribution:
    return InvocationAttribution(
        tier=tier,  # type: ignore[arg-type]
        role=role,
        model_id=model_id,
        tokens=TokenCounts(input_tokens=tokens_in, output_tokens=tokens_out),
        cache=CacheCounts(read_tokens=cache_read),
    )


def _price(model_id: str, tokens_in: int, tokens_out: int, cache_read: int = 0) -> float:
    usage = TokenUsage(
        input_tokens=tokens_in, output_tokens=tokens_out, cache_read_tokens=cache_read
    )
    return cost_for_usage(usage, model_id).total


def _table(tmp_path: Path, *, provisional: object) -> Path:
    text = DEFAULT_PRICING_PATH.read_text(encoding="utf-8")
    lines = [
        f"provisional: {str(provisional).lower()}" if line.startswith("provisional:") else line
        for line in text.splitlines()
    ]
    path = tmp_path / "pricing.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_lines_group_by_tier_role_and_model_and_price_each_invocation() -> None:
    """Two supervisor rounds merge; each group's cost equals the sum of per-invocation prices."""
    invocations = [
        _inv("supervisor", "supervisor", OPUS, 100, 20),
        _inv("subagent", "research", HAIKU, 40, 10, cache_read=7),
        _inv("supervisor", "supervisor", OPUS, 50, 15),
        _inv("tool", "research", LUNA, 8, 4),
    ]
    lines = cost_lines(invocations, withheld=False)
    assert [(line["tier"], line["role"], line["model_id"]) for line in lines] == [
        ("supervisor", "supervisor", OPUS),
        ("subagent", "research", HAIKU),
        ("tool", "research", LUNA),
    ]
    supervisor = lines[0]
    assert (supervisor["input_tokens"], supervisor["output_tokens"]) == (150, 35)
    assert supervisor["cost_usd"] == pytest.approx(_price(OPUS, 100, 20) + _price(OPUS, 50, 15))
    assert lines[1]["cache_read_tokens"] == 7
    assert lines[1]["cost_usd"] == pytest.approx(_price(HAIKU, 40, 10, cache_read=7))
    assert turn_cost(lines, withheld=False) == pytest.approx(
        sum(line["cost_usd"] or 0.0 for line in lines)
    )


def test_same_model_in_two_roles_is_two_lines() -> None:
    """The role is part of the grouping key."""
    lines = cost_lines(
        [_inv("subagent", "research", HAIKU, 1, 1), _inv("subagent", "analytics", HAIKU, 1, 1)],
        withheld=False,
    )
    assert [line["role"] for line in lines] == ["research", "analytics"]


def test_zero_token_records_are_skipped() -> None:
    """A structural zero-token record (blank model) yields no line."""
    assert cost_lines([_inv("tool", "research", "", 0, 0)], withheld=False) == ()


def test_withheld_pricing_leaves_every_cost_unset() -> None:
    """While pricing is withheld, lines carry tokens but no cost and the total is ``None``."""
    lines = cost_lines([_inv("supervisor", "supervisor", OPUS, 10, 10)], withheld=True)
    assert lines[0]["cost_usd"] is None
    assert lines[0]["input_tokens"] == 10
    assert turn_cost(lines, withheld=True) is None


def test_unpriced_model_raises() -> None:
    """A model missing from the price table is an error, never a silent zero."""
    with pytest.raises(KeyError):
        cost_lines([_inv("tool", "research", "vendor.unknown-model", 1, 1)], withheld=False)


def test_turn_cost_refuses_an_unpriced_line_when_not_withheld() -> None:
    """Mixing an unpriced line into a published total is refused."""
    lines = cost_lines([_inv("supervisor", "supervisor", OPUS, 1, 1)], withheld=True)
    with pytest.raises(ValueError, match="unpriced"):
        turn_cost(lines, withheld=False)


def test_turn_cost_refuses_a_non_finite_total() -> None:
    """A non-finite total is refused rather than published."""
    lines = cost_lines([_inv("supervisor", "supervisor", OPUS, 1, 1)], withheld=False)
    poisoned: tuple[CostLine, ...] = ({**lines[0], "cost_usd": math.inf},)
    with pytest.raises(ValueError, match="not finite"):
        turn_cost(poisoned, withheld=False)


@pytest.mark.parametrize("provisional", [True, False])
def test_is_cost_withheld_reads_the_provisional_flag(tmp_path: Path, provisional: bool) -> None:
    """The flag is read from the table, both values."""
    assert is_cost_withheld(pricing_path=_table(tmp_path, provisional=provisional)) is provisional


def test_is_cost_withheld_rejects_a_non_boolean_flag(tmp_path: Path) -> None:
    """A malformed flag fails loudly instead of defaulting either way."""
    with pytest.raises(ValueError, match="provisional"):
        is_cost_withheld(pricing_path=_table(tmp_path, provisional='"maybe"'))

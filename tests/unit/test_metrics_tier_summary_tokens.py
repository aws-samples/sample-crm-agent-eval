# SPDX-License-Identifier: MIT
"""Tests for the per-tier prompt and cache token totals in :mod:`eval.metrics`.

Anthropic reports input tokens exclusive of cache tokens; OpenAI reports them inclusive. The
total prompt figure is summed per call, so a call whose cache counts exceed its reported input
clamps on its own rather than against the tier's sums.
"""

from __future__ import annotations

import pytest

from agents.tier_attribution import CacheCounts, InvocationAttribution, TokenCounts
from eval.metrics import (
    aggregate_tier_role_model,
    aggregate_tier_summary,
    attribution_prompt_tokens,
    not_measured_latency_metrics,
)

_ANTHROPIC = "anthropic.claude-sonnet-5-5"
_OPENAI = "openai.gpt-6.2-sol"


def _call(
    tier: str,
    model_id: str,
    *,
    input_tokens: int,
    read: int = 0,
    write: int = 0,
    output_tokens: int = 5,
    role: str | None = None,
) -> InvocationAttribution:
    return InvocationAttribution(
        tier=tier,  # type: ignore[arg-type]
        role=role or ("supervisor" if tier == "supervisor" else "research"),
        model_id=model_id,
        tokens=TokenCounts(input_tokens=input_tokens, output_tokens=output_tokens),
        cache=CacheCounts(read_tokens=read, write_tokens=write),
    )


_ANTHROPIC_SUPERVISOR = (
    _call("supervisor", _ANTHROPIC, input_tokens=100, read=50, write=20),
    _call("supervisor", _ANTHROPIC, input_tokens=30),
)
_OPENAI_TOOL = (
    _call("tool", _OPENAI, input_tokens=100, read=60),
    _call("tool", _OPENAI, input_tokens=10, read=30),
)


def _summary(calls: tuple[InvocationAttribution, ...]) -> dict[str, dict[str, object]]:
    rows = aggregate_tier_role_model(calls, withhold_cost=True)
    summary = aggregate_tier_summary(rows, not_measured_latency_metrics(), (), withhold_cost=True)
    fields = summary.to_report_fields()
    return {tier: entry for tier, entry in fields.items() if isinstance(entry, dict)}


def test_an_exclusive_call_prompt_adds_cache_read_and_write_to_input() -> None:
    assert attribution_prompt_tokens(_ANTHROPIC_SUPERVISOR[0]) == 170
    assert attribution_prompt_tokens(_ANTHROPIC_SUPERVISOR[1]) == 30


def test_an_inclusive_call_prompt_is_its_reported_input() -> None:
    assert attribution_prompt_tokens(_OPENAI_TOOL[0]) == 100


def test_an_inclusive_call_whose_cache_exceeds_its_input_clamps_per_call() -> None:
    assert attribution_prompt_tokens(_OPENAI_TOOL[1]) == 30


def test_a_zero_token_call_counts_zero_without_a_model_id() -> None:
    structural = _call("subagent", "", input_tokens=0, output_tokens=0)
    assert attribution_prompt_tokens(structural) == 0


def test_a_token_bearing_call_of_no_known_family_is_refused() -> None:
    with pytest.raises(ValueError, match="no recognised provider-family prefix"):
        attribution_prompt_tokens(_call("tool", "acme.model-1", input_tokens=1))


def test_a_row_reports_its_summed_per_call_prompt_tokens() -> None:
    (row,) = aggregate_tier_role_model(_ANTHROPIC_SUPERVISOR, withhold_cost=True)
    assert row.prompt_tokens == 200
    assert row.to_report_fields()["prompt_tokens"] == 200


def test_a_row_reports_prompt_tokens_right_after_cache_write_tokens() -> None:
    (row,) = aggregate_tier_role_model(_ANTHROPIC_SUPERVISOR, withhold_cost=True)
    keys = list(row.to_report_fields())
    assert keys[keys.index("cache_write_tokens") + 1] == "prompt_tokens"


def test_an_anthropic_tier_totals_pin_every_figure() -> None:
    entry = _summary(_ANTHROPIC_SUPERVISOR)["supervisor"]
    assert entry["input_tokens"] == 130
    assert entry["total_prompt_tokens"] == 200
    assert entry["cache_read_tokens"] == 50
    assert entry["cache_write_tokens"] == 20
    assert entry["output_tokens"] == 10


def test_an_openai_tier_totals_pin_every_figure_with_the_per_call_clamp() -> None:
    entry = _summary(_OPENAI_TOOL)["tool"]
    assert entry["input_tokens"] == 110
    assert entry["total_prompt_tokens"] == 130
    assert entry["cache_read_tokens"] == 90
    assert entry["cache_write_tokens"] == 0
    assert entry["output_tokens"] == 10


def test_each_tier_totals_only_its_own_calls() -> None:
    summary = _summary(_ANTHROPIC_SUPERVISOR + _OPENAI_TOOL)
    assert summary["supervisor"]["total_prompt_tokens"] == 200
    assert summary["tool"]["total_prompt_tokens"] == 130
    assert summary["subagent"]["total_prompt_tokens"] == 0
    assert summary["subagent"]["cache_read_tokens"] == 0
    assert summary["subagent"]["cache_write_tokens"] == 0


def test_a_tier_sums_prompt_tokens_across_its_role_and_model_rows() -> None:
    calls = (
        _call("subagent", _ANTHROPIC, input_tokens=10, read=4, write=2, role="research"),
        _call("subagent", _OPENAI, input_tokens=20, read=25, role="mutation"),
    )
    entry = _summary(calls)["subagent"]
    assert entry["total_prompt_tokens"] == 16 + 25
    assert entry["cache_read_tokens"] == 29
    assert entry["cache_write_tokens"] == 2


def test_a_tier_entry_reports_token_keys_in_the_fixed_order() -> None:
    keys = list(_summary(_ANTHROPIC_SUPERVISOR)["supervisor"])
    assert keys[:6] == [
        "input_tokens",
        "total_prompt_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "output_tokens",
        "cost_total",
    ]

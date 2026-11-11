# SPDX-License-Identifier: MIT
"""Tests for :mod:`agents.prompt_estimate`: the byte-based prompt estimate and the refusal check."""

from __future__ import annotations

import json
import math

import pytest

from agents.budget import RequestRefusedError, TurnBudget
from agents.prompt_estimate import (
    BYTES_PER_TOKEN_ESTIMATE,
    estimate_prompt_tokens,
    refuse_if_over_limit,
)


def _bytes(messages: list[dict[str, object]], tools: list[dict[str, object]]) -> int:
    """The byte length the estimate is defined over, computed independently of the module."""
    return len(
        json.dumps(
            [messages, tools], ensure_ascii=False, separators=(",", ":"), default=str
        ).encode("utf-8")
    )


def _messages_of_bytes(total: int) -> list[dict[str, object]]:
    """One user message whose request encodes to exactly ``total`` bytes with no tools."""
    base = _bytes([{"role": "user", "content": ""}], [])
    return [{"role": "user", "content": "x" * (total - base)}]


def test_three_bytes_count_as_one_token() -> None:
    assert BYTES_PER_TOKEN_ESTIMATE == 3


@pytest.mark.parametrize(("total", "expected"), [(300, 100), (301, 101), (302, 101), (303, 101)])
def test_the_estimate_rounds_bytes_up_to_whole_tokens(total: int, expected: int) -> None:
    messages = _messages_of_bytes(total)
    assert _bytes(messages, []) == total
    assert estimate_prompt_tokens(messages, []) == expected


def test_tool_schemas_are_part_of_the_estimate() -> None:
    messages: list[dict[str, object]] = [{"role": "user", "content": "find acme"}]
    tools: list[dict[str, object]] = [
        {"name": "search_accounts", "description": "d" * 600, "input_schema": {"type": "object"}}
    ]
    assert estimate_prompt_tokens(messages, tools) - estimate_prompt_tokens(messages, []) >= 200
    assert estimate_prompt_tokens(messages, tools) == math.ceil(_bytes(messages, tools) / 3)


def test_non_ascii_text_is_measured_in_utf8_bytes() -> None:
    ascii_messages: list[dict[str, object]] = [{"role": "user", "content": "a" * 30}]
    cjk_messages: list[dict[str, object]] = [{"role": "user", "content": "漢" * 30}]
    assert (
        estimate_prompt_tokens(cjk_messages, []) - estimate_prompt_tokens(ascii_messages, []) == 20
    )


def test_a_value_json_cannot_encode_is_rendered_with_str() -> None:
    class _Opaque:
        def __str__(self) -> str:
            return "o" * 90

    messages: list[dict[str, object]] = [{"role": "user", "content": _Opaque()}]
    assert estimate_prompt_tokens(messages, []) == math.ceil(_bytes(messages, []) / 3)


def test_a_request_at_the_limit_is_admitted() -> None:
    budget = TurnBudget()
    messages = _messages_of_bytes(3 * 1_000)
    refuse_if_over_limit(messages, [], limit=1_000, tier="tool", role="research", budget=budget)
    assert not budget.is_exhausted()


def test_a_request_one_token_over_the_limit_is_refused_and_recorded() -> None:
    budget = TurnBudget()
    messages = _messages_of_bytes(3 * 1_000 + 1)
    with pytest.raises(RequestRefusedError, match="1001 prompt tokens exceeds the tool tier"):
        refuse_if_over_limit(messages, [], limit=1_000, tier="tool", role="research", budget=budget)
    assert budget.exhausted_dimensions() == ("request_prompt_limit",)
    refusal = budget.refused_request
    assert refusal is not None
    assert (refusal.estimated_prompt_tokens, refusal.limit, refusal.tier, refusal.role) == (
        1_001,
        1_000,
        "tool",
        "research",
    )


def test_a_refusal_without_a_budget_still_raises() -> None:
    with pytest.raises(RequestRefusedError):
        refuse_if_over_limit(
            _messages_of_bytes(30), [], limit=1, tier="subagent", role="research", budget=None
        )

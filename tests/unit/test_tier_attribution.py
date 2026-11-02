# SPDX-License-Identifier: MIT
"""Unit tests for :mod:`agents.tier_attribution`.

A token-bearing invocation requires a non-blank ``model_id``; a zero-token record does not. Also
covers value-object validation, frozen shape, and ``OutcomeKind`` parity with the supervisor.
"""

from __future__ import annotations

import dataclasses
from typing import cast, get_args

import pytest

from agents.supervisor import SubagentOutcomeKind
from agents.tier_attribution import (
    CacheCounts,
    InvocationAttribution,
    OutcomeKind,
    TokenCounts,
    ToolCallAttribution,
)
from providers.capabilities import Tier


class TestTokenCounts:
    def test_valid_counts_and_any_tokens(self) -> None:
        assert TokenCounts(input_tokens=10, output_tokens=0).any_tokens is True
        assert TokenCounts(input_tokens=0, output_tokens=0).any_tokens is False

    @pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
    def test_negative_raises(self, field: str) -> None:
        kwargs = {"input_tokens": 0, "output_tokens": 0, field: -1}
        with pytest.raises(ValueError, match=f"{field} must be non-negative"):
            TokenCounts(**kwargs)

    def test_is_frozen(self) -> None:
        tokens = TokenCounts(input_tokens=1, output_tokens=1)
        with pytest.raises(dataclasses.FrozenInstanceError):
            tokens.input_tokens = 2  # type: ignore[misc]

    def test_has_no_instance_dict_slots(self) -> None:
        assert not hasattr(TokenCounts(input_tokens=1, output_tokens=1), "__dict__")


class TestCacheCounts:
    def test_defaults_to_zero_and_any_tokens(self) -> None:
        assert CacheCounts().any_tokens is False
        assert CacheCounts(read_tokens=5).any_tokens is True
        assert CacheCounts(write_tokens=5).any_tokens is True

    @pytest.mark.parametrize("field", ["read_tokens", "write_tokens"])
    def test_negative_raises(self, field: str) -> None:
        with pytest.raises(ValueError, match=f"{field} must be non-negative"):
            CacheCounts(**{field: -1})

    def test_is_frozen_and_slotted(self) -> None:
        cache = CacheCounts(read_tokens=1)
        with pytest.raises(dataclasses.FrozenInstanceError):
            cache.read_tokens = 2  # type: ignore[misc]
        assert not hasattr(cache, "__dict__")


class TestInvocationAttribution:
    def _valid(self, **overrides: object) -> InvocationAttribution:
        kwargs: dict[str, object] = {
            "tier": "supervisor",
            "role": "supervisor",
            "model_id": "anthropic.claude-opus-5-5",
            "tokens": TokenCounts(input_tokens=100, output_tokens=20),
            "cache": CacheCounts(read_tokens=10),
            "latency_ms": 42.5,
        }
        kwargs.update(overrides)
        return InvocationAttribution(**kwargs)  # type: ignore[arg-type]

    def test_valid_record(self) -> None:
        record = self._valid()
        assert record.tier == "supervisor"
        assert record.model_id == "anthropic.claude-opus-5-5"
        assert record.bears_tokens is True

    def test_token_bearing_without_model_id_raises(self) -> None:
        # The headline rule: input tokens present, attribution absent.
        with pytest.raises(ValueError, match="cannot be recorded without attribution"):
            self._valid(model_id="")

    def test_output_tokens_alone_without_model_id_raises(self) -> None:
        with pytest.raises(ValueError, match="without attribution"):
            self._valid(
                model_id="   ",
                tokens=TokenCounts(input_tokens=0, output_tokens=7),
                cache=CacheCounts(),
            )

    def test_cache_tokens_alone_without_model_id_raises(self) -> None:
        with pytest.raises(ValueError, match="without attribution"):
            self._valid(
                model_id="",
                tokens=TokenCounts(input_tokens=0, output_tokens=0),
                cache=CacheCounts(read_tokens=1),
            )

    def test_zero_token_structural_record_allows_blank_model_id(self) -> None:
        # A tier that made no call: representable at count 0 with no model named.
        record = self._valid(
            tier="tool",
            role="tool",
            model_id="",
            tokens=TokenCounts(input_tokens=0, output_tokens=0),
            cache=CacheCounts(),
            latency_ms=None,
        )
        assert record.bears_tokens is False
        assert record.model_id == ""

    def test_blank_role_raises_even_at_zero_tokens(self) -> None:
        with pytest.raises(ValueError, match="role must be a non-empty attribution"):
            self._valid(
                role="  ",
                model_id="",
                tokens=TokenCounts(input_tokens=0, output_tokens=0),
                cache=CacheCounts(),
            )

    def test_unknown_tier_raises(self) -> None:
        with pytest.raises(ValueError, match="tier must be one of"):
            self._valid(tier=cast(Tier, "orchestrator"))

    def test_negative_latency_raises(self) -> None:
        with pytest.raises(ValueError, match="latency_ms must be non-negative"):
            self._valid(latency_ms=-0.1)

    def test_none_latency_allowed(self) -> None:
        assert self._valid(latency_ms=None).latency_ms is None

    def test_is_frozen_and_slotted(self) -> None:
        record = self._valid()
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.model_id = "other"  # type: ignore[misc]
        assert not hasattr(record, "__dict__")


class TestToolCallAttribution:
    def _valid(self, **overrides: object) -> ToolCallAttribution:
        kwargs: dict[str, object] = {
            "tool_name": "search_accounts",
            "role": "research",
            "latency_ms": 12.0,
            "outcome_kind": "data",
            "truncated": False,
        }
        kwargs.update(overrides)
        return ToolCallAttribution(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.parametrize("kind", ["data", "no_match", "denied", "tool_failure"])
    def test_valid_for_every_outcome_kind(self, kind: str) -> None:
        assert self._valid(outcome_kind=cast(OutcomeKind, kind)).outcome_kind == kind

    def test_blank_tool_name_raises(self) -> None:
        with pytest.raises(ValueError, match="tool_name must be a non-empty attribution"):
            self._valid(tool_name="")

    def test_blank_role_raises(self) -> None:
        with pytest.raises(ValueError, match="role must be a non-empty attribution"):
            self._valid(role="  ")

    def test_unknown_outcome_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="outcome_kind must be one of"):
            self._valid(outcome_kind=cast(OutcomeKind, "success"))

    def test_negative_latency_raises(self) -> None:
        with pytest.raises(ValueError, match="latency_ms must be non-negative"):
            self._valid(latency_ms=-1.0)

    def test_none_latency_allowed_and_truncated_carried(self) -> None:
        record = self._valid(latency_ms=None, truncated=True)
        assert record.latency_ms is None
        assert record.truncated is True

    def test_is_frozen_and_slotted(self) -> None:
        record = self._valid()
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.outcome_kind = "denied"  # type: ignore[misc]
        assert not hasattr(record, "__dict__")


def test_outcome_kind_matches_supervisor_taxonomy() -> None:
    """The local OutcomeKind is exactly the supervisor's four-way delegation taxonomy."""
    assert set(get_args(OutcomeKind)) == set(get_args(SubagentOutcomeKind))

# SPDX-License-Identifier: MIT
"""Tests for per-family cached-read token accounting and its effect on cost.

Pins each family's declared accounting (exclusive or inclusive of cached reads), that every family
has one, and that a publishable currency figure is refused under an unconfirmed declaration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, get_args

import pytest
import yaml

from crm.metering import (
    TOKEN_ACCOUNTING,
    CacheReadAccounting,
    Cost,
    Family,
    TokenAccounting,
    TokenUsage,
    UnconfirmedTokenAccountingError,
    billable_input_tokens,
    build_usage_record,
    cost_for_usage,
    family_for_model_id,
    token_accounting_for,
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_PRICING_PATH: Final[Path] = _REPO_ROOT / "config" / "pricing.yaml"

#: Distinct non-zero per-category rates, so a wrong charge base shows in the computed cost.
_INPUT_RATE: Final[float] = 3.0
_OUTPUT_RATE: Final[float] = 15.0
_CACHE_WRITE_RATE: Final[float] = 3.75
_CACHE_READ_RATE: Final[float] = 0.3

_MODEL_ID: Final[str] = "anthropic.claude-opus-5-5"
_OPENAI_MODEL_ID: Final[str] = "openai.gpt-6-astra"
_PROFILE: Final[str] = f"global.{_MODEL_ID}"
_OPENAI_PROFILE: Final[str] = f"global.{_OPENAI_MODEL_ID}"
_USAGE: Final[TokenUsage] = TokenUsage(
    input_tokens=1_000_000,
    output_tokens=100_000,
    cache_write_tokens=50_000,
    cache_read_tokens=400_000,
)


def _price_table(tmp_path: Path, *, provisional: bool) -> Path:
    """Write a price table with one model per family, non-zero rates and a ``provisional`` flag."""
    rates = {
        "input": _INPUT_RATE,
        "output": _OUTPUT_RATE,
        "cache_write": _CACHE_WRITE_RATE,
        "cache_read": _CACHE_READ_RATE,
    }
    payload: dict[str, Any] = {
        "schema_version": 2,
        "price_table_version": "test-1",
        "provisional": provisional,
        "currency": "USD",
        "unit": "per_1m_tokens",
        "routes": {"global": {"multiplier": 1.0}, "geo": {"multiplier": 1.10}},
        "models": {_MODEL_ID: dict(rates), _OPENAI_MODEL_ID: dict(rates)},
    }
    path = tmp_path / "pricing.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _declare(
    monkeypatch: pytest.MonkeyPatch, family: Family, accounting: CacheReadAccounting
) -> None:
    """Replace one family's declared accounting for the duration of a test, evidence unchanged."""
    declared = TOKEN_ACCOUNTING[family]
    monkeypatch.setitem(
        TOKEN_ACCOUNTING,
        family,
        TokenAccounting(
            family=declared.family,
            cache_read_usage_field=declared.cache_read_usage_field,
            cache_read_accounting=accounting,
            evidence_direction=declared.evidence_direction,
            evidence=declared.evidence,
        ),
    )


# ---------------------------------------------------------------------------------------------
# The declaration itself
# ---------------------------------------------------------------------------------------------


def test_every_provider_family_carries_a_declared_accounting_with_evidence() -> None:
    """Every member of `Family` carries a declared accounting with evidence."""
    for family in get_args(Family):
        declaration = token_accounting_for(family)
        assert declaration.family == family
        assert declaration.cache_read_usage_field
        assert declaration.cache_read_accounting in get_args(CacheReadAccounting)
        assert declaration.evidence_direction in get_args(CacheReadAccounting)
        assert len(declaration.evidence) > 100, "a declaration must carry its own evidence"


def test_both_families_are_settled_from_authoritative_aws_docs() -> None:
    """Anthropic is declared exclusive and OpenAI inclusive, each with an AWS-doc quote and URL."""
    anthropic = token_accounting_for("anthropic")
    assert anthropic.cache_read_accounting == "exclusive"
    assert anthropic.evidence_direction == "exclusive"
    assert "optimizing-cost-and-latency-with-amazon-bedrock-prompt-caching" in anthropic.evidence
    assert "aws.amazon.com" in anthropic.evidence

    openai = token_accounting_for("openai")
    assert openai.cache_read_accounting == "inclusive"
    assert openai.evidence_direction == "inclusive"
    assert "input_tokens = cached_tokens + cache_write_tokens" in openai.evidence
    assert "introducing-explicit-prompt-caching-for-openai" in openai.evidence
    # The OpenAI evidence carries a caveat covering the OpenAI models in the matrix.
    assert "CAVEAT:" in openai.evidence
    assert "the OpenAI models in the matrix" in openai.evidence


def test_every_price_table_model_resolves_to_a_declared_family() -> None:
    """No priced model can reach the cost path with no accounting attached to it."""
    models = yaml.safe_load(_PRICING_PATH.read_text(encoding="utf-8"))["models"]
    assert models
    for model_id in models:
        assert family_for_model_id(model_id) in get_args(Family)


def test_an_unrecognised_model_id_names_no_family_rather_than_defaulting() -> None:
    with pytest.raises(ValueError, match="no recognised provider-family prefix"):
        family_for_model_id("mistral.some-model")


# ---------------------------------------------------------------------------------------------
# What each accounting charges
# ---------------------------------------------------------------------------------------------


def test_inclusive_accounting_subtracts_both_cached_read_and_cache_write_from_the_input_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inclusive accounting bills input_tokens - cache_read - cache_write at the input rate."""
    _declare(monkeypatch, "anthropic", "inclusive")
    path = _price_table(tmp_path, provisional=True)

    cost = cost_for_usage(_USAGE, _MODEL_ID, inference_profile=_PROFILE, pricing_path=path)

    # 1_000_000 - 400_000 cache_read - 50_000 cache_write = 550_000
    assert billable_input_tokens(_USAGE, "anthropic") == 550_000
    assert cost.input == pytest.approx(0.55 * _INPUT_RATE)
    assert cost.cache_read == pytest.approx(0.4 * _CACHE_READ_RATE)
    assert cost.cache_write == pytest.approx(0.05 * _CACHE_WRITE_RATE)
    assert cost.cache_read_accounting == "inclusive"


def test_exclusive_accounting_charges_input_cache_read_and_cache_write_additively(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exclusive accounting charges input, cache-read and cache-write counts whole at own rates."""
    _declare(monkeypatch, "anthropic", "exclusive")
    path = _price_table(tmp_path, provisional=True)

    cost = cost_for_usage(_USAGE, _MODEL_ID, inference_profile=_PROFILE, pricing_path=path)

    assert billable_input_tokens(_USAGE, "anthropic") == 1_000_000
    assert cost.input == pytest.approx(1.0 * _INPUT_RATE)
    assert cost.cache_read == pytest.approx(0.4 * _CACHE_READ_RATE)
    assert cost.cache_write == pytest.approx(0.05 * _CACHE_WRITE_RATE)
    assert cost.cache_read_accounting == "exclusive"


def test_shipped_anthropic_prices_exclusively_with_a_non_zero_cached_read(tmp_path: Path) -> None:
    """Under the shipped declaration a non-zero cached read does not reduce Anthropic's input base."""
    path = _price_table(tmp_path, provisional=False)
    cost = cost_for_usage(_USAGE, _MODEL_ID, inference_profile=_PROFILE, pricing_path=path)
    assert billable_input_tokens(_USAGE, "anthropic") == 1_000_000
    assert cost.input == pytest.approx(1.0 * _INPUT_RATE)
    assert cost.cache_read_accounting == "exclusive"


def test_shipped_openai_prices_inclusively_with_a_non_zero_cached_read(tmp_path: Path) -> None:
    """Under the shipped declaration cached reads and writes are subtracted from OpenAI's input base."""
    path = _price_table(tmp_path, provisional=False)
    cost = cost_for_usage(
        _USAGE, _OPENAI_MODEL_ID, inference_profile=_OPENAI_PROFILE, pricing_path=path
    )
    assert billable_input_tokens(_USAGE, "openai") == 550_000
    assert cost.input == pytest.approx(0.55 * _INPUT_RATE)
    assert cost.cache_read == pytest.approx(0.4 * _CACHE_READ_RATE)
    assert cost.cache_write == pytest.approx(0.05 * _CACHE_WRITE_RATE)
    assert cost.cache_read_accounting == "inclusive"


def test_unconfirmed_accounting_prices_additively_while_the_table_is_provisional(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A family declared unconfirmed prices input and cached reads additively on a provisional table."""
    _declare(monkeypatch, "anthropic", "unconfirmed")
    path = _price_table(tmp_path, provisional=True)

    cost = cost_for_usage(_USAGE, _MODEL_ID, inference_profile=_PROFILE, pricing_path=path)

    assert billable_input_tokens(_USAGE, "anthropic") == 1_000_000
    assert cost.input == pytest.approx(1.0 * _INPUT_RATE)
    assert cost.cache_read == pytest.approx(0.4 * _CACHE_READ_RATE)
    assert cost.cache_read_accounting == "unconfirmed"


def test_inclusive_cache_counts_above_input_tokens_floor_the_billable_count_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inclusive cache counts exceeding input_tokens floor the billable input count at zero."""
    _declare(monkeypatch, "anthropic", "inclusive")
    usage = TokenUsage(
        input_tokens=100, output_tokens=0, cache_read_tokens=250, cache_write_tokens=90
    )
    assert billable_input_tokens(usage, "anthropic") == 0


# ---------------------------------------------------------------------------------------------
# The publication guard
# ---------------------------------------------------------------------------------------------


def test_clearing_provisional_under_an_unconfirmed_accounting_stops_rather_than_guesses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pricing a non-provisional table under an unconfirmed accounting raises."""
    _declare(monkeypatch, "anthropic", "unconfirmed")
    path = _price_table(tmp_path, provisional=False)

    with pytest.raises(UnconfirmedTokenAccountingError) as excinfo:
        cost_for_usage(_USAGE, _MODEL_ID, inference_profile=_PROFILE, pricing_path=path)

    assert "'anthropic'" in str(excinfo.value)
    assert "TOKEN_ACCOUNTING" in str(excinfo.value)


def test_the_guard_reaches_the_sole_record_constructor_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`build_usage_record` raises under an unconfirmed accounting on a non-provisional table."""
    _declare(monkeypatch, "anthropic", "unconfirmed")
    path = _price_table(tmp_path, provisional=False)

    with pytest.raises(UnconfirmedTokenAccountingError):
        build_usage_record(
            correlation_id="c",
            turn_id="t",
            invocation_id="i",
            rep_id="r",
            model_id=_MODEL_ID,
            family="anthropic",
            tier="supervisor",
            usage=_USAGE,
            inference_profile=_PROFILE,
            pricing_path=path,
        )


def test_the_shipped_settled_families_price_a_publishable_table_without_raising(
    tmp_path: Path,
) -> None:
    """Both shipped families price a non-provisional table under their declared accounting."""
    path = _price_table(tmp_path, provisional=False)

    anthropic_cost = cost_for_usage(
        _USAGE, _MODEL_ID, inference_profile=_PROFILE, pricing_path=path
    )
    assert anthropic_cost.input == pytest.approx(1.0 * _INPUT_RATE)  # exclusive: whole input base

    openai_cost = cost_for_usage(
        _USAGE, _OPENAI_MODEL_ID, inference_profile=_OPENAI_PROFILE, pricing_path=path
    )
    assert openai_cost.input == pytest.approx(0.55 * _INPUT_RATE)  # inclusive: read+write removed


def test_a_cost_figure_never_travels_without_the_accounting_that_produced_it(
    tmp_path: Path,
) -> None:
    """A usage record's log line carries the invoked family's cache-read accounting."""
    record = build_usage_record(
        correlation_id="c",
        turn_id="t",
        invocation_id="i",
        rep_id="r",
        model_id=_MODEL_ID,
        family="anthropic",
        tier="supervisor",
        usage=_USAGE,
        inference_profile=_PROFILE,
        pricing_path=_price_table(tmp_path, provisional=True),
    )
    assert record.cost.cache_read_accounting == "exclusive"
    assert record.cost.cache_read_accounting == (
        token_accounting_for("anthropic").cache_read_accounting
    )
    assert isinstance(record.cost, Cost)

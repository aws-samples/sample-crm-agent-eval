# SPDX-License-Identifier: MIT
"""Assert long-context tier selection and route multipliers in cost_for_usage.

Prices against the shipped ``config/pricing.yaml``, with the rates restated literally so a changed
table reds the test rather than moving with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from crm.inference_route import UnknownInferenceRoute
from crm.metering import Cost, Family, TokenUsage, cost_for_usage, prompt_tokens

REPO_ROOT = Path(__file__).resolve().parents[2]
PRICING_PATH = REPO_ROOT / "config" / "pricing.yaml"

HAIKU = "anthropic.claude-haiku-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
SOL = "openai.gpt-6.1-sol"

#: (model, family, threshold, standard rates, long-context rates); rates are in/out/cw/cr.
TIERED: list[tuple[str, Family, int, tuple[float, ...], tuple[float, ...]]] = [
    (HAIKU, "anthropic", 100_000, (0.10, 0.50, 0.125, 0.01), (0.50, 2.50, 0.625, 0.05)),
    (SOL, "openai", 272_000, (2.00, 10.00, 2.50, 0.10), (4.00, 15.00, 5.00, 0.20)),
    (
        "openai.gpt-6.1-terra",
        "openai",
        272_000,
        (1.00, 5.00, 1.25, 0.05),
        (2.00, 7.50, 2.50, 0.10),
    ),
    ("openai.gpt-6-luna", "openai", 272_000, (0.10, 0.50, 0.125, 0.01), (0.20, 0.75, 0.25, 0.02)),
    (
        "openai.gpt-6-astra",
        "openai",
        272_000,
        (10.00, 50.00, 12.50, 1.00),
        (20.00, 75.00, 25.00, 2.00),
    ),
]

OUTPUT = 1_000
CACHE_WRITE = 2_000
CACHE_READ = 3_000


def _usage_with_prompt(family: Family, prompt: int) -> TokenUsage:
    """Return usage with every token class non-zero whose prompt size is exactly ``prompt``."""
    if family == "openai":
        return TokenUsage(
            input_tokens=prompt,
            output_tokens=OUTPUT,
            cache_write_tokens=CACHE_WRITE,
            cache_read_tokens=CACHE_READ,
        )
    return TokenUsage(
        input_tokens=prompt - CACHE_WRITE - CACHE_READ,
        output_tokens=OUTPUT,
        cache_write_tokens=CACHE_WRITE,
        cache_read_tokens=CACHE_READ,
    )


def _expected(usage: TokenUsage, family: Family, rates: tuple[float, ...], mult: float) -> Cost:
    """Return the cost the four rates and a multiplier should produce for ``usage``."""
    billable = usage.input_tokens
    if family == "openai":
        billable -= usage.cache_read_tokens + usage.cache_write_tokens
    per = 1_000_000
    return Cost(
        input=billable / per * rates[0] * mult,
        output=usage.output_tokens / per * rates[1] * mult,
        cache_write=usage.cache_write_tokens / per * rates[2] * mult,
        cache_read=usage.cache_read_tokens / per * rates[3] * mult,
        price_table_version="1.3.0",
        cache_read_accounting="inclusive" if family == "openai" else "exclusive",
        pricing_tier="standard",
        route="global",
    )


def _assert_categories(actual: Cost, expected: Cost) -> None:
    for field in ("input", "output", "cache_write", "cache_read"):
        assert getattr(actual, field) == pytest.approx(getattr(expected, field), rel=1e-12), field


def _price(usage: TokenUsage, model_id: str, prefix: str = "global.") -> Cost:
    return cost_for_usage(
        usage, model_id, inference_profile=f"{prefix}{model_id}", pricing_path=PRICING_PATH
    )


def test_prompt_tokens_adds_cache_tokens_for_exclusive_and_reads_inclusive_input_whole() -> None:
    """Prompt size is input+cache_read+cache_write for Anthropic and input_tokens for OpenAI."""
    usage = TokenUsage(
        input_tokens=10_000, output_tokens=5, cache_write_tokens=2_000, cache_read_tokens=3_000
    )
    assert prompt_tokens(usage, "anthropic") == 15_000
    assert prompt_tokens(usage, "openai") == 10_000


@pytest.mark.parametrize(("model_id", "family", "threshold", "standard", "long"), TIERED)
def test_prompt_exactly_at_the_threshold_is_standard(
    model_id: str,
    family: Family,
    threshold: int,
    standard: tuple[float, ...],
    long: tuple[float, ...],
) -> None:
    """A request whose prompt size equals the threshold prices at the standard rates."""
    usage = _usage_with_prompt(family, threshold)
    assert prompt_tokens(usage, family) == threshold
    cost = _price(usage, model_id)
    assert cost.pricing_tier == "standard"
    _assert_categories(cost, _expected(usage, family, standard, 1.0))


@pytest.mark.parametrize(("model_id", "family", "threshold", "standard", "long"), TIERED)
def test_one_token_over_the_threshold_prices_all_four_classes_long_context(
    model_id: str,
    family: Family,
    threshold: int,
    standard: tuple[float, ...],
    long: tuple[float, ...],
) -> None:
    """One prompt token over the threshold moves input, output and both cache classes."""
    usage = _usage_with_prompt(family, threshold + 1)
    cost = _price(usage, model_id)
    assert cost.pricing_tier == "long_context"
    assert cost.route == "global"
    _assert_categories(cost, _expected(usage, family, long, 1.0))
    standard_cost = _expected(usage, family, standard, 1.0)
    for field in ("input", "output", "cache_write", "cache_read"):
        assert getattr(cost, field) > getattr(standard_cost, field), field


def test_anthropic_crosses_the_threshold_through_cache_tokens_alone() -> None:
    """Uncached input under the threshold plus cache tokens over it is long-context."""
    usage = TokenUsage(
        input_tokens=60_000, output_tokens=100, cache_write_tokens=20_000, cache_read_tokens=30_000
    )
    assert usage.input_tokens < 100_000 < prompt_tokens(usage, "anthropic")
    assert _price(usage, HAIKU).pricing_tier == "long_context"


def test_openai_crosses_the_threshold_through_cache_tokens_alone() -> None:
    """An inclusive input whose uncached remainder is under the threshold is still long-context."""
    usage = TokenUsage(
        input_tokens=300_000,
        output_tokens=100,
        cache_write_tokens=50_000,
        cache_read_tokens=100_000,
    )
    uncached = usage.input_tokens - usage.cache_write_tokens - usage.cache_read_tokens
    assert uncached < 272_000 < prompt_tokens(usage, "openai")
    assert _price(usage, SOL).pricing_tier == "long_context"


@pytest.mark.parametrize("prefix", ["us.", "eu.", "apac.", "ca.", "jp.", "au."])
def test_geo_route_multiplies_standard_rates_by_1_10(prefix: str) -> None:
    """A geographic profile under the threshold prices at standard rates times 1.10."""
    usage = _usage_with_prompt("anthropic", 50_000)
    cost = _price(usage, HAIKU, prefix)
    assert (cost.pricing_tier, cost.route) == ("standard", "geo")
    _assert_categories(cost, _expected(usage, "anthropic", TIERED[0][3], 1.10))


def test_geo_route_multiplies_long_context_rates_by_1_10() -> None:
    """A geographic profile over the threshold prices at long-context rates times 1.10."""
    usage = _usage_with_prompt("anthropic", 100_001)
    cost = _price(usage, HAIKU, "us.")
    assert (cost.pricing_tier, cost.route) == ("long_context", "geo")
    _assert_categories(cost, _expected(usage, "anthropic", TIERED[0][4], 1.10))
    assert cost.input == pytest.approx((100_001 - 5_000) / 1_000_000 * 0.55, rel=1e-12)


def test_model_without_a_long_context_block_stays_standard_and_takes_the_geo_multiplier() -> None:
    """Sonnet 5.5 has no long-context tier: a huge geo prompt is standard rates times 1.10."""
    usage = _usage_with_prompt("anthropic", 900_000)
    cost = _price(usage, SONNET, "eu.")
    assert (cost.pricing_tier, cost.route) == ("standard", "geo")
    _assert_categories(cost, _expected(usage, "anthropic", (2.00, 10.00, 2.50, 0.10), 1.10))


@pytest.mark.parametrize(
    "profile",
    [HAIKU, "", f"uk.{HAIKU}", f"global.{SONNET}", f"us.{HAIKU}-v2"],
)
def test_unknown_prefix_bare_id_or_mismatched_profile_raises(profile: str) -> None:
    """A profile that does not resolve to a route for this model is refused, never priced."""
    usage = TokenUsage(input_tokens=10, output_tokens=10)
    with pytest.raises(UnknownInferenceRoute):
        cost_for_usage(usage, HAIKU, inference_profile=profile, pricing_path=PRICING_PATH)


def test_unknown_model_raises_even_with_a_valid_profile() -> None:
    """A model absent from the price table is refused under a well-formed profile."""
    usage = TokenUsage(input_tokens=10, output_tokens=10)
    with pytest.raises(KeyError):
        cost_for_usage(
            usage,
            "anthropic.claude-unknown",
            inference_profile="global.anthropic.claude-unknown",
            pricing_path=PRICING_PATH,
        )


def test_inference_profile_is_a_required_keyword() -> None:
    """Pricing without an inference profile is a TypeError, so nothing defaults to global."""
    usage = TokenUsage(input_tokens=10, output_tokens=10)
    with pytest.raises(TypeError):
        cost_for_usage(usage, HAIKU, pricing_path=PRICING_PATH)  # type: ignore[call-arg]


def test_a_table_without_the_route_multiplier_is_refused(tmp_path: Path) -> None:
    """A price table that declares no multiplier for the resolved route fails loudly."""
    pricing: dict[str, Any] = yaml.safe_load(PRICING_PATH.read_text(encoding="utf-8"))
    del pricing["routes"]["geo"]
    path = tmp_path / "pricing.yaml"
    path.write_text(yaml.safe_dump(pricing), encoding="utf-8")
    usage = TokenUsage(input_tokens=10, output_tokens=10)
    assert _price(usage, HAIKU).route == "global"
    with pytest.raises(KeyError, match="geo"):
        cost_for_usage(usage, HAIKU, inference_profile=f"us.{HAIKU}", pricing_path=path)

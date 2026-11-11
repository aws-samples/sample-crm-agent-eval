# SPDX-License-Identifier: MIT
"""Assert the price table is well formed, versioned, and honestly marked provisional."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PRICING_PATH = REPO_ROOT / "config" / "pricing.yaml"
CAPABILITIES_PATH = REPO_ROOT / "config" / "model_capabilities.yaml"

RATE_FIELDS = ("input", "output", "cache_write", "cache_read")


@pytest.fixture(scope="module")
def pricing() -> dict[str, Any]:
    """The parsed price table."""
    data = yaml.safe_load(PRICING_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def capability_models() -> set[str]:
    """Model identifiers declared in the capability matrix."""
    data = yaml.safe_load(CAPABILITIES_PATH.read_text(encoding="utf-8"))
    return set(data["models"])


def test_price_table_is_versioned(pricing: dict[str, Any]) -> None:
    """The table declares a non-empty price_table_version."""
    version = pricing["price_table_version"]
    assert isinstance(version, str) and version, "price_table_version must be a non-empty string"


def test_units_and_currency_are_explicit(pricing: dict[str, Any]) -> None:
    """The table's currency is USD and its unit is per 1M tokens."""
    assert pricing["currency"] == "USD"
    assert pricing["unit"] == "per_1m_tokens"


def test_every_capability_model_has_a_price_entry(
    pricing: dict[str, Any], capability_models: set[str]
) -> None:
    """The priced models are exactly the capability matrix's models."""
    assert set(pricing["models"]) == capability_models


@pytest.mark.parametrize("field", RATE_FIELDS)
def test_every_model_declares_every_rate_field(pricing: dict[str, Any], field: str) -> None:
    """Every model declares every rate field as a non-negative number."""
    for model_id, rates in pricing["models"].items():
        assert field in rates, f"{model_id} is missing the {field!r} rate"
        assert isinstance(rates[field], int | float), f"{model_id}.{field} must be numeric"
        assert rates[field] >= 0, f"{model_id}.{field} must not be negative"


def test_reasoning_tokens_are_attributed_to_output(pricing: dict[str, Any]) -> None:
    """Reasoning and thinking tokens bill as output."""
    assert pricing["reasoning_tokens_billed_as"] == "output"


def test_provisional_flag_is_consistent_with_the_rates(pricing: dict[str, Any]) -> None:
    """An all-zero table stays provisional; a non-provisional table has real rates and sourcing."""
    all_zero = all(
        rates[field] == 0 for rates in pricing["models"].values() for field in RATE_FIELDS
    )
    provisional = pricing["provisional"]
    assert isinstance(provisional, bool)

    if all_zero:
        assert provisional is True, (
            "every rate is a zero placeholder, so the table must remain provisional; "
            "populate real rates before clearing the flag"
        )

    if provisional is False:
        assert not all_zero, "a non-provisional table must carry real rates"
        assert pricing["sourced_on"] is not None, "a cleared flag requires a sourcing date"
        assert pricing["sourced_by"] is not None, "a cleared flag requires an attributed human"


def test_sourcing_provenance_fields_exist(pricing: dict[str, Any]) -> None:
    """The provenance keys are present even while unpopulated."""
    for field in ("sourced_from", "sourced_on", "sourced_by"):
        assert field in pricing, f"missing provenance field {field!r}"
    assert pricing["sourced_from"], "sourced_from must describe where rates come from"


#: The standard global rates of price table 1.2.0, which 1.3.0 must carry unchanged.
RATES_1_2_0 = {
    "anthropic.claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "anthropic.claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),
    "anthropic.claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "openai.gpt-6-astra": (10.00, 50.00, 12.50, 1.00),
    "openai.gpt-6.1-sol": (2.00, 10.00, 2.50, 0.10),
    "openai.gpt-6.1-terra": (1.00, 5.00, 1.25, 0.05),
    "openai.gpt-6-luna": (0.10, 0.50, 0.125, 0.01),
}

#: The long-context blocks: threshold, then input, output, cache-write and cache-read rates.
LONG_CONTEXT = {
    "anthropic.claude-haiku-5-5": (100_000, 0.50, 2.50, 0.625, 0.05),
    "openai.gpt-6.1-sol": (272_000, 4.00, 15.00, 5.00, 0.20),
    "openai.gpt-6.1-terra": (272_000, 2.00, 7.50, 2.50, 0.10),
    "openai.gpt-6-luna": (272_000, 0.20, 0.75, 0.25, 0.02),
    "openai.gpt-6-astra": (272_000, 20.00, 75.00, 25.00, 2.00),
}


def test_price_table_is_schema_2_version_1_3_0(pricing: dict[str, Any]) -> None:
    """The tiered, route-aware table is schema 2 at price table version 1.3.0."""
    assert pricing["schema_version"] == 2
    assert pricing["price_table_version"] == "1.3.0"
    assert pricing["sourced_on"] == "2026-11-08"


def test_every_1_2_0_standard_rate_is_unchanged(pricing: dict[str, Any]) -> None:
    """Each model's standard global rates equal the 1.2.0 values literally."""
    assert set(pricing["models"]) == set(RATES_1_2_0)
    for model_id, expected in RATES_1_2_0.items():
        rates = pricing["models"][model_id]
        assert tuple(rates[field] for field in RATE_FIELDS) == expected, model_id


def test_routes_declare_global_at_base_and_geo_at_ten_percent_premium(
    pricing: dict[str, Any],
) -> None:
    """The route multipliers are declared once: global 1.0, geo 1.10, nothing else."""
    assert pricing["routes"] == {"global": {"multiplier": 1.0}, "geo": {"multiplier": 1.10}}


def test_long_context_blocks_are_exactly_the_five_tiered_models(pricing: dict[str, Any]) -> None:
    """Haiku 5.5 and the four OpenAI models carry a long-context block; Sonnet and Opus do not."""
    with_block = {m for m, rates in pricing["models"].items() if "long_context" in rates}
    assert with_block == set(LONG_CONTEXT)
    for model_id, (threshold, *expected) in LONG_CONTEXT.items():
        block = pricing["models"][model_id]["long_context"]
        assert set(block) == {"above_prompt_tokens", *RATE_FIELDS}, model_id
        assert block["above_prompt_tokens"] == threshold, model_id
        assert [block[field] for field in RATE_FIELDS] == expected, model_id


def test_sourcing_cites_the_model_cards_and_the_bedrock_pricing_page(
    pricing: dict[str, Any],
) -> None:
    """The provenance names the long-context and geo-route sources."""
    sourced_from = pricing["sourced_from"]
    assert "model card" in sourced_from
    assert "Amazon Bedrock pricing page" in sourced_from
    assert "Geo/In-region" in sourced_from


def test_reasoning_token_comment_does_not_claim_separate_recording() -> None:
    """The price table never claims reasoning tokens are recorded apart from output tokens."""
    text = PRICING_PATH.read_text(encoding="utf-8")
    assert "output tokens and recorded separately" not in text
    assert "counted inside output_tokens" in text

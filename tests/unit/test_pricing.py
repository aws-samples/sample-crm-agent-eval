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

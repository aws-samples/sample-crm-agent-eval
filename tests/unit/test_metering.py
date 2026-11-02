# SPDX-License-Identifier: MIT
"""Usage attribution and cost computation for per-invocation metering.

Tokens and cost are attributed to model and tier, and cache read and write cost stay separable.
Cost-shape tests use a temporary price table; one test checks the shipped ``config/pricing.yaml``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from crm.metering import (
    TOKEN_ACCOUNTING,
    Cost,
    TokenUsage,
    UsageRecord,
    build_usage_record,
    cost_for_usage,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PRICING_PATH = REPO_ROOT / "config" / "pricing.yaml"
CAPABILITIES_PATH = REPO_ROOT / "config" / "model_capabilities.yaml"

ANTHROPIC_MODEL = "anthropic.claude-sonnet-5-5"
OPENAI_MODEL = "openai.gpt-6.1-sol"


def _shipped_pricing() -> dict[str, Any]:
    pricing: dict[str, Any] = yaml.safe_load(PRICING_PATH.read_text(encoding="utf-8"))
    return pricing


def _capability_models() -> dict[str, Any]:
    models: dict[str, Any] = yaml.safe_load(CAPABILITIES_PATH.read_text(encoding="utf-8"))["models"]
    return models


def _write_pricing(
    tmp_path: Path, *, provisional: bool, rates: dict[str, float], model_id: str = ANTHROPIC_MODEL
) -> Path:
    """Write a temp price table with explicit rates and ``provisional`` flag; return its path."""
    payload: dict[str, Any] = {
        "schema_version": 1,
        "price_table_version": "test-metering-1",
        "provisional": provisional,
        "currency": "USD",
        "unit": "per_1m_tokens",
        "models": {model_id: dict(rates)},
    }
    path = tmp_path / "pricing.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_shipped_pricing_table_is_final_with_real_published_rates() -> None:
    """The shipped price table is non-provisional and declares numeric rates for every model."""
    pricing = yaml.safe_load(PRICING_PATH.read_text(encoding="utf-8"))
    assert pricing["provisional"] is False
    assert pricing["models"], "the shipped table must publish rates for at least one model"
    for rates in pricing["models"].values():
        for field in ("input", "output", "cache_write", "cache_read"):
            assert isinstance(rates[field], (int, float))


def test_shipped_pricing_table_prices_exactly_the_capability_matrix_models() -> None:
    """Every model the matrix serves is priced, and no retired model keeps a rate."""
    pricing = _shipped_pricing()
    assert pricing["price_table_version"] == "1.2.0"
    assert set(pricing["models"]) == set(_capability_models())


@pytest.mark.parametrize("model_id", sorted(_capability_models()))
def test_shipped_rates_are_what_cost_for_usage_charges(model_id: str) -> None:
    """Each category's cost is its token count times the YAML rate for that model."""
    rates = _shipped_pricing()["models"][model_id]
    usage = TokenUsage(input_tokens=2_000_000, output_tokens=3_000_000)
    cost = cost_for_usage(usage, model_id, pricing_path=PRICING_PATH)
    assert cost.input == pytest.approx(2 * rates["input"])
    assert cost.output == pytest.approx(3 * rates["output"])
    cached = TokenUsage(
        input_tokens=4_000_000,
        output_tokens=0,
        cache_write_tokens=1_000_000,
        cache_read_tokens=4_000_000,
    )
    cached_cost = cost_for_usage(cached, model_id, pricing_path=PRICING_PATH)
    assert cached_cost.cache_write == pytest.approx(rates["cache_write"])
    assert cached_cost.cache_read == pytest.approx(4 * rates["cache_read"])
    assert cached_cost.price_table_version == "1.2.0"


def test_token_usage_rejects_negative_counts() -> None:
    """A negative token count cannot come from a real provider response."""
    with pytest.raises(ValueError, match="input_tokens"):
        TokenUsage(input_tokens=-1, output_tokens=0)


def test_token_usage_defaults_cache_fields_to_zero() -> None:
    """A model that never reports cache activity still produces a well-formed usage record."""
    usage = TokenUsage(input_tokens=100, output_tokens=50)
    assert usage.cache_write_tokens == 0
    assert usage.cache_read_tokens == 0


def test_cost_categories_stay_separable_not_blended(tmp_path: Path) -> None:
    """Each cost category is computed from its own rate and reported as a separate field."""
    rates = {"input": 3.0, "output": 15.0, "cache_write": 5.0, "cache_read": 0.3}
    path = _write_pricing(tmp_path, provisional=True, rates=rates)
    usage = TokenUsage(
        input_tokens=1000, output_tokens=500, cache_write_tokens=2000, cache_read_tokens=3000
    )
    cost = cost_for_usage(usage, ANTHROPIC_MODEL, pricing_path=path)
    assert isinstance(cost, Cost)
    assert cost.input == pytest.approx(1000 / 1_000_000 * 3.0)
    assert cost.output == pytest.approx(500 / 1_000_000 * 15.0)
    assert cost.cache_write == pytest.approx(2000 / 1_000_000 * 5.0)
    assert cost.cache_read == pytest.approx(3000 / 1_000_000 * 0.3)
    # Four distinct category values, so none was derived from another.
    assert len({cost.input, cost.output, cost.cache_write, cost.cache_read}) == 4
    assert cost.total == pytest.approx(
        cost.input + cost.output + cost.cache_write + cost.cache_read
    )
    assert cost.price_table_version


def test_cost_for_usage_unknown_model_raises() -> None:
    """A model absent from the price table must fail loudly, never default to a silent zero."""
    usage = TokenUsage(input_tokens=1, output_tokens=1)
    with pytest.raises(KeyError):
        cost_for_usage(usage, "not-a-real-model", pricing_path=PRICING_PATH)


@pytest.mark.parametrize(
    ("model_id", "family", "tier"),
    [
        (ANTHROPIC_MODEL, "anthropic", "subagent"),
        (OPENAI_MODEL, "openai", "subagent"),
    ],
)
def test_build_usage_record_attributes_model_and_tier(
    model_id: str, family: str, tier: str
) -> None:
    """A usage record names the model and tier it is attributed to."""
    usage = TokenUsage(input_tokens=10, output_tokens=20)
    record = build_usage_record(
        correlation_id="corr-1",
        turn_id="turn-1",
        invocation_id="inv-1",
        rep_id="rep-1",
        model_id=model_id,
        family=family,  # type: ignore[arg-type]
        tier=tier,  # type: ignore[arg-type]
        usage=usage,
        pricing_path=PRICING_PATH,
    )
    assert record.model_id == model_id
    assert record.family == family
    assert record.tier == tier
    assert record.usage is usage


def test_usage_record_requires_non_empty_identifiers() -> None:
    """A record that cannot be attributed to a turn, invocation, or rep is a programming error."""
    usage = TokenUsage(input_tokens=1, output_tokens=1)
    cost = Cost(
        input=0.0,
        output=0.0,
        cache_write=0.0,
        cache_read=0.0,
        price_table_version="v",
        cache_read_accounting="unconfirmed",
    )
    with pytest.raises(ValueError, match="rep_id"):
        UsageRecord(
            correlation_id="c",
            turn_id="t",
            invocation_id="i",
            rep_id="",
            model_id="m",
            family="anthropic",
            tier="tool",
            usage=usage,
            cost=cost,
        )


def test_cache_thresholds_differ_per_model_and_are_carried_on_the_record() -> None:
    """Usage records carry each model's cache checkpoint threshold from the capability matrix."""
    thresholds = {
        model_id: (entry["family"], entry["prompt_cache_min_checkpoint_tokens"])
        for model_id, entry in _capability_models().items()
        if entry["prompt_cache_min_checkpoint_tokens"] is not None
    }
    assert len({threshold for _, threshold in thresholds.values()}) > 1, (
        "the matrix must carry at least two distinct cache thresholds for this test to compare"
    )
    first_id, (first_family, first_threshold) = next(iter(thresholds.items()))
    second_id, (second_family, second_threshold) = next(
        (model_id, value) for model_id, value in thresholds.items() if value[1] != first_threshold
    )

    usage = TokenUsage(input_tokens=5000, output_tokens=100, cache_read_tokens=5000)
    first_record = build_usage_record(
        correlation_id="corr-2",
        turn_id="turn-2",
        invocation_id="inv-first",
        rep_id="rep-1",
        model_id=first_id,
        family=first_family,
        tier="supervisor",
        usage=usage,
        cache_min_checkpoint_tokens=first_threshold,
        pricing_path=PRICING_PATH,
    )
    second_record = build_usage_record(
        correlation_id="corr-2",
        turn_id="turn-2",
        invocation_id="inv-second",
        rep_id="rep-1",
        model_id=second_id,
        family=second_family,
        tier="tool",
        usage=usage,
        cache_min_checkpoint_tokens=second_threshold,
        pricing_path=PRICING_PATH,
    )
    assert first_record.cache_min_checkpoint_tokens == first_threshold
    assert second_record.cache_min_checkpoint_tokens == second_threshold
    assert first_record.cache_min_checkpoint_tokens != second_record.cache_min_checkpoint_tokens
    assert first_record.correlation_id == second_record.correlation_id
    assert first_record.usage.cache_read_tokens == second_record.usage.cache_read_tokens


# --------------------------------------------------------------------------------------
# redact() — structural record-payload redaction
# --------------------------------------------------------------------------------------


def test_redact_catches_a_record_under_an_unlisted_key() -> None:
    """A record nested under a key absent from ``_REDACT_VALUE_KEYS`` is redacted by shape."""
    from crm.metering import redact

    record = {"account_id": "acct-1", "name": "Acme Corp", "owner_rep_id": "rep-1"}
    cleaned = redact({"result": record})
    assert cleaned == {"result": "[REDACTED]"}


def test_redact_catches_a_record_inside_a_list_under_an_unlisted_key() -> None:
    """A record inside a list, under an unlisted key, must still be redacted."""
    from crm.metering import redact

    record = {"opportunity_id": "opp-1", "stage": "Negotiation", "value": 5000}
    cleaned = redact({"items": [record]})
    assert cleaned == {"items": ["[REDACTED]"]}


def test_redact_catches_a_bare_record_with_no_enclosing_key() -> None:
    """A record passed with no enclosing key at all must still be redacted."""
    from crm.metering import redact

    record = {"contact_id": "cont-1", "name": "Jane Doe", "status": "active"}
    assert redact(record) == "[REDACTED]"


def test_redact_still_honours_the_key_name_denylist() -> None:
    """The key-name denylist path redacts a record under a denylisted key."""
    from crm.metering import redact

    cleaned = redact({"before_snapshot": {"account_id": "acct-1", "name": "Acme"}})
    assert cleaned == {"before_snapshot": "[REDACTED]"}


def test_redact_does_not_flag_a_field_subset_with_no_primary_key_attribute() -> None:
    """A field mapping with no primary-key attribute is not treated as a record and is kept."""
    from crm.metering import redact

    fields = {"input_tokens": 10, "output_tokens": 20, "cost_total": 0.0}
    assert redact(fields) == fields


def test_redact_still_catches_a_credential_shaped_string_under_an_unlisted_key() -> None:
    """The independent shape-based secret check runs alongside the record-shape check."""
    from crm.metering import redact

    opaque_secret = "a" * 40
    cleaned = redact({"result": opaque_secret})
    assert cleaned == {"result": "[REDACTED]"}


def test_the_openai_cache_caveat_names_all_of_the_matrix_models_or_none() -> None:
    """The openai accounting caveat never names a partial subset of the matrix's OpenAI models."""
    names = {
        model_id.rsplit("-", 1)[-1]
        for model_id in _capability_models()
        if model_id.startswith("openai.")
    }
    evidence = TOKEN_ACCOUNTING["openai"].evidence.lower()
    named = {name for name in names if re.search(rf"\b{re.escape(name)}\b", evidence)}
    assert len(names) > 1
    assert named in (set(), names), f"caveat names {sorted(named)} of {sorted(names)}"

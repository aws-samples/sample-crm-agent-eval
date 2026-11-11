# SPDX-License-Identifier: MIT
"""Aggregated eval metrics: cost from the versioned price table, cache separability, no verdict.

Covers measured dimensions, exclusions counted and categorised, cache-driven cost variation
reported distinctly from model-driven variation, absolute cost withheld on request, and the
measurement-only posture (no pass/fail verdict in this module's output).
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import cast

import pytest

from agents.tier_attribution import (
    CacheCounts,
    InvocationAttribution,
    TokenCounts,
    ToolCallAttribution,
)
from crm.metering import TokenUsage, UsageRecord, build_usage_record
from eval.metrics import (
    TASK_SPREAD_REPORT_KEY,
    TIER_ROLE_MODEL_REPORT_KEY,
    TOOL_METRICS_REPORT_KEY,
    CallLatencySample,
    CostTotals,
    Exclusion,
    ExclusionCategory,
    MissingAttributionError,
    ModelMetrics,
    TaskSpread,
    UsageTotals,
    aggregate_latency_metrics,
    aggregate_model_metrics,
    aggregate_task_spread,
    aggregate_tier_role_model,
    aggregate_tool_calls,
    build_tier_report_fields,
    require_records_attributed,
    tally_exclusions,
)
from eval.pricing import (
    cache_min_checkpoint_tokens,
    inference_profile_for,
    is_cost_provisional,
)
from providers.capabilities import load_capability_matrix

REPO_ROOT = Path(__file__).resolve().parents[2]
PRICING_PATH = REPO_ROOT / "config" / "pricing.yaml"
CAPABILITIES_PATH = REPO_ROOT / "config" / "model_capabilities.yaml"

_MATRIX = load_capability_matrix()
#: The anthropic model in the matrix that no tier assigns.
OPUS = next(
    model_id
    for model_id, model in _MATRIX.models.items()
    if model.family == "anthropic"
    and model_id not in {assignment.model_id for assignment in _MATRIX.tiers["anthropic"].values()}
)
SONNET = _MATRIX.model_for("anthropic", "subagent").model_id
HAIKU = _MATRIX.model_for("anthropic", "tool").model_id
SOL = _MATRIX.model_for("openai", "subagent").model_id


def _record(model_id: str, invocation_id: str, **usage_kwargs: int) -> UsageRecord:
    """Build a priced UsageRecord against the real shipped price table, for test brevity."""
    usage = TokenUsage(
        input_tokens=usage_kwargs.get("input_tokens", 100),
        output_tokens=usage_kwargs.get("output_tokens", 50),
        cache_write_tokens=usage_kwargs.get("cache_write_tokens", 0),
        cache_read_tokens=usage_kwargs.get("cache_read_tokens", 0),
    )
    return build_usage_record(
        correlation_id="corr-1",
        turn_id="turn-1",
        invocation_id=invocation_id,
        rep_id="rep-1",
        model_id=model_id,
        family="anthropic",
        tier="subagent",
        usage=usage,
        inference_profile=inference_profile_for(model_id),
        cache_min_checkpoint_tokens=cache_min_checkpoint_tokens(model_id),
    )


# --- The shipped table is final; withheld behaviour is driven by withhold_cost ----------------


def test_shipped_price_table_is_final_not_provisional() -> None:
    """The shipped price table is not provisional, so cost is published against it."""
    assert is_cost_provisional(pricing_path=PRICING_PATH) is False


# --- Aggregation: token totals, cost totals, per-model correctness -----------------------------


def test_aggregate_sums_token_totals_across_included_records() -> None:
    """Included records' token counts sum exactly, with cache reads/writes kept separate."""
    included = [
        _record(SONNET, "inv-1", input_tokens=100, output_tokens=50, cache_read_tokens=20),
        _record(SONNET, "inv-2", input_tokens=200, output_tokens=75, cache_write_tokens=30),
    ]
    metrics = aggregate_model_metrics(SONNET, included, [], withhold_cost=False)
    assert metrics.usage.input_tokens == 300
    assert metrics.usage.output_tokens == 125
    assert metrics.usage.cache_read_tokens == 20
    assert metrics.usage.cache_write_tokens == 30
    assert metrics.included_invocation_count == 2


def test_aggregate_rejects_a_record_for_the_wrong_model() -> None:
    """A record whose model_id disagrees with the aggregate's model_id is rejected."""
    included = [_record(SONNET, "inv-1")]
    with pytest.raises(ValueError, match="expected"):
        aggregate_model_metrics(OPUS, included, [], withhold_cost=False)


def test_aggregate_with_no_included_records_reports_zero_totals() -> None:
    """A model with every invocation excluded still produces a well-formed, zeroed report."""
    metrics = aggregate_model_metrics(SONNET, [], [], withhold_cost=False)
    assert metrics.included_invocation_count == 0
    assert metrics.usage.input_tokens == 0
    assert metrics.cost is None  # no included records to price; nothing to withhold either


def test_aggregate_refuses_to_mix_price_table_versions() -> None:
    """Records priced against different table versions are refused rather than summed."""
    record_a = _record(SONNET, "inv-1")
    import dataclasses

    record_b = dataclasses.replace(
        record_a,
        invocation_id="inv-2",
        cost=dataclasses.replace(record_a.cost, price_table_version="9.9.9-different"),
    )
    with pytest.raises(ValueError, match="multiple price table versions"):
        aggregate_model_metrics(SONNET, [record_a, record_b], [], withhold_cost=False)


# --- Cost withheld on request, tokens always present -------------------------------------------


def test_cost_is_withheld_when_requested() -> None:
    """withhold_cost=True omits the Cost aggregate entirely, never a fabricated zero."""
    included = [_record(SONNET, "inv-1", input_tokens=1000, output_tokens=500)]
    metrics = aggregate_model_metrics(SONNET, included, [], withhold_cost=True)
    assert metrics.cost is None
    # Token totals are never withheld; only currency figures are.
    assert metrics.usage.input_tokens == 1000
    assert metrics.usage.output_tokens == 500


def test_report_fields_flag_cost_withholding_explicitly() -> None:
    """The rendered report distinguishes 'withheld' from 'zero' via an explicit flag."""
    included = [_record(SONNET, "inv-1")]
    withheld = aggregate_model_metrics(SONNET, included, [], withhold_cost=True)
    published = aggregate_model_metrics(SONNET, included, [], withhold_cost=False)

    withheld_fields = withheld.to_report_fields()
    published_fields = published.to_report_fields()

    assert withheld_fields["cost_withheld_provisional"] is True
    assert "cost_total" not in withheld_fields

    assert published_fields["cost_withheld_provisional"] is False
    assert "cost_total" in published_fields


def test_default_withhold_cost_wiring_matches_the_real_table_which_now_publishes() -> None:
    """withhold_cost=eval.pricing.is_cost_provisional() publishes cost against the shipped table."""
    included = [_record(SONNET, "inv-1")]
    metrics = aggregate_model_metrics(
        SONNET,
        included,
        [],
        withhold_cost=is_cost_provisional(pricing_path=PRICING_PATH),
    )
    assert metrics.cost is not None


def test_withhold_cost_true_still_withholds_regardless_of_the_shipped_flag() -> None:
    """The withheld branch stays tested independently of the shipped provisional flag."""
    included = [_record(SONNET, "inv-1")]
    metrics = aggregate_model_metrics(SONNET, included, [], withhold_cost=True)
    assert metrics.cost is None


# --- Cache-driven cost variation stays separable from model-driven ------------------------------


def test_cache_min_checkpoint_tokens_differ_sharply_by_model() -> None:
    """Cache thresholds are read per model: 512 for each Anthropic tier, 1024 for GPT-6.1 Sol."""
    assert cache_min_checkpoint_tokens(OPUS, capabilities_path=CAPABILITIES_PATH) == 512
    assert cache_min_checkpoint_tokens(SONNET, capabilities_path=CAPABILITIES_PATH) == 512
    assert cache_min_checkpoint_tokens(HAIKU, capabilities_path=CAPABILITIES_PATH) == 512
    assert cache_min_checkpoint_tokens(SOL, capabilities_path=CAPABILITIES_PATH) == 1024


def test_cache_min_checkpoint_tokens_raises_for_unknown_model() -> None:
    """An unpriced or unregistered model must fail loudly, not silently resolve to None."""
    with pytest.raises(KeyError, match="unknown.model"):
        cache_min_checkpoint_tokens("unknown.model", capabilities_path=CAPABILITIES_PATH)


def test_model_metrics_carries_its_own_cache_threshold_alongside_cost() -> None:
    """Each model's aggregate carries its own cache threshold, read from the capability matrix."""
    opus_metrics = aggregate_model_metrics(OPUS, [_record(OPUS, "inv-1")], [], withhold_cost=True)
    sol_metrics = aggregate_model_metrics(SOL, [_record(SOL, "inv-2")], [], withhold_cost=True)
    assert opus_metrics.cache_min_checkpoint_tokens == 512
    assert sol_metrics.cache_min_checkpoint_tokens == 1024
    assert opus_metrics.cache_min_checkpoint_tokens != sol_metrics.cache_min_checkpoint_tokens


def test_cache_cost_total_is_isolated_from_non_cache_cost() -> None:
    """CostTotals.cache_total sums only the two cache categories, never blended with input/output.

    cache_total equals cache_write + cache_read, and total equals input + output + cache_total.
    """
    included = [
        _record(
            SONNET,
            "inv-1",
            input_tokens=1000,
            output_tokens=500,
            cache_write_tokens=2000,
            cache_read_tokens=3000,
        )
    ]
    metrics = aggregate_model_metrics(SONNET, included, [], withhold_cost=False)
    assert metrics.cost is not None
    assert metrics.cost.cache_total == metrics.cost.cache_write + metrics.cost.cache_read
    assert metrics.cost.total == (
        metrics.cost.input + metrics.cost.output + metrics.cost.cache_total
    )


def test_report_fields_expose_cache_totals_distinctly_from_grand_total() -> None:
    """A published report names cost_cache_total separately from cost_total, never only the sum."""
    included = [
        _record(SONNET, "inv-1", cache_write_tokens=100, cache_read_tokens=200),
    ]
    metrics = aggregate_model_metrics(SONNET, included, [], withhold_cost=False)
    fields = metrics.to_report_fields()
    assert "cost_cache_total" in fields
    assert "cost_total" in fields
    assert "cache_min_checkpoint_tokens" in fields
    assert "cache_read_tokens" in fields
    assert "cache_write_tokens" in fields


# --- Exclusions flow through to the aggregate, never dropped -----------------------------------


def test_exclusions_are_reported_on_the_model_metrics_not_silently_absorbed() -> None:
    """An excluded invocation appears in the aggregate's exclusion tally, not just left out."""
    included = [_record(SONNET, "inv-1")]
    exclusions = [
        Exclusion(invocation_id="inv-cold", category=ExclusionCategory.COLD_START),
        Exclusion(invocation_id="inv-throttled", category=ExclusionCategory.THROTTLE),
    ]
    metrics = aggregate_model_metrics(SONNET, included, exclusions, withhold_cost=True)
    assert metrics.exclusions.total == 2
    assert metrics.exclusions.by_category[ExclusionCategory.COLD_START] == 1
    assert metrics.exclusions.by_category[ExclusionCategory.THROTTLE] == 1
    fields = metrics.to_report_fields()
    assert fields["exclusions"] == metrics.exclusions.to_report_fields()


def test_included_count_excludes_the_excluded_invocations() -> None:
    """included_invocation_count reflects only what entered the statistics, not the raw total."""
    included = [_record(SONNET, "inv-1"), _record(SONNET, "inv-2")]
    exclusions = [Exclusion(invocation_id="inv-3", category=ExclusionCategory.RETRY)]
    metrics = aggregate_model_metrics(SONNET, included, exclusions, withhold_cost=True)
    assert metrics.included_invocation_count == 2
    assert metrics.exclusions.total == 1


# --- Measurement-only posture: no verdict anywhere ----------------------------------------------


def test_model_metrics_report_fields_contain_no_verdict_vocabulary() -> None:
    """The rendered report never names pass, fail, verdict, threshold-crossed, or a recommendation.

    Walks the rendered field names and string values for verdict-shaped vocabulary.
    """
    included = [_record(SONNET, "inv-1", cache_write_tokens=10, cache_read_tokens=20)]
    exclusions = [Exclusion(invocation_id="inv-2", category=ExclusionCategory.THROTTLE)]
    metrics = aggregate_model_metrics(SONNET, included, exclusions, withhold_cost=False)

    fields = metrics.to_report_fields()
    forbidden_substrings = ("pass", "fail", "verdict", "threshold_crossed", "recommend")

    def _walk(value: object) -> list[str]:
        found: list[str] = []
        if isinstance(value, str):
            lowered = value.lower()
            found.extend(word for word in forbidden_substrings if word in lowered)
        elif isinstance(value, dict):
            for key, item in value.items():
                found.extend(word for word in forbidden_substrings if word in key.lower())
                found.extend(_walk(item))
        elif isinstance(value, list | tuple):
            for item in value:
                found.extend(_walk(item))
        return found

    assert _walk(fields) == []


def test_module_source_contains_no_verdict_function_or_class() -> None:
    """No function, class, or top-level name in eval/metrics.py names a verdict concept."""
    metrics_path = REPO_ROOT / "src" / "eval" / "metrics.py"
    tree = ast.parse(metrics_path.read_text(encoding="utf-8"), filename=str(metrics_path))
    forbidden = ("verdict", "pass_fail", "threshold_crossed", "recommend")
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
    lowered_names = [name.lower() for name in names]
    violations = [name for name in lowered_names if any(word in name for word in forbidden)]
    assert violations == []


# --- AST guard: no vendor SDK import anywhere under src/eval/ ----------------------------------

#: Model-vendor SDK module roots that must never appear as an import anywhere under src/eval/.
#: ``boto3``/``botocore`` are not in the set.
_FORBIDDEN_VENDOR_IMPORT_ROOTS = ("anthropic", "openai")


def _imported_module_roots(tree: ast.Module) -> set[str]:
    """Collect the top-level module name of every import statement in a parsed module."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_vendor_sdk_import_anywhere_under_src_eval() -> None:
    """No file under src/eval/ imports a model-vendor SDK, at any nesting depth in its AST."""
    eval_dir = REPO_ROOT / "src" / "eval"
    violations: list[str] = []
    for path in sorted(eval_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = _imported_module_roots(tree) & set(_FORBIDDEN_VENDOR_IMPORT_ROOTS)
        if found:
            violations.append(f"{path.relative_to(REPO_ROOT)}: imports {sorted(found)}")
    assert violations == [], "\n".join(violations)


# --- cost_excluding_cache: non-cache cost isolated from cache cost -----------------------------


def test_cost_excluding_cache_equals_input_plus_output() -> None:
    """cost_excluding_cache is the complement of cache_total; the two partition total."""
    cost = CostTotals(
        input=1.0, output=2.0, cache_write=0.5, cache_read=0.25, price_table_version="v"
    )
    assert cost.cost_excluding_cache == 3.0
    assert cost.cost_excluding_cache + cost.cache_total == cost.total


def test_cost_excluding_cache_is_reported_only_when_cost_is_not_withheld() -> None:
    """cost_excluding_cache is emitted beside cost_cache_total, and omitted (not zeroed) when
    cost is withheld."""
    cost = CostTotals(
        input=1.0, output=2.0, cache_write=0.5, cache_read=0.25, price_table_version="v"
    )
    published = ModelMetrics(
        model_id=SONNET,
        included_invocation_count=1,
        usage=UsageTotals(
            input_tokens=0, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0
        ),
        cost=cost,
        cache_min_checkpoint_tokens=None,
        exclusions=tally_exclusions([]),
    )
    published_fields = published.to_report_fields()
    assert published_fields["cost_excluding_cache"] == 3.0
    assert published_fields["cost_cache_total"] == 0.75

    withheld_fields = aggregate_model_metrics(SONNET, [], [], withhold_cost=True).to_report_fields()
    assert "cost_excluding_cache" not in withheld_fields


# --- Latency travels on ModelMetrics alongside cost, never blended into it ----------------------


def test_model_metrics_carries_measured_latency_report_fields() -> None:
    """A measured arm's report carries the exact latency keys the dashboard reads."""
    latency = aggregate_latency_metrics(
        measured=True,
        task_latencies_ms=[10.0, 20.0],
        call_samples=[CallLatencySample(tier="supervisor", latency_ms=15.0)],
    )
    metrics = aggregate_model_metrics(SONNET, [], [], withhold_cost=True, latency=latency)
    fields = metrics.to_report_fields()
    assert fields["latency_measured"] is True
    assert fields["latency_percentile_method"] == "nearest-rank"
    assert fields["task_latency_ms_p50"] == 10.0
    assert fields["task_latency_ms_p95"] == 20.0
    by_tier = fields["call_latency_ms_by_tier"]
    assert isinstance(by_tier, dict)
    assert by_tier["supervisor"] == {"p50": 15.0, "p95": 15.0, "count": 1}
    assert by_tier["tool"] == {"p50": None, "p95": None, "count": 0}


def test_model_metrics_defaults_latency_to_not_measured_when_none_supplied() -> None:
    """A caller that supplies no latency reports ``latency_measured: false`` with null figures."""
    metrics = aggregate_model_metrics(SONNET, [], [], withhold_cost=True)
    fields = metrics.to_report_fields()
    assert fields["latency_measured"] is False
    assert fields["task_latency_ms_p50"] is None
    assert fields["task_latency_ms_p95"] is None
    by_tier = fields["call_latency_ms_by_tier"]
    assert isinstance(by_tier, dict)
    assert set(by_tier) == {"supervisor", "subagent", "tool"}


def test_model_metrics_is_a_frozen_dataclass_instance() -> None:
    """ModelMetrics results are immutable, matching the rest of this module's dataclasses."""
    metrics = aggregate_model_metrics(SONNET, [], [], withhold_cost=True)
    assert isinstance(metrics, ModelMetrics)
    with pytest.raises(AttributeError):
        metrics.model_id = "different"  # type: ignore[misc]


# ---------------------------------------------------------------------------------------------
# Three-tier measurement: (tier, role, model_id) aggregation, per-tool aggregation, per-task
# spread, and the attribution-missing hard error.
# ---------------------------------------------------------------------------------------------


def _attribution(
    tier: str,
    role: str,
    model_id: str,
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    latency_ms: float | None = None,
) -> InvocationAttribution:
    """Build an InvocationAttribution for aggregation tests."""
    return InvocationAttribution(
        tier=tier,  # type: ignore[arg-type]
        role=role,
        model_id=model_id,
        tokens=TokenCounts(input_tokens=input_tokens, output_tokens=output_tokens),
        cache=CacheCounts(read_tokens=cache_read_tokens, write_tokens=cache_write_tokens),
        latency_ms=latency_ms,
    )


def _tool_call(
    tool_name: str,
    role: str,
    outcome: str,
    *,
    latency_ms: float | None = None,
    truncated: bool = False,
) -> ToolCallAttribution:
    """Build a ToolCallAttribution for aggregation tests."""
    return ToolCallAttribution(
        tool_name=tool_name,
        role=role,
        latency_ms=latency_ms,
        outcome_kind=outcome,  # type: ignore[arg-type]
        truncated=truncated,
    )


def test_require_records_attributed_passes_when_every_token_record_is_matched() -> None:
    records = [_record(OPUS, "inv-1"), _record(SONNET, "inv-2")]
    attributions = [
        _attribution("subagent", "research", OPUS),
        _attribution("subagent", "mutation", SONNET),
    ]
    require_records_attributed(records, attributions)  # no raise


def test_require_records_attributed_raises_when_a_token_record_has_no_attribution() -> None:
    records = [_record(OPUS, "inv-1"), _record(SONNET, "inv-2")]
    attributions = [_attribution("subagent", "research", OPUS)]  # SONNET record unmatched
    with pytest.raises(MissingAttributionError, match="inv-2"):
        require_records_attributed(records, attributions)


def test_require_records_attributed_ignores_a_zero_token_record() -> None:
    zero = _record(OPUS, "inv-zero", input_tokens=0, output_tokens=0)
    require_records_attributed([zero], [])  # a tier that made no call needs no attribution


def test_aggregate_tier_role_model_groups_and_sums_tokens() -> None:
    attributions = [
        _attribution("supervisor", "supervisor", OPUS, input_tokens=100, output_tokens=10),
        _attribution("supervisor", "supervisor", OPUS, input_tokens=200, output_tokens=20),
        _attribution("tool", "research", HAIKU, input_tokens=5, output_tokens=1),
    ]
    rows = aggregate_tier_role_model(attributions, withhold_cost=False)
    assert len(rows) == 2
    # deterministic (tier, role, model_id) order: 'supervisor' < 'tool'
    assert (rows[0].tier, rows[0].role, rows[0].model_id) == ("supervisor", "supervisor", OPUS)
    assert rows[0].included_invocation_count == 2
    assert rows[0].usage.input_tokens == 300
    assert rows[0].usage.output_tokens == 30
    assert rows[1].tier == "tool" and rows[1].included_invocation_count == 1


def test_aggregate_tier_role_model_withholds_cost_when_requested() -> None:
    rows = aggregate_tier_role_model(
        [_attribution("supervisor", "supervisor", OPUS)], withhold_cost=True
    )
    assert rows[0].cost is None
    fields = rows[0].to_report_fields()
    assert fields["cost_withheld_provisional"] is True
    assert "cost_total" not in fields


def test_aggregate_tier_role_model_prices_group_when_not_withheld() -> None:
    rows = aggregate_tier_role_model(
        [_attribution("supervisor", "supervisor", OPUS)], withhold_cost=False
    )
    assert rows[0].cost is not None
    assert rows[0].cost.total > 0
    fields = rows[0].to_report_fields()
    assert fields["cost_total"] == rows[0].cost.total
    assert fields["cost_excluding_cache"] == rows[0].cost.cost_excluding_cache


def test_aggregate_tier_role_model_latency_measured_only_when_a_call_recorded_one() -> None:
    measured = aggregate_tier_role_model(
        [
            _attribution("tool", "research", HAIKU, latency_ms=10.0),
            _attribution("tool", "research", HAIKU, latency_ms=30.0),
        ],
        withhold_cost=False,
    )
    assert measured[0].call_latency_measured is True
    assert measured[0].call_latency_ms_p50 == 10.0
    assert measured[0].call_latency_ms_p95 == 30.0

    stub = aggregate_tier_role_model(
        [_attribution("tool", "research", HAIKU, latency_ms=None)], withhold_cost=False
    )
    assert stub[0].call_latency_measured is False
    assert stub[0].call_latency_ms_p50 is None
    assert stub[0].call_latency_ms_p95 is None


def test_aggregate_tool_calls_counts_and_distributes_outcomes() -> None:
    calls = [
        _tool_call("get_account", "research", "data", latency_ms=5.0),
        _tool_call("get_account", "research", "data", latency_ms=15.0, truncated=True),
        _tool_call("get_account", "research", "denied"),
    ]
    rows = aggregate_tool_calls(calls)
    assert len(rows) == 1
    row = rows[0]
    assert row.tool_name == "get_account" and row.role == "research"
    assert row.count == 3
    assert row.truncated_count == 1
    # every outcome kind present even at zero
    assert row.outcome_distribution == {
        "data": 2,
        "no_match": 0,
        "denied": 1,
        "tool_failure": 0,
    }
    assert row.call_latency_ms_p50 == 5.0
    assert row.call_latency_ms_p95 == 15.0


def test_aggregate_tool_calls_keys_one_tool_under_three_roles_as_three_rows() -> None:
    # One tool called by the supervisor and by two subagents yields one row per role.
    calls = [
        _tool_call("get_account", "supervisor", "data", latency_ms=5.0),
        _tool_call("get_account", "research", "data", latency_ms=10.0),
        _tool_call("get_account", "research", "denied"),
        _tool_call("get_account", "mutation", "data", truncated=True),
    ]
    rows = aggregate_tool_calls(calls)

    # One row per (tool_name, role), sorted by (tool_name, role).
    assert [(r.tool_name, r.role) for r in rows] == [
        ("get_account", "mutation"),
        ("get_account", "research"),
        ("get_account", "supervisor"),
    ]
    by_role = {r.role: r for r in rows}
    assert by_role["research"].count == 2
    assert by_role["research"].outcome_distribution == {
        "data": 1,
        "no_match": 0,
        "denied": 1,
        "tool_failure": 0,
    }
    assert by_role["supervisor"].count == 1
    assert by_role["supervisor"].call_latency_ms_p50 == 5.0
    assert by_role["mutation"].count == 1
    assert by_role["mutation"].truncated_count == 1


def test_aggregate_task_spread_reports_min_mean_max_and_success_fraction() -> None:
    rows = aggregate_task_spread({"task-b": [1.0, 0.0], "task-a": [1.0, 1.0, 0.5]})
    # sorted by task id
    assert [r.task_id for r in rows] == ["task-a", "task-b"]
    a = rows[0]
    assert a.repeats == 3
    assert a.correctness_min == 0.5
    assert a.correctness_max == 1.0
    assert a.correctness_mean == pytest.approx((1.0 + 1.0 + 0.5) / 3)
    assert a.success_fraction == pytest.approx(2 / 3)
    b = rows[1]
    assert b.success_fraction == 0.5


def test_aggregate_task_spread_raises_on_a_task_with_no_repeats() -> None:
    with pytest.raises(ValueError, match="no repeats"):
        aggregate_task_spread({"task-empty": []})


def test_tier_role_model_report_fields_carry_no_verdict_vocabulary() -> None:
    rows = aggregate_tier_role_model(
        [_attribution("supervisor", "supervisor", OPUS, latency_ms=1.0)], withhold_cost=False
    )
    blob = " ".join(str(v).lower() for v in rows[0].to_report_fields().values())
    for word in ("winner", "best", "worst", "pass", "fail", "recommend", "verdict"):
        assert word not in blob


def test_new_aggregation_dataclasses_are_frozen() -> None:
    spread = TaskSpread(
        task_id="t",
        repeats=1,
        correctness_min=1.0,
        correctness_mean=1.0,
        correctness_max=1.0,
        success_fraction=1.0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        spread.repeats = 2  # type: ignore[misc]


def test_build_tier_report_fields_nests_all_three_under_fixed_keys() -> None:
    tier_rows = aggregate_tier_role_model(
        [_attribution("supervisor", "supervisor", OPUS, latency_ms=1.0)], withhold_cost=False
    )
    tool_rows = aggregate_tool_calls([_tool_call("get_account", "research", "data")])
    spread_rows = aggregate_task_spread({"task-a": [1.0, 0.0]})

    report = build_tier_report_fields(tier_rows, tool_rows, spread_rows)
    assert set(report) == {
        TIER_ROLE_MODEL_REPORT_KEY,
        TOOL_METRICS_REPORT_KEY,
        TASK_SPREAD_REPORT_KEY,
    }
    # each value is a list of the dataclass's flat report mappings, in aggregator order
    assert report[TIER_ROLE_MODEL_REPORT_KEY] == [tier_rows[0].to_report_fields()]
    assert report[TOOL_METRICS_REPORT_KEY] == [tool_rows[0].to_report_fields()]
    assert report[TASK_SPREAD_REPORT_KEY] == [spread_rows[0].to_report_fields()]
    # spread entries carry task_id so a consumer can join onto per-task rows
    spread_list = cast("list[dict[str, object]]", report[TASK_SPREAD_REPORT_KEY])
    assert spread_list[0]["task_id"] == "task-a"


def test_build_tier_report_fields_emits_empty_lists_when_nothing_aggregated() -> None:
    report = build_tier_report_fields((), (), ())
    assert report[TIER_ROLE_MODEL_REPORT_KEY] == []
    assert report[TOOL_METRICS_REPORT_KEY] == []
    assert report[TASK_SPREAD_REPORT_KEY] == []


# --- Per-call pricing and long-context counts ------------------------------------------------


def test_inference_profile_for_reads_the_configured_profile() -> None:
    """Every capability-matrix model resolves to the inference profile its entry declares."""
    for model_id, model in _MATRIX.models.items():
        assert inference_profile_for(model_id) == model.inference_profile


def test_inference_profile_for_refuses_an_unconfigured_model(tmp_path: Path) -> None:
    """An absent model or an entry without a profile raises rather than defaulting to global."""
    with pytest.raises(ValueError, match="no configured inference_profile"):
        inference_profile_for("anthropic.claude-unknown")
    path = tmp_path / "model_capabilities.yaml"
    path.write_text(f"models:\n  {HAIKU}:\n    family: anthropic\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no configured inference_profile"):
        inference_profile_for(HAIKU, capabilities_path=path)


def test_tier_role_model_prices_each_call_and_counts_long_context_calls() -> None:
    """Five 30K Haiku calls and one 120K call: one long-context call, priced per call."""
    attributions = [_attribution("tool", "research", HAIKU, input_tokens=30_000)] * 5 + [
        _attribution("tool", "research", HAIKU, input_tokens=120_000)
    ]
    (row,) = aggregate_tier_role_model(attributions, withhold_cost=False)
    assert row.long_context_invocation_count == 1
    assert row.to_report_fields()["long_context_invocation_count"] == 1
    assert row.cost is not None
    standard_input = 5 * 30_000 / 1_000_000 * 0.10
    long_input = 120_000 / 1_000_000 * 0.50
    assert row.cost.input == pytest.approx(standard_input + long_input)
    assert row.cost.output == pytest.approx(5 * 50 / 1e6 * 0.50 + 50 / 1e6 * 2.50)


def test_tier_role_model_counts_long_context_calls_even_when_cost_is_withheld() -> None:
    """The long-context count is not a currency figure, so withholding cost keeps it."""
    (row,) = aggregate_tier_role_model(
        [_attribution("tool", "research", HAIKU, input_tokens=100_001)], withhold_cost=True
    )
    assert row.cost is None
    assert row.long_context_invocation_count == 1


def test_a_call_exactly_at_the_threshold_is_not_counted_long_context() -> None:
    """A prompt of exactly the threshold is standard and does not move the count."""
    (row,) = aggregate_tier_role_model(
        [_attribution("tool", "research", HAIKU, input_tokens=100_000)], withhold_cost=False
    )
    assert row.long_context_invocation_count == 0


def test_model_metrics_count_included_records_priced_long_context() -> None:
    """The arm model metrics count included records whose cost tier is long-context."""
    included = [
        _record(HAIKU, "inv-1", input_tokens=30_000),
        _record(HAIKU, "inv-2", input_tokens=120_000),
        _record(HAIKU, "inv-3", input_tokens=99_000, cache_read_tokens=2_000),
    ]
    metrics = aggregate_model_metrics(HAIKU, included, [], withhold_cost=False)
    assert metrics.long_context_invocation_count == 2
    assert metrics.to_report_fields()["long_context_invocation_count"] == 2


def test_aggregate_tier_role_model_without_profiles_matches_the_configured_profiles() -> None:
    """Omitting ``inference_profiles`` prices exactly as passing each model's configured one."""
    attributions = [_attribution("supervisor", "supervisor", "anthropic.claude-sonnet-5-5")]
    configured = {"anthropic.claude-sonnet-5-5": "global.anthropic.claude-sonnet-5-5"}
    assert aggregate_tier_role_model(attributions, withhold_cost=False) == (
        aggregate_tier_role_model(attributions, withhold_cost=False, inference_profiles=configured)
    )


def test_aggregate_tier_role_model_prices_under_an_injected_profiles_route() -> None:
    """An injected geo profile applies the geo route multiplier instead of the configured one."""
    model_id = "anthropic.claude-sonnet-5-5"
    attributions = [_attribution("supervisor", "supervisor", model_id)]
    (global_row,) = aggregate_tier_role_model(attributions, withhold_cost=False)
    (geo_row,) = aggregate_tier_role_model(
        attributions, withhold_cost=False, inference_profiles={model_id: f"us.{model_id}"}
    )
    assert global_row.cost is not None and geo_row.cost is not None
    assert geo_row.cost.total == pytest.approx(global_row.cost.total * 1.10)


def test_aggregate_tier_role_model_refuses_a_profile_map_missing_a_priced_model() -> None:
    attributions = [_attribution("supervisor", "supervisor", "anthropic.claude-sonnet-5-5")]
    with pytest.raises(KeyError):
        aggregate_tier_role_model(attributions, withhold_cost=False, inference_profiles={})

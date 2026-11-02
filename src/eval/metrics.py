# SPDX-License-Identifier: MIT
"""Eval metrics: categorised exclusions, aggregated usage/cost, latency, and per-tier aggregates.

Excluded invocations (cold start, retry, throttle) are tallied by category rather than dropped.
Cost is summed from the :class:`crm.metering.Cost` already attached to each usage record, and
cache token counts and the cache checkpoint threshold are reported alongside the cost totals.
The module reports numbers only; it renders no verdict, threshold comparison, or recommendation.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from agents.tier_attribution import InvocationAttribution, OutcomeKind, ToolCallAttribution
from crm.metering import Tier, TokenUsage, UsageRecord, cost_for_usage
from eval.pricing import cache_min_checkpoint_tokens as _cache_min_checkpoint_tokens
from eval.scoring import SCORED_TIERS, TierScores

#: The four delegation outcome kinds, in report order. Mirrors
#: :data:`agents.tier_attribution.OutcomeKind`.
_OUTCOME_KINDS: Final[tuple[OutcomeKind, ...]] = ("data", "no_match", "denied", "tool_failure")


class ExclusionCategory(StrEnum):
    """The closed set of reasons an invocation is excluded from latency/cost statistics."""

    COLD_START = "cold_start"
    RETRY = "retry"
    THROTTLE = "throttle"


@dataclass(frozen=True, slots=True)
class Exclusion:
    """One excluded invocation and the category it was excluded under.

    Attributes:
        invocation_id: The excluded invocation's identifier.
        category: Why the invocation was excluded.
        detail: Free-text context, for example the retry attempt number or the throttling error
            code. Never used in place of ``category`` for grouping.
    """

    invocation_id: str
    category: ExclusionCategory
    detail: str = ""

    def __post_init__(self) -> None:
        """Reject an exclusion that cannot be traced back to a specific invocation."""
        if not self.invocation_id:
            raise ValueError("invocation_id must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ExclusionTally:
    """Categorised counts of every excluded invocation.

    Attributes:
        by_category: Count of exclusions per :class:`ExclusionCategory`, every category present
            even at zero.
        exclusions: Every individual :class:`Exclusion`.
    """

    by_category: dict[ExclusionCategory, int]
    exclusions: tuple[Exclusion, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        """Sum of exclusions across every category."""
        return sum(self.by_category.values())

    def to_report_fields(self) -> dict[str, object]:
        """Render this tally as a flat, JSON-safe mapping with every category present."""
        return {
            "total_excluded": self.total,
            "by_category": {
                category.value: self.by_category.get(category, 0) for category in ExclusionCategory
            },
        }


def tally_exclusions(exclusions: list[Exclusion]) -> ExclusionTally:
    """Build an :class:`ExclusionTally`, with every category present even at zero.

    Args:
        exclusions: Every excluded invocation, in any order.

    Returns:
        A tally with per-category counts and the preserved exclusion list.
    """
    counts = Counter(exclusion.category for exclusion in exclusions)
    by_category = {category: counts.get(category, 0) for category in ExclusionCategory}
    return ExclusionTally(by_category=by_category, exclusions=tuple(exclusions))


#: The percentile method reported in the ``latency_percentile_method`` field.
LATENCY_PERCENTILE_METHOD: Final[str] = "nearest-rank"

#: Every tier, each present in a latency report even at a zero count. Matches
#: :data:`crm.metering.Tier`.
_ALL_TIERS: Final[tuple[Tier, ...]] = ("supervisor", "subagent", "tool")

#: Public alias of :data:`_ALL_TIERS`, in report order.
ALL_TIERS: Final = _ALL_TIERS


def nearest_rank_percentile(values: Sequence[float], percentile: float) -> float:
    """Return the nearest-rank ``percentile`` of a non-empty sample.

    Orders the sample ascending and takes the value at the 1-indexed rank
    ``ceil(percentile / 100 * n)``, without interpolation.

    Args:
        values: The sample, in any order. Must be non-empty.
        percentile: The percentile to compute, in the half-open range ``(0, 100]``.

    Returns:
        The value at the nearest rank.

    Raises:
        ValueError: When ``values`` is empty or ``percentile`` falls outside ``(0, 100]``.
    """
    if not values:
        raise ValueError("nearest_rank_percentile requires a non-empty sample")
    if not 0 < percentile <= 100:
        raise ValueError(f"percentile must be in (0, 100], got {percentile}")
    ordered = sorted(values)
    rank = math.ceil((percentile / 100.0) * len(ordered))
    return ordered[rank - 1]


@dataclass(frozen=True, slots=True)
class CallLatencySample:
    """One model invocation's wall-clock latency, attributed to the tier it was invoked for.

    Attributes:
        tier: The tier slot this invocation served.
        latency_ms: The invocation's wall-clock duration in milliseconds, measured by the provider
            adapter's monotonic clock and passed through unchanged by :mod:`eval.live`.
    """

    tier: Tier
    latency_ms: float

    def __post_init__(self) -> None:
        """Reject a negative duration, which a monotonic clock cannot produce."""
        if self.latency_ms < 0:
            raise ValueError(f"latency_ms must not be negative, got {self.latency_ms}")


@dataclass(frozen=True, slots=True)
class TierLatency:
    """Nearest-rank P50/P95 of one tier's call latency, with the sample count behind it.

    Attributes:
        p50: The tier's 50th-percentile call latency in milliseconds, or ``None`` when the tier had
            no measured invocation.
        p95: The tier's 95th-percentile call latency in milliseconds, or ``None`` when the tier had
            no measured invocation.
        count: How many measured invocations the percentiles were computed over.
    """

    p50: float | None
    p95: float | None
    count: int

    def to_report_fields(self) -> dict[str, object]:
        """Render this tier's latency as a flat, JSON-safe mapping in a fixed key order."""
        return {"p50": self.p50, "p95": self.p95, "count": self.count}


@dataclass(frozen=True, slots=True)
class LatencyMetrics:
    """Per-arm task and per-tier call latency, or an explicit "not measured" result.

    A stub arm is reported ``measured=False`` with null figures.

    Attributes:
        measured: Whether latency was measured for this arm. ``False`` for a stub arm.
        task_p50_ms: Nearest-rank 50th-percentile wall-clock of the golden-task runs, or ``None``
            when unmeasured or when no task ran.
        task_p95_ms: Nearest-rank 95th-percentile of the golden-task runs, or ``None`` likewise.
        by_tier: Per-tier call latency, one entry for every :data:`_ALL_TIERS` member.
    """

    measured: bool
    task_p50_ms: float | None
    task_p95_ms: float | None
    by_tier: dict[Tier, TierLatency]

    def to_report_fields(self) -> dict[str, object]:
        """Render latency as a flat, JSON-safe mapping under the key names the frontend reads.

        Keys: ``latency_measured``, ``latency_percentile_method``, ``task_latency_ms_p50``,
        ``task_latency_ms_p95`` and ``call_latency_ms_by_tier`` (a
        ``{supervisor|subagent|tool: {p50, p95, count}}`` mapping).
        """
        return {
            "latency_measured": self.measured,
            "latency_percentile_method": LATENCY_PERCENTILE_METHOD,
            "task_latency_ms_p50": self.task_p50_ms,
            "task_latency_ms_p95": self.task_p95_ms,
            "call_latency_ms_by_tier": {
                tier: self.by_tier[tier].to_report_fields() for tier in _ALL_TIERS
            },
        }


def not_measured_latency_metrics() -> LatencyMetrics:
    """Return the "not measured" latency result: null figures, every tier present at count 0."""
    return LatencyMetrics(
        measured=False,
        task_p50_ms=None,
        task_p95_ms=None,
        by_tier={tier: TierLatency(p50=None, p95=None, count=0) for tier in _ALL_TIERS},
    )


def aggregate_latency_metrics(
    *,
    measured: bool,
    task_latencies_ms: Sequence[float],
    call_samples: Sequence[CallLatencySample],
) -> LatencyMetrics:
    """Aggregate one arm's task and per-tier call latencies into a :class:`LatencyMetrics`.

    When ``measured`` is ``False`` the samples are ignored and :func:`not_measured_latency_metrics`
    is returned. Otherwise percentiles are nearest-rank; a dimension with no sample reports
    ``None``, and every tier in :data:`_ALL_TIERS` is present even at a zero count.

    Args:
        measured: Whether latency was measured for this arm.
        task_latencies_ms: Each golden-task run's wall-clock in milliseconds.
        call_samples: Each measured model invocation's latency, tagged with its tier.

    Returns:
        The aggregated latency metrics.
    """
    if not measured:
        return not_measured_latency_metrics()

    task_p50 = nearest_rank_percentile(task_latencies_ms, 50) if task_latencies_ms else None
    task_p95 = nearest_rank_percentile(task_latencies_ms, 95) if task_latencies_ms else None

    by_tier: dict[Tier, TierLatency] = {}
    for tier in _ALL_TIERS:
        tier_values = [sample.latency_ms for sample in call_samples if sample.tier == tier]
        if tier_values:
            by_tier[tier] = TierLatency(
                p50=nearest_rank_percentile(tier_values, 50),
                p95=nearest_rank_percentile(tier_values, 95),
                count=len(tier_values),
            )
        else:
            by_tier[tier] = TierLatency(p50=None, p95=None, count=0)

    return LatencyMetrics(
        measured=True, task_p50_ms=task_p50, task_p95_ms=task_p95, by_tier=by_tier
    )


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Summed token counts across a set of included usage records, cache kept separate.

    Attributes:
        input_tokens: Sum of the input-token counts the provider reported, exactly as reported.
            Whether that reported count already contains ``cache_read_tokens`` differs by provider
            family and is declared in :data:`crm.metering.TOKEN_ACCOUNTING`.
        output_tokens: Sum of output tokens, including reasoning/thinking tokens.
        cache_write_tokens: Sum of tokens written to a new cache checkpoint.
        cache_read_tokens: Sum of tokens served from an existing cache checkpoint.
    """

    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int


@dataclass(frozen=True, slots=True)
class CostTotals:
    """Summed cost across a set of included usage records, broken out by category.

    Attributes:
        input: Summed cost of the tokens charged at the input rate, per
            :func:`crm.metering.billable_input_tokens`.
        output: Summed cost of output tokens.
        cache_write: Summed cost of tokens written to a new cache checkpoint.
        cache_read: Summed cost of tokens served from an existing cache checkpoint.
        price_table_version: The single price table version every summed record was priced
            against.
    """

    input: float
    output: float
    cache_write: float
    cache_read: float
    price_table_version: str

    @property
    def total(self) -> float:
        """Sum of all four cost categories."""
        return self.input + self.output + self.cache_write + self.cache_read

    @property
    def cost_excluding_cache(self) -> float:
        """Sum of the non-cache cost categories (input + output); with :attr:`cache_total` it
        partitions :attr:`total`."""
        return self.input + self.output

    @property
    def cache_total(self) -> float:
        """Sum of the two cache-attributable cost categories."""
        return self.cache_write + self.cache_read


@dataclass(frozen=True, slots=True)
class ModelMetrics:
    """Aggregated measured dimensions for one model across a set of included invocations.

    Attributes:
        model_id: The Bedrock model identifier these metrics summarise.
        included_invocation_count: Number of invocations included in ``usage`` and ``cost``.
        usage: Summed token counts across included invocations.
        cost: Summed cost across included invocations, or ``None`` while cost is withheld.
            ``usage`` is populated either way.
        cache_min_checkpoint_tokens: This model's prompt-cache minimum checkpoint size in tokens,
            or ``None`` when the model documents no threshold.
        exclusions: Categorised tally of invocations excluded from ``usage`` and ``cost``.
        latency: Per-arm task and per-tier call latency, or an explicit "not measured" result
            for a stub arm.
    """

    model_id: str
    included_invocation_count: int
    usage: UsageTotals
    cost: CostTotals | None
    cache_min_checkpoint_tokens: int | None
    exclusions: ExclusionTally
    latency: LatencyMetrics = field(default_factory=not_measured_latency_metrics)

    def to_report_fields(self) -> dict[str, object]:
        """Render this model's metrics as a flat, JSON-safe mapping for a result artifact.

        Cost keys are omitted (not zeroed) while cost is withheld, and
        ``cost_withheld_provisional`` records the omission.
        """
        fields: dict[str, object] = {
            "model_id": self.model_id,
            "included_invocation_count": self.included_invocation_count,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_write_tokens": self.usage.cache_write_tokens,
            "cache_read_tokens": self.usage.cache_read_tokens,
            "cache_min_checkpoint_tokens": self.cache_min_checkpoint_tokens,
            "cost_withheld_provisional": self.cost is None,
            "exclusions": self.exclusions.to_report_fields(),
            **self.latency.to_report_fields(),
        }
        if self.cost is not None:
            fields.update(
                {
                    "cost_input": self.cost.input,
                    "cost_output": self.cost.output,
                    "cost_cache_write": self.cost.cache_write,
                    "cost_cache_read": self.cost.cache_read,
                    "cost_excluding_cache": self.cost.cost_excluding_cache,
                    "cost_cache_total": self.cost.cache_total,
                    "cost_total": self.cost.total,
                    "price_table_version": self.cost.price_table_version,
                }
            )
        return fields


_DEFAULT_CAPABILITIES_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "config" / "model_capabilities.yaml"
)


def aggregate_model_metrics(
    model_id: str,
    included: list[UsageRecord],
    exclusions: list[Exclusion],
    *,
    withhold_cost: bool,
    latency: LatencyMetrics | None = None,
    capabilities_path: Path = _DEFAULT_CAPABILITIES_PATH,
) -> ModelMetrics:
    """Aggregate one model's included usage records and excluded invocations into one report.

    Args:
        model_id: The Bedrock model identifier every record in ``included`` must share.
        included: Usage records included in the latency/cost statistics. Each must have
            ``model_id`` equal to the ``model_id`` argument.
        exclusions: Every invocation excluded for this model, categorised.
        withhold_cost: Whether to omit absolute cost from the result. Callers pass
            :func:`eval.pricing.is_cost_provisional`.
        latency: This arm's aggregated task/call latency, or ``None`` to record it as not
            measured (a stub arm).
        capabilities_path: Path to ``config/model_capabilities.yaml``, for the cache threshold
            lookup. Overridable for tests.

    Returns:
        A :class:`ModelMetrics` with token totals always populated, cost populated only when
        ``withhold_cost`` is ``False``, and exclusions always populated regardless.

    Raises:
        ValueError: When any record in ``included`` has a ``model_id`` other than the ``model_id``
            argument, or when included records were priced against more than one price table
            version.
    """
    for record in included:
        if record.model_id != model_id:
            raise ValueError(
                f"record {record.invocation_id!r} has model_id {record.model_id!r}, "
                f"expected {model_id!r}"
            )

    usage = UsageTotals(
        input_tokens=sum(record.usage.input_tokens for record in included),
        output_tokens=sum(record.usage.output_tokens for record in included),
        cache_write_tokens=sum(record.usage.cache_write_tokens for record in included),
        cache_read_tokens=sum(record.usage.cache_read_tokens for record in included),
    )

    cost: CostTotals | None = None
    if not withhold_cost and included:
        price_table_versions = {record.cost.price_table_version for record in included}
        if len(price_table_versions) > 1:
            raise ValueError(
                f"included records for {model_id!r} were priced against multiple price table "
                f"versions: {sorted(price_table_versions)}; aggregation refuses to mix them"
            )
        cost = CostTotals(
            input=sum(record.cost.input for record in included),
            output=sum(record.cost.output for record in included),
            cache_write=sum(record.cost.cache_write for record in included),
            cache_read=sum(record.cost.cache_read for record in included),
            price_table_version=price_table_versions.pop(),
        )

    try:
        threshold = _cache_min_checkpoint_tokens(model_id, capabilities_path=capabilities_path)
    except KeyError:
        threshold = None

    return ModelMetrics(
        model_id=model_id,
        included_invocation_count=len(included),
        usage=usage,
        cost=cost,
        cache_min_checkpoint_tokens=threshold,
        exclusions=tally_exclusions(exclusions),
        latency=latency if latency is not None else not_measured_latency_metrics(),
    )


# ---------------------------------------------------------------------------------------------
# Three-tier measurement: aggregation keyed by (tier, role, model_id), per-tool aggregation, and
# per-task spread across repeats, over the attribution records from `agents.tier_attribution`.
# ---------------------------------------------------------------------------------------------


class MissingAttributionError(RuntimeError):
    """Raised when a token-bearing usage record has no matching ``(tier, role, model_id)``
    attribution."""


def require_records_attributed(
    records: Sequence[UsageRecord], attributions: Sequence[InvocationAttribution]
) -> None:
    """Assert every token-bearing usage record is matched by a token-bearing attribution.

    Records and attributions are matched as a ``(tier, model_id)`` multiset, since a usage record
    carries no role.

    Args:
        records: The priced usage records the sweep produced.
        attributions: The per-invocation attributions recorded at the dispatch site.

    Raises:
        MissingAttributionError: When a token-bearing record has no matching attribution.
    """
    available: Counter[tuple[str, str]] = Counter(
        (attribution.tier, attribution.model_id)
        for attribution in attributions
        if attribution.bears_tokens
    )
    for record in records:
        usage = record.usage
        bears = bool(
            usage.input_tokens
            or usage.output_tokens
            or usage.cache_read_tokens
            or usage.cache_write_tokens
        )
        if not bears:
            continue
        key = (record.tier, record.model_id)
        if available[key] <= 0:
            raise MissingAttributionError(
                f"usage record {record.invocation_id!r} bears tokens for "
                f"{record.tier}/{record.model_id} but no attribution names that invocation; a "
                "token-bearing record must carry (tier, role, model_id) attribution"
            )
        available[key] -= 1


def _latency_percentiles(
    latencies: Sequence[float | None],
) -> tuple[bool, float | None, float | None]:
    """Return ``(measured, p50, p95)`` over the non-``None`` latencies, nearest-rank.

    ``measured`` is ``True`` only when at least one latency was recorded; otherwise the result is
    ``(False, None, None)``.
    """
    measured = [value for value in latencies if value is not None]
    if not measured:
        return False, None, None
    return True, nearest_rank_percentile(measured, 50), nearest_rank_percentile(measured, 95)


@dataclass(frozen=True, slots=True)
class TierRoleModelMetrics:
    """Aggregated usage, cost, and call latency for one ``(tier, role, model_id)`` group.

    Attributes:
        tier: The model tier (``supervisor``/``subagent``/``tool``).
        role: The agent role that made the calls.
        model_id: The resolved model id that spent the tokens.
        included_invocation_count: How many attributions this group summed.
        usage: Summed token counts across the group.
        cost: Summed cost, or ``None`` while cost is withheld.
        call_latency_measured: Whether any call in this group recorded a latency (live), vs a stub.
        call_latency_ms_p50: Nearest-rank P50 call latency in ms, or ``None`` when not measured.
        call_latency_ms_p95: Nearest-rank P95 call latency in ms, or ``None`` when not measured.
    """

    tier: Tier
    role: str
    model_id: str
    included_invocation_count: int
    usage: UsageTotals
    cost: CostTotals | None
    call_latency_measured: bool
    call_latency_ms_p50: float | None
    call_latency_ms_p95: float | None

    def to_report_fields(self) -> dict[str, object]:
        """Render as a flat JSON-safe mapping; cost keys appear only when cost is not withheld."""
        fields: dict[str, object] = {
            "tier": self.tier,
            "role": self.role,
            "model_id": self.model_id,
            "included_invocation_count": self.included_invocation_count,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_tokens": self.usage.cache_read_tokens,
            "cache_write_tokens": self.usage.cache_write_tokens,
            "call_latency_measured": self.call_latency_measured,
            "latency_percentile_method": LATENCY_PERCENTILE_METHOD,
            "call_latency_ms_p50": self.call_latency_ms_p50,
            "call_latency_ms_p95": self.call_latency_ms_p95,
            "cost_withheld_provisional": self.cost is None,
        }
        if self.cost is not None:
            fields.update(
                {
                    "cost_input": self.cost.input,
                    "cost_output": self.cost.output,
                    "cost_cache_write": self.cost.cache_write,
                    "cost_cache_read": self.cost.cache_read,
                    "cost_excluding_cache": self.cost.cost_excluding_cache,
                    "cost_cache_total": self.cost.cache_total,
                    "cost_total": self.cost.total,
                    "price_table_version": self.cost.price_table_version,
                }
            )
        return fields


def aggregate_tier_role_model(
    attributions: Sequence[InvocationAttribution],
    *,
    withhold_cost: bool,
    pricing_path: Path | None = None,
) -> tuple[TierRoleModelMetrics, ...]:
    """Aggregate invocation attributions into one row per ``(tier, role, model_id)`` group.

    Tokens are summed per group; cost is the sum of each attribution's own priced cost, or
    ``None`` when ``withhold_cost`` is set. Call latency is the nearest-rank P50/P95 over the
    group's recorded latencies. Groups are returned in ``(tier, role, model_id)`` sorted order.

    Args:
        attributions: Per-invocation attributions from the dispatch site.
        withhold_cost: Whether to omit currency figures; callers pass
            :func:`eval.pricing.is_cost_provisional`.
        pricing_path: Optional override of the price table path (for tests); defaults to the
            shipped table via :func:`crm.metering.cost_for_usage`.

    Returns:
        One :class:`TierRoleModelMetrics` per group, in deterministic key order.
    """
    grouped: dict[tuple[Tier, str, str], list[InvocationAttribution]] = {}
    for attribution in attributions:
        key = (attribution.tier, attribution.role, attribution.model_id)
        grouped.setdefault(key, []).append(attribution)

    results: list[TierRoleModelMetrics] = []
    for (tier, role, model_id), group in sorted(grouped.items()):
        usage = UsageTotals(
            input_tokens=sum(a.tokens.input_tokens for a in group),
            output_tokens=sum(a.tokens.output_tokens for a in group),
            cache_write_tokens=sum(a.cache.write_tokens for a in group),
            cache_read_tokens=sum(a.cache.read_tokens for a in group),
        )
        cost = _price_group(group, model_id, withhold_cost=withhold_cost, pricing_path=pricing_path)
        measured, p50, p95 = _latency_percentiles([a.latency_ms for a in group])
        results.append(
            TierRoleModelMetrics(
                tier=tier,
                role=role,
                model_id=model_id,
                included_invocation_count=len(group),
                usage=usage,
                cost=cost,
                call_latency_measured=measured,
                call_latency_ms_p50=p50,
                call_latency_ms_p95=p95,
            )
        )
    return tuple(results)


def _price_group(
    group: Sequence[InvocationAttribution],
    model_id: str,
    *,
    withhold_cost: bool,
    pricing_path: Path | None,
) -> CostTotals | None:
    """Sum each attribution's own priced cost for one group, or ``None`` when withheld.

    A group with no token-bearing attribution, or a blank ``model_id``, prices to ``None``.
    """
    if withhold_cost or not model_id.strip():
        return None
    priced = [a for a in group if a.bears_tokens]
    if not priced:
        return None
    input_cost = output_cost = cache_write_cost = cache_read_cost = 0.0
    versions: set[str] = set()
    for attribution in priced:
        usage = TokenUsage(
            input_tokens=attribution.tokens.input_tokens,
            output_tokens=attribution.tokens.output_tokens,
            cache_write_tokens=attribution.cache.write_tokens,
            cache_read_tokens=attribution.cache.read_tokens,
        )
        cost = (
            cost_for_usage(usage, model_id)
            if pricing_path is None
            else cost_for_usage(usage, model_id, pricing_path=pricing_path)
        )
        input_cost += cost.input
        output_cost += cost.output
        cache_write_cost += cost.cache_write
        cache_read_cost += cost.cache_read
        versions.add(cost.price_table_version)
    return CostTotals(
        input=input_cost,
        output=output_cost,
        cache_write=cache_write_cost,
        cache_read=cache_read_cost,
        price_table_version=versions.pop(),
    )


@dataclass(frozen=True, slots=True)
class ToolAggregate:
    """Per-tool aggregation across a sweep: count, call latency, and outcome distribution.

    Attributes:
        tool_name: The dispatched tool's name.
        role: The role under which these calls were dispatched; one row per
            ``(tool_name, role)`` pair.
        count: How many times the tool was called.
        call_latency_ms_p50: Nearest-rank P50 dispatch latency in ms, or ``None`` when unmeasured.
        call_latency_ms_p95: Nearest-rank P95 dispatch latency in ms, or ``None`` when unmeasured.
        truncated_count: How many of the calls returned a truncated result.
        outcome_distribution: Count per :data:`agents.tier_attribution.OutcomeKind`, every kind
            present even at zero.
    """

    tool_name: str
    role: str
    count: int
    call_latency_ms_p50: float | None
    call_latency_ms_p95: float | None
    truncated_count: int
    outcome_distribution: dict[OutcomeKind, int]

    def to_report_fields(self) -> dict[str, object]:
        """Render as a flat JSON-safe mapping in a fixed key order."""
        return {
            "tool_name": self.tool_name,
            "role": self.role,
            "count": self.count,
            "call_latency_ms_p50": self.call_latency_ms_p50,
            "call_latency_ms_p95": self.call_latency_ms_p95,
            "truncated_count": self.truncated_count,
            "outcome_distribution": {
                kind: self.outcome_distribution.get(kind, 0) for kind in _OUTCOME_KINDS
            },
        }


def aggregate_tool_calls(
    tool_calls: Sequence[ToolCallAttribution],
) -> tuple[ToolAggregate, ...]:
    """Aggregate per-tool-call attributions into one row per ``(tool_name, role)`` group.

    Each row carries count, nearest-rank P50/P95 dispatch latency (over calls that recorded one),
    truncation count, and the four-way outcome distribution. Rows are returned in
    ``(tool_name, role)`` sorted order.
    """
    grouped: dict[tuple[str, str], list[ToolCallAttribution]] = {}
    for call in tool_calls:
        grouped.setdefault((call.tool_name, call.role), []).append(call)

    results: list[ToolAggregate] = []
    for (tool_name, role), calls in sorted(grouped.items()):
        _, p50, p95 = _latency_percentiles([call.latency_ms for call in calls])
        distribution = Counter(call.outcome_kind for call in calls)
        results.append(
            ToolAggregate(
                tool_name=tool_name,
                role=role,
                count=len(calls),
                call_latency_ms_p50=p50,
                call_latency_ms_p95=p95,
                truncated_count=sum(1 for call in calls if call.truncated),
                outcome_distribution={kind: distribution.get(kind, 0) for kind in _OUTCOME_KINDS},
            )
        )
    return tuple(results)


@dataclass(frozen=True, slots=True)
class TaskSpread:
    """Per-task spread of correctness across repeats: min/mean/max and success fraction.

    Attributes:
        task_id: The golden task these repeats scored.
        repeats: How many repeats contributed.
        correctness_min: The lowest correctness across the repeats.
        correctness_mean: The arithmetic mean correctness.
        correctness_max: The highest correctness.
        success_fraction: Fraction of repeats scoring a full ``1.0``.
    """

    task_id: str
    repeats: int
    correctness_min: float
    correctness_mean: float
    correctness_max: float
    success_fraction: float

    def to_report_fields(self) -> dict[str, object]:
        """Render as a flat JSON-safe mapping in a fixed key order."""
        return {
            "task_id": self.task_id,
            "repeats": self.repeats,
            "correctness_min": self.correctness_min,
            "correctness_mean": self.correctness_mean,
            "correctness_max": self.correctness_max,
            "success_fraction": self.success_fraction,
        }


def aggregate_task_spread(
    correctness_by_task: Mapping[str, Sequence[float]],
) -> tuple[TaskSpread, ...]:
    """Aggregate per-repeat correctness into one spread row per task.

    Args:
        correctness_by_task: Task id → its correctness value from each repeat (a non-empty
            sequence; a repeat that scored the task contributes one value).

    Returns:
        One :class:`TaskSpread` per task, in ``task_id`` sorted order.

    Raises:
        ValueError: When a task maps to an empty sequence.
    """
    results: list[TaskSpread] = []
    for task_id, values in sorted(correctness_by_task.items()):
        if not values:
            raise ValueError(f"task {task_id!r} has no repeats to compute a spread over")
        results.append(
            TaskSpread(
                task_id=task_id,
                repeats=len(values),
                correctness_min=min(values),
                correctness_mean=sum(values) / len(values),
                correctness_max=max(values),
                success_fraction=sum(1 for value in values if value == 1.0) / len(values),
            )
        )
    return tuple(results)


# ---------------------------------------------------------------------------------------------
# Arm report keys under which the three aggregations reach the matrix renderer and the dashboard.
# ---------------------------------------------------------------------------------------------

#: Arm report key: list of :meth:`TierRoleModelMetrics.to_report_fields` mappings.
TIER_ROLE_MODEL_REPORT_KEY: Final[str] = "tier_role_model_metrics"
#: Arm report key: list of :meth:`ToolAggregate.to_report_fields` mappings.
TOOL_METRICS_REPORT_KEY: Final[str] = "tool_metrics"
#: Arm report key: list of :meth:`TaskSpread.to_report_fields` mappings (each carries ``task_id``).
TASK_SPREAD_REPORT_KEY: Final[str] = "task_spread"


def build_tier_report_fields(
    tier_role_model: Sequence[TierRoleModelMetrics],
    tool_aggregates: Sequence[ToolAggregate],
    task_spreads: Sequence[TaskSpread],
) -> dict[str, object]:
    """Assemble the three aggregations into an arm's report dict under their fixed keys.

    The harness merges the result into an arm's report fields. Each value is a list of the
    corresponding dataclass's ``to_report_fields()`` mapping, in aggregator order.
    """
    return {
        TIER_ROLE_MODEL_REPORT_KEY: [row.to_report_fields() for row in tier_role_model],
        TOOL_METRICS_REPORT_KEY: [row.to_report_fields() for row in tool_aggregates],
        TASK_SPREAD_REPORT_KEY: [row.to_report_fields() for row in task_spreads],
    }


#: Arm report key: the :meth:`TierSummary.to_report_fields` mapping.
TIER_SUMMARY_REPORT_KEY: Final[str] = "tier_summary"


@dataclass(frozen=True, slots=True)
class TierSuccess:
    """How many of a tier's applicable turns met that tier's expectation.

    Attributes:
        passed: Applicable turns whose tier score was met.
        applicable: Turns for which the tier was applicable.
    """

    passed: int
    applicable: int

    @property
    def rate(self) -> float | None:
        """``passed / applicable``, or ``None`` when no turn was applicable."""
        return self.passed / self.applicable if self.applicable else None


@dataclass(frozen=True, slots=True)
class RepeatCompletion:
    """How many of a tier's applicable tasks passed in a strict majority of their repeats.

    Attributes:
        completed: Applicable tasks that passed in more than half of their applicable repeats.
        applicable: Tasks for which the tier was applicable in at least one repeat.
        repeats: The per-task repeat count, equal across tasks.
    """

    completed: int
    applicable: int
    repeats: int

    @property
    def rate(self) -> float | None:
        """``completed / applicable``, or ``None`` when no task was applicable."""
        return self.completed / self.applicable if self.applicable else None

    def to_report_fields(self) -> dict[str, object]:
        """Render as ``{completed, applicable, repeats, rate}``."""
        return {
            "completed": self.completed,
            "applicable": self.applicable,
            "repeats": self.repeats,
            "rate": self.rate,
        }


def completion_over_repeats(tier_scores: Sequence[TierScores], tier: str) -> RepeatCompletion:
    """Count the tasks completed at ``tier`` over repeats.

    Args:
        tier_scores: One :func:`eval.scoring.score_tiers` result per turn, every repeat of every
            task; turns are grouped by ``task_id``.
        tier: One of :data:`eval.scoring.SCORED_TIERS`.

    Returns:
        The completion count; a task is completed when its tier passed in a strict majority of
        the repeats in which the tier was applicable.

    Raises:
        ValueError: Tasks have unequal repeat counts, or ``tier`` is not a scored tier.
    """
    if tier not in SCORED_TIERS:
        raise ValueError(f"tier must be one of {SCORED_TIERS}, got {tier!r}")
    by_task: dict[str, list[TierScores]] = {}
    for scores in tier_scores:
        by_task.setdefault(scores.task_id, []).append(scores)
    repeat_counts = {len(turns) for turns in by_task.values()}
    if len(repeat_counts) > 1:
        raise ValueError(f"unequal repeat counts across tasks: {sorted(repeat_counts)}")
    completed = 0
    applicable = 0
    for turns in by_task.values():
        applicable_scores = [
            turn.by_tier()[tier] for turn in turns if turn.by_tier()[tier].applicable
        ]
        if not applicable_scores:
            continue
        applicable += 1
        passes = sum(1 for score in applicable_scores if score.passed)
        if passes * 2 > len(applicable_scores):
            completed += 1
    return RepeatCompletion(
        completed=completed,
        applicable=applicable,
        repeats=repeat_counts.pop() if repeat_counts else 0,
    )


@dataclass(frozen=True, slots=True)
class TierSummaryEntry:
    """One tier's tokens, cost, call latency and success across an arm.

    Attributes:
        tier: The model tier.
        input_tokens: Summed input tokens of every invocation at this tier.
        output_tokens: Summed output tokens of every invocation at this tier.
        cost_total: Summed cost of this tier's invocations, or ``None`` while cost is withheld.
        latency_p50_ms: The tier's nearest-rank P50 call latency, or ``None`` when unmeasured.
        latency_p95_ms: The tier's nearest-rank P95 call latency, or ``None`` when unmeasured.
        success: The tier's success count over its applicable turns.
        completion_over_repeats: The tier's task completion over repeats.
    """

    tier: Tier
    input_tokens: int
    output_tokens: int
    cost_total: float | None
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    success: TierSuccess
    completion_over_repeats: RepeatCompletion

    def to_report_fields(self) -> dict[str, object]:
        """Render as a flat JSON-safe mapping in a fixed key order."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_total": self.cost_total,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "success": {
                "passed": self.success.passed,
                "applicable": self.success.applicable,
                "rate": self.success.rate,
            },
            "completion_over_repeats": self.completion_over_repeats.to_report_fields(),
        }


@dataclass(frozen=True, slots=True)
class TierSummary:
    """An arm's per-tier summary, one entry per tier in :data:`_ALL_TIERS` order.

    Attributes:
        entries: Every tier's summary.
    """

    entries: tuple[TierSummaryEntry, ...]

    def to_report_fields(self) -> dict[str, object]:
        """Render as ``{<tier>: <entry fields>}`` in tier order."""
        return {entry.tier: entry.to_report_fields() for entry in self.entries}


def aggregate_tier_summary(
    tier_role_model: Sequence[TierRoleModelMetrics],
    latency: LatencyMetrics,
    tier_scores: Sequence[TierScores],
    *,
    withhold_cost: bool,
) -> TierSummary:
    """Roll an arm's per-group metrics, per-tier latency and per-turn tier scores up per tier.

    Args:
        tier_role_model: The arm's :func:`aggregate_tier_role_model` rows; tokens and cost are
            summed over every row at a tier.
        latency: The arm's aggregated latency; each tier's P50/P95 is read from
            ``latency.by_tier``.
        tier_scores: One :func:`eval.scoring.score_tiers` result per turn the arm ran, every
            repeat included.
        withhold_cost: Whether currency figures are withheld; ``cost_total`` is then ``None``.

    Returns:
        One entry per tier, every tier present even with no invocation or no applicable turn.
    """
    entries: list[TierSummaryEntry] = []
    for tier in _ALL_TIERS:
        rows = [row for row in tier_role_model if row.tier == tier]
        cost_total = (
            None if withhold_cost else sum(row.cost.total for row in rows if row.cost is not None)
        )
        scores = [scores.by_tier()[tier] for scores in tier_scores]
        applicable = [score for score in scores if score.applicable]
        tier_latency = latency.by_tier[tier]
        entries.append(
            TierSummaryEntry(
                tier=tier,
                input_tokens=sum(row.usage.input_tokens for row in rows),
                output_tokens=sum(row.usage.output_tokens for row in rows),
                cost_total=cost_total,
                latency_p50_ms=tier_latency.p50,
                latency_p95_ms=tier_latency.p95,
                success=TierSuccess(
                    passed=sum(1 for score in applicable if score.passed),
                    applicable=len(applicable),
                ),
                completion_over_repeats=completion_over_repeats(tier_scores, tier),
            )
        )
    return TierSummary(entries=tuple(entries))


#: Delegation-outcome bucket for a turn that reported no recognised outcome.
DELEGATION_OUTCOME_NONE: Final[str] = "none"


@dataclass(frozen=True, slots=True)
class TaskTierObservation:
    """One task unit's inputs to :func:`aggregate_tier_task_metrics`.

    Attributes:
        task_id: The golden task's ``id``.
        tier_scores: The turn's per-tier scores.
        invocations: Every model invocation of the turn, each tagged with its tier.
        delegation_outcome: The turn's delegation outcome (one of the four outcome kinds), or
            ``None`` when the turn reported none.
        resolved_efforts: The effort each tier resolved to for the turn (``None`` value: the tier's
            model takes no effort), or ``None`` when the turn recorded no resolved tiers.
        tool_calls: Every dispatched tool call of the turn, in call order, each attributed to the
            role that dispatched it.
    """

    task_id: str
    tier_scores: TierScores
    invocations: tuple[InvocationAttribution, ...]
    delegation_outcome: str | None
    resolved_efforts: Mapping[Tier, str | None] | None
    tool_calls: tuple[ToolCallAttribution, ...] = ()


@dataclass(frozen=True, slots=True)
class TierTaskLatency:
    """One tier's configured model, observed efforts and per-completed-task latency for an arm.

    Attributes:
        tier: The model tier.
        configured_model_id: The model the arm's capability matrix configures at this tier.
        efforts: Distinct efforts observed at this tier across the arm's turns, sorted with
            ``None`` last; empty when no turn recorded one.
        latency_sample_count: How many completed tasks contributed a latency sample.
        completed_task_latency_ms_p50: Nearest-rank P50 of the per-task latency sums, or ``None``
            with no sample or when unmeasured.
        completed_task_latency_ms_p95: Nearest-rank P95 likewise.
        retry_count: Self-corrected retries (:func:`retried_failure_positions`) among the tool
            calls this tier dispatched, over every task the arm ran; always 0 at the tool tier.
    """

    tier: Tier
    configured_model_id: str
    efforts: tuple[str | None, ...]
    latency_sample_count: int
    completed_task_latency_ms_p50: float | None
    completed_task_latency_ms_p95: float | None
    retry_count: int = 0

    def to_report_fields(self) -> dict[str, object]:
        """Render as a flat JSON-safe mapping in a fixed key order."""
        return {
            "configured_model_id": self.configured_model_id,
            "efforts": list(self.efforts),
            "latency_sample_count": self.latency_sample_count,
            "completed_task_latency_ms_p50": self.completed_task_latency_ms_p50,
            "completed_task_latency_ms_p95": self.completed_task_latency_ms_p95,
            "retry_count": self.retry_count,
        }


@dataclass(frozen=True, slots=True)
class TierTaskMetrics:
    """An arm's per-tier task-level metrics and delegation-outcome counts.

    Attributes:
        family: The arm's model family.
        by_tier: One entry per tier, in :data:`ALL_TIERS` order.
        delegation_outcomes: Turn count per outcome kind then ``"none"``, every key present.
    """

    family: str
    by_tier: tuple[TierTaskLatency, ...]
    delegation_outcomes: dict[str, int]

    def to_report_fields(self) -> dict[str, object]:
        """Render as ``{family, by_tier: {<tier>: ...}, delegation_outcomes}``."""
        return {
            "family": self.family,
            "by_tier": {entry.tier: entry.to_report_fields() for entry in self.by_tier},
            "delegation_outcomes": dict(self.delegation_outcomes),
        }


#: The role whose tool calls are the supervisor tier's own; every other role's are the subagent tier's.
SUPERVISOR_ROLE: Final[str] = "supervisor"


def retried_failure_positions(tool_calls: Sequence[ToolCallAttribution]) -> frozenset[int]:
    """Return the positions of the turn's ``tool_failure`` calls that were self-corrected.

    A ``tool_failure`` call is retried when a later call in the same turn dispatches the same
    ``tool_name`` under the same ``role`` and classifies as ``data`` or ``no_match``. A ``denied``
    call is never a failure that a retry clears.
    """
    positions: set[int] = set()
    for index, call in enumerate(tool_calls):
        if call.outcome_kind != "tool_failure":
            continue
        if any(
            later.tool_name == call.tool_name
            and later.role == call.role
            and later.outcome_kind in ("data", "no_match")
            for later in tool_calls[index + 1 :]
        ):
            positions.add(index)
    return frozenset(positions)


def _tier_retry_count(observations: Sequence[TaskTierObservation], tier: Tier) -> int:
    if tier == "tool":
        return 0
    count = 0
    for observation in observations:
        for position in retried_failure_positions(observation.tool_calls):
            is_supervisor = observation.tool_calls[position].role == SUPERVISOR_ROLE
            count += int(is_supervisor == (tier == "supervisor"))
    return count


def _sorted_efforts(values: set[str | None]) -> tuple[str | None, ...]:
    named = sorted(value for value in values if value is not None)
    return (*named, None) if None in values else tuple(named)


def aggregate_tier_task_metrics(
    observations: Sequence[TaskTierObservation],
    *,
    measured: bool,
    family: str,
    configured_model_ids: Mapping[Tier, str],
) -> TierTaskMetrics:
    """Aggregate an arm's task units into per-tier completed-task latency and outcome counts.

    A task is completed at a tier when that tier's score is applicable and met. Each completed
    task with at least one measured call at the tier contributes the sum of those calls' latencies;
    one with none contributes no sample. Percentiles are nearest-rank. When ``measured`` is
    ``False`` every latency figure is ``None`` at a zero count. Efforts, delegation outcomes and
    per-tier retry counts are collected over every observation regardless of ``measured``.

    Args:
        observations: One entry per task unit the arm ran.
        measured: Whether the arm's latencies were measured (``False`` for a stub arm).
        family: The arm's model family.
        configured_model_ids: The configured model id for every tier.

    Returns:
        The arm's :class:`TierTaskMetrics`.

    Raises:
        ValueError: When ``configured_model_ids`` lacks a tier, or an observation carries a
            delegation outcome outside the four outcome kinds.
    """
    missing = [tier for tier in _ALL_TIERS if tier not in configured_model_ids]
    if missing:
        raise ValueError(f"configured_model_ids lacks tiers: {missing}")

    outcomes: dict[str, int] = dict.fromkeys((*_OUTCOME_KINDS, DELEGATION_OUTCOME_NONE), 0)
    for observation in observations:
        outcome = observation.delegation_outcome
        if outcome is None:
            outcomes[DELEGATION_OUTCOME_NONE] += 1
        elif outcome in _OUTCOME_KINDS:
            outcomes[outcome] += 1
        else:
            raise ValueError(
                f"task {observation.task_id}: delegation_outcome must be one of "
                f"{list(_OUTCOME_KINDS)} or None, got {outcome!r}"
            )

    by_tier: list[TierTaskLatency] = []
    for tier in _ALL_TIERS:
        efforts: set[str | None] = set()
        samples: list[float] = []
        for observation in observations:
            if observation.resolved_efforts is not None and tier in observation.resolved_efforts:
                efforts.add(observation.resolved_efforts[tier])
            score = observation.tier_scores.by_tier()[tier]
            if not (measured and score.applicable and score.passed is True):
                continue
            values = [
                invocation.latency_ms
                for invocation in observation.invocations
                if invocation.tier == tier and invocation.latency_ms is not None
            ]
            if values:
                samples.append(sum(values))
        by_tier.append(
            TierTaskLatency(
                tier=tier,
                configured_model_id=configured_model_ids[tier],
                efforts=_sorted_efforts(efforts),
                latency_sample_count=len(samples),
                completed_task_latency_ms_p50=(
                    nearest_rank_percentile(samples, 50) if samples else None
                ),
                completed_task_latency_ms_p95=(
                    nearest_rank_percentile(samples, 95) if samples else None
                ),
                retry_count=_tier_retry_count(observations, tier),
            )
        )
    return TierTaskMetrics(family=family, by_tier=tuple(by_tier), delegation_outcomes=outcomes)

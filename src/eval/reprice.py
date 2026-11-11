# SPDX-License-Identifier: MIT
"""Re-price a stored sweep from its per-call records against the current price table.

``python -m eval.reprice --raw-dir DIR --output-dir OUT [--assume-inference-profile-prefix P]``
reads each arm's archive from ``DIR/*.json`` (:func:`eval.matrix.load_raw_archives`) and its
per-call ``invocation_attributions`` from ``DIR/pre-aggregation/*.json``, prices every call under
its own inference profile, re-aggregates the cost-bearing blocks with the sweep's own functions in
:mod:`eval.metrics`, and writes ``matrix.{json,md,csv}`` plus ``reprice.json`` into ``OUT``.

An entry without ``inference_profile`` is refused unless ``--assume-inference-profile-prefix`` is
given, in which case its profile is ``<prefix><model_id>``; the assertion is echoed to stderr and
recorded in ``reprice.json``. An arm with no pre-aggregation file is refused, as is one whose
per-call records disagree with the arm archive: per ``(tier, role, model_id)`` row, the call count
and token sums must equal the archived ``tier_role_model_metrics``, and per tier the input and
output token sums must equal the archived ``tier_summary``. Exit codes: ``0`` written, ``2``
refused.

:func:`with_archived_pricing` is the writer side of the same per-call keys, used by the harness's
pre-aggregation writer.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

from agents.tier_attribution import CacheCounts, InvocationAttribution, TokenCounts
from crm.inference_route import UnknownInferenceRoute
from crm.metering import (
    Cost,
    TokenUsage,
    UsageRecord,
    build_usage_record,
    cost_for_usage,
    family_for_model_id,
)
from eval.matrix import ArmResult, build_matrix_result, load_raw_archives, write_matrix_artifacts
from eval.metrics import (
    aggregate_model_metrics,
    aggregate_tier_role_model,
    aggregate_tier_summary,
    not_measured_latency_metrics,
)
from eval.pricing import inference_profile_for, is_cost_provisional, load_price_table_status
from eval.sweep_units import arm_total_fields
from providers.capabilities import Tier

#: Per-call keys the pre-aggregation archive carries on every ``invocation_attributions`` entry.
INFERENCE_PROFILE_KEY: Final[str] = "inference_profile"
PRICING_TIER_KEY: Final[str] = "pricing_tier"
ROUTE_KEY: Final[str] = "route"
ARCHIVED_PRICING_KEYS: Final[tuple[str, ...]] = (INFERENCE_PROFILE_KEY, PRICING_TIER_KEY, ROUTE_KEY)

#: Subdirectory of the raw directory holding the pre-aggregation archives.
PRE_AGGREGATION_DIRNAME: Final[str] = "pre-aggregation"

#: The record of how a re-price was performed, written beside the matrix.
REPRICE_RECORD_FILENAME: Final[str] = "reprice.json"

#: The tier whose model names an arm, as in :data:`eval.sweep_units.ARM_MODEL_TIER`.
_ARM_MODEL_TIER: Final[Tier] = "supervisor"

#: Model-metrics keys a re-price recomputes; every other key is carried from the archive.
_MODEL_METRICS_COST_KEYS: Final[tuple[str, ...]] = (
    "cost_input",
    "cost_output",
    "cost_cache_write",
    "cost_cache_read",
    "cost_excluding_cache",
    "cost_cache_total",
    "cost_total",
    "price_table_version",
)
_REPRICED_MODEL_METRICS_KEYS: Final[tuple[str, ...]] = (
    "cost_withheld_provisional",
    "long_context_invocation_count",
    *_MODEL_METRICS_COST_KEYS,
)
_REPRICED_TIER_SUMMARY_KEYS: Final[tuple[str, ...]] = (
    "cost_total",
    "long_context_invocation_count",
)


class RepriceRefused(ValueError):  # noqa: N818
    """Raised when a stored run cannot be re-priced as asked; the message names the reason."""


def attribution_from_entry(entry: Mapping[str, object]) -> InvocationAttribution:
    """Rebuild one :class:`agents.tier_attribution.InvocationAttribution` from its archived dict.

    Raises:
        RepriceRefused: The entry is not shaped like an archived attribution.
    """
    try:
        tokens = cast(Mapping[str, object], entry["tokens"])
        cache = cast(Mapping[str, object], entry["cache"])
        latency = entry.get("latency_ms")
        return InvocationAttribution(
            tier=cast(Tier, entry["tier"]),
            role=str(entry["role"]),
            model_id=str(entry["model_id"]),
            tokens=TokenCounts(
                input_tokens=int(cast(int, tokens["input_tokens"])),
                output_tokens=int(cast(int, tokens["output_tokens"])),
            ),
            cache=CacheCounts(
                read_tokens=int(cast(int, cache["read_tokens"])),
                write_tokens=int(cast(int, cache["write_tokens"])),
            ),
            latency_ms=None if latency is None else float(cast(float, latency)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RepriceRefused(
            f"malformed invocation attribution entry {dict(entry)!r}: {exc}"
        ) from exc


def _usage(attribution: InvocationAttribution) -> TokenUsage:
    return TokenUsage(
        input_tokens=attribution.tokens.input_tokens,
        output_tokens=attribution.tokens.output_tokens,
        cache_write_tokens=attribution.cache.write_tokens,
        cache_read_tokens=attribution.cache.read_tokens,
    )


def price_attribution(attribution: InvocationAttribution, *, inference_profile: str) -> Cost:
    """Price one call under ``inference_profile`` against the current price table."""
    return cost_for_usage(
        _usage(attribution), attribution.model_id, inference_profile=inference_profile
    )


def with_archived_pricing(entry: object) -> dict[str, object]:
    """Return an archived attribution entry with its per-call pricing keys added.

    The profile is the model's configured one (:func:`eval.pricing.inference_profile_for`); the
    pricing tier and route are those :func:`price_attribution` resolves for the call. An entry
    with a blank ``model_id`` (a zero-token structural record) carries ``None`` for all three.

    Raises:
        RepriceRefused: The entry is not shaped like an archived attribution.
        ValueError: The entry's model has no configured inference profile.
    """
    if not isinstance(entry, Mapping):
        raise RepriceRefused(f"invocation attribution entry is not an object: {entry!r}")
    attribution = attribution_from_entry(entry)
    priced: dict[str, object] = dict.fromkeys(ARCHIVED_PRICING_KEYS)
    if attribution.model_id.strip():
        profile = inference_profile_for(attribution.model_id)
        cost = price_attribution(attribution, inference_profile=profile)
        priced = {
            INFERENCE_PROFILE_KEY: profile,
            PRICING_TIER_KEY: cost.pricing_tier,
            ROUTE_KEY: cost.route,
        }
    return {**entry, **priced}


@dataclasses.dataclass(frozen=True, slots=True)
class _ArmCalls:
    """One arm's per-call records and the profile each attributed model is priced under."""

    attributions: tuple[InvocationAttribution, ...]
    profiles: Mapping[str, str]
    assumed_entry_count: int


def _arm_calls(entries: Sequence[object], *, model_id: str, assume_prefix: str | None) -> _ArmCalls:
    """Resolve every archived call of one arm and the single profile per attributed model.

    Raises:
        RepriceRefused: An entry is malformed, a token-bearing entry has no profile and no prefix
            was asserted, or one model carries more than one profile.
    """
    attributions: list[InvocationAttribution] = []
    profiles: dict[str, set[str]] = {}
    unprofiled = 0
    assumed = 0
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise RepriceRefused(f"arm {model_id!r}: attribution entry is not an object: {entry!r}")
        attribution = attribution_from_entry(entry)
        attributions.append(attribution)
        if not attribution.model_id.strip():
            continue
        profile = entry.get(INFERENCE_PROFILE_KEY)
        if profile is None:
            if assume_prefix is None:
                unprofiled += 1
                continue
            profile = f"{assume_prefix}{attribution.model_id}"
            assumed += 1
        if not isinstance(profile, str) or not profile:
            raise RepriceRefused(
                f"arm {model_id!r}: {INFERENCE_PROFILE_KEY} {profile!r} is not a non-empty string"
            )
        profiles.setdefault(attribution.model_id, set()).add(profile)
    if unprofiled:
        raise RepriceRefused(
            f"arm {model_id!r}: {unprofiled} per-call entr{'y' if unprofiled == 1 else 'ies'} "
            f"carry no {INFERENCE_PROFILE_KEY}; pass --assume-inference-profile-prefix to assert "
            "the route they ran on"
        )
    mixed = {model: sorted(found) for model, found in profiles.items() if len(found) > 1}
    if mixed:
        raise RepriceRefused(f"arm {model_id!r}: a model carries more than one profile: {mixed}")
    return _ArmCalls(
        attributions=tuple(attributions),
        profiles={model: found.pop() for model, found in profiles.items()},
        assumed_entry_count=assumed,
    )


#: Per-row figures cross-checked between the per-call records and ``tier_role_model_metrics``.
_ROW_CHECK_KEYS: Final[tuple[str, ...]] = (
    "included_invocation_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def _row_figures(attributions: Sequence[InvocationAttribution]) -> dict[str, int]:
    return {
        "included_invocation_count": len(attributions),
        "input_tokens": sum(a.tokens.input_tokens for a in attributions),
        "output_tokens": sum(a.tokens.output_tokens for a in attributions),
        "cache_read_tokens": sum(a.cache.read_tokens for a in attributions),
        "cache_write_tokens": sum(a.cache.write_tokens for a in attributions),
    }


def cross_check_archive(arm: ArmResult, attributions: Sequence[InvocationAttribution]) -> None:
    """Refuse per-call records that are not the calls the arm archive was aggregated from.

    Per ``(tier, role, model_id)``, the record count and token sums must equal the archived
    ``tier_role_model_metrics`` row (every key the row carries); per tier, the input and output
    token sums must equal the archived ``tier_summary`` entry where it records them.

    Raises:
        RepriceRefused: The archive has no rows to check against, or any figure disagrees.
    """
    model_id = arm.provenance.model_id
    if not arm.tier_role_model_fields:
        raise RepriceRefused(
            f"arm {model_id!r}: the arm archive carries no tier_role_model_metrics, so its "
            "per-call records cannot be cross-checked"
        )
    grouped: dict[tuple[str, str, str], list[InvocationAttribution]] = {}
    for attribution in attributions:
        grouped.setdefault((attribution.tier, attribution.role, attribution.model_id), []).append(
            attribution
        )
    archived_rows: dict[tuple[str, str, str], Mapping[str, object]] = {
        (str(row.get("tier")), str(row.get("role")), str(row.get("model_id"))): row
        for row in arm.tier_role_model_fields
    }
    mismatches: list[str] = []
    for key in sorted(set(grouped) | set(archived_rows)):
        figures = _row_figures(grouped.get(key, []))
        row = archived_rows.get(key, {})
        for name in _ROW_CHECK_KEYS:
            checked = name in row or not row
            if checked and row.get(name, 0) != figures[name]:
                mismatches.append(
                    f"{'/'.join(key)} {name}: archived {row.get(name, 0)!r}, "
                    f"per-call {figures[name]}"
                )
    for tier, entry in arm.tier_summary_fields.items():
        if not isinstance(entry, Mapping):
            continue
        tier_calls = [a for a in attributions if a.tier == tier]
        sums = {
            "input_tokens": sum(a.tokens.input_tokens for a in tier_calls),
            "output_tokens": sum(a.tokens.output_tokens for a in tier_calls),
        }
        for name, total in sums.items():
            if name in entry and entry[name] != total:
                mismatches.append(
                    f"tier_summary[{tier!r}] {name}: archived {entry[name]!r}, per-call {total}"
                )
    if mismatches:
        shown = "; ".join(mismatches[:5])
        more = f" (and {len(mismatches) - 5} more)" if len(mismatches) > 5 else ""
        raise RepriceRefused(
            f"arm {model_id!r}: the pre-aggregation records disagree with the arm archive, so "
            f"they are not the calls it was aggregated from: {shown}{more}"
        )


def _priced_records(
    attributions: Sequence[InvocationAttribution], *, profiles: Mapping[str, str]
) -> list[UsageRecord]:
    """Priced records for every token-attributed call of the arm, every tier."""
    return [
        build_usage_record(
            correlation_id="reprice",
            turn_id="reprice",
            invocation_id=f"reprice-{index}",
            rep_id="reprice",
            model_id=attribution.model_id,
            family=family_for_model_id(attribution.model_id),
            tier=attribution.tier,
            usage=_usage(attribution),
            inference_profile=profiles[attribution.model_id],
        )
        for index, attribution in enumerate(attributions)
        if attribution.model_id.strip()
    ]


def _usage_records(
    attributions: Sequence[InvocationAttribution], *, model_id: str, profiles: Mapping[str, str]
) -> list[UsageRecord]:
    """Priced records for the arm model's supervisor-tier calls, the arm's included set."""
    return [
        build_usage_record(
            correlation_id="reprice",
            turn_id="reprice",
            invocation_id=f"reprice-{index}",
            rep_id="reprice",
            model_id=attribution.model_id,
            family=family_for_model_id(model_id),
            tier=attribution.tier,
            usage=_usage(attribution),
            inference_profile=profiles[attribution.model_id],
        )
        for index, attribution in enumerate(attributions)
        if attribution.tier == _ARM_MODEL_TIER and attribution.model_id == model_id
    ]


def repriced_arm(arm: ArmResult, calls: _ArmCalls, *, withhold_cost: bool) -> ArmResult:
    """Return ``arm`` with its cost-bearing blocks recomputed from ``calls``.

    ``tier_role_model_metrics`` is re-aggregated whole; ``tier_summary`` and ``model_metrics``
    take their cost and long-context keys from the re-aggregation and keep every other key; the
    all-tier arm totals are recomputed over every call; provenance takes the current price table
    version.

    Raises:
        RepriceRefused: The archive's tier summary is not one object per tier.
    """
    model_id = arm.provenance.model_id
    rows = aggregate_tier_role_model(
        calls.attributions, withhold_cost=withhold_cost, inference_profiles=calls.profiles
    )
    summary = aggregate_tier_summary(
        rows, not_measured_latency_metrics(), [], withhold_cost=withhold_cost
    ).to_report_fields()
    tier_summary: dict[str, object] = {}
    for tier, archived in arm.tier_summary_fields.items():
        if not isinstance(archived, Mapping):
            raise RepriceRefused(f"arm {model_id!r}: tier_summary[{tier!r}] is not an object")
        recomputed = cast(Mapping[str, object], summary.get(tier, {}))
        tier_summary[tier] = {
            **archived,
            **{key: recomputed.get(key) for key in _REPRICED_TIER_SUMMARY_KEYS},
        }

    metrics = aggregate_model_metrics(
        model_id,
        included=_usage_records(calls.attributions, model_id=model_id, profiles=calls.profiles),
        exclusions=[],
        withhold_cost=withhold_cost,
    ).to_report_fields()
    archived_metrics = arm.model_metrics_fields
    model_metrics: dict[str, object] = {
        key: metrics[key] if key in _REPRICED_MODEL_METRICS_KEYS else archived_metrics[key]
        for key in metrics
        if key in _REPRICED_MODEL_METRICS_KEYS or key in archived_metrics
    }
    model_metrics.update(
        {
            key: value
            for key, value in archived_metrics.items()
            if key not in model_metrics and key not in _MODEL_METRICS_COST_KEYS
        }
    )

    return dataclasses.replace(
        arm,
        provenance=dataclasses.replace(
            arm.provenance, price_table_version=load_price_table_status().price_table_version
        ),
        model_metrics_fields=model_metrics,
        tier_role_model_fields=tuple(row.to_report_fields() for row in rows),
        tier_summary_fields=tier_summary,
        arm_totals_fields=arm_total_fields(
            _priced_records(calls.attributions, profiles=calls.profiles),
            withhold_cost=withhold_cost,
        ),
    )


def _load_pre_aggregation(raw_dir: Path) -> dict[str, tuple[Path, list[object]]]:
    """Map each pre-aggregation archive's ``model_id`` to its path and attribution entries.

    Raises:
        RepriceRefused: An archive is unreadable or malformed, or two name the same model.
    """
    found: dict[str, tuple[Path, list[object]]] = {}
    for path in sorted((raw_dir / PRE_AGGREGATION_DIRNAME).glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepriceRefused(f"{path}: unreadable pre-aggregation archive: {exc}") from exc
        model_id = payload.get("model_id") if isinstance(payload, dict) else None
        entries = payload.get("invocation_attributions") if isinstance(payload, dict) else None
        if not isinstance(model_id, str) or not isinstance(entries, list):
            raise RepriceRefused(f"{path}: no model_id and invocation_attributions list")
        if model_id in found:
            raise RepriceRefused(
                f"{path} and {found[model_id][0]} both hold pre-aggregation records for {model_id!r}"
            )
        found[model_id] = (path, entries)
    return found


def reprice_run(
    raw_dir: Path, output_dir: Path, *, assume_prefix: str | None = None
) -> dict[str, object]:
    """Re-price the stored run in ``raw_dir`` and write its matrix into ``output_dir``.

    Args:
        raw_dir: The sweep's ``raw/`` directory.
        output_dir: Where ``matrix.{json,md,csv}`` and ``reprice.json`` are written.
        assume_prefix: Route prefix asserted for entries that carry no inference profile.

    Returns:
        The ``reprice.json`` record.

    Raises:
        RepriceRefused: The run cannot be re-priced as asked; nothing is written.
    """
    try:
        arms = load_raw_archives(raw_dir)
    except ValueError as exc:
        raise RepriceRefused(str(exc)) from exc
    pre_aggregation = _load_pre_aggregation(raw_dir)
    withhold_cost = is_cost_provisional()
    repriced: list[ArmResult] = []
    arm_records: list[dict[str, object]] = []
    for arm in arms:
        model_id = arm.provenance.model_id
        if model_id not in pre_aggregation:
            raise RepriceRefused(
                f"arm {model_id!r} has no pre-aggregation archive under "
                f"{raw_dir / PRE_AGGREGATION_DIRNAME}, so it has no per-call granularity to price"
            )
        path, entries = pre_aggregation[model_id]
        calls = _arm_calls(entries, model_id=model_id, assume_prefix=assume_prefix)
        cross_check_archive(arm, calls.attributions)
        try:
            repriced.append(repriced_arm(arm, calls, withhold_cost=withhold_cost))
        except (UnknownInferenceRoute, KeyError) as exc:
            raise RepriceRefused(f"arm {model_id!r}: cannot price: {exc}") from exc
        totals = repriced[-1].arm_totals_fields
        arm_records.append(
            {
                "model_id": model_id,
                **totals,
                "pre_aggregation_archive": path.name,
                "invocation_count": len(calls.attributions),
                "assumed_profile_entry_count": calls.assumed_entry_count,
                "inference_profiles": dict(sorted(calls.profiles.items())),
            }
        )
    record: dict[str, object] = {
        "assumed_inference_profile_prefix": assume_prefix,
        "price_table_version": load_price_table_status().price_table_version,
        "cost_withheld_provisional": withhold_cost,
        "arms": arm_records,
    }
    write_matrix_artifacts(build_matrix_result(repriced), output_dir)
    (output_dir / REPRICE_RECORD_FILENAME).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return record


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.reprice",
        description=(
            "Re-price a stored sweep's per-call records against the current price table and "
            "write matrix.json/matrix.md/matrix.csv plus reprice.json."
        ),
    )
    parser.add_argument("--raw-dir", type=Path, required=True, help="The sweep's raw/ directory.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Where to write the matrix.")
    parser.add_argument(
        "--assume-inference-profile-prefix",
        default=None,
        help=(
            "Route prefix (e.g. 'global.') asserted for per-call entries that carry no "
            "inference_profile; the profile becomes <prefix><model_id>. Without it such entries "
            "are refused."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns ``0`` when written, ``2`` when refused."""
    args = _build_arg_parser().parse_args(argv)
    prefix: str | None = args.assume_inference_profile_prefix
    if prefix is not None:
        print(
            f"asserting inference profile prefix {prefix!r} for per-call entries without one",
            file=sys.stderr,
        )
    try:
        record = reprice_run(args.raw_dir, args.output_dir, assume_prefix=prefix)
    except RepriceRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for arm in cast("list[dict[str, object]]", record["arms"]):
        print(f"re-priced {arm['model_id']} ({arm['invocation_count']} calls)")
    print(f"wrote {args.output_dir / REPRICE_RECORD_FILENAME}")
    return 0


__all__ = [
    "ARCHIVED_PRICING_KEYS",
    "INFERENCE_PROFILE_KEY",
    "PRE_AGGREGATION_DIRNAME",
    "PRICING_TIER_KEY",
    "REPRICE_RECORD_FILENAME",
    "ROUTE_KEY",
    "RepriceRefused",
    "attribution_from_entry",
    "cross_check_archive",
    "main",
    "price_attribution",
    "repriced_arm",
    "reprice_run",
    "with_archived_pricing",
]


if __name__ == "__main__":
    raise SystemExit(main())

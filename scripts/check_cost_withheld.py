#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Cost-withholding gate: no absolute currency figure is published while pricing is provisional.

Invoked by ``scripts/check_cost_withheld.sh``, which resolves the interpreter and default paths
and execs this module. This module implements three checks:

1. While ``config/pricing.yaml`` carries ``provisional: true``, no absolute currency figure may
   appear in a published artifact: every file under ``results/**`` (see
   :func:`iter_published_artifacts`) plus ``README.md`` when ``--readme-file`` is supplied. In a
   structured artifact whose schema this repo generates (:data:`_ENUMERABLE_SCHEMA_RELPATHS`) a
   numeric field is a violation unless its key is a known non-currency field
   (:data:`_NON_CURRENCY_NUMERIC_FIELDS`). Elsewhere the checks are currency-shape and known-key
   detection (:data:`_CURRENCY_SHAPE_PATTERN`, :data:`_CURRENCY_FIELD_NAMES`).
2. The same, on the rendered eval dashboard source (``--frontend-dir``;
   :func:`find_currency_rendering_in_frontend`).
3. While every rate in the price table's ``models`` block, flat and ``long_context``, is zero, no
   arm in ``matrix.json`` may render with ``cost_withheld_provisional`` set to ``false``.

When ``provisional`` is ``false``, every check is skipped and the gate exits 0. When it is ``true``
and no artifact or consumer was examined, the gate exits 3.
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

#: Keys eval.metrics.ModelMetrics.to_report_fields() emits only when cost is not withheld.
#: Presence of any one of these in a rendered arm's model-metrics fields, or as a key, column or
#: textual name followed by a number in a published artifact, is a currency-figure signal.
_CURRENCY_FIELD_NAMES: tuple[str, ...] = (
    "cost_input",
    "cost_output",
    "cost_cache_write",
    "cost_cache_read",
    "cost_cache_total",
    "cost_excluding_cache",
    "cost_total",
)

#: Absolute-currency-figure textual pattern for matrix.md / matrix.csv / raw archives: one of the
#: known cost field names followed by a numeric value, tolerating Markdown table pipes, CSV
#: commas, and JSON-style ``"key": value`` separators between the name and the number, and
#: tolerating the field name appearing again as a later CSV column value on the same line.
_CURRENCY_LINE_PATTERN = re.compile(
    r"(?:" + "|".join(re.escape(name) for name in _CURRENCY_FIELD_NAMES) + r")"
    r"[^0-9\n]{0,40}-?\d"
)


#: Currency *shape* pattern: a currency symbol, ISO code, or unit word adjacent to a numeral, so a
#: figure stated in prose is caught as well as one emitted under a known key.
#:
#: Recognised families:
#:
#: 1. a Unicode currency symbol on either side of a numeral (`$0.47`, `0.47$`, `＄0.47`, `43₽`);
#: 2. an ISO 4217 code on either side of a numeral, any amount of intra-line whitespace
#:    (`USD 0.47`, `0.47    USD`);
#: 3. a numeral before a currency unit word, optionally through one short qualifier
#:    (`47 cents`, `0.47 US dollars`);
#: 4. a spelled-out number immediately before a currency unit word (`forty-seven cents`);
#: 5. any of the above spelled with HTML character references (`&#36;0.47`, `&dollar;0.47`), which
#:    are decoded before matching;
#: 6. any of the above split across a single line break, checked by re-scanning each adjacent line
#:    pair and reporting only a match that spans the boundary.
#:
#: Not detected: an amount spelled out without an adjacent unit word; a numeral followed by a unit
#: word outside `_CURRENCY_UNIT_WORDS`; a figure split across more than one line break; a renamed
#: cost key in a text artifact such as `results/matrix.md`; and a bare number with no currency
#: indication. Each boundary is pinned by a test in `tests/unit/test_cost_withheld_gate.py`.
_CURRENCY_CODES: tuple[str, ...] = ("USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "INR")
_CURRENCY_UNIT_WORDS: tuple[str, ...] = (
    "cents?",
    "dollars?",
    "euros?",
    "pounds?",
    "pence",
)

#: Every Unicode `Sc` (Symbol, currency) code point, as regex-class ranges, including fullwidth and
#: non-Latin symbols (`＄` U+FF04, `₩` U+20A9, `₽` U+20BD).
_CURRENCY_SYMBOL_RANGES: str = (
    "\\$"
    "\u00a2-\u00a5"
    "\u058f"
    "\u060b"
    "\u07fe-\u07ff"
    "\u09f2-\u09f3\u09fb"
    "\u0af1"
    "\u0bf9"
    "\u0e3f"
    "\u17db"
    "\u20a0-\u20c0"
    "\ua838"
    "\ufdfc"
    "\ufe69"
    "\uff04"
    "\uffe0-\uffe1"
    "\uffe5-\uffe6"
)

#: Spelled-out cardinals sufficient to write any plain-English money amount. Matched only when a
#: currency unit word follows.
_NUMBER_WORDS: tuple[str, ...] = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
    "hundred",
    "thousand",
    "million",
    "billion",
)

_SYMBOL_CLASS = f"[{_CURRENCY_SYMBOL_RANGES}]"
_CODE_ALTERNATION = "|".join(_CURRENCY_CODES)
_UNIT_ALTERNATION = "\u00a2|" + "|".join(_CURRENCY_UNIT_WORDS)
_NUMBER_WORD_ALTERNATION = "|".join(_NUMBER_WORDS)

_CURRENCY_SHAPE_PATTERN = re.compile(
    "|".join(
        (
            # $0.47, £ 12, €1,234.00, ＄0.47, `$  0.47` -- a symbol preceding a numeral, any
            # intra-line whitespace, matching the spacing the ISO-code alternations below allow.
            rf"{_SYMBOL_CLASS}[ \t]*\d",
            # 0.47$, 43₽ -- a symbol following a numeral.
            rf"\d[ \t]*{_SYMBOL_CLASS}",
            # USD 0.47, USD $0.47 -- an ISO code preceding a numeral, any intra-line spacing.
            rf"\b(?:{_CODE_ALTERNATION})[ \t]*{_SYMBOL_CLASS}?[ \t]*\d",
            # 0.47 USD, 0.47    USD -- a numeral preceding an ISO code, any intra-line spacing.
            rf"\d[ \t]*(?:{_CODE_ALTERNATION})\b",
            # 47 cents, 0.47 dollars, 0.47 US dollars, 12¢ -- a numeral preceding a currency unit,
            # optionally through one short qualifier word.
            rf"\d[ \t]*(?:[A-Za-z]{{1,3}}[ \t]+)?(?:{_UNIT_ALTERNATION})\b",
            # forty-seven cents, one thousand dollars -- a spelled-out amount before a unit word.
            rf"\b(?:{_NUMBER_WORD_ALTERNATION})"
            rf"(?:[ \t-]+(?:and|{_NUMBER_WORD_ALTERNATION}))*"
            rf"[ \t-]+(?:[A-Za-z]{{1,3}}[ \t-]+)?(?:{_UNIT_ALTERNATION})\b",
        )
    ),
    re.IGNORECASE,
)


class CostWithholdingGateError(Exception):
    """Raised internally to short-circuit to a reported failure with a specific message."""


def load_price_table(pricing_path: Path) -> dict[str, Any]:
    """Parse ``config/pricing.yaml`` into a mapping."""
    data = yaml.safe_load(pricing_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CostWithholdingGateError(f"{pricing_path} did not parse to a mapping")
    return data


def is_provisional(price_table: dict[str, Any]) -> bool:
    """Return the price table's ``provisional`` flag."""
    return bool(price_table.get("provisional", False))


#: Key of a model's optional long-context rate block in the price table.
_LONG_CONTEXT_BLOCK: Final[str] = "long_context"

#: Non-rate fields of a long-context block: the prompt-token threshold above which it applies.
_LONG_CONTEXT_NON_RATE_FIELDS: Final[frozenset[str]] = frozenset({"above_prompt_tokens"})


def _numeric_rate(model_id: str, field_path: str, value: Any) -> float:
    """Return ``value`` as a float, or raise naming the offending price-table field."""
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise CostWithholdingGateError(
            f"price table entry {model_id!r}.{field_path!r} is not numeric: {value!r}"
        ) from exc


def iter_rates(price_table: dict[str, Any]) -> list[tuple[str, str, float]]:
    """Return ``(model_id, field_path, rate)`` for every rate in the ``models`` block.

    Covers each model's flat rates and the rates of its optional ``long_context`` block, whose
    ``above_prompt_tokens`` threshold is not a rate. The ``routes`` block holds multipliers, not
    rates, and is not read. Raises :class:`CostWithholdingGateError` on a non-mapping entry or
    block, or a non-numeric rate.
    """
    models = price_table.get("models", {})
    if not isinstance(models, dict):
        raise CostWithholdingGateError("price table 'models' block is not a mapping")
    rates: list[tuple[str, str, float]] = []
    for model_id, entry in models.items():
        if not isinstance(entry, dict):
            raise CostWithholdingGateError(f"price table entry for {model_id!r} is not a mapping")
        for field_name, value in entry.items():
            if field_name != _LONG_CONTEXT_BLOCK:
                rates.append((model_id, field_name, _numeric_rate(model_id, field_name, value)))
                continue
            if not isinstance(value, dict):
                raise CostWithholdingGateError(
                    f"price table entry {model_id!r}.{field_name!r} is not a mapping"
                )
            for tier_field, tier_value in value.items():
                if tier_field in _LONG_CONTEXT_NON_RATE_FIELDS:
                    continue
                field_path = f"{field_name}.{tier_field}"
                rates.append(
                    (model_id, field_path, _numeric_rate(model_id, field_path, tier_value))
                )
    return rates


def all_rates_are_zero(price_table: dict[str, Any]) -> bool:
    """Return whether every rate :func:`iter_rates` yields, flat and long-context, is zero."""
    return all(rate == 0.0 for _, _, rate in iter_rates(price_table))


#: Characters removed, and whitespace normalised, before a line is matched, so `$<ZWSP>0.47` reads
#: as `$0.47`.
_ZERO_WIDTH_CHARACTERS = "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad"
_UNICODE_SPACES = "\u00a0\u2007\u202f\u2009\u2002\u2003"
_TEXT_NORMALISATION = str.maketrans(
    {ord(c): None for c in _ZERO_WIDTH_CHARACTERS} | {ord(c): " " for c in _UNICODE_SPACES}
)


def _normalise_for_matching(line: str) -> str:
    """Decode HTML character references and remove invisible characters from one line."""
    return html.unescape(line).translate(_TEXT_NORMALISATION)


def find_currency_figures_in_text(text: str, *, source: str) -> list[str]:
    """Return one violation message per line of ``text`` naming an absolute currency figure."""
    violations = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _CURRENCY_LINE_PATTERN.search(_normalise_for_matching(line)):
            violations.append(
                f"{source}:{lineno}: absolute currency figure present: {line.strip()!r}"
            )
    return violations


def find_currency_shapes_in_text(text: str, *, source: str) -> list[str]:
    """Return one violation per line, or per line boundary, carrying a currency-shaped figure.

    Complements :func:`find_currency_figures_in_text`, which only recognises a figure a renderer
    emitted under a known field name. This scans for the shape itself — a symbol, ISO code, unit
    word, or spelled-out amount adjacent to a numeral — so prose stating a figure is caught. Applied
    to every scanned artifact including ``matrix.json``, whose string values are prose too.

    Each line is HTML-unescaped first, so ``&#36;0.47`` is read as ``$0.47``. Every adjacent line
    pair is then re-scanned joined by a single space, and a match reported only when it spans the
    boundary. The recognised families and the undetected ones are enumerated on
    ``_CURRENCY_SHAPE_PATTERN``.

    Args:
        text: The artifact's full text.
        source: Path shown in the violation message.

    Returns:
        One message per matching line or line boundary, naming the matched substring.
    """
    violations = []
    lines = [_normalise_for_matching(line) for line in text.splitlines()]
    for lineno, line in enumerate(lines, start=1):
        match = _CURRENCY_SHAPE_PATTERN.search(line)
        if match is not None:
            violations.append(
                f"{source}:{lineno}: currency-shaped figure present ({match.group(0)!r}): "
                f"{line.strip()!r}"
            )
    for lineno, (first, second) in enumerate(zip(lines, lines[1:], strict=False), start=1):
        boundary = len(first) + 1
        joined = f"{first} {second}"
        for match in _CURRENCY_SHAPE_PATTERN.finditer(joined):
            if match.start() < boundary <= match.end():
                violations.append(
                    f"{source}:{lineno}-{lineno + 1}: currency-shaped figure split across a line "
                    f"break ({match.group(0)!r}): {first.strip()!r} / {second.strip()!r}"
                )
                break
    return violations


def find_currency_figures_in_csv(
    text: str, *, source: str, invert_burden: bool = True
) -> list[str]:
    """Return one violation per CSV data row whose value under a currency-named column is set.

    Parses the CSV and checks header-to-value by column, since a header and its value sit on
    different lines.

    Two column classes are reported: a header in the closed :data:`_CURRENCY_FIELD_NAMES` set with
    any non-blank value, and any other column whose value parses as a number and fails
    :func:`_numeric_field_violation`.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return []
    currency_columns = {
        index: name for index, name in enumerate(header) if name in _CURRENCY_FIELD_NAMES
    }
    violations = []
    for row_number, row in enumerate(reader, start=2):
        for index, name in currency_columns.items():
            if index < len(row) and row[index].strip() != "":
                violations.append(
                    f"{source}:{row_number}: absolute currency figure present in column "
                    f"{name!r}: {row[index]!r}"
                )
        for index, name in enumerate(header):
            if index in currency_columns or index >= len(row):
                continue
            try:
                value = float(row[index].strip())
            except ValueError:
                continue
            problem = _numeric_field_violation(
                source=f"{source}:{row_number}",
                path=f"column {name!r}",
                key=name,
                value=value,
                invert_burden=invert_burden,
            )
            if problem is not None:
                violations.append(problem)
    return violations


def find_currency_figures_in_matrix_json(text: str, *, source: str) -> list[str]:
    """Parse ``matrix.json`` and return one violation per arm that renders a cost field."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CostWithholdingGateError(f"{source}: could not parse as JSON: {exc}") from exc
    violations = []
    arms = data.get("arms", []) if isinstance(data, dict) else []
    for index, arm in enumerate(arms):
        metrics_fields = arm.get("model_metrics", {}) if isinstance(arm, dict) else {}
        if not isinstance(metrics_fields, dict):
            continue
        model_id = metrics_fields.get("model_id", f"arm[{index}]")
        present = [name for name in _CURRENCY_FIELD_NAMES if name in metrics_fields]
        if present:
            violations.append(
                f"{source}: arm {model_id!r} renders currency field(s) {present} "
                "while the price table is provisional"
            )
    return violations


#: Cost-bearing *identifier* pattern, applied only to structured field names — JSON keys at any
#: depth and CSV column headers — and only when the value is numeric. A match is reported as a
#: recognised cost name.
_COST_BEARING_NAME_PATTERN = re.compile(
    r"cost|price|spend|charge|billed|billing|dollar|fee|outlay|expenditure|expense|amount"
    r"|(?<![a-z])usd(?![a-z])|(?<![a-z])eur(?![a-z])|(?<![a-z])gbp(?![a-z])",
    re.IGNORECASE,
)

#: Every numeric field an enumerable-schema artifact is permitted to carry. Any other numeric key is
#: a violation naming itself; a new non-currency metric is added here.
_NON_CURRENCY_NUMERIC_FIELDS: frozenset[str] = frozenset(
    {
        # Correctness and scoring.
        "correctness",
        "mean_correctness",
        "bad_write_rate",
        "tool_call_correct_rate",
        "positive_assertion_satisfaction_rate",
        "positive_assertions_satisfied",
        "positive_assertions_scored",
        "negative_guard_satisfaction_rate",
        "negative_guard_assertions_satisfied",
        "negative_guard_assertions_scored",
        "negative_guard_share_of_assertion_mass",
        "must_not_change_assertions_declared",
        "must_not_change_assertions_with_pre_turn_baseline",
        # Retrieval-narrowing disclosure: fractions of `mean_correctness`, and a count of tasks.
        "mean_correctness_deduction_upper_bound",
        "largest_single_arm_deduction_upper_bound",
        "largest_absolute_inter_arm_mean_correctness_difference",
        "narrowed_tasks_scored",
        # Counts and provenance.
        "included_invocation_count",
        "long_context_invocation_count",
        "arm_long_context_invocation_count",
        *(
            f"{prefix}_{tier}_long_context_invocation_count"
            for prefix in ("tier", "summary")
            for tier in ("supervisor", "subagent", "tool")
        ),
        "scored_task_count",
        "unscored_task_count",
        "total_excluded",
        "dataset_seed",
        # Token usage.
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_min_checkpoint_tokens",
        # Reliability counters.
        "cold_start",
        "retry",
        "throttle",
        # Latency: milliseconds and counts. `call_latency_ms_by_tier` maps each tier to a nested
        # {p50, p95, count} object, whose keys the JSON walk reaches.
        "latency_measured",
        "latency_percentile_method",
        "task_latency_ms_p50",
        "task_latency_ms_p95",
        "call_latency_ms_by_tier",
        "p50",
        "p95",
        "count",
        "call_latency_ms_p50",
        "call_latency_ms_p95",
        *(
            f"call_latency_ms_{tier}_{suffix}"
            for tier in ("supervisor", "subagent", "tool")
            for suffix in ("p50", "p95", "count")
        ),
        # Per-task spread: a repeat count and correctness fractions.
        "repeats",
        "correctness_min",
        "correctness_mean",
        "correctness_max",
        "success_fraction",
        # Per-tool aggregation: call counts, keyed in `outcome_distribution` by outcome kind.
        "truncated_count",
        "data",
        "no_match",
        "denied",
        "tool_failure",
        # Per-tier summary: latency in milliseconds, success counts and rate.
        "latency_p50_ms",
        "latency_p95_ms",
        "passed",
        "applicable",
        "rate",
        *(
            f"tier_{tier}_{suffix}"
            for tier in ("supervisor", "subagent", "tool")
            for suffix in (
                "input_tokens",
                "output_tokens",
                "latency_p50_ms",
                "latency_p95_ms",
                "success_passed",
                "success_applicable",
                "success_rate",
            )
        ),
        # Tier comparison summary.
        "accuracy",
        "completed_task_latency_ms_p50",
        "completed_task_latency_ms_p95",
        "latency_sample_count",
        "invocation_count",
        "task_count",
        "budget_exhausted_task_count",
        "truncated_task_count",
        "none",
        *(
            f"summary_{tier}_{suffix}"
            for tier in ("supervisor", "subagent", "tool")
            for suffix in (
                "completion_passed",
                "completion_applicable",
                "completion_rate",
                "accuracy",
                "completed_task_latency_ms_p50",
                "completed_task_latency_ms_p95",
                "latency_sample_count",
                "invocation_count",
                "input_tokens",
                "output_tokens",
            )
        ),
        "summary_supervisor_budget_exhausted_task_count",
        *(
            f"summary_subagent_delegation_{kind}"
            for kind in ("data", "no_match", "denied", "tool_failure", "none")
        ),
        "summary_tool_truncated_task_count",
        # Self-corrected retries.
        "retry_count",
        *(f"summary_{tier}_retry_count" for tier in ("supervisor", "subagent", "tool")),
        # Task completion over repeats: task counts, repeat count and rate.
        "completed",
        *(
            f"{prefix}_{tier}_completion_over_repeats_{suffix}"
            for prefix in ("tier", "summary")
            for tier in ("supervisor", "subagent", "tool")
            for suffix in ("completed", "applicable", "repeats", "rate")
        ),
    }
)


def _numeric_field_violation(
    *,
    source: str,
    path: str,
    key: str,
    value: Any,
    invert_burden: bool = True,
    ancestors: tuple[str, ...] = (),
) -> str | None:
    """Return why a numeric-valued field may not be published, or `None` when it is allowlisted.

    A recognised cost name on ``key`` or on any of its ``ancestors`` (the enclosing JSON keys,
    outermost first) is reported as one, so a :data:`_NON_CURRENCY_NUMERIC_FIELDS` name exempts a
    value only outside every cost-bearing container. With ``invert_burden``, any other key not in
    :data:`_NON_CURRENCY_NUMERIC_FIELDS` is reported as an unrecognised numeric field.
    """
    if _COST_BEARING_NAME_PATTERN.search(key) is not None:
        return (
            f"{source}: cost-bearing field {path} carries a numeric value "
            f"({value!r}) while the price table is provisional"
        )
    cost_bearing_ancestor = next(
        (name for name in ancestors if _COST_BEARING_NAME_PATTERN.search(name) is not None), None
    )
    if cost_bearing_ancestor is not None:
        return (
            f"{source}: numeric field {path} ({value!r}) sits under cost-bearing key "
            f"{cost_bearing_ancestor!r} while the price table is provisional"
        )
    if not invert_burden or key in _NON_CURRENCY_NUMERIC_FIELDS:
        return None
    return (
        f"{source}: numeric field {path} ({value!r}) is not a known non-currency field, so it "
        f"cannot be published while the price table is provisional -- add it to "
        f"_NON_CURRENCY_NUMERIC_FIELDS if it is not a currency amount"
    )


def _is_published_currency_value(value: Any) -> bool:
    """Whether `value` is a number a cost-bearing field would carry (bools are not)."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def find_currency_fields_in_json(
    text: str, *, source: str, invert_burden: bool = True
) -> list[str]:
    """Return one violation per cost-bearing field or currency-shaped string anywhere in the JSON.

    Every mapping key at any depth is checked against :data:`_CURRENCY_FIELD_NAMES`, every numeric
    value by :func:`_numeric_field_violation`, and every string key and value for a currency shape.

    Args:
        text: The artifact's full text.
        source: Path shown in the violation message.

    Returns:
        One message per finding, naming the JSON path it was found at.

    Raises:
        CostWithholdingGateError: If ``text`` is not parseable JSON.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CostWithholdingGateError(f"{source}: could not parse as JSON: {exc}") from exc

    violations: list[str] = []

    def walk(node: Any, path: str, ancestors: tuple[str, ...] = ()) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}"
                if key in _CURRENCY_FIELD_NAMES and value is not None:
                    violations.append(
                        f"{source}: absolute currency figure present at {child}: {value!r}"
                    )
                elif isinstance(key, str) and _is_published_currency_value(value):
                    problem = _numeric_field_violation(
                        source=source,
                        path=child,
                        key=key,
                        value=value,
                        invert_burden=invert_burden,
                        ancestors=ancestors,
                    )
                    if problem is not None:
                        violations.append(problem)
                if isinstance(key, str):
                    match = _CURRENCY_SHAPE_PATTERN.search(_normalise_for_matching(key))
                    if match is not None:
                        violations.append(
                            f"{source}: currency-shaped figure in key at {child} "
                            f"({match.group(0)!r})"
                        )
                walk(value, child, (*ancestors, key) if isinstance(key, str) else ancestors)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                child = f"{path}[{index}]"
                if ancestors and _is_published_currency_value(value):
                    # A number inside a list is judged by the key the list hangs off.
                    problem = _numeric_field_violation(
                        source=source,
                        path=child,
                        key=ancestors[-1],
                        value=value,
                        invert_burden=invert_burden,
                        ancestors=ancestors[:-1],
                    )
                    if problem is not None:
                        violations.append(problem)
                walk(value, child, ancestors)
        elif isinstance(node, str):
            match = _CURRENCY_SHAPE_PATTERN.search(_normalise_for_matching(node))
            if match is not None:
                violations.append(
                    f"{source}: currency-shaped figure in value at {path} ({match.group(0)!r})"
                )

    walk(data, "$")
    return violations


def find_forced_unwithheld_arms(text: str, *, source: str) -> list[str]:
    """Return one violation per arm whose ``cost_withheld_provisional`` is falsely ``False``.

    Applies only when the price table's rates are all zero (checked by the caller before this
    is invoked): under that condition every arm must report ``cost_withheld_provisional: true``.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CostWithholdingGateError(f"{source}: could not parse as JSON: {exc}") from exc
    violations = []
    arms = data.get("arms", []) if isinstance(data, dict) else []
    for index, arm in enumerate(arms):
        metrics_fields = arm.get("model_metrics", {}) if isinstance(arm, dict) else {}
        if not isinstance(metrics_fields, dict):
            continue
        model_id = metrics_fields.get("model_id", f"arm[{index}]")
        if metrics_fields.get("cost_withheld_provisional") is False:
            violations.append(
                f"{source}: arm {model_id!r} has cost_withheld_provisional=False while the "
                "price table is provisional and all rates are zero"
            )
    return violations


#: Artifacts whose field set this repository generates and therefore knows exhaustively. Only these
#: carry the inverted numeric-field rule (:data:`_NON_CURRENCY_NUMERIC_FIELDS`). Every other file
#: under ``results/`` gets the currency-shape scan and the known-cost-key scan; a cost figure under a
#: renamed key is caught in `matrix.{json,csv}` and `raw/**` only.
_ENUMERABLE_SCHEMA_RELPATHS: tuple[str, ...] = ("matrix.json", "matrix.csv")
_ENUMERABLE_SCHEMA_PARENT_DIRS: tuple[str, ...] = ("raw",)


def has_enumerable_schema(artifact: Path, results_dir: Path) -> bool:
    """Whether ``artifact`` is one this repo generates from a field set it knows exhaustively.

    Drives the inverted numeric-field rule; see :data:`_ENUMERABLE_SCHEMA_RELPATHS`.
    """
    try:
        relative = artifact.resolve().relative_to(results_dir.resolve())
    except ValueError:
        return False
    if relative.as_posix() in _ENUMERABLE_SCHEMA_RELPATHS:
        return True
    return len(relative.parts) > 1 and relative.parts[0] in _ENUMERABLE_SCHEMA_PARENT_DIRS


#: Suffixes excluded from the scan, mapped to the reason. Each entry is pinned by
#: `test_every_binary_exclusion_is_still_load_bearing`.
_BINARY_SUFFIX_EXCLUSIONS: dict[str, str] = {
    ".tfplan": (
        "Terraform's binary plan format; produced by `terraform plan -out` and never hand-authored, "
        "so it cannot carry a prose currency figure. Its readable rendering "
        "(`*.json` from `terraform show -json`) sits in the same directory, which is excluded "
        "wholesale by _EXCLUDED_RESULTS_DIRS -- see that entry for why."
    ),
}

#: Directories under `results/` excluded from the scan, mapped to the reason. Each entry is pinned by
#: `test_every_directory_exclusion_is_still_load_bearing`.
_EXCLUDED_RESULTS_DIRS: dict[str, str] = {
    "plan": (
        "Terraform plan artifacts written by `scripts/deploy.sh` (`results/plan/*.tfplan` and the "
        "`terraform show -json` renderings beside them). `terraform show -json` embeds each "
        "variable's `description` VERBATIM in the plan's `configuration` block, so the NAT gateway's "
        "documented hourly and per-GB cost in `infra/variables.tf` reappears here as a mechanical "
        "echo of tracked source prose. That is a category collision, not a withholding failure: this "
        "gate withholds absolute EVAL COST RESULTS from published artifacts while the price table is "
        "provisional, and a plan dump publishes nothing and reports no eval figure. The cost prose in "
        "those descriptions is deliberate -- `vpc_nat_gateway_enabled` has no default precisely so an "
        "operator reads the price before enabling it."
    ),
}


def _excluded_results_dir(path: Path, results_dir: Path) -> str | None:
    """The `results/`-relative directory excluding ``path``, or ``None`` if it is in scope.

    Matches on the first path component so a nested file (`plan/sub/x.json`) is excluded with its
    parent, and so a *file* named like an excluded directory is not.
    """
    try:
        relative = path.resolve().relative_to(results_dir.resolve())
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) > 1 and parts[0] in _EXCLUDED_RESULTS_DIRS:
        return parts[0]
    return None


def iter_published_artifacts(results_dir: Path, *, readme_path: Path | None = None) -> list[Path]:
    """Return every published artifact path this gate scans that exists on disk.

    Globs ``results/**``, minus :data:`_BINARY_SUFFIX_EXCLUSIONS` and
    :data:`_EXCLUDED_RESULTS_DIRS`, and appends ``readme_path`` when given.
    """
    candidates: list[Path] = []
    if results_dir.is_dir():
        candidates.extend(sorted(p for p in results_dir.rglob("*") if p.is_file()))
    if readme_path is not None:
        candidates.append(readme_path)
    return [
        path
        for path in candidates
        if path.is_file()
        and path.suffix not in _BINARY_SUFFIX_EXCLUSIONS
        and _excluded_results_dir(path, results_dir) is None
    ]


#: Source suffixes of the rendered eval surface.
_FRONTEND_SUFFIXES: frozenset[str] = frozenset({".tsx", ".ts"})

#: A *read* of a currency field's value: member access (`metrics.cost_total`) or string-key index
#: (`metrics["cost_total"]`), not a bare name, so a type declaration such as
#: `readonly cost_total?: number` is not flagged.
_FRONTEND_CURRENCY_READ = re.compile(
    r"(?:\.|\[\s*['\"])(?:" + "|".join(re.escape(n) for n in _CURRENCY_FIELD_NAMES) + r")\b"
)


def find_currency_rendering_in_frontend(frontend_dir: Path) -> list[str]:
    """Return one violation per line of the eval dashboard that reads or states a currency figure.

    Two checks:

    1. A **read** of a known currency field (:data:`_FRONTEND_CURRENCY_READ`).
    2. A **currency-shaped literal** in the source, catching a hardcoded ``$0.47`` in JSX.

    ``cost_withheld_provisional`` is not a currency field. A figure rendered from a renamed field,
    or computed client-side from token counts, is not detected.

    Args:
        frontend_dir: The eval page directory to scan, e.g. ``frontend/src/pages/evals``.

    Returns:
        One message per finding. Files with ``.test.`` in the name are skipped.
    """
    if not frontend_dir.is_dir():
        return []
    violations: list[str] = []
    sources = sorted(
        path
        for path in frontend_dir.rglob("*")
        if path.is_file() and path.suffix in _FRONTEND_SUFFIXES and ".test." not in path.name
    )
    for path in sources:
        text = path.read_text(encoding="utf-8")
        source = str(path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            normalised = _normalise_for_matching(line)
            read = _FRONTEND_CURRENCY_READ.search(normalised)
            if read is not None:
                violations.append(
                    f"{source}:{lineno}: reads currency field {read.group(0)!r} on the rendered "
                    f"page while the price table is provisional: {line.strip()!r}"
                )
            shape = _CURRENCY_SHAPE_PATTERN.search(normalised)
            if shape is not None:
                violations.append(
                    f"{source}:{lineno}: currency-shaped literal in rendered source "
                    f"({shape.group(0)!r}): {line.strip()!r}"
                )
    return violations


EXIT_VERIFIED_NOTHING: Final[int] = 3


@dataclass(frozen=True)
class GateResult:
    """Violations **and** what was examined to find them.

    `verified_nothing` is true only when the price table is provisional and nothing was examined.
    """

    violations: tuple[str, ...]
    artifacts_scanned: int
    consumers_scanned: tuple[str, ...]
    provisional: bool

    @property
    def verified_nothing(self) -> bool:
        """True when the prohibition applies and nothing was examined against it."""
        return self.provisional and not self.artifacts_scanned and not self.consumers_scanned


def run_gate_with_coverage(
    *,
    pricing_path: Path,
    results_dir: Path,
    readme_path: Path | None = None,
    frontend_dir: Path | None = None,
) -> GateResult:
    """Run every check and report the violations alongside the scan that produced them."""
    price_table = load_price_table(pricing_path)

    if not is_provisional(price_table):
        return GateResult(
            violations=(), artifacts_scanned=0, consumers_scanned=(), provisional=False
        )

    violations: list[str] = []
    consumers: list[str] = []
    zero_rates = all_rates_are_zero(price_table)

    artifacts = iter_published_artifacts(results_dir, readme_path=readme_path)
    if artifacts:
        consumers.append("published_artifacts")
    for artifact in artifacts:
        try:
            text = artifact.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            violations.append(
                f"{artifact}: not valid UTF-8, so it cannot be scanned for a currency figure. "
                "Exclude it deliberately or emit a text artifact alongside it."
            )
            continue
        source = str(artifact)
        invert_burden = has_enumerable_schema(artifact, results_dir)
        # The shape scan applies to every artifact; the field-name check then runs in the form
        # matching the artifact's structure.
        violations.extend(find_currency_shapes_in_text(text, source=source))
        if artifact.suffix == ".json":
            # `matrix.json` must parse; any other JSON that does not is scanned as text.
            if artifact.name == "matrix.json":
                violations.extend(
                    find_currency_fields_in_json(text, source=source, invert_burden=invert_burden)
                )
            else:
                try:
                    violations.extend(
                        find_currency_fields_in_json(
                            text, source=source, invert_burden=invert_burden
                        )
                    )
                except CostWithholdingGateError:
                    violations.extend(find_currency_figures_in_text(text, source=source))
        elif artifact.suffix == ".csv":
            violations.extend(
                find_currency_figures_in_csv(text, source=source, invert_burden=invert_burden)
            )
        else:
            violations.extend(find_currency_figures_in_text(text, source=source))
        if artifact.name == "matrix.json":
            violations.extend(find_currency_figures_in_matrix_json(text, source=source))
            if zero_rates:
                violations.extend(find_forced_unwithheld_arms(text, source=source))

    if frontend_dir is not None:
        consumers.append("frontend")
        violations.extend(find_currency_rendering_in_frontend(frontend_dir))

    return GateResult(
        violations=tuple(violations),
        artifacts_scanned=len(artifacts),
        consumers_scanned=tuple(consumers),
        provisional=True,
    )


def run_gate(
    *,
    pricing_path: Path,
    results_dir: Path,
    readme_path: Path | None = None,
    frontend_dir: Path | None = None,
) -> list[str]:
    """The violation list alone, for callers that do not need the coverage half.

    A narrowing accessor over `run_gate_with_coverage`.
    """
    return list(
        run_gate_with_coverage(
            pricing_path=pricing_path,
            results_dir=results_dir,
            readme_path=readme_path,
            frontend_dir=frontend_dir,
        ).violations
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse arguments, run the gate, print violations, and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pricing-file", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument(
        "--readme-file",
        type=Path,
        default=None,
        help="Path to README.md to scan (W6b-3); omit to skip if not applicable.",
    )
    parser.add_argument(
        "--frontend-dir",
        type=Path,
        default=None,
        help=(
            "Path to the eval dashboard source (frontend/src/pages/evals) to scan for a rendered "
            "currency figure; omit to skip the second consumer."
        ),
    )
    args = parser.parse_args(argv)

    try:
        result = run_gate_with_coverage(
            pricing_path=args.pricing_file,
            results_dir=args.results_dir,
            readme_path=args.readme_file,
            frontend_dir=args.frontend_dir,
        )
    except CostWithholdingGateError as exc:
        print(f"check_cost_withheld: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"check_cost_withheld: {exc}", file=sys.stderr)
        return 2

    if result.violations:
        print("check_cost_withheld: FAIL", file=sys.stderr)
        for violation in result.violations:
            print(f"  {violation}", file=sys.stderr)
        return 1

    if result.verified_nothing:
        print(
            "check_cost_withheld: SKIP -- nothing was examined, so this run is not evidence that no "
            f"absolute currency figure is published. results-dir {args.results_dir} holds no "
            "scannable artifact and no other consumer was supplied. `results/` is gitignored, so "
            "this is the fresh-clone and CI shape; run a sweep first, or pass --readme-file / "
            "--frontend-dir. Exit 3 rather than 0 because the prohibition config/pricing.yaml "
            "declares is ACTIVE and this run asserted nothing against it.",
            file=sys.stderr,
        )
        return EXIT_VERIFIED_NOTHING

    if not result.provisional:
        print(
            "check_cost_withheld: OK (config/pricing.yaml is not provisional, so the withholding "
            "prohibition does not apply -- no control went unasserted)"
        )
        return 0

    print(
        "check_cost_withheld: OK (no absolute currency figures published while provisional; "
        f"{result.artifacts_scanned} artifact(s) scanned, "
        f"consumers={list(result.consumers_scanned)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

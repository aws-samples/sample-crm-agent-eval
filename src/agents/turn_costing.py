# SPDX-License-Identifier: MIT
"""Price one chat turn's model usage for the SSE stream.

:func:`cost_lines` groups a turn's :class:`~agents.turn_driver.TurnRecords` invocations by
``(tier, role, model_id)`` and prices each invocation with :func:`crm.metering.cost_for_usage`, the
same function the eval reports use, at the inference profile the capability matrix configures for
its model. Each line counts the invocations priced at long-context rates. When ``withheld`` is true
(see :func:`is_cost_withheld`) every ``cost_usd`` is ``None``. :func:`turn_cost` totals the lines.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

import yaml

from agents.events import CostLine
from agents.tier_attribution import InvocationAttribution
from crm.metering import Cost, TokenUsage, cost_for_usage
from providers.capabilities import CapabilityMatrix, load_capability_matrix

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: The shipped price table.
DEFAULT_PRICING_PATH: Final[Path] = _REPO_ROOT / "config" / "pricing.yaml"


def is_cost_withheld(*, pricing_path: Path = DEFAULT_PRICING_PATH) -> bool:
    """Return whether ``pricing_path`` is ``provisional``, so currency figures are withheld.

    Raises:
        ValueError: The table does not parse to a mapping or its ``provisional`` is not a boolean.
    """
    pricing = yaml.safe_load(pricing_path.read_text(encoding="utf-8"))
    if not isinstance(pricing, dict) or not isinstance(pricing.get("provisional"), bool):
        raise ValueError(f"{pricing_path} must be a mapping with a boolean 'provisional'")
    return bool(pricing["provisional"])


_shipped_matrix: CapabilityMatrix | None = None


def _shipped_capability_matrix() -> CapabilityMatrix:
    """Return the shipped :class:`CapabilityMatrix`, loading it once per process."""
    global _shipped_matrix
    if _shipped_matrix is None:
        _shipped_matrix = load_capability_matrix()
    return _shipped_matrix


def _inference_profile_for(model_id: str, matrix: CapabilityMatrix) -> str:
    model = matrix.models.get(model_id)
    if model is None:
        raise ValueError(f"{model_id!r} has no entry in the capability matrix")
    return model.inference_profile


def cost_lines(
    invocations: Iterable[InvocationAttribution],
    *,
    withheld: bool,
    pricing_path: Path = DEFAULT_PRICING_PATH,
    matrix: CapabilityMatrix | None = None,
) -> tuple[CostLine, ...]:
    """Group token-bearing invocations by ``(tier, role, model_id)`` and price each group.

    Every invocation is priced at its model's configured inference profile, so its pricing tier is
    known even when withheld; only the figures are dropped.

    Args:
        invocations: Every model invocation of the turn, in call order.
        withheld: Whether pricing is provisional; when ``True`` every line's ``cost_usd`` is
            ``None``.
        pricing_path: The price table. Overridable for tests.
        matrix: The capability matrix supplying each model's inference profile. Defaults to the
            shipped ``config/`` matrix, loaded once.

    Returns:
        One line per group, in first-seen order. Zero-token records are skipped.

    Raises:
        KeyError: A model has no entry in the price table.
        ValueError: A model has no entry in the capability matrix.
        crm.inference_route.UnknownInferenceRoute: A configured profile has no known route or names
            another model.
        crm.metering.UnconfirmedTokenAccountingError: The price table is not provisional but a
            model's family has unconfirmed cache accounting.
    """
    groups: dict[tuple[str, str, str], list[InvocationAttribution]] = {}
    for invocation in invocations:
        if invocation.bears_tokens:
            key = (invocation.tier, invocation.role, invocation.model_id)
            groups.setdefault(key, []).append(invocation)
    if not groups:
        return ()
    resolved_matrix = matrix if matrix is not None else _shipped_capability_matrix()
    return tuple(
        _line(members, withheld=withheld, pricing_path=pricing_path, matrix=resolved_matrix)
        for members in groups.values()
    )


def _price(member: InvocationAttribution, *, pricing_path: Path, matrix: CapabilityMatrix) -> Cost:
    return cost_for_usage(
        TokenUsage(
            input_tokens=member.tokens.input_tokens,
            output_tokens=member.tokens.output_tokens,
            cache_write_tokens=member.cache.write_tokens,
            cache_read_tokens=member.cache.read_tokens,
        ),
        member.model_id,
        inference_profile=_inference_profile_for(member.model_id, matrix),
        pricing_path=pricing_path,
    )


def _line(
    members: Sequence[InvocationAttribution],
    *,
    withheld: bool,
    pricing_path: Path,
    matrix: CapabilityMatrix,
) -> CostLine:
    first = members[0]
    costs = [_price(member, pricing_path=pricing_path, matrix=matrix) for member in members]
    return {
        "tier": first.tier,
        "role": first.role,
        "model_id": first.model_id,
        "input_tokens": sum(member.tokens.input_tokens for member in members),
        "output_tokens": sum(member.tokens.output_tokens for member in members),
        "cache_read_tokens": sum(member.cache.read_tokens for member in members),
        "cache_write_tokens": sum(member.cache.write_tokens for member in members),
        "long_context_calls": sum(1 for cost in costs if cost.pricing_tier == "long_context"),
        "cost_usd": None if withheld else sum(cost.total for cost in costs),
    }


def turn_cost(lines: Iterable[CostLine], *, withheld: bool) -> float | None:
    """Return the turn's total cost, or ``None`` when ``withheld``.

    Raises:
        ValueError: A line is unpriced while ``withheld`` is ``False``, or a total is not finite.
    """
    if withheld:
        return None
    total = 0.0
    for line in lines:
        cost = line["cost_usd"]
        if cost is None:
            raise ValueError(
                f"cost line for {line['model_id']} is unpriced but cost is not withheld"
            )
        total += cost
    if not math.isfinite(total):
        raise ValueError(f"turn cost {total!r} is not finite")
    return total


__all__ = [
    "DEFAULT_PRICING_PATH",
    "cost_lines",
    "is_cost_withheld",
    "turn_cost",
]

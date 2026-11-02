# SPDX-License-Identifier: MIT
"""Price one chat turn's model usage for the SSE stream.

:func:`cost_lines` groups a turn's :class:`~agents.turn_driver.TurnRecords` invocations by
``(tier, role, model_id)`` and prices each invocation with :func:`crm.metering.cost_for_usage`, the
same function the eval reports use. When ``withheld`` is true (see :func:`is_cost_withheld`) every
``cost_usd`` is ``None``. :func:`turn_cost` totals the lines.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

import yaml

from agents.events import CostLine
from agents.tier_attribution import InvocationAttribution
from crm.metering import TokenUsage, cost_for_usage

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


def cost_lines(
    invocations: Iterable[InvocationAttribution],
    *,
    withheld: bool,
    pricing_path: Path = DEFAULT_PRICING_PATH,
) -> tuple[CostLine, ...]:
    """Group token-bearing invocations by ``(tier, role, model_id)`` and price each group.

    Args:
        invocations: Every model invocation of the turn, in call order.
        withheld: Whether pricing is provisional; when ``True`` no invocation is priced and every
            line's ``cost_usd`` is ``None``.
        pricing_path: The price table. Overridable for tests.

    Returns:
        One line per group, in first-seen order. Zero-token records are skipped.

    Raises:
        KeyError: A model has no entry in the price table.
        crm.metering.UnconfirmedTokenAccountingError: Pricing is not withheld but a model's family
            has unconfirmed cache accounting.
    """
    groups: dict[tuple[str, str, str], list[InvocationAttribution]] = {}
    for invocation in invocations:
        if invocation.bears_tokens:
            key = (invocation.tier, invocation.role, invocation.model_id)
            groups.setdefault(key, []).append(invocation)
    return tuple(
        _line(members, withheld=withheld, pricing_path=pricing_path) for members in groups.values()
    )


def _line(
    members: Sequence[InvocationAttribution], *, withheld: bool, pricing_path: Path
) -> CostLine:
    first = members[0]
    cost: float | None = None
    if not withheld:
        cost = sum(
            cost_for_usage(
                TokenUsage(
                    input_tokens=member.tokens.input_tokens,
                    output_tokens=member.tokens.output_tokens,
                    cache_write_tokens=member.cache.write_tokens,
                    cache_read_tokens=member.cache.read_tokens,
                ),
                member.model_id,
                pricing_path=pricing_path,
            ).total
            for member in members
        )
    return {
        "tier": first.tier,
        "role": first.role,
        "model_id": first.model_id,
        "input_tokens": sum(member.tokens.input_tokens for member in members),
        "output_tokens": sum(member.tokens.output_tokens for member in members),
        "cache_read_tokens": sum(member.cache.read_tokens for member in members),
        "cache_write_tokens": sum(member.cache.write_tokens for member in members),
        "cost_usd": cost,
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


__all__ = ["DEFAULT_PRICING_PATH", "cost_lines", "is_cost_withheld", "turn_cost"]

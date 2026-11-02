# SPDX-License-Identifier: MIT
"""Exclusions from eval latency/cost statistics are counted and categorised, never dropped.

The taxonomy is closed, every category appears in a tally even at zero, and each excluded invocation
stays traceable to its category and identifier.
"""

from __future__ import annotations

import pytest

from eval.metrics import (
    Exclusion,
    ExclusionCategory,
    ExclusionTally,
    tally_exclusions,
)


def test_exclusion_taxonomy_is_exactly_cold_start_retry_throttle() -> None:
    """The taxonomy is exactly cold_start, retry, and throttle."""
    assert {category.value for category in ExclusionCategory} == {
        "cold_start",
        "retry",
        "throttle",
    }


def test_exclusion_requires_a_nonempty_invocation_id() -> None:
    """An exclusion that cannot be traced to a specific invocation is not a valid measurement."""
    with pytest.raises(ValueError, match="invocation_id"):
        Exclusion(invocation_id="", category=ExclusionCategory.COLD_START)


def test_tally_reports_every_category_even_at_zero() -> None:
    """An empty exclusion list still reports the full taxonomy, each at zero."""
    tally = tally_exclusions([])
    assert tally.total == 0
    assert tally.by_category == {
        ExclusionCategory.COLD_START: 0,
        ExclusionCategory.RETRY: 0,
        ExclusionCategory.THROTTLE: 0,
    }


def test_tally_counts_each_category_independently() -> None:
    """Two cold starts, one retry, and zero throttles tally to exactly that, per category."""
    exclusions = [
        Exclusion(invocation_id="inv-1", category=ExclusionCategory.COLD_START),
        Exclusion(invocation_id="inv-2", category=ExclusionCategory.COLD_START),
        Exclusion(invocation_id="inv-3", category=ExclusionCategory.RETRY, detail="attempt 2"),
    ]
    tally = tally_exclusions(exclusions)
    assert tally.by_category[ExclusionCategory.COLD_START] == 2
    assert tally.by_category[ExclusionCategory.RETRY] == 1
    assert tally.by_category[ExclusionCategory.THROTTLE] == 0
    assert tally.total == 3


def test_tally_preserves_every_individual_exclusion() -> None:
    """The tally keeps every individual exclusion alongside the counts."""
    exclusion = Exclusion(
        invocation_id="inv-7", category=ExclusionCategory.THROTTLE, detail="ThrottlingException"
    )
    tally = tally_exclusions([exclusion])
    assert tally.exclusions == (exclusion,)
    assert tally.exclusions[0].detail == "ThrottlingException"


def test_report_fields_name_every_category_by_string_value() -> None:
    """The rendered report uses the taxonomy's string values, not the Python enum member names."""
    tally = tally_exclusions([Exclusion(invocation_id="inv-1", category=ExclusionCategory.RETRY)])
    fields = tally.to_report_fields()
    assert fields["total_excluded"] == 1
    assert fields["by_category"] == {"cold_start": 0, "retry": 1, "throttle": 0}


def test_tally_is_immutable() -> None:
    """An ExclusionTally is a frozen dataclass."""
    tally = ExclusionTally(by_category=dict.fromkeys(ExclusionCategory, 0))
    with pytest.raises(AttributeError):
        tally.by_category = {}  # type: ignore[misc]

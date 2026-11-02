# SPDX-License-Identifier: MIT
"""`default_family` scopes in-product defaults only; eval sweep arms never depend on it.

Each test forces the loaded matrix's ``default_family`` to every configured family and asserts the
sweep arms resolve identically while ``defaults()`` follows the forced family. No model or AWS call.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

import pytest

from eval.harness import DEFAULT_ARM_SPECS
from eval.sweep_units import ArmSpec, arm_turn_tuning
from providers.base import ResolvedInvocation
from providers.capabilities import CapabilityMatrix, Family, Tier, load_capability_matrix
from providers.tier_tuning import TIERS, defaults, parse_wire_tuning, resolve_turn, to_wire

MATRIX = load_capability_matrix()
FAMILIES: tuple[Family, ...] = tuple(sorted(MATRIX.tiers))


def _forced(family: Family) -> CapabilityMatrix:
    return dataclasses.replace(MATRIX, default_family=family)


def _resolved_arm(matrix: CapabilityMatrix, arm: ArmSpec) -> Mapping[Tier, ResolvedInvocation]:
    """Resolve an arm's tuning as the Runtime does: through the wire block, then the matrix."""
    wire = parse_wire_tuning(to_wire(arm_turn_tuning(arm)), default_source="test")
    return resolve_turn(matrix, wire)


def test_the_matrix_offers_more_than_one_family_to_force() -> None:
    assert len(FAMILIES) > 1


def test_the_sweep_arms_are_anthropic_then_openai() -> None:
    assert [arm.family for arm in DEFAULT_ARM_SPECS] == ["anthropic", "openai"]


@pytest.mark.parametrize("arm", DEFAULT_ARM_SPECS, ids=lambda arm: arm.family)
def test_an_arms_resolved_tiers_are_identical_under_every_default_family(arm: ArmSpec) -> None:
    """Every tier of an arm runs the arm's family, whatever the default family is."""
    resolutions = [_resolved_arm(_forced(family), arm) for family in FAMILIES]
    for resolved in resolutions:
        assert {
            tier: invocation.model.family for tier, invocation in resolved.items()
        } == dict.fromkeys(TIERS, arm.family)
    assert all(resolved == resolutions[0] for resolved in resolutions)


@pytest.mark.parametrize("arm", DEFAULT_ARM_SPECS, ids=lambda arm: arm.family)
def test_an_arms_turn_tuning_names_its_family_at_every_tier(arm: ArmSpec) -> None:
    tuning = arm_turn_tuning(arm)
    assert {tier: request.family for tier, request in tuning.tiers.items()} == dict.fromkeys(
        TIERS, arm.family
    )


def test_an_arm_tuning_without_a_family_would_follow_the_default() -> None:
    """Control: a tuning that omits the family resolves differently per default family."""
    arm = DEFAULT_ARM_SPECS[0]
    block = to_wire(arm_turn_tuning(arm))
    tiers = block["tiers"]
    assert isinstance(tiers, dict)
    for request in tiers.values():
        request.pop("family", None)
    families_seen = {
        resolve_turn(_forced(family), parse_wire_tuning(block, default_source="test"))[
            "supervisor"
        ].model.family
        for family in FAMILIES
    }
    assert families_seen == set(FAMILIES)


@pytest.mark.parametrize("family", FAMILIES)
def test_defaults_follow_the_forced_default_family(family: Family) -> None:
    forced = _forced(family)
    tuning = defaults(forced)
    for tier in TIERS:
        request = tuning.tiers[tier]
        assignment = forced.tiers[family][tier]
        assert request.family == family
        assert request.model == assignment.model_id
        assert request.effort == assignment.tuning.get("effort")

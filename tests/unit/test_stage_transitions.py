# SPDX-License-Identifier: MIT
"""Tests for the opportunity stage machine."""

from __future__ import annotations

import itertools

import pytest

from crm.domain.stages import (
    CLOSED_STAGES,
    OPEN_STAGES,
    PERMITTED_TRANSITION_KINDS,
    REFUSED_TRANSITION_KINDS,
    STAGES,
    TransitionKind,
    classify_transition,
    is_permitted_transition,
)


class TestStageOrderAndOpenSet:
    def test_stage_order_matches_fr_2_2(self) -> None:
        assert STAGES == (
            "Discovery",
            "Qualification",
            "Security Review",
            "Negotiation",
            "Closed Won",
            "Closed Lost",
        )

    def test_open_stages_are_exactly_the_first_four(self) -> None:
        assert {"Discovery", "Qualification", "Security Review", "Negotiation"} == OPEN_STAGES

    def test_closed_stages_are_exactly_the_last_two(self) -> None:
        assert {"Closed Won", "Closed Lost"} == CLOSED_STAGES

    def test_open_and_closed_partition_all_stages_with_no_overlap(self) -> None:
        assert set(STAGES) == OPEN_STAGES | CLOSED_STAGES
        assert set() == OPEN_STAGES & CLOSED_STAGES


class TestClassifyTransition:
    def test_same_stage(self) -> None:
        for stage in STAGES:
            assert classify_transition(stage, stage) is TransitionKind.SAME_STAGE

    def test_advance_between_open_stages(self) -> None:
        assert classify_transition("Discovery", "Qualification") is TransitionKind.ADVANCE
        assert classify_transition("Discovery", "Negotiation") is TransitionKind.ADVANCE

    def test_regression_between_open_stages_is_classified_not_silent(self) -> None:
        """A move back between open stages is classified as `REGRESSION`."""
        assert classify_transition("Negotiation", "Discovery") is TransitionKind.REGRESSION
        assert classify_transition("Qualification", "Discovery") is TransitionKind.REGRESSION

    def test_close_from_open_to_closed(self) -> None:
        assert classify_transition("Negotiation", "Closed Won") is TransitionKind.CLOSE
        assert classify_transition("Discovery", "Closed Lost") is TransitionKind.CLOSE

    def test_reopen_from_closed_to_open_is_classified_not_silent(self) -> None:
        """A move from a closed stage to an open one is classified as `REOPEN`."""
        assert classify_transition("Closed Won", "Discovery") is TransitionKind.REOPEN
        assert classify_transition("Closed Lost", "Negotiation") is TransitionKind.REOPEN

    def test_closed_lateral_between_the_two_closed_stages(self) -> None:
        assert classify_transition("Closed Won", "Closed Lost") is TransitionKind.CLOSED_LATERAL
        assert classify_transition("Closed Lost", "Closed Won") is TransitionKind.CLOSED_LATERAL

    def test_every_ordered_pair_of_stages_is_classified(self) -> None:
        """No pair of stages is left unclassified — the classifier is total over STAGES x STAGES."""
        for from_stage, to_stage in itertools.product(STAGES, STAGES):
            kind = classify_transition(from_stage, to_stage)
            assert isinstance(kind, TransitionKind)

    def test_rejects_unrecognised_from_stage(self) -> None:
        with pytest.raises(ValueError, match="unrecognised stage"):
            classify_transition("Not A Stage", "Discovery")

    def test_rejects_unrecognised_to_stage(self) -> None:
        with pytest.raises(ValueError, match="unrecognised stage"):
            classify_transition("Discovery", "Not A Stage")


class TestTransitionPolicyPartition:
    """Every TransitionKind is in exactly one of PERMITTED/REFUSED."""

    def test_permitted_and_refused_partition_every_kind(self) -> None:
        all_kinds = set(TransitionKind)
        assert all_kinds == PERMITTED_TRANSITION_KINDS | REFUSED_TRANSITION_KINDS
        assert set() == PERMITTED_TRANSITION_KINDS & REFUSED_TRANSITION_KINDS

    def test_regression_is_permitted(self) -> None:
        assert TransitionKind.REGRESSION in PERMITTED_TRANSITION_KINDS

    def test_reopen_is_refused(self) -> None:
        assert TransitionKind.REOPEN in REFUSED_TRANSITION_KINDS

    def test_closed_lateral_is_refused(self) -> None:
        assert TransitionKind.CLOSED_LATERAL in REFUSED_TRANSITION_KINDS

    def test_advance_close_and_same_stage_are_permitted(self) -> None:
        assert TransitionKind.ADVANCE in PERMITTED_TRANSITION_KINDS
        assert TransitionKind.CLOSE in PERMITTED_TRANSITION_KINDS
        assert TransitionKind.SAME_STAGE in PERMITTED_TRANSITION_KINDS


class TestIsPermittedTransition:
    def test_regression_returns_true(self) -> None:
        assert is_permitted_transition("Negotiation", "Discovery") is True

    def test_reopen_returns_false(self) -> None:
        assert is_permitted_transition("Closed Won", "Discovery") is False

    def test_closed_lateral_returns_false(self) -> None:
        assert is_permitted_transition("Closed Won", "Closed Lost") is False

    def test_advance_returns_true(self) -> None:
        assert is_permitted_transition("Discovery", "Negotiation") is True

    def test_agrees_with_classify_transition_for_every_pair(self) -> None:
        for from_stage, to_stage in itertools.product(STAGES, STAGES):
            expected = classify_transition(from_stage, to_stage) in PERMITTED_TRANSITION_KINDS
            assert is_permitted_transition(from_stage, to_stage) is expected

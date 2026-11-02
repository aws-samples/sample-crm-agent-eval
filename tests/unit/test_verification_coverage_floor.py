# SPDX-License-Identifier: MIT
"""Tests for the check-count floor: a verification module that emits too few checks fails.

Covered failure modes: (1) a shortfall against a derived minimum, (2) a deriver that raises,
(3) the harness's single synthesised failure, (4) an expectation derived from the checked inputs.
"""

from __future__ import annotations

import pytest
from scripts.smoke.access_log_delivery import LOG_DESTINATIONS, expected_check_count
from scripts.smoke.harness import SmokeContext, _resolve_expectation, context_for_test

from eval.verification import (
    COVERAGE_CHECK,
    CheckResult,
    ExpectedCheckCount,
    ResourceClassResult,
    UnderivableCheckCount,
    coverage_check,
)


def _pass(name: str) -> CheckResult:
    return CheckResult(resource="r", check=name, status="pass")


class TestExpectedCheckCountRefusesAUselessExpectation:
    def test_a_minimum_below_one_is_refused(self) -> None:
        """A floor of zero is indistinguishable from having no floor."""
        with pytest.raises(ValueError, match="at least 1"):
            ExpectedCheckCount(minimum=0, basis="whatever")

    def test_a_basis_that_says_nothing_is_refused(self) -> None:
        """A blank basis is refused."""
        with pytest.raises(ValueError, match="basis"):
            ExpectedCheckCount(minimum=3, basis="   ")


class TestTheFloorFails:
    def test_zero_checks_is_a_failure_naming_that_it_verified_nothing(self) -> None:
        result = coverage_check("rc", [], ExpectedCheckCount(minimum=4, basis="four things"))
        assert result.status == "fail"
        assert result.check == COVERAGE_CHECK
        assert "verified nothing" in result.detail
        assert "four things" in result.detail

    def test_a_shortfall_against_the_derived_minimum_is_a_failure(self) -> None:
        """Failure mode 1: fewer checks than the derived minimum fails and names both counts."""
        result = coverage_check(
            "rc",
            [_pass("a"), _pass("b")],
            ExpectedCheckCount(minimum=18, basis="2 rows x 9 tables"),
        )
        assert result.status == "fail"
        assert "emitted 2 check(s)" in result.detail
        assert "at least 18" in result.detail
        assert "2 rows x 9 tables" in result.detail

    def test_the_synthesised_failure_alone_does_not_satisfy_the_floor(self) -> None:
        """Failure mode 3: the single synthesised `fail` does not meet a derived minimum."""
        synthesised = [CheckResult(resource="rc", check="checks", status="fail", detail="boom")]
        assert (
            coverage_check("rc", synthesised, ExpectedCheckCount(minimum=18, basis="b")).status
            == "fail"
        )
        # The bare fallback floor (minimum 1) passes the same input.
        assert coverage_check("rc", synthesised, "no deriver").status == "pass"

    def test_meeting_the_minimum_passes_and_states_the_basis(self) -> None:
        result = coverage_check(
            "rc",
            [_pass(str(i)) for i in range(18)],
            ExpectedCheckCount(minimum=18, basis="derived"),
        )
        assert result.status == "pass"
        assert "18 check(s) emitted" in result.detail
        assert "derived" in result.detail


class TestTheFloorFailsClosedWhenItCannotBeDerived:
    def test_an_absent_deriver_yields_a_stated_reason_not_a_missing_assertion(self) -> None:
        resolved = _resolve_expectation(context_for_test(), None)
        assert isinstance(resolved, str)
        assert "declares no expected_check_count" in resolved
        assert coverage_check("rc", [], resolved).status == "fail"

    def test_a_raising_deriver_is_reported_rather_than_dropped(self) -> None:
        """Failure mode 2: a raising deriver resolves to `UnderivableCheckCount` and fails."""

        def _boom(_ctx: SmokeContext) -> ExpectedCheckCount:
            raise RuntimeError("outputs unreadable")

        resolved = _resolve_expectation(context_for_test(), _boom)
        assert isinstance(resolved, UnderivableCheckCount)
        assert "RuntimeError" in resolved.reason
        assert "outputs unreadable" in resolved.reason
        emitted = coverage_check("rc", [], resolved)
        assert emitted.status == "fail"
        assert "outputs unreadable" in emitted.detail


class TestARaiseIsAFailureAndNotAFallback:
    """An `UnderivableCheckCount` fails regardless of how many checks were collected."""

    def test_a_raise_fails_even_when_many_checks_were_collected(self) -> None:
        """200 collected checks still fail against an underivable count."""
        resolved = UnderivableCheckCount(reason="table mapping unreadable")
        emitted = coverage_check("rc", [_pass(str(i)) for i in range(200)], resolved)
        assert emitted.status == "fail"
        assert "cannot be certified as complete" in emitted.detail
        assert "table mapping unreadable" in emitted.detail

    def test_the_old_behaviour_would_have_passed_the_same_input(self) -> None:
        """One input passes a bare-string expectation and fails an underivable one."""
        many = [_pass(str(i)) for i in range(200)]
        assert coverage_check("rc", many, "no deriver at all").status == "pass"
        assert coverage_check("rc", many, UnderivableCheckCount(reason="boom")).status == "fail"

    def test_the_absent_deriver_fallback_is_untouched(self) -> None:
        """The absent-deriver fallback fails on zero checks and passes on one."""
        resolved = _resolve_expectation(context_for_test(), None)
        assert isinstance(resolved, str)
        assert coverage_check("rc", [], resolved).status == "fail"
        assert coverage_check("rc", [_pass("one")], resolved).status == "pass"

    def test_a_reasonless_underivable_count_is_refused(self) -> None:
        """A blank reason is refused."""
        with pytest.raises(ValueError, match="must state why"):
            UnderivableCheckCount(reason="   ")

    def test_a_raise_makes_the_resource_class_result_not_ok(self) -> None:
        """An underivable count makes `ResourceClassResult` not ok with a non-zero exit code."""
        failing = coverage_check("rc", [_pass("a")], UnderivableCheckCount(reason="boom"))
        result = ResourceClassResult(layer=4, resource_class="rc", checks=(_pass("a"), failing))
        assert not result.ok
        assert result.exit_code() != 0


class TestTheCoverageCheckIsAnOrdinaryCheck:
    """The coverage check counts in `failed`, drives `exit_code`, and appears in the JSON output."""

    def test_a_failing_coverage_check_makes_the_result_not_ok_and_exit_non_zero(self) -> None:
        failing = coverage_check("rc", [], ExpectedCheckCount(minimum=1, basis="b"))
        result = ResourceClassResult(layer=4, resource_class="rc", checks=(failing,))
        assert result.ok is False
        assert result.exit_code() == 1
        assert result.coverage is failing
        assert failing in result.failed
        emitted = result.to_json_dict()
        assert [c["check"] for c in emitted["checks"]] == [COVERAGE_CHECK]

    def test_a_result_without_a_coverage_check_reports_none(self) -> None:
        result = ResourceClassResult(layer=4, resource_class="rc", checks=(_pass("a"),))
        assert result.coverage is None


class TestTheFirstAdopterDerivesFromItsInputs:
    """Failure mode 4: the access-log expectation derives from `LOG_DESTINATIONS`."""

    def test_the_access_log_minimum_is_the_destination_count(self) -> None:
        expectation = expected_check_count(context_for_test())
        assert expectation.minimum == len(LOG_DESTINATIONS)
        assert "LOG_DESTINATIONS" in expectation.basis

    def test_the_minimum_is_not_a_hardcoded_literal(self) -> None:
        """The minimum equals the length of the destination table the checks iterate."""
        assert expected_check_count(context_for_test()).minimum == len(LOG_DESTINATIONS)
        assert len(LOG_DESTINATIONS) >= 4, "the destination table shrank; re-check the basis"

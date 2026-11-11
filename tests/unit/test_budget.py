# SPDX-License-Identifier: MIT
"""Tests for :mod:`agents.budget`: the chat and eval caps and the wall-clock turn cap.

Time is driven by an injected monotonic clock; expiry emits an ``error`` event carrying the
``budget_exhausted`` error kind.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from agents.budget import (
    CHAT_BUDGET,
    CHAT_MAX_TOOL_CALLS,
    CHAT_WALL_CLOCK_SECONDS,
    DEFAULT_MAX_TOKENS,
    EVAL_BUDGET,
    EVAL_MAX_TOOL_CALLS,
    EVAL_WALL_CLOCK_SECONDS,
    TurnBudget,
    TurnBudgetSpec,
)
from agents.events import EVENT_KINDS


class _FakeClock:
    """A monotonic clock a test advances explicitly, in seconds."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _budget(clock: _FakeClock, **kwargs: float) -> TurnBudget:
    """Build a budget whose non-wall-clock dimensions are far from their ceilings."""
    return TurnBudget(
        max_tool_calls=1_000,
        max_tokens=1_000_000,
        clock=clock,
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------------------
# Chat and eval caps
# --------------------------------------------------------------------------------------


def test_the_chat_caps_are_100_tool_calls_and_600_seconds() -> None:
    assert CHAT_MAX_TOOL_CALLS == 100
    assert CHAT_WALL_CLOCK_SECONDS == 600.0


def test_the_eval_caps_are_25_tool_calls_and_120_seconds() -> None:
    assert EVAL_MAX_TOOL_CALLS == 25
    assert EVAL_WALL_CLOCK_SECONDS == 120.0


def test_the_token_cap_is_1m_and_shared_by_both_specs() -> None:
    assert DEFAULT_MAX_TOKENS == 1_000_000
    assert CHAT_BUDGET.max_tokens == DEFAULT_MAX_TOKENS
    assert EVAL_BUDGET.max_tokens == DEFAULT_MAX_TOKENS


def test_the_specs_carry_their_own_constants() -> None:
    assert (
        TurnBudgetSpec(
            max_tool_calls=CHAT_MAX_TOOL_CALLS, max_wall_clock_seconds=CHAT_WALL_CLOCK_SECONDS
        )
        == CHAT_BUDGET
    )
    assert (
        TurnBudgetSpec(
            max_tool_calls=EVAL_MAX_TOOL_CALLS, max_wall_clock_seconds=EVAL_WALL_CLOCK_SECONDS
        )
        == EVAL_BUDGET
    )


def test_a_spec_is_immutable() -> None:
    with pytest.raises(AttributeError):
        CHAT_BUDGET.max_tool_calls = 1  # type: ignore[misc]


@pytest.mark.parametrize("spec", [CHAT_BUDGET, EVAL_BUDGET], ids=["chat", "eval"])
def test_from_spec_applies_every_cap_of_the_spec(spec: TurnBudgetSpec) -> None:
    budget = TurnBudget.from_spec(spec, clock=_FakeClock())
    assert budget.max_tool_calls == spec.max_tool_calls
    assert budget.max_wall_clock_seconds == spec.max_wall_clock_seconds
    assert budget.max_tokens == spec.max_tokens


def test_from_spec_starts_the_wall_clock_at_construction() -> None:
    clock = _FakeClock()
    clock.advance(5_000.0)
    budget = TurnBudget.from_spec(CHAT_BUDGET, clock=clock)
    clock.advance(EVAL_WALL_CLOCK_SECONDS)
    assert budget.elapsed_seconds() == pytest.approx(EVAL_WALL_CLOCK_SECONDS)
    assert budget.is_deadline_expired() is False


def test_a_chat_budget_outlasts_the_eval_cap_and_expires_at_600_seconds() -> None:
    clock = _FakeClock()
    budget = TurnBudget.from_spec(CHAT_BUDGET, clock=clock)
    clock.advance(CHAT_WALL_CLOCK_SECONDS - 0.1)
    assert budget.is_deadline_expired() is False
    clock.advance(0.1)
    assert budget.exhausted_dimensions() == ("wall_clock",)


def test_a_chat_budget_permits_100_tool_calls_and_no_more() -> None:
    budget = TurnBudget.from_spec(CHAT_BUDGET, clock=_FakeClock())
    for i in range(CHAT_MAX_TOOL_CALLS - 1):
        budget.record_tool_call(f"read account acc_{i}")
    assert budget.is_exhausted() is False
    budget.record_tool_call("read account acc_last")
    assert budget.exhausted_dimensions() == ("tool_calls",)


def test_an_eval_budget_permits_25_tool_calls_and_no_more() -> None:
    budget = TurnBudget.from_spec(EVAL_BUDGET, clock=_FakeClock())
    for i in range(EVAL_MAX_TOOL_CALLS - 1):
        budget.record_tool_call(f"read account acc_{i}")
    assert budget.is_exhausted() is False
    budget.record_tool_call("read account acc_last")
    assert budget.exhausted_dimensions() == ("tool_calls",)


def test_a_chat_exhaustion_message_names_the_chat_caps() -> None:
    clock = _FakeClock()
    budget = TurnBudget.from_spec(CHAT_BUDGET, clock=clock)
    clock.advance(CHAT_WALL_CLOCK_SECONDS)
    message = budget.exhausted_event().error_message or ""
    assert "of a 600s cap" in message, message
    assert "0 of 100 tool call(s)" in message, message
    assert "120s" not in message, message


def test_an_eval_exhaustion_message_names_the_eval_caps() -> None:
    clock = _FakeClock()
    budget = TurnBudget.from_spec(EVAL_BUDGET, clock=clock)
    for i in range(EVAL_MAX_TOOL_CALLS):
        budget.record_tool_call(f"read account acc_{i}")
    message = budget.exhausted_event().error_message or ""
    assert "(tool_calls)" in message, message
    assert "25 of 25 tool call(s)" in message, message
    assert "of a 120s cap" in message, message


def test_the_exhaustion_message_names_the_token_cap() -> None:
    budget = TurnBudget(max_tool_calls=1_000, max_tokens=10, clock=_FakeClock())
    budget.record_tokens(10)
    assert "10 of 10 token(s)" in (budget.exhausted_event().error_message or "")


def test_a_default_budget_adopts_the_eval_caps() -> None:
    budget = TurnBudget()
    assert budget.max_wall_clock_seconds == EVAL_WALL_CLOCK_SECONDS
    assert budget.max_tool_calls == EVAL_MAX_TOOL_CALLS
    assert budget.max_tokens == DEFAULT_MAX_TOKENS


#: The Terraform variables file declaring the relay Lambda's timeout.
_RELAY_VARIABLES_TF = (
    Path(__file__).resolve().parents[2] / "infra" / "modules" / "agent_relay" / "variables.tf"
)


def _relay_timeout_default_seconds() -> float:
    """Return the relay Lambda's configured timeout, parsed from its own Terraform variable.

    Raises:
        AssertionError: The variable or its default cannot be found.
    """
    assert _RELAY_VARIABLES_TF.is_file(), (
        f"{_RELAY_VARIABLES_TF} is missing. The relay timeout moved; update this test to read its "
        "new home rather than hardcoding a number."
    )
    source = _RELAY_VARIABLES_TF.read_text(encoding="utf-8")
    block = re.search(r'variable\s+"timeout_seconds"\s*\{(.*?)\n\}', source, re.DOTALL)
    assert block is not None, (
        f'no `variable "timeout_seconds"` block in {_RELAY_VARIABLES_TF}; the relay timeout is no '
        "longer declared where this test reads it."
    )
    default = re.search(r"^\s*default\s*=\s*([0-9.]+)", block.group(1), re.MULTILINE)
    assert default is not None, (
        f'`variable "timeout_seconds"` in {_RELAY_VARIABLES_TF} has no `default`; this test cannot '
        "confirm the timeout ordering without one."
    )
    return float(default.group(1))


def test_the_cap_sits_below_the_relays_real_configured_timeout() -> None:
    """The relay timeout read from `infra/modules/agent_relay/variables.tf` exceeds the chat cap."""
    assert _relay_timeout_default_seconds() > CHAT_WALL_CLOCK_SECONDS


def test_the_relay_module_also_refuses_a_timeout_at_or_below_the_turn_cap() -> None:
    """The relay's `timeout_seconds` variable has a validation requiring a value above 600."""
    source = _RELAY_VARIABLES_TF.read_text(encoding="utf-8")
    block = re.search(r'variable\s+"timeout_seconds"\s*\{(.*?)\n\}', source, re.DOTALL)
    assert block is not None
    assert "validation" in block.group(1), (
        "the relay's timeout_seconds variable lost its plan-time validation block; the "
        "timeout ordering would then rest on a default nobody enforces."
    )
    floor = re.search(r"condition\s*=\s*var\.timeout_seconds\s*>\s*([0-9.]+)", block.group(1))
    assert floor is not None and float(floor.group(1)) == CHAT_WALL_CLOCK_SECONDS, (
        "the relay's timeout_seconds validation no longer compares against the chat wall clock."
    )


def test_the_relay_timeout_parser_is_not_vacuous() -> None:
    """The relay timeout parser returns the declared default of 660 seconds."""
    assert _relay_timeout_default_seconds() == 660.0


def test_a_non_positive_wall_clock_cap_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_wall_clock_seconds must be positive"):
        TurnBudget(max_wall_clock_seconds=0)


# --------------------------------------------------------------------------------------
# Deadline behaviour
# --------------------------------------------------------------------------------------


def test_a_fresh_budget_has_no_expired_deadline() -> None:
    clock = _FakeClock()
    budget = _budget(clock)
    assert budget.elapsed_seconds() == 0.0
    assert budget.is_deadline_expired() is False
    assert budget.is_exhausted() is False


def test_the_deadline_is_measured_from_construction_not_from_the_clocks_origin() -> None:
    clock = _FakeClock()
    clock.advance(50_000.0)
    budget = _budget(clock)
    clock.advance(10.0)
    assert budget.elapsed_seconds() == pytest.approx(10.0)
    assert budget.is_deadline_expired() is False


def test_the_deadline_does_not_expire_one_tick_early() -> None:
    clock = _FakeClock()
    budget = _budget(clock)
    clock.advance(EVAL_WALL_CLOCK_SECONDS - 0.1)
    assert budget.is_deadline_expired() is False
    assert budget.is_exhausted() is False


def test_the_deadline_expires_exactly_at_the_cap() -> None:
    clock = _FakeClock()
    budget = _budget(clock)
    clock.advance(EVAL_WALL_CLOCK_SECONDS)
    assert budget.is_deadline_expired() is True
    assert budget.is_exhausted() is True


def test_the_deadline_stays_expired_once_reached() -> None:
    clock = _FakeClock()
    budget = _budget(clock)
    clock.advance(EVAL_WALL_CLOCK_SECONDS + 60.0)
    assert budget.is_deadline_expired() is True
    assert budget.is_exhausted() is True


# --------------------------------------------------------------------------------------
# Expiry emits an `error` event carrying ErrorKind.budget_exhausted
# --------------------------------------------------------------------------------------


def test_deadline_expiry_emits_an_error_event_carrying_the_budget_exhausted_error_kind() -> None:
    clock = _FakeClock()
    budget = _budget(clock)
    clock.advance(EVAL_WALL_CLOCK_SECONDS)

    event = budget.exhausted_event()

    assert event.kind == "error"
    assert event.error_kind == "budget_exhausted"
    assert event.error_message is not None
    assert event.partial_work is not None


def test_budget_exhausted_is_an_error_kind_and_never_an_event_kind() -> None:
    """`budget_exhausted` is not one of the event kinds."""
    assert "budget_exhausted" not in EVENT_KINDS


def test_the_expiry_event_names_the_wall_clock_dimension() -> None:
    clock = _FakeClock()
    budget = _budget(clock)
    clock.advance(EVAL_WALL_CLOCK_SECONDS)

    assert budget.exhausted_dimensions() == ("wall_clock",)
    assert "wall_clock" in (budget.exhausted_event().error_message or "")


def test_the_expiry_event_names_work_already_committed_because_a_cap_is_not_a_rollback() -> None:
    """The expiry event's partial work names a tool call recorded before the cap."""
    clock = _FakeClock()
    budget = _budget(clock)
    budget.record_tool_call("updated opportunity opp_1 stage to Negotiation")
    clock.advance(EVAL_WALL_CLOCK_SECONDS)

    event = budget.exhausted_event()

    assert event.partial_work is not None
    assert "opp_1" in event.partial_work


def test_expiry_with_nothing_committed_still_names_that_explicitly() -> None:
    clock = _FakeClock()
    budget = _budget(clock)
    clock.advance(EVAL_WALL_CLOCK_SECONDS)

    assert budget.exhausted_event().partial_work == "no tool calls completed"


def test_exhausted_event_still_refuses_to_report_before_the_deadline() -> None:
    """Requesting the exhausted event before the deadline raises ``RuntimeError``."""
    clock = _FakeClock()
    budget = _budget(clock)
    clock.advance(EVAL_WALL_CLOCK_SECONDS - 1.0)

    with pytest.raises(RuntimeError, match="before the budget was actually exhausted"):
        budget.exhausted_event()


# --------------------------------------------------------------------------------------
# The three dimensions are independent, and any one alone ends the turn
# --------------------------------------------------------------------------------------


def test_no_dimension_is_reported_exhausted_on_a_fresh_budget() -> None:
    assert _budget(_FakeClock()).exhausted_dimensions() == ()


def test_the_tool_call_dimension_still_exhausts_independently_of_the_clock() -> None:
    clock = _FakeClock()
    budget = TurnBudget(max_tool_calls=1, max_tokens=1_000_000, clock=clock)
    budget.record_tool_call("logged activity act_1")

    assert budget.is_deadline_expired() is False
    assert budget.exhausted_dimensions() == ("tool_calls",)
    assert budget.is_exhausted() is True


def test_the_token_dimension_still_exhausts_independently_of_the_clock() -> None:
    clock = _FakeClock()
    budget = TurnBudget(max_tool_calls=1_000, max_tokens=10, clock=clock)
    budget.record_tokens(10)

    assert budget.is_deadline_expired() is False
    assert budget.exhausted_dimensions() == ("tokens",)
    assert budget.is_exhausted() is True


def test_every_dimension_can_be_reported_at_once_in_a_fixed_order() -> None:
    clock = _FakeClock()
    budget = TurnBudget(max_tool_calls=1, max_tokens=10, clock=clock)
    budget.record_tool_call("logged activity act_1")
    budget.record_tokens(10)
    clock.advance(EVAL_WALL_CLOCK_SECONDS)

    assert budget.exhausted_dimensions() == ("tool_calls", "tokens", "wall_clock")


def test_the_production_default_clock_is_monotonic_and_needs_no_injection() -> None:
    """A budget built the way production builds it measures real elapsed time from construction."""
    budget = TurnBudget()
    assert budget.elapsed_seconds() >= 0.0
    assert budget.is_deadline_expired() is False


# --------------------------------------------------------------------------------------
# A refused request is the request_prompt_limit dimension
# --------------------------------------------------------------------------------------


def test_a_refused_request_exhausts_the_budget_on_its_own_dimension() -> None:
    budget = TurnBudget(max_tool_calls=5, max_tokens=1_000)
    assert not budget.is_exhausted()
    budget.refuse_request(
        estimated_prompt_tokens=312_000, limit=270_000, tier="tool", role="research"
    )
    assert budget.is_exhausted()
    assert budget.exhausted_dimensions() == ("request_prompt_limit",)
    assert budget.tokens_consumed == 0 and budget.tool_calls_made == 0


def test_the_exhausted_event_names_the_estimate_the_limit_and_the_tier() -> None:
    budget = TurnBudget(max_tool_calls=5, max_tokens=1_000)
    budget.refuse_request(
        estimated_prompt_tokens=312_000, limit=270_000, tier="tool", role="research"
    )
    event = budget.exhausted_event()
    assert event.kind == "error" and event.error_kind == "budget_exhausted"
    assert event.error_message is not None
    assert event.error_message.startswith(
        "Turn budget exhausted (request_prompt_limit: estimated 312000 > 270000 prompt tokens "
        "for one request on the tool tier) after "
    )


def test_the_first_refusal_is_the_one_reported() -> None:
    budget = TurnBudget()
    budget.refuse_request(estimated_prompt_tokens=10, limit=5, tier="subagent", role="research")
    budget.refuse_request(estimated_prompt_tokens=99, limit=7, tier="tool", role="research")
    message = budget.exhausted_event().error_message or ""
    assert "estimated 10 > 5" in message and "subagent tier" in message
    assert "99" not in message


def test_a_refusal_is_listed_after_the_other_exhausted_dimensions() -> None:
    budget = TurnBudget(max_tool_calls=1, max_tokens=1_000)
    budget.record_tool_call("called get_account")
    budget.refuse_request(estimated_prompt_tokens=2, limit=1, tier="supervisor", role="supervisor")
    assert budget.exhausted_dimensions() == ("tool_calls", "request_prompt_limit")
    assert "(tool_calls, request_prompt_limit: estimated 2 > 1 " in (
        budget.exhausted_event().error_message or ""
    )


def test_a_refusal_is_logged_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    budget = TurnBudget()
    with caplog.at_level(logging.WARNING, logger="agents.budget"):
        budget.refuse_request(estimated_prompt_tokens=8, limit=4, tier="tool", role="mutation")
    records = [r for r in caplog.records if "model_request_refused" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    assert "tier=tool role=mutation estimated_prompt_tokens=8 limit=4" in records[0].getMessage()


def test_an_unrefused_budget_reports_no_request_prompt_limit_dimension() -> None:
    budget = TurnBudget(max_tool_calls=1)
    budget.record_tool_call("called get_account")
    assert "request_prompt_limit" not in budget.exhausted_dimensions()

# SPDX-License-Identifier: MIT
"""The turn wall clock bounding model waits (:func:`agents.budget.within_deadline`,
:func:`agents.budget.until_deadline`) and the eval turn's hard deadline.

Waits run on real asyncio timers against sub-second caps; each test bounds itself with an outer
``asyncio.wait_for`` so a missing bound fails fast instead of hanging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import warnings
from collections.abc import AsyncIterator, Awaitable, Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest

from agents.budget import (
    EVAL_BUDGET,
    EVAL_TURN_HARD_DEADLINE_SECONDS,
    EVAL_TURN_HARD_MARGIN_SECONDS,
    EVAL_WALL_CLOCK_SECONDS,
    DeadlineReachedError,
    TurnBudget,
    TurnBudgetSpec,
    until_deadline,
    within_deadline,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SWEEP_ASL = _REPO_ROOT / "infra" / "templates" / "eval_sweep.asl.json"
_EVAL_SFN_TF = _REPO_ROOT / "infra" / "eval_sfn.tf"

#: The outer bound every test runs under; far below the waits a missing deadline would leave.
_TEST_CAP_SECONDS = 5.0

T = TypeVar("T")


def _within(awaitable: Awaitable[T], budget: TurnBudget) -> Coroutine[Any, Any, T]:  # noqa: UP047
    return within_deadline(awaitable, budget, tier="subagent", role="research")


def _until(stream: AsyncIterator[T], budget: TurnBudget) -> AsyncIterator[T]:  # noqa: UP047
    return until_deadline(stream, budget, tier="supervisor", role="supervisor")


def _budget(seconds: float) -> TurnBudget:
    return TurnBudget.from_spec(TurnBudgetSpec(max_tool_calls=5, max_wall_clock_seconds=seconds))


def _run(coro: Coroutine[Any, Any, T]) -> T:  # noqa: UP047
    return asyncio.run(asyncio.wait_for(coro, timeout=_TEST_CAP_SECONDS))


async def _slow(value: str, seconds: float) -> str:
    await asyncio.sleep(seconds)
    return value


def test_remaining_seconds_counts_down_from_the_cap() -> None:
    """``remaining_seconds`` is the cap minus elapsed time, and goes non-positive past it."""
    now = [100.0]
    budget = TurnBudget.from_spec(
        TurnBudgetSpec(max_tool_calls=1, max_wall_clock_seconds=10.0), clock=lambda: now[0]
    )
    assert budget.remaining_seconds() == 10.0
    now[0] = 107.5
    assert budget.remaining_seconds() == 2.5
    now[0] = 111.0
    assert budget.remaining_seconds() <= 0


def test_within_deadline_returns_a_value_that_arrives_in_time() -> None:
    """A wait that finishes before the cap returns its value."""
    assert _run(_within(_slow("done", 0.01), _budget(2.0))) == "done"


def test_within_deadline_gives_up_when_the_cap_passes_during_the_wait() -> None:
    """A wait outlasting the cap is cancelled and reported as the deadline, not left running."""
    loop_time: list[float] = []

    async def _scenario() -> None:
        started = asyncio.get_running_loop().time()
        with pytest.raises(DeadlineReachedError):
            await _within(_slow("late", 60.0), _budget(0.1))
        loop_time.append(asyncio.get_running_loop().time() - started)

    _run(_scenario())
    assert loop_time[0] < 1.0


def test_within_deadline_does_not_mistake_the_awaitables_own_timeout_for_the_deadline() -> None:
    """A ``TimeoutError`` raised by the awaited call itself propagates unchanged."""

    async def _times_out() -> str:
        raise TimeoutError("transport deadline")

    with pytest.raises(TimeoutError, match="transport deadline"):
        _run(_within(_times_out(), _budget(5.0)))


def test_within_deadline_on_an_expired_budget_never_starts_the_wait() -> None:
    """Past the cap, the awaitable is closed unstarted, so no "never awaited" warning leaks."""
    started: list[bool] = []

    async def _would_start() -> str:
        started.append(True)
        return "x"

    expired = _budget(1e-9)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(DeadlineReachedError):
            _run(_within(_would_start(), expired))
    assert started == []


def test_until_deadline_passes_a_prompt_stream_through_unchanged() -> None:
    """Every item of a stream that ends in time is yielded, in order."""

    async def _stream() -> AsyncIterator[int]:
        for value in range(3):
            await asyncio.sleep(0)
            yield value

    async def _collect() -> list[int]:
        return [item async for item in _until(_stream(), _budget(2.0))]

    assert _run(_collect()) == [0, 1, 2]


def test_until_deadline_stops_a_stalled_stream_and_cancels_it() -> None:
    """A stream that stalls past the cap ends after the items it produced; the stall is cancelled."""
    cancelled: list[bool] = []

    async def _stalls() -> AsyncIterator[str]:
        yield "first"
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        yield "never"

    budget = _budget(0.1)

    async def _collect() -> list[str]:
        return [item async for item in _until(_stalls(), budget)]

    assert _run(_collect()) == ["first"]
    assert cancelled == [True]
    assert budget.is_deadline_expired()
    assert budget.calls_without_usage == 1


#: The consumer-work test's cap, and the consumer's per-item work, which outlasts it.
_CONSUMER_CAP_SECONDS = 0.5
_CONSUMER_WORK_SECONDS = 0.75


def test_until_deadline_does_not_bound_the_consumers_own_work() -> None:
    """Work between items is not interrupted: a consumer slower than the cap still sees item one."""

    async def _two() -> AsyncIterator[str]:
        yield "a"
        yield "b"

    async def _collect() -> list[str]:
        budget = _budget(_CONSUMER_CAP_SECONDS)
        seen: list[str] = []
        async for item in _until(_two(), budget):
            seen.append(item)
            await asyncio.sleep(_CONSUMER_WORK_SECONDS)
        return seen

    assert _run(_collect()) == ["a"]


def test_the_hard_eval_deadline_is_the_soft_cap_plus_a_positive_margin() -> None:
    """The hard cancel lies past the soft cap; the soft cap is unchanged at 120 s."""
    assert EVAL_WALL_CLOCK_SECONDS == 120.0
    assert EVAL_BUDGET.max_wall_clock_seconds == EVAL_WALL_CLOCK_SECONDS
    assert EVAL_TURN_HARD_MARGIN_SECONDS > 0
    assert EVAL_TURN_HARD_DEADLINE_SECONDS == (
        EVAL_WALL_CLOCK_SECONDS + EVAL_TURN_HARD_MARGIN_SECONDS
    )


def _sweep_client_read_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    """The read timeout of the sweep task's real Runtime client, from its botocore config."""
    from eval import sfn_handlers

    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    sfn_handlers._default_runtime_client.cache_clear()
    try:
        client = sfn_handlers._default_runtime_client()
        return float(client.meta.config.read_timeout)  # type: ignore[attr-defined]
    finally:
        sfn_handlers._default_runtime_client.cache_clear()


def _run_task_timeout() -> float:
    """The sweep state machine's single ``RunTask`` ``TimeoutSeconds``."""
    document = json.loads(_SWEEP_ASL.read_text(encoding="utf-8"))
    timeouts: list[int] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            run_task = node.get("RunTask")
            if isinstance(run_task, dict) and "TimeoutSeconds" in run_task:
                timeouts.append(int(run_task["TimeoutSeconds"]))
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(document)
    assert len(timeouts) == 1, timeouts
    return float(timeouts[0])


def _module_block(text: str, name: str) -> str:
    """The body of the top-level ``module "<name>" { ... }`` block, matched by brace depth."""
    match = re.search(rf'^module\s+"{re.escape(name)}"\s*\{{', text, flags=re.MULTILINE)
    assert match is not None, f"module {name!r} not found"
    depth = 1
    for index in range(match.end(), len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[match.end() : index]
    raise AssertionError(f"module {name!r} is not closed")


def _top_level_attribute(block: str, attribute: str) -> str:
    """The single value of ``attribute`` assigned at the block's own depth, not a nested one."""
    values: list[str] = []
    depth = 0
    for line in block.splitlines():
        assigned = re.match(rf"\s*{re.escape(attribute)}\s*=\s*(\S+)", line)
        if depth == 0 and assigned is not None:
            values.append(assigned.group(1))
        depth += line.count("{") - line.count("}")
    assert len(values) == 1, values
    return values[0]


def _sweep_task_lambda_timeout(text: str) -> float:
    """The sweep_task Lambda's ``timeout_seconds`` from its ``eval_sfn.tf`` module block."""
    block = _module_block(text, "lambda_action_sweep_task")
    return float(_top_level_attribute(block, "timeout_seconds"))


def test_the_sweep_task_lambda_timeout_is_parsed_from_its_module_block() -> None:
    """The parser reads sweep_task's own value, not a sibling module's or a nested block's."""
    text = (
        'module "lambda_action_sweep_control" {\n  timeout_seconds = 30\n}\n'
        'module "lambda_action_sweep_task" {\n'
        "  environment_variables = {\n    timeout_seconds = 5\n  }\n"
        '  path = "${var.x}/y"\n'
        "  timeout_seconds = 300\n}\n"
    )
    assert _sweep_task_lambda_timeout(text) == 300.0


def test_the_timeouts_nest_hard_cancel_then_client_read_then_lambda_then_run_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard eval cancel < client read timeout < sweep_task Lambda timeout < RunTask timeout."""
    read_timeout = _sweep_client_read_timeout(monkeypatch)
    lambda_timeout = _sweep_task_lambda_timeout(_EVAL_SFN_TF.read_text(encoding="utf-8"))
    run_task = _run_task_timeout()
    assert (
        EVAL_WALL_CLOCK_SECONDS
        < EVAL_TURN_HARD_DEADLINE_SECONDS
        < read_timeout
        < lambda_timeout
        < run_task
    )


def test_a_wait_cut_by_the_deadline_is_counted_and_named_in_the_exhaustion_report() -> None:
    """An abandoned model wait is counted; the report says its usage is unrecorded."""
    budget = _budget(0.05)

    async def _scenario() -> None:
        assert await _within(_slow("fast", 0.0), budget) == "fast"
        with pytest.raises(DeadlineReachedError):
            await _within(_slow("late", 60.0), budget)
        with pytest.raises(DeadlineReachedError):
            await _within(_slow("never started", 0.0), budget)

    _run(_scenario())
    assert budget.calls_without_usage == 1
    message = budget.exhausted_event().error_message or ""
    assert "1 model call(s) ended before reporting token usage" in message
    assert "lower bound" in message


def test_an_exhaustion_with_no_cut_wait_does_not_claim_one() -> None:
    """A budget that expired between steps reports no cut-off calls."""
    now = [0.0]
    budget = TurnBudget.from_spec(
        TurnBudgetSpec(max_tool_calls=1, max_wall_clock_seconds=1.0), clock=lambda: now[0]
    )
    now[0] = 2.0
    assert "ended before reporting" not in (budget.exhausted_event().error_message or "")


def test_within_deadline_on_an_expired_budget_cancels_a_passed_future() -> None:
    """A task handed in after the cap is cancelled, not left running."""

    async def _scenario() -> bool:
        task = asyncio.ensure_future(_slow("x", 60.0))
        with pytest.raises(DeadlineReachedError):
            await _within(task, _budget(1e-9))
        await asyncio.sleep(0)
        return task.cancelled()

    assert _run(_scenario()) is True


def test_until_deadline_closes_a_stream_left_suspended_between_items() -> None:
    """When the cap passes while the consumer works, the suspended model stream is closed."""
    closed: list[bool] = []

    async def _stream() -> AsyncIterator[str]:
        try:
            yield "a"
            yield "b"
        finally:
            closed.append(True)

    budget = _budget(0.05)

    async def _collect() -> tuple[list[str], list[bool]]:
        seen: list[str] = []
        async for item in _until(_stream(), budget):
            seen.append(item)
            await asyncio.sleep(0.1)
        return seen, list(closed)

    seen, closed_when_iteration_ended = _run(_collect())
    assert seen == ["a"]
    assert closed_when_iteration_ended == [True]


def test_a_cut_wait_is_logged_naming_its_tier_and_role(caplog: pytest.LogCaptureFixture) -> None:
    """The cut-off line names tier and role and says the usage is unrecorded."""
    with (
        caplog.at_level(logging.WARNING, logger="agents.budget"),
        pytest.raises(DeadlineReachedError),
    ):
        _run(within_deadline(_slow("late", 60.0), _budget(0.05), tier="tool", role="mutation"))
    lines = [json.loads(r.message) for r in caplog.records if r.name == "agents.budget"]
    assert lines == [
        {
            "event": "model_call_usage_unrecorded",
            "reason": "tier=tool role=mutation cause=deadline",
        }
    ]


def test_closing_the_bounded_stream_closes_the_model_stream_at_once() -> None:
    """A consumer that stops early and closes the bounded stream closes the model stream too."""
    closed: list[bool] = []

    async def _stream() -> AsyncIterator[str]:
        try:
            yield "a"
            yield "b"
        finally:
            closed.append(True)

    async def _scenario() -> list[bool]:
        model_stream = _stream()
        bounded = _until(model_stream, _budget(5.0))
        assert await anext(bounded) == "a"
        await bounded.aclose()  # type: ignore[attr-defined]
        return list(closed)

    assert _run(_scenario()) == [True]


def test_within_deadline_on_an_expired_budget_counts_a_call_already_in_flight() -> None:
    """Past the cap, a wait belonging to a call already under way is counted once."""
    budget = _budget(1e-9)
    with pytest.raises(DeadlineReachedError):
        _run(within_deadline(_slow("x", 0.0), budget, tier="tool", role="research", in_flight=True))
    assert budget.calls_without_usage == 1


def test_until_deadline_counts_a_stream_cut_after_its_first_item_once() -> None:
    """The cap passes while the consumer works on item one: the cut stream is counted once."""

    async def _two() -> AsyncIterator[str]:
        yield "a"
        yield "b"

    budget = _budget(0.05)

    async def _collect() -> list[str]:
        seen: list[str] = []
        async for item in _until(_two(), budget):
            seen.append(item)
            await asyncio.sleep(0.1)
        return seen

    assert _run(_collect()) == ["a"]
    assert budget.calls_without_usage == 1


def test_until_deadline_does_not_count_a_stream_it_never_started() -> None:
    """A stream whose first item is never awaited made no model call, so nothing is counted."""
    started: list[bool] = []

    async def _stream() -> AsyncIterator[str]:
        started.append(True)
        yield "a"

    budget = _budget(1e-9)

    async def _collect() -> list[str]:
        return [item async for item in _until(_stream(), budget)]

    assert _run(_collect()) == []
    assert started == []
    assert budget.calls_without_usage == 0


def test_a_stream_that_ends_in_time_is_not_counted() -> None:
    """A stream fully consumed before the cap leaves the count at zero."""

    async def _two() -> AsyncIterator[str]:
        yield "a"
        yield "b"

    budget = _budget(2.0)

    async def _collect() -> list[str]:
        return [item async for item in _until(_two(), budget)]

    assert _run(_collect()) == ["a", "b"]
    assert budget.calls_without_usage == 0


def test_within_deadline_on_an_expired_budget_does_not_count_a_metered_call() -> None:
    """Past the cap, a call that already reported usage still raises but is not counted."""
    budget = _budget(1e-9)
    with pytest.raises(DeadlineReachedError):
        _run(
            within_deadline(
                _slow("x", 0.0), budget, tier="tool", role="research", in_flight=True, metered=True
            )
        )
    assert budget.calls_without_usage == 0


def test_within_deadline_cut_mid_wait_does_not_count_a_metered_call() -> None:
    """The cap passes during the wait of a call that already reported usage: not counted."""
    budget = _budget(0.05)
    with pytest.raises(DeadlineReachedError):
        _run(within_deadline(_slow("x", 1.0), budget, tier="tool", role="research", metered=True))
    assert budget.calls_without_usage == 0


@pytest.mark.parametrize(("metered_item", "expected"), [("usage", 0), ("never", 1)])
def test_until_deadline_does_not_count_a_stream_cut_after_a_metered_item(
    metered_item: str, expected: int
) -> None:
    """A stream cut after an item ``is_metered`` accepts is not counted; otherwise it is, once."""

    async def _two() -> AsyncIterator[str]:
        yield "usage"
        yield "b"

    budget = _budget(0.05)

    async def _collect() -> list[str]:
        seen: list[str] = []
        bounded = until_deadline(
            _two(),
            budget,
            tier="supervisor",
            role="supervisor",
            is_metered=lambda item: item == metered_item,
        )
        async for item in bounded:
            seen.append(item)
            await asyncio.sleep(0.1)
        return seen

    assert _run(_collect()) == ["usage"]
    assert budget.calls_without_usage == expected

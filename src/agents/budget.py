# SPDX-License-Identifier: MIT
"""Turn budgets bounding each agent invocation.

A :class:`TurnBudget` bounds one agent turn on three dimensions: tool calls, tokens, and wall-clock
time. Two fixed caps exist: :data:`CHAT_BUDGET` (100 tool calls, 600 s) for chat turns and
:data:`EVAL_BUDGET` (25 tool calls, 120 s) for eval turns; build a budget from one with
:meth:`TurnBudget.from_spec`. On exhaustion the caller emits
:meth:`TurnBudget.exhausted_event`, a terminal ``error`` event with
:data:`agents.events.ErrorKind` ``budget_exhausted`` naming the partial work done. Writes already
committed when a cap is hit stay committed.

Every wait on a model is bounded by the turn's remaining wall clock: :func:`until_deadline` for a
provider stream and :func:`within_deadline` for one awaitable. Neither interrupts synchronous work
already running, so a dispatched tool call always completes. The eval turn's hard cancel,
:data:`EVAL_TURN_HARD_DEADLINE_SECONDS`, sits a margin past the soft cap so the soft
``budget_exhausted`` path ends the turn first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final

from agents.events import AgentEvent, budget_exhausted_event

#: Maximum tool calls permitted in one chat turn.
CHAT_MAX_TOOL_CALLS: Final[int] = 100

#: The hard wall-clock cap of one chat turn, in seconds.
CHAT_WALL_CLOCK_SECONDS: Final[float] = 600.0

#: Maximum tool calls permitted in one eval turn.
EVAL_MAX_TOOL_CALLS: Final[int] = 25

#: The hard wall-clock cap of one eval turn, in seconds.
EVAL_WALL_CLOCK_SECONDS: Final[float] = 120.0

#: Seconds the eval turn's hard cancel waits past :data:`EVAL_WALL_CLOCK_SECONDS`, so a model wait
#: the soft cap ends has time to unwind into a ``budget_exhausted`` observation.
EVAL_TURN_HARD_MARGIN_SECONDS: Final[float] = 30.0

#: The Runtime's hard cancel of one eval turn. Must stay below the sweep task's Runtime client
#: ``read_timeout`` (``eval.sfn_handlers``) and the state machine's ``RunTask`` ``TimeoutSeconds``.
EVAL_TURN_HARD_DEADLINE_SECONDS: Final[float] = (
    EVAL_WALL_CLOCK_SECONDS + EVAL_TURN_HARD_MARGIN_SECONDS
)

#: Maximum combined input and output tokens permitted in one turn, chat or eval.
DEFAULT_MAX_TOKENS: Final[int] = 200_000

_logger = logging.getLogger(__name__)


def _require_positive_caps(max_tool_calls: int, max_tokens: int, wall_clock: float) -> None:
    """Raise :class:`ValueError` unless both counts are positive integers and the clock finite > 0."""
    for name, count in (("max_tool_calls", max_tool_calls), ("max_tokens", max_tokens)):
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError(f"{name} must be positive (an integer), got {count!r}")
    if (
        isinstance(wall_clock, bool)
        or not isinstance(wall_clock, int | float)
        or not math.isfinite(wall_clock)
        or wall_clock <= 0
    ):
        raise ValueError(f"max_wall_clock_seconds must be positive and finite, got {wall_clock!r}")


@dataclass(frozen=True, slots=True)
class TurnBudgetSpec:
    """The caps of one kind of turn, from which :meth:`TurnBudget.from_spec` builds a budget.

    Attributes:
        max_tool_calls: Tool calls permitted in the turn.
        max_wall_clock_seconds: The turn's hard wall-clock cap, in seconds.
        max_tokens: Combined input and output tokens permitted in the turn.
    """

    max_tool_calls: int
    max_wall_clock_seconds: float
    max_tokens: int = DEFAULT_MAX_TOKENS

    def __post_init__(self) -> None:
        """Reject a cap that is not a finite, positive number."""
        _require_positive_caps(self.max_tool_calls, self.max_tokens, self.max_wall_clock_seconds)


#: The caps of a chat turn (relay and Gateway).
CHAT_BUDGET: Final[TurnBudgetSpec] = TurnBudgetSpec(
    max_tool_calls=CHAT_MAX_TOOL_CALLS, max_wall_clock_seconds=CHAT_WALL_CLOCK_SECONDS
)

#: The caps of an eval turn (harness and the Runtime's `eval_turn` mode).
EVAL_BUDGET: Final[TurnBudgetSpec] = TurnBudgetSpec(
    max_tool_calls=EVAL_MAX_TOOL_CALLS, max_wall_clock_seconds=EVAL_WALL_CLOCK_SECONDS
)


@dataclass(slots=True)
class TurnBudget:
    """Tracks tool-call, token, and wall-clock consumption for one agent turn.

    Attributes:
        max_tool_calls: Tool calls permitted before :meth:`is_exhausted` reports ``True`` on that
            dimension.
        max_tokens: Combined input and output tokens permitted before :meth:`is_exhausted` reports
            ``True`` on that dimension.
        max_wall_clock_seconds: The turn's hard wall-clock cap, defaulting to
            :data:`EVAL_WALL_CLOCK_SECONDS`. Measured from construction, which is the start of the
            turn.
        clock: The monotonic time source, in seconds; injectable for tests.
        tool_calls_made: Tool calls recorded so far via :meth:`record_tool_call`.
        tokens_consumed: Tokens recorded so far via :meth:`record_tokens`.
        completed_actions: Descriptions of work completed so far, in call order, consumed by
            :meth:`exhausted_event` to name the partial work done.
        calls_without_usage: Model calls that ended before reporting their token usage, recorded
            via :meth:`note_call_without_usage`: a wait :func:`within_deadline` abandoned at the
            deadline, or a round the turn stopped mid-stream. The turn's recorded usage and cost
            are a lower bound whenever this is non-zero.
        started_at: The ``clock`` reading at construction, against which the deadline is measured.
    """

    max_tool_calls: int = EVAL_MAX_TOOL_CALLS
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_wall_clock_seconds: float = EVAL_WALL_CLOCK_SECONDS
    clock: Callable[[], float] = time.monotonic
    tool_calls_made: int = 0
    tokens_consumed: int = 0
    completed_actions: list[str] = field(default_factory=list)
    calls_without_usage: int = 0
    started_at: float = field(init=False)

    def __post_init__(self) -> None:
        """Reject a cap that is not a finite, positive number and start the wall clock."""
        _require_positive_caps(self.max_tool_calls, self.max_tokens, self.max_wall_clock_seconds)
        self.started_at = self.clock()

    @classmethod
    def from_spec(
        cls, spec: TurnBudgetSpec, *, clock: Callable[[], float] = time.monotonic
    ) -> TurnBudget:
        """Return a fresh budget carrying every cap of ``spec``, its wall clock started now.

        Args:
            spec: The caps to apply, normally :data:`CHAT_BUDGET` or :data:`EVAL_BUDGET`.
            clock: The monotonic time source, in seconds; injectable for tests.
        """
        return cls(
            max_tool_calls=spec.max_tool_calls,
            max_tokens=spec.max_tokens,
            max_wall_clock_seconds=spec.max_wall_clock_seconds,
            clock=clock,
        )

    def record_tool_call(self, description: str) -> None:
        """Record one completed tool call and its human-readable description.

        Args:
            description: What the tool call accomplished, named plainly enough to appear verbatim
                in a budget-exhaustion report (for example ``"updated opportunity opp_123"``).
        """
        self.tool_calls_made += 1
        self.completed_actions.append(description)

    def record_tokens(self, count: int) -> None:
        """Record tokens consumed by one model invocation within this turn.

        Args:
            count: Combined input and output tokens consumed. Must not be negative.

        Raises:
            ValueError: If ``count`` is negative.
        """
        if count < 0:
            raise ValueError(f"token count must not be negative, got {count}")
        self.tokens_consumed += count

    def elapsed_seconds(self) -> float:
        """Return wall-clock seconds since this turn started."""
        return self.clock() - self.started_at

    def note_call_without_usage(self, *, tier: str, role: str, cause: str) -> None:
        """Record one model call whose token usage will never be reported, and log it.

        Args:
            tier: The call's tier (``supervisor``, ``subagent`` or ``tool``).
            role: The agent role it ran for.
            cause: Why it ended unmetered, for example ``deadline`` or ``turn_stopped``.
        """
        self.calls_without_usage += 1
        _logger.warning(
            json.dumps(
                {
                    "event": "model_call_usage_unrecorded",
                    "reason": f"tier={tier} role={role} cause={cause}",
                }
            )
        )

    def remaining_seconds(self) -> float:
        """Return wall-clock seconds left before the cap; zero or negative once it has passed."""
        return self.max_wall_clock_seconds - self.elapsed_seconds()

    def is_deadline_expired(self) -> bool:
        """Return whether the wall-clock cap has been reached.

        Checked between steps by :class:`agents.supervisor.TurnGuard` and between subagent rounds;
        a tool call already in flight when the deadline passes completes.
        """
        return self.elapsed_seconds() >= self.max_wall_clock_seconds

    def is_exhausted(self) -> bool:
        """Return whether any of the three dimensions has reached its ceiling.

        The dimensions are the tool-call count, the token count, and the wall-clock deadline. Any
        one alone ends the turn.
        """
        return (
            self.tool_calls_made >= self.max_tool_calls
            or self.tokens_consumed >= self.max_tokens
            or self.is_deadline_expired()
        )

    def exhausted_dimensions(self) -> tuple[str, ...]:
        """Return the names of every dimension currently at or past its ceiling.

        Returns:
            Any of ``"tool_calls"``, ``"tokens"``, ``"wall_clock"``, in that fixed order. Empty
            when the budget is not exhausted at all.
        """
        dimensions: list[str] = []
        if self.tool_calls_made >= self.max_tool_calls:
            dimensions.append("tool_calls")
        if self.tokens_consumed >= self.max_tokens:
            dimensions.append("tokens")
        if self.is_deadline_expired():
            dimensions.append("wall_clock")
        return tuple(dimensions)

    def exhausted_event(self) -> AgentEvent:
        """Build the terminal event reporting exhaustion and the partial work done so far.

        Returns:
            A terminal ``error`` event with ``error_kind="budget_exhausted"`` whose
            ``error_message`` names which dimension ran out and how many model calls ended
            before reporting usage, and whose ``partial_work`` names every action
            recorded via :meth:`record_tool_call`, or states that none completed.

        Raises:
            RuntimeError: If called while :meth:`is_exhausted` is ``False``.
        """
        if not self.is_exhausted():
            raise RuntimeError("exhausted_event called before the budget was actually exhausted")
        if self.completed_actions:
            partial_work = "; ".join(self.completed_actions)
        else:
            partial_work = "no tool calls completed"
        return budget_exhausted_event(
            partial_work=partial_work,
            error_message=(
                f"Turn budget exhausted ({', '.join(self.exhausted_dimensions())}) after "
                f"{self.tool_calls_made} of {self.max_tool_calls} tool call(s), "
                f"{self.tokens_consumed} of {self.max_tokens} token(s), and "
                f"{self.elapsed_seconds():.1f}s of a {self.max_wall_clock_seconds:.0f}s cap."
                + (
                    f" {self.calls_without_usage} model call(s) ended before reporting token"
                    " usage, so the reported usage and cost are a lower bound."
                    if self.calls_without_usage
                    else ""
                )
            ),
        )


class DeadlineReachedError(Exception):
    """Raised by :func:`within_deadline` when the turn's wall clock runs out during the wait."""


async def within_deadline[T](
    awaitable: Awaitable[T],
    budget: TurnBudget,
    *,
    tier: str,
    role: str,
    in_flight: bool = False,
    metered: bool = False,
) -> T:
    """Await ``awaitable``, giving up when ``budget``'s wall clock runs out.

    A wait abandoned mid-flight is recorded with :meth:`TurnBudget.note_call_without_usage`
    (cause ``deadline``), naming ``tier`` and ``role``. With ``in_flight``, the model call the wait
    belongs to is already under way, so a cap that passed before the wait is recorded too. With
    ``metered``, that call has already reported its usage, so neither path records it.

    Raises:
        DeadlineReachedError: The cap passed before or during the wait. A wait never started is
            closed (a coroutine) or cancelled (a task or future); one in flight was cancelled at
            its suspension point. A ``TimeoutError`` raised by ``awaitable`` itself propagates.
    """
    remaining = budget.remaining_seconds()
    if remaining <= 0:
        if isinstance(awaitable, asyncio.Future):
            awaitable.cancel()
        else:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
        if in_flight and not metered:
            budget.note_call_without_usage(tier=tier, role=role, cause="deadline")
        raise DeadlineReachedError
    scope = asyncio.timeout(remaining)
    try:
        async with scope:
            return await awaitable
    except TimeoutError:
        if scope.expired():
            if not metered:
                budget.note_call_without_usage(tier=tier, role=role, cause="deadline")
            raise DeadlineReachedError from None
        raise


async def until_deadline[T](
    stream: AsyncIterator[T],
    budget: TurnBudget,
    *,
    tier: str,
    role: str,
    is_metered: Callable[[T], bool] | None = None,
) -> AsyncIterator[T]:
    """Yield ``stream``'s items until it ends or ``budget``'s wall clock runs out, then stop.

    Only the wait for each next item is bounded; the consumer's work between items is not. The
    caller tells the two endings apart with :meth:`TurnBudget.is_deadline_expired`. A stream cut
    off after its first item is recorded once with :meth:`TurnBudget.note_call_without_usage`,
    unless ``is_metered`` returned true for an item already yielded. ``stream`` is closed
    (``aclose``) when this generator finishes or is closed.
    """
    iterator = aiter(stream)
    started = False
    metered = False
    try:
        while True:
            try:
                item = await within_deadline(
                    anext(iterator),
                    budget,
                    tier=tier,
                    role=role,
                    in_flight=started,
                    metered=metered,
                )
            except (StopAsyncIteration, DeadlineReachedError):
                return
            started = True
            metered = metered or (is_metered is not None and is_metered(item))
            yield item
    finally:
        aclose = getattr(iterator, "aclose", None)
        if callable(aclose):
            await aclose()


__all__ = [
    "CHAT_BUDGET",
    "CHAT_MAX_TOOL_CALLS",
    "CHAT_WALL_CLOCK_SECONDS",
    "DEFAULT_MAX_TOKENS",
    "EVAL_BUDGET",
    "EVAL_MAX_TOOL_CALLS",
    "EVAL_TURN_HARD_DEADLINE_SECONDS",
    "EVAL_TURN_HARD_MARGIN_SECONDS",
    "EVAL_WALL_CLOCK_SECONDS",
    "DeadlineReachedError",
    "TurnBudget",
    "TurnBudgetSpec",
    "until_deadline",
    "within_deadline",
]

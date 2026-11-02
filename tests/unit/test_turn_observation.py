# SPDX-License-Identifier: MIT
"""Tests for :mod:`eval.turn_observation`: the observer, the observed turn and its wire form."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest

import eval.turn_observation as turn_observation
from agents.budget import EVAL_BUDGET, TurnBudgetSpec
from agents.registry import RegisteredTool, full_registry
from agents.tier_attribution import (
    CacheCounts,
    InvocationAttribution,
    TokenCounts,
    ToolCallAttribution,
)
from eval.metrics import Exclusion, ExclusionCategory
from eval.scoring import ObservedProposal, ObservedToolArguments, ToolCall, TurnOutcome
from eval.sweep_units import ArmSpec, arm_turn_tuning
from eval.turn_observation import (
    SERIALISED_TURN_SCHEMA,
    USAGE_LOWER_BOUND_KEY,
    ObservedTurn,
    ObservingDispatch,
    deserialise_turn,
    observe_turn,
    serialise_turn,
)
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from providers.capabilities import Family, load_capability_matrix
from providers.stub import StubProvider

MATRIX = load_capability_matrix()
_TOOLS = full_registry()


def _tool(name: str) -> RegisteredTool:
    return _TOOLS[name]


def _ok(body: Mapping[str, object]) -> dict[str, Any]:
    return {"statusCode": 200, "body": dict(body)}


def _denied() -> dict[str, Any]:
    return {
        "statusCode": 403,
        "error": {"code": "scope_denied", "missing_scope": "account:acc-9", "message": "no"},
    }


def _not_found() -> dict[str, Any]:
    return {"statusCode": 404, "error": {"code": "not_found", "message": "none"}}


def _failure() -> dict[str, Any]:
    return {"statusCode": 502, "error": {"code": "tool_failure", "message": "boom"}}


def _observe(
    *outputs: dict[str, Any], calls: list[tuple[str, dict[str, object]]]
) -> ObservingDispatch:
    queue = list(outputs)
    observer = ObservingDispatch(lambda _tool, _input: queue.pop(0))
    for name, tool_input in calls:
        observer(_tool(name), tool_input)
    return observer


# --- ObservingDispatch -------------------------------------------------------------------------


def test_denied_overrides_every_earlier_outcome() -> None:
    observer = _observe(
        _not_found(),
        _denied(),
        calls=[("get_account", {"account_id": "a"}), ("get_account", {"account_id": "b"})],
    )
    outcome = observer.outcome(tenant="eval-x", budget_exhausted=False)
    assert (outcome.denial_outcome, outcome.denial_missing_scope) == ("denied", "account:acc-9")


def test_first_no_match_or_tool_failure_wins_over_later_ones() -> None:
    observer = _observe(
        _failure(),
        _not_found(),
        calls=[("get_account", {"account_id": "a"}), ("get_account", {"account_id": "b"})],
    )
    assert observer.outcome(tenant="t", budget_exhausted=False).denial_outcome == "tool_failure"


def test_data_is_reported_only_when_nothing_else_occurred() -> None:
    data_only = _observe(_ok({"account_id": "a"}), calls=[("get_account", {"account_id": "a"})])
    assert data_only.outcome(tenant="t", budget_exhausted=False).denial_outcome == "data"
    nothing = ObservingDispatch(lambda _t, _i: _ok({}))
    assert nothing.outcome(tenant="t", budget_exhausted=True).denial_outcome is None


def test_calls_targets_and_arguments_are_recorded_in_order() -> None:
    observer = _observe(
        _ok({"items": []}),
        _ok({"account_id": "acc-1"}),
        calls=[
            ("search_accounts", {"q": "acme", "target_table": "accounts"}),
            ("get_account", {"account_id": "acc-1"}),
        ],
    )
    outcome = observer.outcome(tenant="eval-x", budget_exhausted=False)
    assert outcome.tool_calls == (
        ToolCall(tool_name="search_accounts", target_table="accounts", target_id=None),
        ToolCall(tool_name="get_account", target_table=None, target_id="acc-1"),
    )
    assert observer.tool_arguments == (
        ObservedToolArguments(
            tool_name="search_accounts", arguments={"q": "acme", "target_table": "accounts"}
        ),
        ObservedToolArguments(tool_name="get_account", arguments={"account_id": "acc-1"}),
    )


def test_recorded_arguments_are_a_copy_taken_before_dispatch() -> None:
    """A dispatch that mutates its input does not change what was observed."""

    def mutating(_tool: RegisteredTool, tool_input: dict[str, object]) -> dict[str, Any]:
        tool_input["account_id"] = "rewritten"
        return _ok({})

    observer = ObservingDispatch(mutating)
    observer(_tool("get_account"), {"account_id": "acc-1"})
    assert observer.tool_arguments[0].arguments == {"account_id": "acc-1"}


def test_truncation_is_reported_and_its_disclosure_tracked() -> None:
    disclosed = _observe(
        _ok({"items": [], "truncated": True, "examined_count": 50}),
        calls=[("search_accounts", {"q": "a"})],
    ).outcome(tenant="t", budget_exhausted=False)
    assert disclosed.truncated is True
    plain = _observe(_ok({"items": []}), calls=[("search_accounts", {"q": "a"})])
    assert plain.outcome(tenant="t", budget_exhausted=False).truncated is False


def test_a_proposal_is_recorded_with_its_label() -> None:
    proposal = {
        "proposal": {
            "proposal_id": "prop-1",
            "target_table": "opportunities",
            "after_payload": {"description": "renewal"},
        }
    }
    outcome = _observe(_ok(proposal), calls=[("submit_proposal", {})]).outcome(
        tenant="t", budget_exhausted=False
    )
    assert outcome.proposals == (ObservedProposal(proposal_id="prop-1"),)
    assert "proposal_instead_of_direct_commit" in outcome.observed_outcomes


def test_a_direct_update_of_a_high_risk_field_is_labelled() -> None:
    outcome = _observe(
        _ok({}),
        calls=[("update_opportunity", {"opportunity_id": "o", "fields": {"stage": "closed_won"}})],
    ).outcome(tenant="t", budget_exhausted=False)
    assert "direct_commit_of_high_risk_field" in outcome.observed_outcomes


# --- observe_turn ------------------------------------------------------------------------------


class _Throttled(ModelProvider):
    """A stub that records one exclusion per stream call, as the live retrying wrapper does."""

    def __init__(self) -> None:
        self._inner = StubProvider()
        self._pending: list[Exclusion] = []

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self._pending.append(
            Exclusion(
                invocation_id=f"inv-{len(self._pending)}", category=ExclusionCategory.THROTTLE
            )
        )
        return self._inner.stream(invocation, messages, tools)

    def drain_exclusions(self) -> list[Exclusion]:
        drained, self._pending = self._pending, []
        return drained


def _observed(provider: ModelProvider, *, ticks: list[float] | None = None) -> ObservedTurn:
    clock_values = iter(ticks or [])
    return asyncio.run(
        observe_turn(
            prompt="which accounts slipped?",
            task_id="gt-unit",
            tenant="eval-unit",
            providers=lambda _family: provider,
            matrix=MATRIX,
            tuning=arm_turn_tuning(ArmSpec(family="anthropic")),
            dispatch=lambda tool, _input: _ok({"items": [], "tool": tool.name}),
            turn_clock=(lambda: next(clock_values)) if ticks else None,
        )
    )


def test_observe_turn_counts_stream_calls_and_drains_exclusions() -> None:
    turn = _observed(_Throttled())
    assert turn.stream_calls >= 1
    assert len(turn.exclusions) == turn.stream_calls
    assert {e.category for e in turn.exclusions} == {ExclusionCategory.THROTTLE}
    assert turn.outcome.tenant == "eval-unit"
    assert turn.invocations and all(inv.tier == "supervisor" for inv in turn.invocations)


def test_observe_turn_measures_latency_only_with_a_clock() -> None:
    assert _observed(StubProvider()).turn_latency_ms is None
    assert _observed(StubProvider(), ticks=[10.0, 10.25]).turn_latency_ms == pytest.approx(250.0)


def test_observe_turn_reaches_each_tier_familys_provider() -> None:
    asked: list[Family] = []

    def providers(family: Family) -> ModelProvider:
        asked.append(family)
        return StubProvider()

    asyncio.run(
        observe_turn(
            prompt="p",
            task_id="gt-unit",
            tenant="eval-unit",
            providers=providers,
            matrix=MATRIX,
            tuning=arm_turn_tuning(ArmSpec(family="openai")),
            dispatch=lambda _t, _i: _ok({}),
        )
    )
    assert asked and set(asked) == {"openai"}


class _Scripted(ModelProvider):
    """Replays one event list per ``stream`` call in order, repeating the last."""

    def __init__(self, rounds: list[list[ProviderEvent]]) -> None:
        self._rounds = rounds
        self._calls = 0

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        index = min(self._calls, len(self._rounds) - 1)
        self._calls += 1
        for event in self._rounds[index]:
            yield event


def _usage() -> ProviderEvent:
    return ProviderEvent(type="usage", input_tokens=4, output_tokens=2)


def _call(name: str, tool_input: dict[str, object], *, awaiting: bool) -> list[ProviderEvent]:
    return [
        ProviderEvent(type="tool_call", tool_name=name, tool_input=tool_input),
        _usage(),
        ProviderEvent(type="completed", awaiting_tools=awaiting),
    ]


_DELEGATE = _call("research", {"intent": "look up opp-1"}, awaiting=True)
_LEAF = _call("get_opportunity", {"opportunity_id": "opp-1"}, awaiting=False)
_LEAF_THEN_ANOTHER_ROUND = _call("get_opportunity", {"opportunity_id": "opp-1"}, awaiting=True)
_ACTOR_CALLS = _call("get_opportunity", {"opportunity_id": "opp-1"}, awaiting=False)
_ACTOR_NO_CALL = [
    ProviderEvent(type="text_delta", text="I cannot"),
    _usage(),
    ProviderEvent(type="completed", text="I cannot"),
]
_ACTOR_ERRORS = [ProviderEvent(type="error", error_message="model error"), _usage()]
_DONE = [
    ProviderEvent(type="text_delta", text="done"),
    _usage(),
    ProviderEvent(type="completed", text="done"),
]


def _delegated_turn(*rounds: list[ProviderEvent]) -> tuple[ObservedTurn, list[str]]:
    dispatched: list[str] = []
    provider = _Scripted(list(rounds))

    def dispatch(tool: RegisteredTool, _input: dict[str, object]) -> dict[str, Any]:
        dispatched.append(tool.name)
        return _ok({"opportunity": {"opportunity_id": "opp-1", "name": "Renewal"}})

    turn = asyncio.run(
        observe_turn(
            prompt="look up opp-1",
            task_id="gt-unit",
            tenant="eval-unit",
            providers=lambda _family: provider,
            matrix=MATRIX,
            tuning=arm_turn_tuning(ArmSpec(family="anthropic")),
            dispatch=dispatch,
        )
    )
    return turn, dispatched


def test_control_a_tool_tier_call_that_dispatches_reports_data() -> None:
    turn, dispatched = _delegated_turn(_DELEGATE, _LEAF, _ACTOR_CALLS, _DONE)
    assert dispatched == ["get_opportunity"]
    assert turn.outcome.denial_outcome == "data"


@pytest.mark.parametrize("actor_round", [_ACTOR_NO_CALL, _ACTOR_ERRORS], ids=["no_call", "error"])
def test_a_tool_tier_round_that_dispatches_nothing_reports_tool_failure(
    actor_round: list[ProviderEvent],
) -> None:
    turn, dispatched = _delegated_turn(_DELEGATE, _LEAF, actor_round, _DONE)
    assert dispatched == []
    assert [call.outcome_kind for call in turn.tool_calls] == ["tool_failure"]
    assert turn.outcome.denial_outcome == "tool_failure"


def test_an_undispatched_tool_tier_failure_retried_into_data_reports_data() -> None:
    turn, dispatched = _delegated_turn(
        _DELEGATE, _LEAF_THEN_ANOTHER_ROUND, _ACTOR_NO_CALL, _LEAF, _ACTOR_CALLS, _DONE
    )
    assert dispatched == ["get_opportunity"]
    assert [call.outcome_kind for call in turn.tool_calls] == ["tool_failure", "data"]
    assert turn.outcome.denial_outcome == "data"


def test_records_missing_a_dispatched_call_fall_back_to_dispatch_order_without_raising() -> None:
    observer = _observe(
        _failure(),
        _ok({"opportunity": {"opportunity_id": "opp-1"}}),
        calls=[("get_opportunity", {}), ("get_opportunity", {})],
    )
    records = [
        ToolCallAttribution(
            tool_name="get_opportunity",
            role="research",
            latency_ms=None,
            outcome_kind="data",
            truncated=False,
        )
    ]
    outcome = observer.outcome(tenant="t", budget_exhausted=False, tool_calls=records)
    assert outcome.denial_outcome == "tool_failure"


# --- serialise / deserialise -------------------------------------------------------------------


def _rich_turn() -> ObservedTurn:
    return ObservedTurn(
        outcome=TurnOutcome(
            tenant="eval-abc",
            tool_calls=(
                ToolCall("get_account", None, "acc-1"),
                ToolCall("search_accounts", "accounts"),
            ),
            proposals=(ObservedProposal("prop-1"),),
            denial_outcome="denied",
            denial_missing_scope="account:acc-9",
            budget_exhausted=True,
            truncated=True,
            truncation_disclosed=False,
            observed_outcomes=frozenset({"z_label", "a_label"}),
        ),
        tool_arguments=(
            ObservedToolArguments("get_account", {"account_id": "acc-1", "n": [1, 2]}),
        ),
        invocations=(
            InvocationAttribution(
                tier="supervisor",
                role="supervisor",
                model_id="anthropic.claude-opus-5-5",
                tokens=TokenCounts(input_tokens=10, output_tokens=3),
                cache=CacheCounts(read_tokens=2, write_tokens=1),
                latency_ms=12.5,
            ),
            InvocationAttribution(
                tier="tool",
                role="research",
                model_id="",
                tokens=TokenCounts(input_tokens=0, output_tokens=0),
                cache=CacheCounts(),
            ),
        ),
        tool_calls=(
            ToolCallAttribution(
                tool_name="get_account",
                role="research",
                latency_ms=None,
                outcome_kind="denied",
                truncated=False,
            ),
        ),
        stream_calls=3,
        exclusions=(Exclusion("inv-1", ExclusionCategory.RETRY, "attempt 2"),),
        turn_latency_ms=1234.5,
    )


def test_a_rich_turn_round_trips_exactly() -> None:
    turn = _rich_turn()
    assert deserialise_turn(serialise_turn(turn)) == turn


def test_a_real_observed_turn_round_trips_through_strict_json() -> None:
    turn = _observed(_Throttled(), ticks=[1.0, 2.0])
    wire = json.loads(json.dumps(serialise_turn(turn), allow_nan=False))
    assert deserialise_turn(wire) == turn


def test_the_wire_form_is_versioned_and_sorts_labels() -> None:
    wire = serialise_turn(_rich_turn())
    assert wire["schema"] == SERIALISED_TURN_SCHEMA
    assert wire["outcome"]["observed_outcomes"] == ["a_label", "z_label"]
    assert set(wire) == {
        "schema",
        "outcome",
        "tool_arguments",
        "records",
        "stream_calls",
        "exclusions",
        "turn_latency_ms",
    }


def _mutated(path: tuple[object, ...], value: object, *, delete: bool = False) -> dict[str, Any]:
    wire = copy.deepcopy(serialise_turn(_rich_turn()))
    cursor: Any = wire
    for key in path[:-1]:
        cursor = cursor[key]
    if delete:
        del cursor[path[-1]]
    else:
        cursor[path[-1]] = value
    return wire


@pytest.mark.parametrize(
    ("path", "value", "delete"),
    [
        (("schema",), 3, False),
        (("schema",), True, False),
        (("outcome",), None, True),
        (("extra",), 1, False),
        (("outcome", "budget_exhausted"), "yes", False),
        (("outcome", "tool_calls", 0, "tool_name"), 5, False),
        (("outcome", "observed_outcomes"), [1], False),
        (("outcome", "proposals", 0, "surplus"), "x", False),
        (("tool_arguments", 0, "arguments"), "not-an-object", False),
        (("records", "invocations", 0, "tier"), "orchestrator", False),
        (("records", "invocations", 0, "tokens", "input_tokens"), -1, False),
        (("records", "invocations", 0, "tokens", "input_tokens"), 1.5, False),
        (("records", "tool_calls", 0, "outcome_kind"), "maybe", False),
        (("stream_calls",), -1, False),
        (("stream_calls",), True, False),
        (("exclusions", 0, "category"), "meteor", False),
        (("exclusions", 0, "invocation_id"), "", False),
        (("turn_latency_ms",), "fast", False),
    ],
    ids=lambda value: str(value),
)
def test_deserialise_refuses_a_malformed_payload(
    path: tuple[object, ...], value: object, delete: bool
) -> None:
    with pytest.raises(ValueError):
        deserialise_turn(_mutated(path, value, delete=delete))


def test_the_writer_emits_schema_2() -> None:
    assert serialise_turn(_rich_turn())["schema"] == 2


def test_a_schema_1_body_from_a_skewed_runtime_is_refused_with_a_named_mismatch() -> None:
    """An older Runtime body (schema 1, same shape) fails as a version skew."""
    stale = _mutated(("schema",), 1)
    with pytest.raises(ValueError) as refused:
        deserialise_turn(stale)
    assert str(refused.value) == "turn.schema must be 2, got 1"


def test_deserialise_refuses_a_non_object() -> None:
    with pytest.raises(ValueError):
        deserialise_turn([])  # type: ignore[arg-type]


# --- usage completeness ------------------------------------------------------------------------


class _StallsAfterFirstRound(ModelProvider):
    """The supervisor calls one tool and asks for another round, which never answers."""

    def __init__(self) -> None:
        self.calls = 0

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.calls += 1
        return self._first() if self.calls == 1 else self._stall()

    async def _first(self) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(
            type="tool_call", tool_name="search_accounts", tool_input={}, tool_id="t1"
        )
        yield ProviderEvent(type="usage", input_tokens=5, output_tokens=1)
        yield ProviderEvent(type="completed", awaiting_tools=True)

    async def _stall(self) -> AsyncIterator[ProviderEvent]:
        await asyncio.sleep(3600)
        yield ProviderEvent(type="completed", awaiting_tools=False)


def test_a_turn_with_a_call_cut_at_the_deadline_is_observed_as_a_usage_lower_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observation carries the cut: its recorded usage under-counts the turn."""
    monkeypatch.setattr(
        turn_observation,
        "EVAL_BUDGET",
        TurnBudgetSpec(max_tool_calls=EVAL_BUDGET.max_tool_calls, max_wall_clock_seconds=0.2),
    )
    turn = asyncio.run(asyncio.wait_for(_observed_async(_StallsAfterFirstRound()), timeout=5.0))
    assert turn.outcome.budget_exhausted is True
    assert turn.usage_lower_bound is True
    wire = serialise_turn(turn)
    assert wire[USAGE_LOWER_BOUND_KEY] is True
    assert deserialise_turn(json.loads(json.dumps(wire))) == turn


def test_a_turn_that_reported_all_its_usage_is_not_a_lower_bound_and_omits_the_key() -> None:
    """A complete turn serialises exactly as before the key existed."""
    turn = _observed(StubProvider())
    assert turn.usage_lower_bound is False
    assert USAGE_LOWER_BOUND_KEY not in serialise_turn(turn)


def test_a_wire_turn_without_the_key_reads_as_complete_usage() -> None:
    """An observation from a Runtime that predates the key decodes with the flag false."""
    wire = serialise_turn(_rich_turn())
    assert USAGE_LOWER_BOUND_KEY not in wire
    assert deserialise_turn(wire).usage_lower_bound is False


def test_a_non_boolean_usage_lower_bound_is_refused() -> None:
    """The optional key is still strictly typed."""
    wire = serialise_turn(_rich_turn())
    wire[USAGE_LOWER_BOUND_KEY] = "yes"
    with pytest.raises(ValueError, match=USAGE_LOWER_BOUND_KEY):
        deserialise_turn(wire)


async def _observed_async(provider: ModelProvider) -> ObservedTurn:
    return await observe_turn(
        prompt="which accounts slipped?",
        task_id="gt-unit",
        tenant="eval-unit",
        providers=lambda _family: provider,
        matrix=MATRIX,
        tuning=arm_turn_tuning(ArmSpec(family="anthropic")),
        dispatch=lambda tool, _input: _ok({"items": [], "tool": tool.name}),
    )

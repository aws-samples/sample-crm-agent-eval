# SPDX-License-Identifier: MIT
"""Tests for the supervisor contract: routing, outcome discipline, truncation, proposals, tuning.

Runs against :class:`providers.stub.StubProvider` and hand-built payloads; no model or AWS call.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from agents.budget import TurnBudget
from agents.events import AgentEvent
from agents.registry import tool_registry_for
from agents.subagents import SUBAGENT_SPECS_BY_ROLE
from agents.supervisor import (
    DIRECT_TOOL_NAMES,
    SUPERVISOR_ROLE,
    SUPERVISOR_TIER,
    SubagentOutcome,
    SupervisorTurn,
    TextAccumulator,
    classify_tool_result,
    describe_truncation,
    page_context_briefing,
    proposal_event_for,
    provider_event_to_agent_event,
    synthesize_reply,
)
from crm.errors import NotFound, wrap_aws_error
from crm.toolspec.page_context import (
    BY_KIND,
    BoardPageContext,
    EntityPageContext,
    ListPageContext,
    OpaquePageContext,
    PageContext,
)
from providers.base import ProviderEvent, Tuning
from providers.capabilities import load_capability_matrix
from providers.stub import StubProvider

REPO_ROOT = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------------------
# Direct tools for high-frequency reads
# --------------------------------------------------------------------------------------


def test_direct_tool_names_is_exactly_the_toolspecs_full_supervisor_grant() -> None:
    """`DIRECT_TOOL_NAMES` equals the toolspec-generated registry's grant for the supervisor role."""
    expected = {tool.name for tool in tool_registry_for(SUPERVISOR_ROLE)}
    assert expected == DIRECT_TOOL_NAMES
    assert {"search_opportunities", "get_opportunity"} <= DIRECT_TOOL_NAMES


def test_direct_tool_names_reacts_to_a_registry_change_rather_than_being_memoised_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reloading `agents.supervisor` over a patched `ALL_TOOLS` drops the removed tool.

    The module is reloaded against the real toolspec afterwards so later tests see the real one.
    """
    import importlib

    from crm.toolspec import definitions as toolspec_definitions

    original_all_tools = toolspec_definitions.ALL_TOOLS
    narrowed_tools = tuple(tool for tool in original_all_tools if tool.name != "pipeline_aggregate")
    assert len(narrowed_tools) == len(original_all_tools) - 1, (
        "fixture assumption: pipeline_aggregate must exist in ALL_TOOLS for this test to be "
        "meaningful"
    )

    import agents.supervisor as supervisor_module

    monkeypatch.setattr(toolspec_definitions, "ALL_TOOLS", narrowed_tools)
    try:
        reloaded_supervisor = importlib.reload(supervisor_module)
        assert "pipeline_aggregate" not in reloaded_supervisor.DIRECT_TOOL_NAMES
    finally:
        monkeypatch.setattr(toolspec_definitions, "ALL_TOOLS", original_all_tools)
        importlib.reload(supervisor_module)


def test_supervisor_turn_direct_tool_names_matches_the_declared_constant() -> None:
    """A live turn's direct tool names equal the module constant, not a hand-copied subset."""
    turn = _build_turn()
    assert turn.direct_tool_names() == DIRECT_TOOL_NAMES


def test_direct_tools_are_sourced_from_the_generated_registry_for_the_supervisor_role() -> None:
    """Every direct tool name is granted to the supervisor role by the toolspec-derived registry."""
    supervisor_registry_names = {tool.name for tool in tool_registry_for(SUPERVISOR_ROLE)}
    assert supervisor_registry_names >= DIRECT_TOOL_NAMES


def test_supervisor_dispatchable_subagent_roles_are_exactly_the_four_subagents() -> None:
    """The supervisor may delegate to exactly the four subagents, by role name."""
    turn = _build_turn()
    assert turn.dispatchable_subagent_roles() == frozenset(SUBAGENT_SPECS_BY_ROLE)
    assert turn.dispatchable_subagent_roles() == {"research", "mutation", "analytics", "composer"}


def test_direct_tool_names_and_subagent_roles_are_disjoint() -> None:
    """No direct tool name collides with a subagent role name."""
    turn = _build_turn()
    assert DIRECT_TOOL_NAMES.isdisjoint(turn.dispatchable_subagent_roles())


# --------------------------------------------------------------------------------------
# The three-way (plus no-match) outcome discipline is structural
# --------------------------------------------------------------------------------------


def test_subagent_outcome_kind_is_closed_to_the_four_named_kinds() -> None:
    with pytest.raises(ValueError, match="unrecognised subagent outcome kind"):
        SubagentOutcome(kind="fabricated")  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["success", "not_found", "DENIED", ""])
def test_every_fabricated_outcome_kind_is_rejected(kind: str) -> None:
    with pytest.raises(ValueError, match="unrecognised subagent outcome kind"):
        SubagentOutcome(kind=kind)  # type: ignore[arg-type]


def test_data_outcome_requires_data_and_forbids_denial_and_failure_fields() -> None:
    outcome = SubagentOutcome(kind="data", data={"opportunity_id": "opp-1"})
    assert outcome.data == {"opportunity_id": "opp-1"}
    with pytest.raises(ValueError, match="data outcome requires data"):
        SubagentOutcome(kind="data")
    with pytest.raises(ValueError, match="must not be set"):
        SubagentOutcome(kind="data", data={"a": 1}, missing_scope="account:a1")


def test_no_match_outcome_forbids_every_kind_specific_field() -> None:
    outcome = SubagentOutcome(kind="no_match")
    assert outcome.data is None
    with pytest.raises(ValueError, match="must not be set"):
        SubagentOutcome(kind="no_match", data={"a": 1})
    with pytest.raises(ValueError, match="must not be set"):
        SubagentOutcome(kind="no_match", error_message="boom")


def test_denied_outcome_requires_missing_scope_and_message_forbids_data() -> None:
    outcome = SubagentOutcome(
        kind="denied", missing_scope="account:acct-1", denial_message="You do not have access."
    )
    assert outcome.missing_scope == "account:acct-1"
    with pytest.raises(ValueError, match="requires a non-empty missing_scope"):
        SubagentOutcome(kind="denied", denial_message="You do not have access.")
    with pytest.raises(ValueError, match="requires denial_message"):
        SubagentOutcome(kind="denied", missing_scope="account:acct-1")
    with pytest.raises(ValueError, match="must not be set"):
        SubagentOutcome(
            kind="denied",
            missing_scope="account:acct-1",
            denial_message="denied",
            data={"leaked": "data"},
        )


def test_tool_failure_outcome_requires_error_message_and_forbids_data() -> None:
    outcome = SubagentOutcome(kind="tool_failure", error_message="The service is busy.")
    assert outcome.error_message == "The service is busy."
    with pytest.raises(ValueError, match="requires error_message"):
        SubagentOutcome(kind="tool_failure")
    with pytest.raises(ValueError, match="must not be set"):
        SubagentOutcome(kind="tool_failure", error_message="boom", data={"leaked": "data"})


def test_classify_tool_result_recognises_scope_denial_and_names_the_missing_scope() -> None:
    """A ``ScopeDenied`` response classifies as ``denied`` and names the missing scope."""
    raw = {
        "statusCode": 403,
        "error": {
            "code": "scope_denied",
            "message": "You do not have access to that account.",
            "missing_scope": "account:acct-99",
        },
    }
    outcome = classify_tool_result(raw)
    assert outcome.kind == "denied"
    assert outcome.missing_scope == "account:acct-99"
    assert outcome.denial_message == "You do not have access to that account."
    assert outcome.data is None


def test_classify_tool_result_recognises_a_non_denial_error_as_tool_failure() -> None:
    """A ``Conflict`` error classifies as ``tool_failure``, distinct from a scope denial."""
    raw = {
        "statusCode": 409,
        "error": {"code": "conflict", "message": "The record changed before this could apply."},
    }
    outcome = classify_tool_result(raw)
    assert outcome.kind == "tool_failure"
    assert outcome.error_message == "The record changed before this could apply."
    assert outcome.missing_scope is None


def test_classify_tool_result_defaults_a_scope_denial_without_missing_scope_to_unknown() -> None:
    outcome = classify_tool_result({"error": {"code": "scope_denied", "message": "denied"}})
    assert outcome.kind == "denied"
    assert outcome.missing_scope == "unknown"


def test_classify_tool_result_recognises_not_found_as_no_match() -> None:
    """A ``not_found`` error is an absence, not a tool failure."""
    raw = {
        "statusCode": 404,
        "error": {"code": "not_found", "message": "That record does not exist."},
    }
    outcome = classify_tool_result(raw)
    assert outcome.kind == "no_match"
    assert outcome.error_message is None
    assert outcome.missing_scope is None
    assert outcome.data is None
    assert synthesize_reply(outcome) == "No matching records were found."


def test_classify_tool_result_recognises_contact_departed_as_denied_naming_status_active() -> None:
    """A ``contact_departed`` refusal is a policy denial naming ``status:active``."""
    message = "That contact has left the company. Reactivate them or choose another contact."
    raw = {
        "statusCode": 409,
        "error": {
            "code": "contact_departed",
            "message": message,
            "missing_scope": "status:active",
        },
    }
    outcome = classify_tool_result(raw)
    assert outcome.kind == "denied"
    assert outcome.missing_scope == "status:active"
    assert outcome.denial_message == message
    assert outcome.error_message is None
    assert synthesize_reply(outcome) == f"{message} (missing scope: status:active)"


def test_classify_tool_result_defaults_contact_departed_scope_to_status_active() -> None:
    outcome = classify_tool_result({"error": {"code": "contact_departed", "message": "departed"}})
    assert outcome.kind == "denied"
    assert outcome.missing_scope == "status:active"


@pytest.mark.parametrize("code", ["internal_error", "validation_failed", "made_up_code", None])
def test_classify_tool_result_treats_any_other_error_code_as_tool_failure(code: str | None) -> None:
    outcome = classify_tool_result({"error": {"code": code, "message": "nope"}})
    assert outcome.kind == "tool_failure"
    assert outcome.error_message == "nope"


@pytest.mark.parametrize("code", [["scope_denied"], {"code": "not_found"}, 404])
def test_classify_tool_result_treats_a_non_string_code_as_tool_failure(code: object) -> None:
    """A malformed error block classifies as ``tool_failure`` rather than raising."""
    outcome = classify_tool_result({"error": {"code": code, "message": "nope"}})
    assert outcome.kind == "tool_failure"
    assert outcome.error_message == "nope"


def test_classify_tool_result_reports_a_missing_table_as_tool_failure() -> None:
    """A wrapped DynamoDB ``ResourceNotFoundException`` is a fault, never ``no_match``."""
    client_error = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "table absent"}}, "Query"
    )
    payload = wrap_aws_error(client_error, operation="query").to_api_response()
    assert classify_tool_result(payload).kind == "tool_failure"


def test_classify_tool_result_reports_a_rendered_record_not_found_as_no_match() -> None:
    payload = NotFound("That record does not exist.").to_api_response()
    assert classify_tool_result(payload).kind == "no_match"


def test_classify_tool_result_recognises_a_successful_empty_list_as_no_match() -> None:
    """An empty ``items`` list on a successful call is ``no_match``."""
    raw = {"statusCode": 200, "body": {"items": []}}
    outcome = classify_tool_result(raw)
    assert outcome.kind == "no_match"
    assert outcome.data is None


def test_classify_tool_result_recognises_a_nonempty_payload_as_data() -> None:
    raw = {"statusCode": 200, "body": {"opportunity_id": "opp-1", "name": "Acme deal"}}
    outcome = classify_tool_result(raw)
    assert outcome.kind == "data"
    assert outcome.data == {"opportunity_id": "opp-1", "name": "Acme deal"}


def test_classify_tool_result_handles_an_unwrapped_body_the_same_way() -> None:
    """A tool result without a ``body`` wrapper (e.g. a stub-provider payload) still classifies
    correctly by treating the payload itself as the body."""
    raw: dict[str, object] = {"opportunity_id": "opp-1"}
    outcome = classify_tool_result(raw)
    assert outcome.kind == "data"
    assert outcome.data == {"opportunity_id": "opp-1"}


def test_classify_tool_result_never_produces_data_alongside_denial_or_failure() -> None:
    """Neither denial nor failure payloads ever yield a populated ``data`` field."""
    denial = classify_tool_result(
        {"error": {"code": "scope_denied", "message": "denied", "missing_scope": "account:a1"}}
    )
    failure = classify_tool_result({"error": {"code": "conflict", "message": "stale"}})
    assert denial.data is None
    assert failure.data is None


# --------------------------------------------------------------------------------------
# synthesize_reply: denial and failure are reported verbatim, never papered over
# --------------------------------------------------------------------------------------


def test_synthesize_reply_for_denied_names_the_missing_scope_and_the_message() -> None:
    outcome = SubagentOutcome(
        kind="denied",
        missing_scope="account:acct-42",
        denial_message="You do not have access to that account.",
    )
    reply = synthesize_reply(outcome)
    assert "account:acct-42" in reply
    assert "You do not have access to that account." in reply


def test_synthesize_reply_for_tool_failure_carries_only_the_user_safe_message() -> None:
    outcome = SubagentOutcome(
        kind="tool_failure", error_message="The service is busy. Retry shortly."
    )
    reply = synthesize_reply(outcome)
    assert "The service is busy. Retry shortly." in reply


def test_synthesize_reply_for_no_match_states_plainly_that_nothing_matched() -> None:
    outcome = SubagentOutcome(kind="no_match")
    reply = synthesize_reply(outcome)
    assert "no matching records" in reply.lower()


def test_synthesize_reply_for_data_does_not_echo_denial_or_failure_language() -> None:
    outcome = SubagentOutcome(kind="data", data={"opportunity_id": "opp-1"})
    reply = synthesize_reply(outcome)
    assert "denied" not in reply.lower()
    assert "could not be completed" not in reply.lower()


def test_synthesize_reply_appends_a_supplied_truncation_notice_verbatim() -> None:
    outcome = SubagentOutcome(kind="data", data={"opportunity_id": "opp-1"})
    reply = synthesize_reply(outcome, truncation_notice="Search stopped early after 2000 records.")
    assert "Search stopped early after 2000 records." in reply


@pytest.mark.parametrize(
    "kind,kwargs",
    [
        ("denied", {"missing_scope": "account:a1", "denial_message": "denied"}),
        ("tool_failure", {"error_message": "boom"}),
        ("no_match", {}),
        ("data", {"data": {"a": 1}}),
    ],
)
def test_synthesize_reply_never_raises_for_any_valid_outcome_kind(
    kind: str, kwargs: dict[str, object]
) -> None:
    """Every legally constructible outcome renders to text without error."""
    outcome = SubagentOutcome(kind=kind, **kwargs)  # type: ignore[arg-type]
    assert isinstance(synthesize_reply(outcome), str)


# --------------------------------------------------------------------------------------
# Truncation is surfaced explicitly, from both signal sources
# --------------------------------------------------------------------------------------


def test_describe_truncation_reports_the_search_ceiling_signal_with_examined_count() -> None:
    tool_output: dict[str, object] = {
        "body": {"items": [], "truncated": True, "examined_count": 2000}
    }
    notice = describe_truncation(tool_output, event_truncated=False)
    assert notice is not None
    assert "2000" in notice


def test_describe_truncation_reports_the_byte_ceiling_signal_independently() -> None:
    tool_output: dict[str, object] = {"body": {"items": [{"a": 1}]}}
    notice = describe_truncation(tool_output, event_truncated=True)
    assert notice is not None
    assert "too large" in notice.lower()


def test_describe_truncation_returns_none_when_neither_signal_fired() -> None:
    tool_output: dict[str, object] = {
        "body": {"items": [{"a": 1}], "truncated": False, "examined_count": 3}
    }
    assert describe_truncation(tool_output, event_truncated=False) is None


def test_describe_truncation_prefers_reporting_both_independently_not_exclusively() -> None:
    """The search-ceiling message is reported even when the event-level flag is also set."""
    tool_output: dict[str, object] = {
        "body": {"items": [], "truncated": True, "examined_count": 2000}
    }
    notice = describe_truncation(tool_output, event_truncated=True)
    assert notice is not None
    assert "2000" in notice


# --------------------------------------------------------------------------------------
# A high-risk write surfaces as a structured proposal event, never prose
# --------------------------------------------------------------------------------------


def test_proposal_event_for_renders_a_proposal_kind_event_with_before_after() -> None:
    tool_output: dict[str, object] = {
        "body": {
            "proposal": {
                "proposal_id": "prop-1",
                "before_snapshot": {"stage": "Discovery"},
                "after_payload": {"stage": "Negotiation"},
                "reason": "Customer confirmed budget.",
                "target_table": "opportunities",
                "target_id": "opp-1",
            }
        }
    }
    event = proposal_event_for(tool_output)
    assert event.kind == "proposal"
    assert event.proposal_id == "prop-1"
    assert event.proposal_summary is not None
    assert event.proposal_summary["before"] == {"stage": "Discovery"}
    assert event.proposal_summary["after"] == {"stage": "Negotiation"}


def test_proposal_event_for_raises_rather_than_inventing_a_placeholder_when_malformed() -> None:
    with pytest.raises(ValueError, match="does not carry a proposal"):
        proposal_event_for({"body": {}})
    with pytest.raises(ValueError, match="missing a non-empty proposal_id"):
        proposal_event_for({"body": {"proposal": {"before_snapshot": {}}}})


def test_the_agent_event_schema_has_no_kind_by_which_a_high_risk_write_could_be_confirmed_in_prose() -> (
    None
):
    """Exactly one event kind carries proposal content, and it is not ``completed``."""
    from agents.events import EVENT_KINDS

    assert "proposal" in EVENT_KINDS
    proposal_event = AgentEvent(
        kind="proposal", proposal_id="prop-1", proposal_summary={"before": {}, "after": {}}
    )
    assert proposal_event.kind != "completed"
    assert proposal_event.text is None


# --------------------------------------------------------------------------------------
# Budget exhaustion is terminal and reported, naming partial work
# --------------------------------------------------------------------------------------


def test_supervisor_turn_reports_exhaustion_naming_completed_actions() -> None:
    turn = _build_turn(budget=TurnBudget(max_tool_calls=2))
    turn.record_tool_call("searched opportunities for 'Acme'")
    turn.record_tool_call("fetched opportunity opp-1")
    assert turn.is_exhausted()
    event = turn.exhausted_event()
    assert event.kind == "error"
    assert event.error_kind == "budget_exhausted"
    assert event.partial_work is not None
    assert "searched opportunities for 'Acme'" in event.partial_work
    assert "fetched opportunity opp-1" in event.partial_work


def test_supervisor_turn_exhaustion_with_no_completed_actions_states_that_explicitly() -> None:
    turn = _build_turn(budget=TurnBudget(max_tokens=1))
    turn.record_tokens(5)
    assert turn.is_exhausted()
    event = turn.exhausted_event()
    assert event.partial_work == "no tool calls completed"


def test_supervisor_turn_refuses_to_report_exhaustion_before_it_actually_happened() -> None:
    """A turn nowhere near its budget cannot be made to report a fabricated terminal state."""
    turn = _build_turn()
    assert not turn.is_exhausted()
    with pytest.raises(RuntimeError, match="before the budget was actually exhausted"):
        turn.exhausted_event()


# --------------------------------------------------------------------------------------
# Mid-conversation retuning applies to the next turn only
# --------------------------------------------------------------------------------------


def test_a_turn_built_without_an_override_uses_the_matrix_default_not_a_hardcoded_value() -> None:
    turn = _build_turn()
    tuning = turn.effective_tuning()
    assert tuning.effort is None
    assert tuning.family is None


def test_a_turn_built_with_an_override_uses_exactly_that_override() -> None:
    override = Tuning(source="ui.settings.tuning.effort", effort="high")
    turn = _build_turn(tuning_override=override)
    assert turn.effective_tuning() is override


def test_retuning_one_turn_does_not_affect_a_freshly_built_sibling_turn() -> None:
    """Two turns sharing a matrix and provider differ only by their own tuning argument."""
    matrix = load_capability_matrix()
    provider = StubProvider()

    retuned = SupervisorTurn(
        provider=provider,
        matrix=matrix,
        budget=TurnBudget(),
        tuning_override=Tuning(source="ui.settings.tuning.effort", effort="high"),
    )
    default_turn = SupervisorTurn(provider=provider, matrix=matrix, budget=TurnBudget())

    assert retuned.effective_tuning().effort == "high"
    assert default_turn.effective_tuning().effort is None


def test_retuning_does_not_mutate_the_shared_capability_matrix() -> None:
    """Constructing a retuned turn leaves the shared capability matrix unchanged."""
    matrix = load_capability_matrix()
    before = dict(matrix.tiers[matrix.default_family]["supervisor"].tuning)

    _build_turn(
        matrix=matrix, tuning_override=Tuning(source="ui.settings.tuning.effort", effort="high")
    )

    after = dict(matrix.tiers[matrix.default_family]["supervisor"].tuning)
    assert before == after


def test_resolve_invocation_honours_a_valid_per_turn_override_against_the_stub_provider() -> None:
    """A turn's resolved invocation reflects a valid effort override against the real matrix."""
    matrix = load_capability_matrix()
    model = matrix.model_for(matrix.default_family, SUPERVISOR_TIER)
    assert model.effort.supported, (
        f"the resolved supervisor model {model.model_id!r} does not accept an effort override; "
        "this test's override-honoured assertion below would be vacuous without it"
    )
    accepted_effort = model.effort.values[0]

    turn = _build_turn(
        matrix=matrix,
        tuning_override=Tuning(source="ui.settings.tuning.effort", effort=accepted_effort),
    )
    resolved = turn.resolve_invocation()
    assert resolved.effort == accepted_effort
    assert resolved.model.model_id == model.model_id


# --------------------------------------------------------------------------------------
# provider_event_to_agent_event: the seam-to-closed-set mapping never invents required fields
# --------------------------------------------------------------------------------------


def test_provider_event_to_agent_event_folds_text_delta_into_none() -> None:
    # Non-streaming caller (no stream_event_id): text_delta folds into completed, so maps to None.
    assert provider_event_to_agent_event(ProviderEvent(type="text_delta", text="partial")) is None


def test_provider_event_to_agent_event_maps_text_delta_to_a_frame_when_streaming() -> None:
    # Streaming caller passes a turn-stable event id: the delta becomes a partial=True frame.
    event = provider_event_to_agent_event(
        ProviderEvent(type="text_delta", text="hello "), stream_event_id="ans-1"
    )
    assert event is not None
    assert event.kind == "text_delta"
    assert event.text == "hello "
    assert event.event_id == "ans-1"
    assert event.partial is True


def test_provider_event_to_agent_event_streaming_text_delta_without_text_raises() -> None:
    with pytest.raises(ValueError, match="text_delta provider event missing text"):
        provider_event_to_agent_event(
            ProviderEvent(type="text_delta", text=None), stream_event_id="ans-1"
        )


def test_provider_event_to_agent_event_maps_tool_call_faithfully() -> None:
    event = provider_event_to_agent_event(
        ProviderEvent(type="tool_call", tool_name="get_account", tool_input={"account_id": "a1"})
    )
    assert event is not None
    assert event.kind == "tool_call"
    assert event.tool_name == "get_account"


def test_provider_event_to_agent_event_truncates_an_oversized_tool_result_items_list() -> None:
    big_items = [{"opportunity_id": f"opp-{i}", "padding": "x" * 2000} for i in range(50)]
    event = provider_event_to_agent_event(
        ProviderEvent(type="tool_result", tool_output={"items": big_items})
    )
    assert event is not None
    assert event.kind == "tool_result"
    assert event.truncated is True


def test_provider_event_to_agent_event_maps_usage_and_completed() -> None:
    usage_event = provider_event_to_agent_event(
        ProviderEvent(type="usage", input_tokens=10, output_tokens=5)
    )
    assert usage_event is not None
    assert usage_event.kind == "usage"

    completed_event = provider_event_to_agent_event(
        ProviderEvent(type="completed", text="done", input_tokens=10, output_tokens=5)
    )
    assert completed_event is not None
    assert completed_event.kind == "completed"
    assert completed_event.text == "done"


def test_provider_event_to_agent_event_completed_falls_back_to_accumulated_text() -> None:
    """A provider whose ``completed`` event carries no ``text`` of its own (the shape
    :class:`providers.stub.StubProvider` produces on its text-reply branch) still yields a
    ``completed`` event carrying the text accumulated from preceding ``text_delta`` events."""
    event = provider_event_to_agent_event(
        ProviderEvent(type="completed", input_tokens=10, output_tokens=5),
        accumulated_text="streamed reply content",
    )
    assert event is not None
    assert event.kind == "completed"
    assert event.text == "streamed reply content"


def test_provider_event_to_agent_event_maps_error_to_model_failure() -> None:
    event = provider_event_to_agent_event(ProviderEvent(type="error", error_message="unavailable"))
    assert event is not None
    assert event.kind == "error"
    assert event.error_kind == "model_failure"
    assert event.error_message == "unavailable"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"type": "tool_call"},
        {"type": "tool_result"},
        {"type": "usage", "input_tokens": 1},
        {"type": "completed"},
        {"type": "error"},
    ],
)
def test_provider_event_to_agent_event_never_invents_a_missing_required_field(
    kwargs: dict[str, object],
) -> None:
    """Every provider event type that requires a field raises rather than substituting a
    fabricated placeholder when that field is absent."""
    with pytest.raises(ValueError):
        provider_event_to_agent_event(ProviderEvent(**kwargs))  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# End-to-end against the stub provider: no live model call, no vendor SDK, no AWS call
# --------------------------------------------------------------------------------------


def test_supervisor_turn_streams_against_the_stub_provider_with_no_live_call() -> None:
    """A full turn resolves an invocation and streams events from the stub provider."""

    async def _collect() -> list[AgentEvent]:
        turn = _build_turn()
        resolved = turn.resolve_invocation()
        accumulator = TextAccumulator()
        collected: list[AgentEvent] = []
        # tools=[] forces the stub provider's text-reply branch.
        async for provider_event in turn.provider.stream(resolved, messages=[], tools=[]):
            accumulator.observe(provider_event)
            mapped = provider_event_to_agent_event(
                provider_event, accumulated_text=accumulator.text
            )
            if mapped is not None:
                collected.append(mapped)
        return collected

    events = asyncio.run(_collect())
    assert events
    assert events[-1].kind == "completed"


# --------------------------------------------------------------------------------------
# The page-context briefing hands the model the exact `kind` discriminator token
# --------------------------------------------------------------------------------------

#: One minimal well-formed instance per page-context union variant, keyed by its `kind`.
_BRIEFING_SAMPLES: dict[str, PageContext] = {
    "entity": EntityPageContext(
        route="/opportunities/opp-1", entity_type="opportunities", entity_id="opp-1"
    ),
    "list": ListPageContext(route="/opportunities", entity_type="opportunities"),
    "board": BoardPageContext(route="/pipeline"),
    "opaque": OpaquePageContext(route="/custom", reason="a client-only sort with no server form"),
}


def test_briefing_samples_cover_exactly_the_closed_union() -> None:
    """The briefing samples cover exactly the kinds in `BY_KIND`."""
    assert set(_BRIEFING_SAMPLES) == set(BY_KIND)


@pytest.mark.parametrize("kind", sorted(BY_KIND))
def test_page_context_briefing_states_the_kind_discriminator_token(kind: str) -> None:
    """Every briefing branch names the exact `kind="<token>"` to pass to `get_page_context`."""
    briefing = page_context_briefing(_BRIEFING_SAMPLES[kind])
    assert f'kind="{kind}"' in briefing


# --------------------------------------------------------------------------------------
# Provider seam hygiene: no vendor SDK import anywhere under src/agents/supervisor.py
# --------------------------------------------------------------------------------------

_FORBIDDEN_VENDOR_IMPORT_ROOTS = ("anthropic", "openai", "boto3", "botocore")


def test_supervisor_module_imports_no_vendor_sdk() -> None:
    """`src/agents/supervisor.py` imports no vendor SDK, checked by AST."""
    source = (REPO_ROOT / "src" / "agents" / "supervisor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        names: list[str | None] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module]
        for name in names:
            if not name:
                continue
            root = name.split(".", 1)[0]
            assert root not in _FORBIDDEN_VENDOR_IMPORT_ROOTS, (
                f"supervisor.py imports forbidden vendor module {name!r}"
            )


# --------------------------------------------------------------------------------------
# Test helpers
# --------------------------------------------------------------------------------------


def _build_turn(
    *,
    matrix: object | None = None,
    budget: TurnBudget | None = None,
    tuning_override: Tuning | None = None,
) -> SupervisorTurn:
    """Build a :class:`SupervisorTurn` against the stub provider and the real capability matrix."""
    resolved_matrix = matrix if matrix is not None else load_capability_matrix()
    return SupervisorTurn(
        provider=StubProvider(),
        matrix=resolved_matrix,  # type: ignore[arg-type]
        budget=budget if budget is not None else TurnBudget(),
        tuning_override=tuning_override,
    )

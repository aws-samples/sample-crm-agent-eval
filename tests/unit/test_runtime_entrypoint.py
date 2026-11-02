# SPDX-License-Identifier: MIT
"""Tests for the AgentCore Runtime entrypoint: identity, per-tier tuning, SSE, and resumption.

Runs against hand-built payloads and :class:`providers.stub.StubProvider`, with no model or AWS
call.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from agents.budget import CHAT_BUDGET
from agents.events import AgentEvent
from agents.runtime_entrypoint import (
    InMemorySessionStore,
    RuntimeInvocation,
    build_supervisor_turn,
    decode_invocation,
    format_sse_line,
    resolve_identity,
    resolve_tuning,
    run_turn,
)
from crm.envelope import IDENTITY_CHANNEL_KEYS
from crm.errors import AuthenticationFailed, ValidationFailed
from providers.capabilities import CapabilityMatrix, Family, load_capability_matrix
from providers.stub import StubProvider
from providers.tier_tuning import TIERS, WIRE_SCHEMA_VERSION, TurnTuning

REPO_ROOT = Path(__file__).resolve().parents[2]

_APIGW_PAYLOAD = {
    "requestContext": {
        "requestId": "req-1",
        "authorizer": {"jwt": {"claims": {"custom:rep_id": "rep-1", "custom:book": "acc-1,acc-2"}}},
    },
    "prompt": "What is the status of Acme?",
    "session_id": "session-abc",
}


# --------------------------------------------------------------------------------------
# Identity is obtained solely through crm.envelope.decode_identity
# --------------------------------------------------------------------------------------


def test_resolve_identity_decodes_a_valid_apigw_shaped_payload() -> None:
    """A payload carrying `requestContext` claims decodes to a scoped Identity."""
    identity = resolve_identity(_APIGW_PAYLOAD)
    assert identity.rep_id == "rep-1"
    assert identity.book == frozenset({"acc-1", "acc-2"})
    assert identity.source == "apigw"


def test_resolve_identity_raises_authentication_failed_on_no_recognised_channel() -> None:
    """A payload carrying none of the recognised identity channel keys is rejected outright."""
    with pytest.raises(AuthenticationFailed):
        resolve_identity({"prompt": "hello", "session_id": "s1"})


def test_resolve_identity_raises_on_missing_rep_id_claim() -> None:
    """A payload whose claims omit the rep id claim is rejected, never given empty scope."""
    payload = {
        "requestContext": {"requestId": "r1", "authorizer": {"jwt": {"claims": {}}}},
    }
    with pytest.raises(AuthenticationFailed):
        resolve_identity(payload)


def test_runtime_entrypoint_module_calls_decode_identity_not_identity_from_claims() -> None:
    """runtime_entrypoint.py calls `decode_identity`, never `identity_from_claims` or `Identity`."""
    source = (REPO_ROOT / "src" / "agents" / "runtime_entrypoint.py").read_text()
    tree = ast.parse(source)
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "decode_identity" in called_names
    assert "identity_from_claims" not in called_names
    assert "Identity" not in called_names


def test_identity_channel_keys_are_never_hand_duplicated_in_this_module() -> None:
    """runtime_entrypoint.py declares no frozenset duplicating `IDENTITY_CHANNEL_KEYS`."""
    source = (REPO_ROOT / "src" / "agents" / "runtime_entrypoint.py").read_text()
    tree = ast.parse(source)
    frozenset_literals = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
    ]
    assert not frozenset_literals, "runtime_entrypoint.py must not declare its own frozenset"
    assert IDENTITY_CHANNEL_KEYS


# --------------------------------------------------------------------------------------
# Tuning fails fast, naming the offending source, before any turn starts
# --------------------------------------------------------------------------------------


def _wire(**tiers: dict[str, object]) -> dict[str, object]:
    """A current-schema wire tuning block carrying ``tiers``."""
    return {"schema": WIRE_SCHEMA_VERSION, "source": "ui.settings", "tiers": tiers}


def _other_family(matrix: CapabilityMatrix) -> Family:
    return "openai" if matrix.default_family == "anthropic" else "anthropic"


def test_resolve_tuning_with_no_tuning_block_resolves_every_tier_to_its_default() -> None:
    """Omitting `tuning` resolves each tier to the default family's configured model."""
    matrix = load_capability_matrix()
    tuning, resolved = resolve_tuning(matrix, raw_tuning=None)
    assert tuning.tiers == {}
    assert tuning.source == "agent.invocation.tuning"
    assert set(resolved) == set(TIERS)
    for tier in TIERS:
        assert (
            resolved[tier].model.model_id == matrix.model_for(matrix.default_family, tier).model_id
        )


def test_resolve_tuning_rejects_a_flat_schema_one_block_naming_the_schema_field() -> None:
    """The old flat `{family, effort}` block is refused rather than silently reinterpreted."""
    matrix = load_capability_matrix()
    with pytest.raises(ValidationFailed) as exc_info:
        resolve_tuning(matrix, raw_tuning={"family": "openai", "effort": "low"})
    assert exc_info.value.source == "tuning.schema"


@pytest.mark.parametrize("schema", [2, WIRE_SCHEMA_VERSION])
@pytest.mark.parametrize("value", [0.5, None])
@pytest.mark.parametrize("tier", TIERS)
def test_resolve_tuning_refuses_any_temperature_key_naming_that_tier_field(
    tier: str, value: object, schema: int
) -> None:
    """A tier carrying `temperature` (null included, any schema) names that tier's field."""
    matrix = load_capability_matrix()
    raw = {"schema": schema, "tiers": {tier: {"temperature": value}}}
    with pytest.raises(ValidationFailed) as exc_info:
        resolve_tuning(matrix, raw_tuning=raw)
    source = f"tuning.tiers.{tier}.temperature"
    assert exc_info.value.source == source
    assert source in exc_info.value.message


def test_resolve_tuning_refuses_a_schema_two_block_without_temperature() -> None:
    """A temperature-free schema-2 block still fails on the schema field."""
    matrix = load_capability_matrix()
    with pytest.raises(ValidationFailed) as exc_info:
        resolve_tuning(matrix, raw_tuning={"schema": 2, "tiers": {"tool": {"effort": "low"}}})
    assert exc_info.value.source == "tuning.schema"


def test_decode_invocation_refuses_a_chat_turn_carrying_a_temperature() -> None:
    """A chat turn's tuning with a temperature is refused at decode, before any turn exists."""
    matrix = load_capability_matrix()
    payload = {**_APIGW_PAYLOAD, "tuning": _wire(subagent={"temperature": 0.2})}
    with pytest.raises(ValidationFailed) as exc_info:
        decode_invocation(payload, matrix)
    assert exc_info.value.source == "tuning.tiers.subagent.temperature"


def test_resolve_tuning_rejects_an_effort_value_naming_the_tier_field() -> None:
    """An effort value the tier's model rejects fails fast, naming `tuning.tiers.<tier>.effort`."""
    matrix = load_capability_matrix()
    with pytest.raises(ValidationFailed) as exc_info:
        resolve_tuning(matrix, raw_tuning=_wire(supervisor={"effort": "not-a-real-effort-level"}))
    assert exc_info.value.source == "tuning.tiers.supervisor.effort"


def test_resolve_tuning_rejects_an_unknown_family_naming_the_tier_field() -> None:
    """A family the matrix does not know is a ValidationFailed naming that tier's family field."""
    matrix = load_capability_matrix()
    with pytest.raises(ValidationFailed) as exc_info:
        resolve_tuning(matrix, raw_tuning=_wire(tool={"family": "gemini"}))
    assert exc_info.value.source == "tuning.tiers.tool.family"


def test_resolve_tuning_resolves_each_tier_in_its_own_family() -> None:
    """A mixed block resolves the supervisor in one family and the other tiers in another."""
    matrix = load_capability_matrix()
    other = _other_family(matrix)
    tuning, resolved = resolve_tuning(
        matrix,
        raw_tuning=_wire(supervisor={"family": other}, tool={"family": matrix.default_family}),
    )
    assert tuning.source == "ui.settings"
    assert resolved["supervisor"].model.family == other
    assert resolved["supervisor"].model.model_id == matrix.model_for(other, "supervisor").model_id
    assert resolved["subagent"].model.family == matrix.default_family
    assert resolved["tool"].model.family == matrix.default_family


def test_resolve_tuning_records_the_default_source_when_the_block_names_none() -> None:
    """A block without `source` records the caller's default source."""
    matrix = load_capability_matrix()
    tuning, _ = resolve_tuning(
        matrix, raw_tuning={"schema": WIRE_SCHEMA_VERSION, "tiers": {}}, default_source="eval.arm"
    )
    assert tuning.source == "eval.arm"


def test_decode_invocation_carries_the_decoded_per_tier_tuning() -> None:
    """`decode_invocation` keeps the decoded `TurnTuning` and every tier's resolution."""
    matrix = load_capability_matrix()
    other = _other_family(matrix)
    payload = {**_APIGW_PAYLOAD, "tuning": _wire(subagent={"family": other})}
    invocation = decode_invocation(payload, matrix)
    assert isinstance(invocation.tuning, TurnTuning)
    assert invocation.tuning.tiers["subagent"].family == other
    assert invocation.resolved_tiers["subagent"].model.family == other
    assert invocation.resolved_invocation is invocation.resolved_tiers["supervisor"]
    assert invocation.resolved_invocation.model.family == matrix.default_family


def test_decode_invocation_raises_tuning_error_before_touching_identity_consumers() -> None:
    """An invalid tuning value is rejected by decode_invocation itself, not deferred to the turn."""
    matrix = load_capability_matrix()
    payload = {**_APIGW_PAYLOAD, "tuning": _wire(supervisor={"effort": "definitely-invalid"})}
    with pytest.raises(ValidationFailed):
        decode_invocation(payload, matrix)


def test_decode_invocation_raises_authentication_failed_before_validating_tuning() -> None:
    """A payload with no identity channel is rejected even before any tuning is inspected."""
    matrix = load_capability_matrix()
    payload = {"prompt": "hi", "tuning": {"effort": "also-invalid"}}
    with pytest.raises(AuthenticationFailed):
        decode_invocation(payload, matrix)


def test_decode_invocation_succeeds_and_carries_prompt_and_session_id() -> None:
    """A well-formed payload decodes to a RuntimeInvocation naming the caller's prompt and session."""
    matrix = load_capability_matrix()
    invocation = decode_invocation(_APIGW_PAYLOAD, matrix)
    assert isinstance(invocation, RuntimeInvocation)
    assert invocation.prompt == "What is the status of Acme?"
    assert invocation.session_id == "session-abc"
    assert invocation.identity.rep_id == "rep-1"


def test_decode_invocation_falls_back_to_identity_session_id_when_payload_omits_one() -> None:
    """A payload with no top-level session_id uses the identity's own session id instead."""
    matrix = load_capability_matrix()
    payload = {
        "requestContext": {
            "requestId": "req-9",
            "authorizer": {"jwt": {"claims": {"custom:rep_id": "rep-2"}}},
        },
        "prompt": "hello",
    }
    invocation = decode_invocation(payload, matrix)
    # apigw source derives session_id from requestContext.requestId inside decode_identity.
    assert invocation.session_id == "req-9"


# --------------------------------------------------------------------------------------
# One JSON event per SSE line, drawn from the closed event set
# --------------------------------------------------------------------------------------


def test_format_sse_line_emits_exactly_one_json_object_per_line() -> None:
    """Each SSE frame carries exactly one JSON object, terminated by a blank line."""
    event = AgentEvent(kind="completed", text="done")
    line = format_sse_line(event)
    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    body = line[len("data: ") : -2]
    # Exactly one JSON value parses from the body; a second json.loads on any suffix must fail.
    parsed = json.loads(body)
    assert isinstance(parsed, dict)
    assert parsed["kind"] == "completed"
    assert parsed["text"] == "done"


def test_format_sse_line_round_trips_every_closed_event_kind() -> None:
    """Each closed event kind other than `text_delta` renders to a single valid JSON SSE line."""
    events = [
        AgentEvent(kind="tool_call", tool_name="search_opportunities", tool_input={"q": "Acme"}),
        AgentEvent(kind="tool_result", tool_output={"items": []}),
        AgentEvent(kind="proposal", proposal_id="p1", proposal_summary={"before": {}, "after": {}}),
        AgentEvent(kind="usage", input_tokens=10, output_tokens=5),
        AgentEvent(kind="completed", text="ok"),
        AgentEvent(kind="error", error_kind="tool_failure", error_message="failed"),
    ]
    for event in events:
        line = format_sse_line(event)
        body = line[len("data: ") : -2]
        parsed = json.loads(body)
        assert parsed["kind"] == event.kind


def test_format_sse_line_never_embeds_a_newline_inside_the_json_body() -> None:
    """A single event never spans more than one SSE line, even with multi-line text content."""
    event = AgentEvent(kind="completed", text="line one\nline two\nline three")
    line = format_sse_line(event)
    # Exactly one data: prefix and the frame is exactly one JSON body followed by the blank line.
    assert line.count("data: ") == 1
    body = line[len("data: ") : -2]
    assert json.loads(body)["text"] == "line one\nline two\nline three"


# --------------------------------------------------------------------------------------
# A dropped connection is resumable by session id
# --------------------------------------------------------------------------------------


def test_run_turn_records_and_streams_every_event_on_a_fresh_session() -> None:
    """A fresh turn (resume_from=0) streams every event and records it in the store."""
    store = InMemorySessionStore()
    invocation = _fake_invocation(session_id="sess-1")
    events = iter(
        [
            AgentEvent(kind="tool_call", tool_name="search_opportunities", tool_input={}),
            AgentEvent(kind="completed", text="done"),
        ]
    )
    lines = list(run_turn(invocation, events, store=store, resume_from=0))
    assert len(lines) == 2
    assert store.last_sequence("sess-1") == 2


def test_run_turn_replays_only_events_after_resume_from_on_reconnect() -> None:
    """A reconnecting client supplying resume_from gets only the events it has not yet seen."""
    store = InMemorySessionStore()
    invocation = _fake_invocation(session_id="sess-2")
    first_events = [
        AgentEvent(kind="tool_call", tool_name="search_opportunities", tool_input={}),
        AgentEvent(kind="tool_result", tool_output={"items": []}),
        AgentEvent(kind="completed", text="done"),
    ]
    # Simulate the original turn completing and being recorded.
    for event in first_events:
        store.append("sess-2", event)

    # Client saw only the first event (sequence 1) before the connection dropped.
    replay_lines = list(run_turn(invocation, iter([]), store=store, resume_from=1))
    assert len(replay_lines) == 2
    kinds = [json.loads(line[len("data: ") : -2])["kind"] for line in replay_lines]
    assert kinds == ["tool_result", "completed"]


def test_run_turn_replay_never_consumes_the_live_event_iterator() -> None:
    """On a pure replay, the turn's own event stream (which would re-invoke the model) is untouched."""
    store = InMemorySessionStore()
    invocation = _fake_invocation(session_id="sess-3")
    store.append("sess-3", AgentEvent(kind="completed", text="done"))

    consumed: list[AgentEvent] = []

    def _tracking_events() -> Iterator[AgentEvent]:
        for event in [AgentEvent(kind="completed", text="should not run")]:
            consumed.append(event)
            yield event

    list(run_turn(invocation, _tracking_events(), store=store, resume_from=0))
    assert consumed == []


def test_run_turn_with_resume_from_at_or_beyond_last_sequence_runs_a_fresh_turn() -> None:
    """resume_from equal to (or beyond) the last recorded sequence means nothing to replay, so a
    genuinely new turn's events are consumed and recorded normally."""
    store = InMemorySessionStore()
    invocation = _fake_invocation(session_id="sess-4")
    store.append("sess-4", AgentEvent(kind="completed", text="turn one"))

    new_events = iter([AgentEvent(kind="completed", text="turn two")])
    lines = list(run_turn(invocation, new_events, store=store, resume_from=1))
    assert len(lines) == 1
    body = json.loads(lines[0][len("data: ") : -2])
    assert body["text"] == "turn two"
    assert store.last_sequence("sess-4") == 2


def test_in_memory_session_store_events_after_returns_empty_tuple_for_unknown_session() -> None:
    """A session id never seen before yields no events and no error."""
    store = InMemorySessionStore()
    assert store.events_after("never-seen", sequence=0) == ()
    assert store.last_sequence("never-seen") == 0


def test_in_memory_session_store_sequence_numbers_are_stable_and_monotonic() -> None:
    """Sequence numbers assigned by append() are 1-based and strictly increasing."""
    store = InMemorySessionStore()
    seq1 = store.append("sess-5", AgentEvent(kind="completed", text="a"))
    seq2 = store.append("sess-5", AgentEvent(kind="completed", text="b"))
    assert seq1 == 1
    assert seq2 == 2


# --------------------------------------------------------------------------------------
# End-to-end smoke: decode -> build turn -> stream one event set through SSE framing.
# No live model call; StubProvider only.
# --------------------------------------------------------------------------------------


def test_end_to_end_decode_and_build_supervisor_turn_against_the_stub_provider() -> None:
    """A well-formed invocation decodes and builds a runnable SupervisorTurn against the stub."""
    matrix = load_capability_matrix()
    invocation = decode_invocation(_APIGW_PAYLOAD, matrix)
    turn = build_supervisor_turn(invocation, StubProvider(), matrix)
    resolved = turn.resolve_invocation()
    assert resolved.model is not None


def test_build_supervisor_turn_carries_the_supervisor_tier_family() -> None:
    """A turn built from an invocation whose supervisor runs OpenAI resolves to OpenAI."""
    matrix = load_capability_matrix()
    payload = {**_APIGW_PAYLOAD, "tuning": _wire(supervisor={"family": "openai"})}
    invocation = decode_invocation(payload, matrix)
    turn = build_supervisor_turn(invocation, StubProvider(), matrix)
    assert turn.tuning_override is not None
    assert turn.tuning_override.family == "openai"
    resolved = turn.resolve_invocation()
    assert resolved.model.family == "openai"
    assert resolved.model.model_id == matrix.model_for("openai", "supervisor").model_id


def test_build_supervisor_turn_pins_the_already_validated_tuning_not_a_fresh_source() -> None:
    """The turn's tuning override carries the resolved family and effort, never re-derived."""
    matrix = load_capability_matrix()
    invocation = decode_invocation(_APIGW_PAYLOAD, matrix)
    turn = build_supervisor_turn(invocation, StubProvider(), matrix)
    override = turn.tuning_override
    assert override is not None
    assert override.effort == invocation.resolved_invocation.effort
    assert override.family == invocation.resolved_invocation.model.family


def test_build_supervisor_turn_defaults_to_the_chat_budget() -> None:
    """With no budget argument the turn runs under `CHAT_BUDGET`'s caps."""
    matrix = load_capability_matrix()
    turn = build_supervisor_turn(decode_invocation(_APIGW_PAYLOAD, matrix), StubProvider(), matrix)
    assert turn.budget.max_tool_calls == CHAT_BUDGET.max_tool_calls
    assert turn.budget.max_wall_clock_seconds == CHAT_BUDGET.max_wall_clock_seconds


# --------------------------------------------------------------------------------------
# Heartbeats are streamed but never recorded for resume
# --------------------------------------------------------------------------------------


def test_run_turn_streams_a_heartbeat_but_does_not_record_it() -> None:
    """A heartbeat reaches the client and leaves the session's sequence numbers unchanged."""
    store = InMemorySessionStore()
    invocation = _fake_invocation(session_id="sess-hb")
    events = iter(
        [
            AgentEvent(kind="heartbeat"),
            AgentEvent(kind="tool_call", tool_name="search_opportunities", tool_input={}),
            AgentEvent(kind="heartbeat"),
            AgentEvent(kind="completed", text="done"),
        ]
    )
    lines = list(run_turn(invocation, events, store=store))
    kinds = [json.loads(line[len("data: ") : -2])["kind"] for line in lines]
    assert kinds == ["heartbeat", "tool_call", "heartbeat", "completed"]
    assert store.last_sequence("sess-hb") == 2
    replayed = [event.kind for event in store.events_after("sess-hb", sequence=0)]
    assert replayed == ["tool_call", "completed"]


# --------------------------------------------------------------------------------------
# No vendor SDK import anywhere in this module
# --------------------------------------------------------------------------------------

_FORBIDDEN_VENDOR_IMPORT_ROOTS = frozenset({"anthropic", "openai", "boto3", "botocore"})


def test_runtime_entrypoint_module_imports_no_vendor_sdk() -> None:
    """AST sweep of runtime_entrypoint.py: no import statement names a vendor SDK root module."""
    source = (REPO_ROOT / "src" / "agents" / "runtime_entrypoint.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in _FORBIDDEN_VENDOR_IMPORT_ROOTS, (
                    f"runtime_entrypoint.py imports forbidden vendor module {alias.name!r}"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            root = node.module.split(".")[0]
            assert root not in _FORBIDDEN_VENDOR_IMPORT_ROOTS, (
                f"runtime_entrypoint.py imports forbidden vendor module {node.module!r}"
            )


# --------------------------------------------------------------------------------------
# Test helpers
# --------------------------------------------------------------------------------------


def _fake_invocation(*, session_id: str) -> RuntimeInvocation:
    """Build a minimal RuntimeInvocation for tests exercising only run_turn/session resumption."""
    matrix = load_capability_matrix()
    tuning, resolved = resolve_tuning(matrix, raw_tuning=None)
    identity = resolve_identity(_APIGW_PAYLOAD)
    return RuntimeInvocation(
        identity=identity,
        session_id=session_id,
        prompt="irrelevant for these tests",
        tuning=tuning,
        resolved_tiers=resolved,
    )

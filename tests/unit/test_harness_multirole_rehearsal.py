# SPDX-License-Identifier: MIT
"""Offline end-to-end rehearsal of a delegating sweep through every post-run stage.

Drives the real :func:`eval.harness.run_sweep` through aggregation, artifact rendering and
:func:`eval.harness.assert_artifacts` with a scripted provider (patched in at
``eval.harness._build_provider``) that dispatches ``get_account`` under the supervisor, research and
mutation roles in one turn, and asserts the pre-aggregation raw records are archived on disk.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from pathlib import Path

import boto3
import httpx2
import pytest
import yaml
from botocore.credentials import Credentials
from moto import mock_aws

from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import harness
from eval.harness import ArmSpec, assert_artifacts, run_sweep
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from tests.integration._aws_eventstream import encode_chunk_event

# A supervisor direct tool that is also a research and a mutation subagent leaf.
_TOOL = "get_account"
_TOOL_INPUT: dict[str, object] = {"account_id": "acct-0000"}


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _round(*events: ProviderEvent) -> list[ProviderEvent]:
    return list(events)


def _delegation_rounds() -> list[list[ProviderEvent]]:
    """Script one turn that dispatches ``get_account`` under supervisor, research, and mutation.

    One event list per provider ``stream`` call, in the order ``drive_turn`` makes them: a
    supervisor round that calls the tool directly, a supervisor round that delegates to research
    (whose subagent + tool-actor rounds follow), a supervisor round that delegates to mutation
    (its two rounds follow), then the supervisor's final answer. Each round carries a ``usage``
    event so every invocation is token-bearing and attributed.
    """

    def _delegate(role: str) -> list[list[ProviderEvent]]:
        return [
            _round(  # supervisor: delegate to the subagent
                ProviderEvent(
                    type="tool_call", tool_name=role, tool_input={"intent": "look it up"}
                ),
                ProviderEvent(type="usage", input_tokens=10, output_tokens=5),
                ProviderEvent(type="completed", awaiting_tools=True),
            ),
            _round(  # subagent: name the leaf tool to run
                ProviderEvent(type="tool_call", tool_name=_TOOL, tool_input=_TOOL_INPUT),
                ProviderEvent(type="usage", input_tokens=6, output_tokens=3),
                ProviderEvent(type="completed", awaiting_tools=False),
            ),
            _round(  # tool-tier actor: formulate the leaf's args (dispatch fires here)
                ProviderEvent(type="tool_call", tool_name=_TOOL, tool_input=_TOOL_INPUT),
                ProviderEvent(type="usage", input_tokens=4, output_tokens=2),
                ProviderEvent(type="completed", awaiting_tools=False),
            ),
        ]

    return [
        _round(  # supervisor: call get_account directly (supervisor role)
            ProviderEvent(type="tool_call", tool_name=_TOOL, tool_input=_TOOL_INPUT),
            ProviderEvent(type="usage", input_tokens=10, output_tokens=5),
            ProviderEvent(type="completed", awaiting_tools=True),
        ),
        *_delegate("research"),
        *_delegate("mutation"),
        _round(  # supervisor: final answer
            ProviderEvent(type="text_delta", text="done"),
            ProviderEvent(type="usage", input_tokens=5, output_tokens=2),
            ProviderEvent(type="completed", text="done"),
        ),
    ]


class _DelegatingScriptedProvider(ModelProvider):
    """Replays :func:`_delegation_rounds` one event list per ``stream`` call, in call order."""

    def __init__(self) -> None:
        self._rounds = _delegation_rounds()
        self.call_count = 0

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        index = min(self.call_count, len(self._rounds) - 1)
        self.call_count += 1
        for event in self._rounds[index]:
            yield event


#: The fixed call latency (ms) the live-mode fake stamps on every ``usage`` event; every group's
#: P50 and P95 equal it.
_FAKE_CALL_LATENCY_MS = 42.0


class _DelegatingLatencyProvider(_DelegatingScriptedProvider):
    """A delegating scripted provider that stamps each usage event with a call latency, like a live arm.

    Tool dispatch latency is not faked: on the live path ``_run_arm`` times each in-process dispatch
    with ``time.monotonic``.
    """

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        async for event in super().stream(invocation, messages, tools):
            if event.type == "usage":
                yield replace(event, latency_ms=_FAKE_CALL_LATENCY_MS)
            else:
                yield event


def _write_single_task_golden(path: Path) -> None:
    """Write a one-task golden file whose only assertions are negative guards a read-only turn meets.

    No positive assertion (``expected_field_changes``/``expected_reads``/``expected_proposal``/
    ``expected_denial``), so scoring never depends on which record exists in the empty rehearsal
    tenant; the turn reads only, so ``must_not_change`` and ``max_proposal_count: 0`` hold.
    """
    task: dict[str, object] = {
        "id": "gt-rehearsal-multirole",
        "category": "lookup",
        "derived_from": "multi-role rehearsal",
        "prompt": "Look up the account.",
        "actor": {"rep_id": "rep-rehearsal", "book": [], "full_visibility": True},
        "scoring_mode": "machine",
        "unscored_reason": None,
        "assertions": {
            "expected_field_changes": None,
            "must_not_change": [],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "expected_reads": [],
            "forbidden_outcomes": [],
        },
        "notes": "Rehearsal task: negative guards only; the turn's value is the multi-role dispatch.",
    }
    path.write_text(yaml.safe_dump({"tasks": [task]}, sort_keys=False), encoding="utf-8")


def test_get_account_is_a_supervisor_research_and_mutation_tool() -> None:
    """Guard the rehearsal's premise: the scripted tool really is reachable under all three roles."""
    from agents.registry import tool_registry_for
    from agents.subagents import SUBAGENT_SPECS_BY_ROLE
    from agents.supervisor import SUPERVISOR_ROLE

    assert _TOOL in {t.name for t in tool_registry_for(SUPERVISOR_ROLE)}
    for role in ("research", "mutation"):
        assert _TOOL in {t.name for t in SUBAGENT_SPECS_BY_ROLE[role].tools}


def test_delegating_sweep_runs_every_post_run_stage_with_one_tool_under_three_roles(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn dispatching one tool under three roles completes with one row per ``(tool, role)``."""
    golden = tmp_path / "golden_tasks.yaml"
    _write_single_task_golden(golden)

    monkeypatch.setattr(harness, "_build_provider", lambda *_a, **_k: _DelegatingScriptedProvider())

    result = run_sweep(
        store=store,
        mode="stub",
        arm_specs=(ArmSpec(family="anthropic"),),
        golden_tasks_path=golden,
        output_dir=tmp_path,
        archive_raw=True,
        run_id="run-rehearsal",
    )

    # The turn dispatched get_account under all three roles: the aggregation produced three distinct
    # (tool_name, role) rows for it rather than raising.
    arm = result.arm_runs[0].arm_result
    multirole = {
        (row["tool_name"], row["role"]) for row in arm.tool_fields if row["tool_name"] == _TOOL
    }
    assert multirole == {
        (_TOOL, "supervisor"),
        (_TOOL, "research"),
        (_TOOL, "mutation"),
    }, multirole
    # No two rows share a (tool, role) key — the aggregation collapsed by the pair, not the tool.
    keys = [(row["tool_name"], row["role"]) for row in arm.tool_fields]
    assert len(keys) == len(set(keys))

    # Rendering stage: all three published artifacts exist on disk.
    for name in ("matrix.json", "matrix.md", "matrix.csv"):
        assert (tmp_path / name).is_file()

    # The --assert-artifacts stage the live path runs at pass: well-formedness over the same output.
    assert_artifacts(
        output_dir=tmp_path,
        expected_arm_count=1,
        expected_task_count=1,
        raw_archive_paths=result.raw_archive_paths,
    )

    # The pre-aggregation raw records survived: a post-run crash would be recoverable from this.
    raw_files = list((tmp_path / "raw" / "pre-aggregation").glob("pre-aggregation-arm-*.json"))
    assert len(raw_files) == 1, raw_files
    payload = json.loads(raw_files[0].read_text(encoding="utf-8"))
    dispatched_roles = {
        tc["role"] for tc in payload["tool_call_attributions"] if tc["tool_name"] == _TOOL
    }
    assert {"supervisor", "research", "mutation"} <= dispatched_roles


def test_live_mode_sweep_runs_the_measured_latency_post_run_path(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``mode="live"`` post-run path attributes measured latency to every tier, row and tool.

    Runs offline: the live-substrate pairing gate is stubbed out and the provider is a delegating
    fake that reports per-invocation latency.
    """
    golden = tmp_path / "golden_tasks.yaml"
    _write_single_task_golden(golden)

    monkeypatch.setattr(
        harness, "_assert_live_mode_is_paired_with_the_live_substrate", lambda *_a, **_k: None
    )
    monkeypatch.setattr(harness, "_build_provider", lambda *_a, **_k: _DelegatingLatencyProvider())

    result = run_sweep(
        store=store,
        mode="live",
        arm_specs=(ArmSpec(family="anthropic"),),
        golden_tasks_path=golden,
        output_dir=tmp_path,
        archive_raw=True,
        run_id="run-live-latency",
    )

    arm = result.arm_runs[0].arm_result
    fields = arm.model_metrics_fields
    # The measured-latency branch ran: the arm reports measured latency with non-null task figures.
    assert fields["latency_measured"] is True
    assert fields["task_latency_ms_p50"] is not None
    assert fields["task_latency_ms_p95"] is not None

    # (1) Each tier's call latency is attributed to the tier that made the call: 4 supervisor
    # rounds (direct, two delegations, final answer), 2 subagent rounds and 2 tool-tier rounds.
    by_tier = fields["call_latency_ms_by_tier"]
    assert isinstance(by_tier, dict)
    expected_tier_counts = {"supervisor": 4, "subagent": 2, "tool": 2}
    for tier_name, expected_count in expected_tier_counts.items():
        entry = by_tier[tier_name]
        assert isinstance(entry, dict), (tier_name, entry)
        assert entry["count"] == expected_count, (tier_name, entry)
        assert entry["p50"] == _FAKE_CALL_LATENCY_MS, (tier_name, entry)
        assert entry["p95"] == _FAKE_CALL_LATENCY_MS, (tier_name, entry)

    # (2) Every (tier, role, model) row records measured, non-null call latency over exactly that
    # row's invocation count.
    rows = arm.tier_role_model_fields
    expected_rows = {
        ("supervisor", "supervisor"): 4,
        ("subagent", "research"): 1,
        ("subagent", "mutation"): 1,
        ("tool", "research"): 1,
        ("tool", "mutation"): 1,
    }
    seen_rows = {(str(r["tier"]), str(r["role"])): r for r in rows}
    assert set(seen_rows) == set(expected_rows), seen_rows
    for key, expected_count in expected_rows.items():
        row = seen_rows[key]
        assert row["included_invocation_count"] == expected_count, (key, row)
        assert row["call_latency_measured"] is True, (key, row)
        assert row["call_latency_ms_p50"] is not None, (key, row)
        assert row["call_latency_ms_p95"] is not None, (key, row)

    # (3) Every (tool, role) dispatch row records non-null dispatch latency.
    tool_rows = {(str(r["tool_name"]), str(r["role"])): r for r in arm.tool_fields}
    for key in ((_TOOL, "supervisor"), (_TOOL, "research"), (_TOOL, "mutation")):
        row = tool_rows[key]
        assert row["count"] == 1, (key, row)
        assert row["call_latency_ms_p50"] is not None, (key, row)
        assert row["call_latency_ms_p95"] is not None, (key, row)

    # The stages that follow the latency branch still ran to completion on the live-mode path.
    for name in ("matrix.json", "matrix.md", "matrix.csv"):
        assert (tmp_path / name).is_file()
    assert_artifacts(
        output_dir=tmp_path,
        expected_arm_count=1,
        expected_task_count=1,
        raw_archive_paths=result.raw_archive_paths,
    )
    raw_files = list((tmp_path / "raw" / "pre-aggregation").glob("pre-aggregation-arm-*.json"))
    assert len(raw_files) == 1, raw_files
    # The archived attributions carry their own latency.
    payload = json.loads(raw_files[0].read_text(encoding="utf-8"))
    assert any(a["latency_ms"] is not None for a in payload["invocation_attributions"])
    assert any(t["latency_ms"] is not None for t in payload["tool_call_attributions"])


# --------------------------------------------------------------------------------------
# The live post-run path through the real adapters
# --------------------------------------------------------------------------------------

#: The wall-clock length of every successful model call on the injected clock, in seconds. Chosen
#: exactly representable in binary so every per-row P50/P95 is exactly this many milliseconds.
_REAL_CALL_SECONDS = 0.125

#: How far the injected clock advances during each retried (503/429) attempt. A wrapper or adapter
#: that timed the retry would report at least this much, never ``_REAL_CALL_SECONDS``.
_RETRIED_ATTEMPT_SECONDS = 5.0

#: Rounds of :func:`_real_model_rounds` whose first attempt the transport answers with a transient
#: status before the SDK's own retry succeeds.
_INJECTED_RETRIES = {0: 503, 5: 429}


def _real_model_rounds() -> list[list[ProviderEvent]]:
    """Script one delegating turn as a real model streams it, one event list per model call.

    Unlike :func:`_delegation_rounds`, every round that calls a tool ends awaiting its result, the
    only shape either wire can express (a Messages ``tool_use`` stop / a Responses
    ``function_call``). So each subagent is re-invoked with its leaf's result and answers.
    """

    def call(name: str, tool_input: dict[str, object]) -> list[ProviderEvent]:
        return _round(
            ProviderEvent(type="tool_call", tool_name=name, tool_input=tool_input),
            ProviderEvent(type="usage", input_tokens=10, output_tokens=5),
            ProviderEvent(type="completed", awaiting_tools=True),
        )

    def answer(text: str) -> list[ProviderEvent]:
        return _round(
            ProviderEvent(type="text_delta", text=text),
            ProviderEvent(type="usage", input_tokens=6, output_tokens=3),
            ProviderEvent(type="completed", awaiting_tools=False),
        )

    def delegate(role: str) -> list[list[ProviderEvent]]:
        return [
            call(role, {"intent": "look it up"}),  # supervisor delegates
            call(_TOOL, _TOOL_INPUT),  # subagent names its leaf
            call(_TOOL, _TOOL_INPUT),  # tool-tier actor formulates the leaf's args
            answer("found"),  # subagent answers from the leaf's result
        ]

    return [
        call(_TOOL, _TOOL_INPUT),  # supervisor calls get_account directly
        *delegate("research"),
        *delegate("mutation"),
        answer("done"),  # supervisor's final answer
    ]


class _SteppingClock:
    """A monotonic clock that only the mock transport advances."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _TimedBody(httpx2.AsyncByteStream):
    """A response body that takes exactly ``_REAL_CALL_SECONDS`` on the injected clock to read.

    Single-pass like a socket: a second iteration (the SDK's post-``[DONE]`` drain) yields nothing.
    """

    def __init__(self, frames: list[bytes], clock: _SteppingClock) -> None:
        self._frames = frames
        self._clock = clock
        self._read = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._read:
            return
        self._read = True
        self._clock.now += _REAL_CALL_SECONDS
        for frame in self._frames:
            yield frame


def _anthropic_frames(index: int, round_events: list[ProviderEvent]) -> list[bytes]:
    """Encode one scripted round as Bedrock ``InvokeModelWithResponseStream`` chunk frames."""
    usage = next(e for e in round_events if e.type == "usage")
    calls_tool = any(e.type == "tool_call" for e in round_events)
    events: list[dict[str, object]] = [
        {
            "type": "message_start",
            "message": {
                "id": f"msg_rehearsal_{index}",
                "type": "message",
                "role": "assistant",
                "model": "rehearsal",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": usage.input_tokens, "output_tokens": 1},
            },
        }
    ]
    for block, event in enumerate(e for e in round_events if e.type in {"tool_call", "text_delta"}):
        if event.type == "tool_call":
            start: dict[str, object] = {
                "type": "tool_use",
                "id": f"toolu_rehearsal_{index}_{block}",
                "name": event.tool_name,
                "input": {},
            }
            delta: dict[str, object] = {
                "type": "input_json_delta",
                "partial_json": json.dumps(event.tool_input),
            }
        else:
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": event.text}
        events += [
            {"type": "content_block_start", "index": block, "content_block": start},
            {"type": "content_block_delta", "index": block, "delta": delta},
            {"type": "content_block_stop", "index": block},
        ]
    events += [
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": "tool_use" if calls_tool else "end_turn",
                "stop_sequence": None,
            },
            "usage": {"output_tokens": usage.output_tokens},
        },
        {"type": "message_stop"},
    ]
    return [encode_chunk_event(event) for event in events]


def _openai_frames(index: int, round_events: list[ProviderEvent]) -> list[bytes]:
    """Encode one scripted round as Bedrock's OpenAI Responses ``text/event-stream`` frames."""
    usage = next(e for e in round_events if e.type == "usage")
    events: list[dict[str, object]] = []
    for position, event in enumerate(
        e for e in round_events if e.type in {"tool_call", "text_delta"}
    ):
        if event.type == "tool_call":
            events.append(
                {
                    "type": "response.output_item.done",
                    "sequence_number": position,
                    "output_index": position,
                    "item": {
                        "type": "function_call",
                        "id": f"fc_rehearsal_{index}_{position}",
                        "call_id": f"call_rehearsal_{index}_{position}",
                        "name": event.tool_name,
                        "arguments": json.dumps(event.tool_input),
                        "status": "completed",
                    },
                }
            )
        else:
            events.append(
                {
                    "type": "response.output_text.delta",
                    "sequence_number": position,
                    "item_id": f"msg_rehearsal_{index}",
                    "output_index": position,
                    "content_index": 0,
                    "delta": event.text,
                }
            )
    events.append(
        {
            "type": "response.completed",
            "sequence_number": len(events),
            "response": {
                "id": f"resp_rehearsal_{index}",
                "object": "response",
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": (usage.input_tokens or 0) + (usage.output_tokens or 0),
                    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        }
    )
    return [f"data: {json.dumps(event)}\n\n".encode() for event in events] + [b"data: [DONE]\n\n"]


class _ScriptedWire:
    """Serves :func:`_delegation_rounds` over the wire for one family, injecting transient statuses.

    Every request is answered in order; a round listed in :data:`_INJECTED_RETRIES` is first
    answered with its status, which the vendor SDK retries on its own.
    """

    def __init__(self, family: str, clock: _SteppingClock) -> None:
        self.family = family
        self.statuses: list[int] = []
        self._clock = clock
        self._responses: list[tuple[int, list[bytes]]] = []
        encode = _anthropic_frames if family == "anthropic" else _openai_frames
        for index, round_events in enumerate(_real_model_rounds()):
            if index in _INJECTED_RETRIES:
                self._responses.append((_INJECTED_RETRIES[index], []))
            self._responses.append((200, encode(index, round_events)))
        self.transport = httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        status, frames = self._responses.pop(0) if self._responses else (400, [])
        self.statuses.append(status)
        if status != 200:
            self._clock.now += _RETRIED_ATTEMPT_SECONDS
            return httpx2.Response(status, json={"message": "injected"})
        content_type = (
            "application/vnd.amazon.eventstream"
            if self.family == "anthropic"
            else "text/event-stream"
        )
        return httpx2.Response(
            200, stream=_TimedBody(frames, self._clock), headers={"content-type": content_type}
        )


class _FakeCredentialSession:
    """A boto3-session stand-in resolving static fake credentials; no credential chain is read."""

    region_name = "us-east-1"

    def get_credentials(self) -> Credentials:
        return Credentials("AKIDEXAMPLE", "fake-secret-key")


async def _no_retry_sleep(*_: object, **__: object) -> None:
    return None


@pytest.fixture
def real_adapter_wires(monkeypatch: pytest.MonkeyPatch) -> dict[str, _ScriptedWire]:
    """Route live-mode provider construction to the REAL adapters over scripted mock transports.

    Only the wire, the credentials and the clock are substituted. ``harness._build_provider``,
    ``eval.live.build_live_provider`` and ``ThrottleRetryingProvider`` run unmodified.
    """
    from anthropic import AsyncAnthropicBedrock
    from anthropic.lib.bedrock import _auth
    from openai import AsyncBedrockOpenAI

    from eval import live
    from providers.anthropic_adapter import AnthropicAdapter
    from providers.openai_adapter import OpenAIAdapter

    for name in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_BEDROCK_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_auth, "_get_session", lambda **_: _FakeCredentialSession())
    monkeypatch.setattr(AsyncAnthropicBedrock, "_sleep_for_retry", _no_retry_sleep)
    monkeypatch.setattr(AsyncBedrockOpenAI, "_sleep_for_retry", _no_retry_sleep)
    monkeypatch.setattr(
        harness, "_assert_live_mode_is_paired_with_the_live_substrate", lambda *_a, **_k: None
    )

    wires: dict[str, _ScriptedWire] = {}

    def build(family: str, *, region_name: str) -> ModelProvider:
        clock = _SteppingClock()
        wire = _ScriptedWire(family, clock)
        wires[family] = wire
        adapter_type = AnthropicAdapter if family == "anthropic" else OpenAIAdapter
        return adapter_type(
            region_name=region_name,
            session=_FakeCredentialSession(),
            transport=wire.transport,
            clock=clock,
        )

    monkeypatch.setattr(live, "build_provider_for_family", build)
    return wires


def test_real_adapter_live_sweep_threads_sdk_latency_and_retried_attempts_into_every_row(
    store: Store, tmp_path: Path, real_adapter_wires: dict[str, _ScriptedWire]
) -> None:
    """Both families' real adapters, over a wire that injects 503→200 and 429→200, meter correctly.

    Every ``(tier, role, model)`` row counts exactly its invocations, carries the SDK-measured call
    latency of the successful attempt only (P50 = P95 = ``_REAL_CALL_SECONDS``) and a cost, and each
    arm tallies exactly one RETRY and one THROTTLE exclusion for the attempts the SDK retried.
    """
    golden = tmp_path / "golden_tasks.yaml"
    _write_single_task_golden(golden)

    result = run_sweep(
        store=store,
        mode="live",
        arm_specs=(ArmSpec(family="anthropic"), ArmSpec(family="openai")),
        golden_tasks_path=golden,
        output_dir=tmp_path,
        archive_raw=True,
        run_id="run-real-adapter-rehearsal",
    )

    expected_ms = _REAL_CALL_SECONDS * 1000.0
    expected_rows = {
        ("supervisor", "supervisor"): 4,
        ("subagent", "research"): 2,
        ("subagent", "mutation"): 2,
        ("tool", "research"): 1,
        ("tool", "mutation"): 1,
    }
    assert sorted(real_adapter_wires) == ["anthropic", "openai"]
    for family, wire in real_adapter_wires.items():
        rounds = len(_real_model_rounds())
        assert wire.statuses.count(200) == rounds, (family, wire.statuses)
        assert sorted(s for s in wire.statuses if s != 200) == [429, 503], (family, wire.statuses)

    assert len(result.arm_runs) == 2
    for arm_run in result.arm_runs:
        arm = arm_run.arm_result
        fields = arm.model_metrics_fields
        label = fields["model_id"]
        assert fields["latency_measured"] is True, label
        assert fields["exclusions"] == {
            "total_excluded": 2,
            "by_category": {"cold_start": 0, "retry": 1, "throttle": 1},
        }, (label, fields["exclusions"])

        by_tier = fields["call_latency_ms_by_tier"]
        assert isinstance(by_tier, dict)
        for tier_name, count in {"supervisor": 4, "subagent": 4, "tool": 2}.items():
            entry = by_tier[tier_name]
            assert isinstance(entry, dict), (label, tier_name, entry)
            assert (entry["count"], entry["p50"], entry["p95"]) == (
                count,
                expected_ms,
                expected_ms,
            ), (label, tier_name, entry)

        rows = {(str(r["tier"]), str(r["role"])): r for r in arm.tier_role_model_fields}
        assert set(rows) == set(expected_rows), (label, rows)
        for key, count in expected_rows.items():
            row = rows[key]
            assert row["included_invocation_count"] == count, (label, key, row)
            assert row["call_latency_measured"] is True, (label, key, row)
            assert row["call_latency_ms_p50"] == expected_ms, (label, key, row)
            assert row["call_latency_ms_p95"] == expected_ms, (label, key, row)
            assert isinstance(row["cost_total"], float) and row["cost_total"] > 0, (label, key, row)

    for name in ("matrix.json", "matrix.md", "matrix.csv"):
        assert (tmp_path / name).is_file()
    assert_artifacts(
        output_dir=tmp_path,
        expected_arm_count=2,
        expected_task_count=1,
        raw_archive_paths=result.raw_archive_paths,
    )

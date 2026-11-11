# SPDX-License-Identifier: MIT
"""Seam 5 (MCP side): the Gateway #2 chat target's Runtime payload is what the Runtime decodes.

The producer is the real `gateway.chat_target.handle`, against a `moto` store, recording the exact
bytes it sends to `InvokeAgentRuntime`. The consumer is the Runtime's real path for a chat turn:
`agents.runtime_server._authenticated_payload` and then `agents.runtime_entrypoint.decode_invocation`.
Covers the user and service principals and saved and default tuning.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from agents.events import AgentEvent
from agents.runtime_entrypoint import decode_invocation, format_sse_line
from agents.runtime_server import _authenticated_payload
from crm.envelope import IDENTITY_KEY, decode_identity
from crm.errors import AuthenticationFailed
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.user_settings import save_settings
from gateway.chat_target import ChatTargetConfig, handle
from providers.capabilities import load_capability_matrix
from providers.tier_tuning import TIERS, TierRequest, TurnTuning, defaults

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_MATRIX = load_capability_matrix()
_CONFIG = ChatTargetConfig(
    table_prefix="",
    runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/crm-agent-abc",
    runtime_qualifier="DEFAULT",
)
_MESSAGE = "How is my pipeline?"


class _RecordingRuntime:
    """Records the one `invoke_agent_runtime` call and answers with a real `completed` frame."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        body = format_sse_line(AgentEvent(kind="completed", text="ok")).encode("utf-8")
        return {"response": io.BytesIO(body), "contentType": "text/event-stream"}

    def sent_payload(self) -> dict[str, Any]:
        assert len(self.calls) == 1, self.calls
        payload: dict[str, Any] = json.loads(self.calls[0]["payload"])
        return payload


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _gateway_identity(principal_type: str, rep_id: str) -> dict[str, Any]:
    return {
        "rep_id": rep_id,
        "book": ["A-2", "A-1"],
        "full_visibility": False,
        "principal_type": principal_type,
        "session_id": "mcp-session-1",
        "source": "gateway",
        "tenant": "demo",
    }


def _call(store: Store, principal_type: str, rep_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    runtime = _RecordingRuntime()
    result = handle(
        {"message": _MESSAGE, IDENTITY_KEY: _gateway_identity(principal_type, rep_id)},
        store=store,
        runtime=runtime,
        config=_CONFIG,
        matrix=_MATRIX,
    )
    assert "isError" not in result, result
    return result, runtime.sent_payload()


@pytest.mark.parametrize(("principal_type", "rep_id"), [("user", "rep-a"), ("service", "svc-1")])
def test_the_runtime_decodes_the_chat_target_payload(
    store: Store, principal_type: str, rep_id: str
) -> None:
    """The bytes sent decode, on the Runtime's own path, to the caller, the message and the turn."""
    result, sent = _call(store, principal_type, rep_id)

    invocation = decode_invocation(_authenticated_payload(sent), _MATRIX)

    assert invocation.identity.source == "agent"
    assert invocation.identity.principal_type == principal_type
    assert invocation.identity.rep_id == rep_id
    assert invocation.identity.book == frozenset({"A-1", "A-2"})
    assert invocation.identity.tenant == "demo"
    assert invocation.identity.full_visibility is False
    assert invocation.identity.groups == frozenset()
    assert invocation.prompt == _MESSAGE
    assert invocation.session_id == result["conversation_id"]
    assert set(invocation.resolved_tiers) == set(TIERS)


def test_saved_user_tuning_reaches_the_runtime_resolved(store: Store) -> None:
    saved = TurnTuning(
        source="ui", tiers={"tool": TierRequest(family="openai", model="openai.gpt-6-luna")}
    )
    identity = decode_identity({IDENTITY_KEY: _gateway_identity("user", "rep-a")})
    save_settings(store, identity, saved, matrix=_MATRIX)

    _, sent = _call(store, "user", "rep-a")
    invocation = decode_invocation(_authenticated_payload(sent), _MATRIX)

    assert invocation.resolved_tiers["tool"].model.model_id == "openai.gpt-6-luna"


def test_a_service_principal_runs_the_configured_defaults(store: Store) -> None:
    _, sent = _call(store, "service", "svc-1")
    invocation = decode_invocation(_authenticated_payload(sent), _MATRIX)
    expected = defaults(_MATRIX)

    for tier in TIERS:
        assert invocation.resolved_tiers[tier].model.model_id == expected.tiers[tier].model


def test_the_payload_carries_only_the_agent_identity_channel(store: Store) -> None:
    """Nothing the Runtime strips is load-bearing: the stripped payload decodes the same."""
    _, sent = _call(store, "user", "rep-a")

    assert set(sent) == {"prompt", "session_id", "tuning", "_agent_identity"}
    assert _authenticated_payload(sent) == sent


def test_an_identity_block_the_runtime_cannot_decode_is_refused_control() -> None:
    """Control: the consumer is strict, so the agreement above is not vacuous."""
    with pytest.raises(AuthenticationFailed):
        decode_invocation(
            {"prompt": _MESSAGE, "_agent_identity": {"claims": {}, "principal_type": "robot"}},
            _MATRIX,
        )


@pytest.mark.parametrize("principal_type", ["user", "service"])
def test_the_chat_target_forwards_the_principal_type_the_runtime_requires(
    store: Store, principal_type: str
) -> None:
    """The sent block declares the caller's type; dropping it makes the Runtime refuse."""
    rep_id = "svc-1" if principal_type == "service" else "rep-a"
    _, sent = _call(store, principal_type, rep_id)
    assert sent["_agent_identity"]["principal_type"] == principal_type

    del sent["_agent_identity"]["principal_type"]
    with pytest.raises(AuthenticationFailed):
        decode_invocation(_authenticated_payload(sent), _MATRIX)


@pytest.mark.parametrize("declared", ["absent", None, ""])
def test_a_gateway_identity_without_a_principal_type_reaches_no_runtime(
    store: Store, declared: object
) -> None:
    """The chat target refuses an `_identity` that declares no type; nothing is sent."""
    identity = _gateway_identity("user", "rep-a")
    if declared == "absent":
        del identity["principal_type"]
    else:
        identity["principal_type"] = declared
    runtime = _RecordingRuntime()
    result = handle(
        {"message": _MESSAGE, IDENTITY_KEY: identity},
        store=store,
        runtime=runtime,
        config=_CONFIG,
        matrix=_MATRIX,
    )
    assert result.get("isError") is True, result
    assert runtime.calls == []

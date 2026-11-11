# SPDX-License-Identifier: MIT
"""The bounds on :mod:`crm.dispatch`'s Lambda client.

One blocked synchronous tool call plus the Runtime's idle heartbeat interval must fit inside
CloudFront's relay-origin ``origin_read_timeout``. The bounds are pinned, the inequality is derived
from the Terraform and runtime sources, and the no-retry behaviour is exercised against a local
socket that never answers. No AWS call.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoRegionError,
    ReadTimeoutError,
)
from starlette.testclient import TestClient

import agents.runtime_server as runtime_server
import crm.dispatch as dispatch
from agents.runtime_server import HEARTBEAT_INTERVAL_SECONDS
from crm.envelope import AGENT_IDENTITY_KEY, CLAIM_REP_ID, CLAIM_TENANT
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from tests.unit.test_timeout_chain import INFRA, _block, _code, _number, _origin_read_timeout

#: Seconds the read timeout must exceed the action Lambda's own timeout by, so a handler that runs
#: to its limit still returns its error rather than a client-side timeout.
_MIN_READ_MARGIN_SECONDS = 2.0


def _action_lambda_timeout() -> float:
    return _number(_block(_code(INFRA / "main.tf"), 'module "lambda_action"'), "timeout_seconds")


def _worst_blocked_dispatch_seconds() -> float:
    return dispatch.TOTAL_MAX_ATTEMPTS * (
        dispatch.CONNECT_TIMEOUT_SECONDS + dispatch.READ_TIMEOUT_SECONDS
    )


def test_the_client_config_carries_the_named_bounds() -> None:
    config = dispatch.client_config()
    assert config.connect_timeout == dispatch.CONNECT_TIMEOUT_SECONDS == 2.0
    assert config.read_timeout == dispatch.READ_TIMEOUT_SECONDS == 35.0
    assert config.retries == {"mode": "standard", "total_max_attempts": 1}
    assert dispatch.TOTAL_MAX_ATTEMPTS == 1


def test_the_process_wide_client_is_built_with_the_bounded_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real `boto3` client `invoke_action` uses by default carries the bounds."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(dispatch, "_lambda_client", None)
    client = dispatch.lambda_client()
    config = client.meta.config  # type: ignore[attr-defined]
    assert config.connect_timeout == dispatch.CONNECT_TIMEOUT_SECONDS
    assert config.read_timeout == dispatch.READ_TIMEOUT_SECONDS
    assert config.retries["total_max_attempts"] == dispatch.TOTAL_MAX_ATTEMPTS
    assert dispatch.lambda_client() is client


def test_the_read_timeout_outlasts_the_action_lambda_timeout() -> None:
    """At least the deployed action-Lambda timeout plus a margin, and at most 35 s."""
    assert _action_lambda_timeout() + _MIN_READ_MARGIN_SECONDS <= dispatch.READ_TIMEOUT_SECONDS
    assert dispatch.READ_TIMEOUT_SECONDS <= 35.0


def test_a_blocked_dispatch_plus_the_heartbeat_interval_fits_the_cloudfront_read_timeout() -> None:
    """No frame can reach the relay origin while a tool call blocks; the gap must stay under 60 s."""
    gap = HEARTBEAT_INTERVAL_SECONDS + _worst_blocked_dispatch_seconds()
    assert gap < _origin_read_timeout()


@pytest.fixture
def silent_endpoint() -> Iterator[tuple[str, list[socket.socket]]]:
    """A local HTTP endpoint that accepts connections and never answers; records each accept."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.2)
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except OSError:
                continue
            accepted.append(connection)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}", accepted
    stop.set()
    thread.join(timeout=2)
    for connection in accepted:
        connection.close()
    server.close()


def _silent_client(monkeypatch: pytest.MonkeyPatch, endpoint: str) -> dispatch.LambdaInvoker:
    import boto3

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    client: dispatch.LambdaInvoker = boto3.client(
        "lambda", region_name="us-east-1", endpoint_url=endpoint, config=dispatch.client_config()
    )
    return client


def test_a_timed_out_invoke_is_never_retried(
    monkeypatch: pytest.MonkeyPatch, silent_endpoint: tuple[str, list[socket.socket]]
) -> None:
    """A read timeout ends after exactly one attempt, so a write handler is never re-run."""
    endpoint, accepted = silent_endpoint
    monkeypatch.setattr(dispatch, "READ_TIMEOUT_SECONDS", 0.2)
    client = _silent_client(monkeypatch, endpoint)

    result = dispatch.invoke_action("crm-agent-eval-create_account", {"x": 1}, client=client)
    assert result["error"]["code"] == "internal_error"
    assert len(accepted) == 1


def test_the_attempt_counter_discriminates_a_retrying_policy(
    monkeypatch: pytest.MonkeyPatch, silent_endpoint: tuple[str, list[socket.socket]]
) -> None:
    """Control: the same endpoint under three attempts sees three connections."""
    endpoint, accepted = silent_endpoint
    monkeypatch.setattr(dispatch, "READ_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(dispatch, "TOTAL_MAX_ATTEMPTS", 3)
    client = _silent_client(monkeypatch, endpoint)

    result = dispatch.invoke_action("crm-agent-eval-create_account", {"x": 1}, client=client)
    assert result["error"]["code"] == "internal_error"
    assert len(accepted) == 3


# ------------------------------------------------------------------------------------------------
# A failed invoke is a taxonomy payload, never a raw botocore exception
# ------------------------------------------------------------------------------------------------


class _RaisingInvoker:
    """A `LambdaInvoker` whose `invoke` raises ``exc``."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def invoke(self, **_kwargs: Any) -> Any:
        self.calls += 1
        raise self.exc


_FAILURES: list[tuple[str, Exception]] = [
    ("read_timeout", ReadTimeoutError(endpoint_url="https://lambda.us-east-1.amazonaws.com")),
    ("connect_timeout", ConnectTimeoutError(endpoint_url="https://lambda.us-east-1.amazonaws.com")),
    (
        "endpoint_connection",
        EndpointConnectionError(endpoint_url="https://lambda.us-east-1.amazonaws.com"),
    ),
    (
        "client_error_throttle",
        ClientError(
            {"Error": {"Code": "TooManyRequestsException", "Message": "Rate exceeded"}}, "Invoke"
        ),
    ),
    (
        "client_error_missing_function",
        ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Function not found"}},
            "Invoke",
        ),
    ),
    (
        "client_error_access_denied",
        ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "not authorized arn:aws:..."}},
            "Invoke",
        ),
    ),
]


@pytest.mark.parametrize(("name", "exc"), _FAILURES, ids=[name for name, _ in _FAILURES])
def test_a_failed_invoke_returns_a_generic_internal_error_payload(
    name: str, exc: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """Every SDK failure is one `internal_error` payload, never a record fact or the SDK's text."""
    invoker = _RaisingInvoker(exc)
    with caplog.at_level(logging.WARNING, logger=dispatch.__name__):
        result = dispatch.invoke_action("crm-agent-eval-get_account", {"x": 1}, client=invoker)

    assert result == {
        "statusCode": 500,
        "error": {
            "code": "internal_error",
            "message": "The request could not be completed.",
            "next_step": "stop",
        },
    }
    assert invoker.calls == 1
    logged = [json.loads(r.message) for r in caplog.records if "action_invoke_failed" in r.message]
    assert len(logged) == 1
    assert type(exc).__name__ in logged[0]["reason"]
    assert "operation=lambda:Invoke" in logged[0]["reason"]


def test_a_client_that_cannot_be_built_is_also_a_taxonomy_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `NoRegionError` while building the process-wide client does not escape either."""

    def _no_region() -> dispatch.LambdaInvoker:
        raise NoRegionError()

    monkeypatch.setattr(dispatch, "lambda_client", _no_region)
    result = dispatch.invoke_action("crm-agent-eval-get_account", {"x": 1})
    assert result["error"]["code"] == "internal_error"


# ------------------------------------------------------------------------------------------------
# Agent side: a timed-out tool call is a tool_failure, never a model_failure or fabricated data
# ------------------------------------------------------------------------------------------------

_CLAIMS: dict[str, Any] = {CLAIM_REP_ID: "rep-1", "custom:book": "acc-1", CLAIM_TENANT: "demo"}


@pytest.fixture
def timed_out_lambda(monkeypatch: pytest.MonkeyPatch) -> _RaisingInvoker:
    invoker = _RaisingInvoker(ReadTimeoutError(endpoint_url="https://lambda.example"))
    monkeypatch.setattr(dispatch, "lambda_client", lambda: invoker)
    monkeypatch.setenv("ACTION_LAMBDA_PREFIX", "crm-agent-eval")
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    runtime_server._stub_provider = None
    runtime_server._live_providers_by_family = {}
    return invoker


def test_a_timed_out_delegated_tool_call_classifies_as_tool_failure(
    timed_out_lambda: _RaisingInvoker,
) -> None:
    """Through the Runtime's delegation mode: `tool_failure`, no reported data, an honest reply."""
    response = TestClient(runtime_server.app).post(
        "/invocations",
        json={
            "mode": "delegation",
            "tool_name": "get_account",
            "tool_input": {"account_id": "acc-1"},
            AGENT_IDENTITY_KEY: {
                "claims": dict(_CLAIMS),
                "session_id": "sess-timeout",
                "principal_type": "user",
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert timed_out_lambda.calls == 1
    assert body["outcome"]["kind"] == "tool_failure"
    assert body["reported_result"] is None
    assert body["reply_text"] == (
        "That request could not be completed: The request could not be completed."
    )


class _GetAccountThenAnswer(ModelProvider):
    """Calls `get_account` directly, then answers with fixed text."""

    def __init__(self) -> None:
        self.calls = 0

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.calls += 1
        return self._emit(first=self.calls == 1)

    async def _emit(self, *, first: bool) -> AsyncIterator[ProviderEvent]:
        if first:
            yield ProviderEvent(
                type="tool_call",
                tool_name="get_account",
                tool_input={"account_id": "acc-1"},
                tool_id="t1",
            )
            yield ProviderEvent(type="usage", input_tokens=1, output_tokens=1)
            yield ProviderEvent(type="completed", awaiting_tools=True)
            return
        yield ProviderEvent(type="text_delta", text="I could not reach that record.")
        yield ProviderEvent(type="usage", input_tokens=1, output_tokens=1)
        yield ProviderEvent(type="completed", awaiting_tools=False)


def test_a_timed_out_tool_call_in_a_chat_turn_is_a_tool_result_error_not_a_model_failure(
    timed_out_lambda: _RaisingInvoker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat turn carries the failure as its tool result and still completes."""
    provider = _GetAccountThenAnswer()
    monkeypatch.setattr(runtime_server, "_model_provider", lambda *_a: provider)
    with TestClient(runtime_server.app).stream(
        "POST",
        "/invocations",
        json={
            "prompt": "How is Acme?",
            "session_id": "sess-timeout-chat",
            AGENT_IDENTITY_KEY: {
                "claims": dict(_CLAIMS),
                "session_id": "sess-timeout-chat",
                "principal_type": "user",
            },
        },
    ) as response:
        assert response.status_code == 200
        frames = [
            json.loads(line.removeprefix("data: "))
            for line in "".join(response.iter_text()).splitlines()
            if line.startswith("data: ")
        ]

    kinds = [frame["kind"] for frame in frames]
    assert timed_out_lambda.calls == 1
    assert "error" not in kinds
    assert kinds[-1] == "completed"
    tool_results = [frame for frame in frames if frame["kind"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_output"]["error"]["code"] == "internal_error"

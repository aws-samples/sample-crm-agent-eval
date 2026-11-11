# SPDX-License-Identifier: MIT
"""The Runtime's ``eval_turn`` mode in :mod:`agents.runtime_server`.

Drives ``POST /invocations`` through :class:`starlette.testclient.TestClient` with the real
:class:`providers.stub.StubProvider` or per-family fakes, and a fake tool dispatch. No model call and
no AWS call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

import agents.runtime_server as runtime_server
from agents.budget import EVAL_BUDGET, EVAL_TURN_HARD_MARGIN_SECONDS, TurnBudgetSpec
from crm.envelope import AGENT_IDENTITY_KEY, CLAIM_REP_ID, CLAIM_TENANT, Identity
from eval.turn_observation import deserialise_turn, serialise_turn
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, RetriedAttempt
from providers.tier_tuning import WIRE_SCHEMA_VERSION

EVAL_TENANT = "eval-run123"

_ARM_TUNING: dict[str, Any] = {
    "schema": WIRE_SCHEMA_VERSION,
    "source": "eval.arm",
    "tiers": {
        "supervisor": {"family": "anthropic", "effort": "low"},
        "subagent": {"family": "anthropic"},
        "tool": {"family": "anthropic"},
    },
}


def _request(
    *, tenant: str = EVAL_TENANT, provider_mode: str = "stub", **overrides: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "mode": "eval_turn",
        AGENT_IDENTITY_KEY: {
            "claims": {
                CLAIM_REP_ID: "eval-rep",
                "custom:book": "acc-1,acc-2",
                "custom:full_visibility": "false",
                CLAIM_TENANT: tenant,
            },
            "session_id": "run123-00-gt-01-account-lookup-padded",
            "principal_type": "user",
        },
        "prompt": "What is the status of Acme?",
        "task_id": "gt-01-account-lookup",
        "provider_mode": provider_mode,
        "tuning": _ARM_TUNING,
    }
    body.update(overrides)
    return {key: value for key, value in body.items() if value is not None}


class _ScriptedProvider(ModelProvider):
    """Calls `get_account` once, then answers; optionally reports one SDK-retried 429 attempt."""

    def __init__(self, *, report_retry: bool = False) -> None:
        self.report_retry = report_retry
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
        retried = (RetriedAttempt(status=429, error_type=None),) if self.report_retry else ()
        if first:
            yield ProviderEvent(
                type="tool_call",
                tool_name="get_account",
                tool_input={"account_id": "acc-1"},
                tool_id="t1",
            )
            yield ProviderEvent(
                type="usage", input_tokens=3, output_tokens=1, retried_attempts=retried
            )
            yield ProviderEvent(type="completed", awaiting_tools=True)
            return
        yield ProviderEvent(type="text_delta", text="Acme is healthy.")
        yield ProviderEvent(type="usage", input_tokens=2, output_tokens=1)
        yield ProviderEvent(type="completed", awaiting_tools=False)


@pytest.fixture(autouse=True)
def _reset_process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    runtime_server._matrix = None
    runtime_server._stub_provider = None
    runtime_server._live_providers_by_family = {}
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    yield
    runtime_server._stub_provider = None
    runtime_server._live_providers_by_family = {}


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, object]]]:
    """Replace the action-Lambda dispatch; records (identity tenant, tool name, tool input)."""
    calls: list[tuple[str, str, dict[str, object]]] = []

    def _production_dispatch(identity: Identity) -> Any:
        def _dispatch(tool: Any, tool_input: dict[str, object]) -> dict[str, Any]:
            calls.append((identity.tenant, tool.name, dict(tool_input)))
            return {"statusCode": 200, "body": {"account_id": "acc-1", "name": "Acme"}}

        return _dispatch

    monkeypatch.setattr(runtime_server, "production_dispatch", _production_dispatch)
    return calls


@pytest.fixture
def no_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the request reaches the turn itself."""

    def _never(**_kwargs: object) -> dict[str, Any]:
        raise AssertionError("a rejected eval_turn must never start a turn")

    monkeypatch.setattr(runtime_server, "_run_eval_turn", _never)


def _live_adapters(monkeypatch: pytest.MonkeyPatch, **providers: ModelProvider) -> list[str]:
    """Select a live `AGENT_PROVIDER` and serve ``providers`` per family; returns families built."""
    built: list[str] = []

    def _build(family: str, *, region_name: str) -> ModelProvider:
        built.append(family)
        return providers[family]

    monkeypatch.setattr("providers.factory.build_provider_for_family", _build)
    monkeypatch.setenv("AGENT_PROVIDER", "live")
    return built


@pytest.fixture
def client() -> TestClient:
    return TestClient(runtime_server.app)


# ------------------------------------------------------------------------------------------------
# Guards: identity, tenant, required fields, provider mode
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tenant", ["demo", "canary", "eval-", "evaluation-1", "EVAL-run123"])
@pytest.mark.usefixtures("no_turn")
def test_a_non_eval_tenant_is_refused_with_403(client: TestClient, tenant: str) -> None:
    """Only an `eval-<run_id>` tenant may run an eval turn; a user's `demo` token cannot."""
    response = client.post("/invocations", json=_request(tenant=tenant))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scope_denied"
    assert "text/event-stream" not in response.headers["content-type"]


@pytest.mark.usefixtures("no_turn")
def test_an_eval_turn_without_identity_is_refused_with_401(client: TestClient) -> None:
    """No `_agent_identity` block means no turn."""
    body = _request()
    del body[AGENT_IDENTITY_KEY]
    response = client.post("/invocations", json=body)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


@pytest.mark.usefixtures("no_turn")
def test_a_forged_gateway_identity_channel_does_not_reach_the_eval_turn(client: TestClient) -> None:
    """An `_identity` block naming an eval tenant is stripped; with no `_agent_identity`, it is 401."""
    body = _request()
    body["_identity"] = body.pop(AGENT_IDENTITY_KEY)
    response = client.post("/invocations", json=body)
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("overrides", "source"),
    [
        ({"prompt": None}, "agent.invocation.prompt"),
        ({"prompt": "   "}, "agent.invocation.prompt"),
        ({"task_id": None}, "agent.invocation.task_id"),
        ({"tuning": None}, "agent.invocation.tuning"),
        ({"tuning": {"family": "anthropic", "effort": "low"}}, "tuning.schema"),
        (
            {"tuning": {"schema": WIRE_SCHEMA_VERSION, "tiers": {"tool": {"family": "gemini"}}}},
            "tuning.tiers.tool.family",
        ),
        (
            {"tuning": {**_ARM_TUNING, "tiers": {"subagent": {"temperature": 0.2}}}},
            "tuning.tiers.subagent.temperature",
        ),
        (
            {"tuning": {"schema": 2, "tiers": {"tool": {"temperature": None}}}},
            "tuning.tiers.tool.temperature",
        ),
        ({"provider_mode": None}, "agent.invocation.provider_mode"),
        ({"provider_mode": "free"}, "agent.invocation.provider_mode"),
    ],
)
@pytest.mark.usefixtures("no_turn")
def test_an_invalid_eval_turn_request_is_a_400_naming_the_field(
    client: TestClient, overrides: dict[str, Any], source: str
) -> None:
    """Every required field and the arm's schema-3 tuning are checked before the turn starts."""
    body = _request()
    for key, value in overrides.items():
        if value is None:
            body.pop(key, None)
        else:
            body[key] = value
    response = client.post("/invocations", json=body)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["source"] == source


@pytest.mark.usefixtures("no_turn")
def test_live_provider_mode_is_refused_when_the_runtime_is_stub_only(client: TestClient) -> None:
    """`provider_mode: live` needs a live `AGENT_PROVIDER`; it can never upgrade a stub Runtime."""
    response = client.post("/invocations", json=_request(provider_mode="live"))
    assert response.status_code == 400
    assert response.json()["error"]["source"] == "agent.invocation.provider_mode"


# ------------------------------------------------------------------------------------------------
# A stub eval turn: serialised observation, never a live adapter
# ------------------------------------------------------------------------------------------------


def test_a_stub_eval_turn_returns_the_serialised_observation(
    client: TestClient, dispatched: list[tuple[str, str, dict[str, object]]]
) -> None:
    """The 200 body is exactly `serialise_turn`'s wire object for the eval tenant's turn."""
    response = client.post("/invocations", json=_request())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()

    turn = deserialise_turn(body)
    assert serialise_turn(turn) == body
    assert turn.outcome.tenant == EVAL_TENANT
    assert turn.stream_calls >= 1
    assert turn.exclusions == ()
    assert turn.turn_latency_ms is not None and turn.turn_latency_ms >= 0
    assert {inv.tier for inv in turn.invocations} >= {"supervisor"}
    assert all(tenant == EVAL_TENANT for tenant, _, _ in dispatched)
    assert "usage_lower_bound" not in body
    assert turn.usage_lower_bound is False


def test_stub_provider_mode_is_honoured_even_on_a_live_runtime(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    dispatched: list[tuple[str, str, dict[str, object]]],
) -> None:
    """`provider_mode: stub` can only lower spend: no live adapter is built on a live Runtime."""
    built = _live_adapters(monkeypatch)
    response = client.post("/invocations", json=_request(provider_mode="stub"))
    assert response.status_code == 200
    assert built == []
    assert deserialise_turn(response.json()).stream_calls >= 1


# ------------------------------------------------------------------------------------------------
# A live eval turn: per-family adapters under bounded retry, exclusions per turn
# ------------------------------------------------------------------------------------------------


def test_a_live_eval_turn_dispatches_as_the_eval_identity_and_reports_tool_calls(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    dispatched: list[tuple[str, str, dict[str, object]]],
) -> None:
    """The live turn runs the family adapter and observes the dispatched tool call and arguments."""
    adapter = _ScriptedProvider()
    built = _live_adapters(monkeypatch, anthropic=adapter)
    response = client.post("/invocations", json=_request(provider_mode="live"))
    assert response.status_code == 200, response.text
    turn = deserialise_turn(response.json())

    assert built == ["anthropic"]
    assert adapter.calls == 2
    assert turn.stream_calls == 2
    assert dispatched == [(EVAL_TENANT, "get_account", {"account_id": "acc-1"})]
    assert [call.tool_name for call in turn.outcome.tool_calls] == ["get_account"]
    assert [args.tool_name for args in turn.tool_arguments] == ["get_account"]


def test_a_live_eval_turn_reports_retry_exclusions_and_does_not_carry_them_to_the_next_turn(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    dispatched: list[tuple[str, str, dict[str, object]]],
) -> None:
    """Each turn wraps the adapter afresh, so one turn's throttle never appears in another's body."""
    adapter = _ScriptedProvider(report_retry=True)
    _live_adapters(monkeypatch, anthropic=adapter)
    first = deserialise_turn(
        client.post("/invocations", json=_request(provider_mode="live")).json()
    )
    assert [exclusion.category.value for exclusion in first.exclusions] == ["throttle"]

    adapter.report_retry = False
    adapter.calls = 0
    second = deserialise_turn(
        client.post("/invocations", json=_request(provider_mode="live")).json()
    )
    assert second.exclusions == ()


def test_a_failing_eval_turn_is_a_500_taxonomy_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception inside the turn becomes `internal_error` JSON, never a stack trace."""

    def _boom(**_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("model exploded with secret detail")

    monkeypatch.setattr(runtime_server, "_run_eval_turn", _boom)
    response = client.post("/invocations", json=_request())
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert "secret" not in response.text


def test_each_live_eval_turn_gets_its_own_retry_wrapper_over_the_cached_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two turns' resolvers wrap the one cached family adapter in distinct retry wrappers."""
    from eval.live import ThrottleRetryingProvider

    adapter = _ScriptedProvider()
    built = _live_adapters(monkeypatch, anthropic=adapter)
    first = runtime_server._eval_turn_providers("live")
    second = runtime_server._eval_turn_providers("live")

    one, two = first("anthropic"), second("anthropic")
    assert isinstance(one, ThrottleRetryingProvider)
    assert isinstance(two, ThrottleRetryingProvider)
    assert one is not two
    assert first("anthropic") is one
    assert built == ["anthropic"]


def test_an_absent_provider_mode_is_reported_as_missing_not_as_the_value_none(
    client: TestClient, no_turn: None
) -> None:
    """An absent `provider_mode` is named as required, never stringified to `'None'`."""
    body = _request()
    del body["provider_mode"]
    error = client.post("/invocations", json=body).json()["error"]
    assert error["message"] == "provider_mode is required."


def test_a_setup_failure_outside_the_taxonomy_is_a_taxonomy_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, no_turn: None
) -> None:
    """A capability-matrix load failure becomes `internal_error` JSON, not a bare server error."""

    def _unreadable() -> Any:
        raise OSError("config/models.yaml unreadable")

    monkeypatch.setattr(runtime_server, "_capability_matrix", _unreadable)
    response = client.post("/invocations", json=_request())
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "unreadable" not in response.text


class _FailingProvider(ModelProvider):
    """Reports a non-retryable in-band failure, which the retry wrapper raises at once."""

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        return self._emit()

    async def _emit(self) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(type="error", error_message="AccessDeniedException: no model access")


def test_a_live_provider_failure_after_retries_is_a_503_provider_unavailable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    dispatched: list[tuple[str, str, dict[str, object]]],
) -> None:
    """`LiveInvocationError` is classified, not folded into the generic 500."""
    _live_adapters(monkeypatch, anthropic=_FailingProvider())
    response = client.post("/invocations", json=_request(provider_mode="live"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"
    assert "AccessDenied" not in response.text


class _HangingProvider(ModelProvider):
    """Never answers within any test's patience."""

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        return self._emit()

    async def _emit(self) -> AsyncIterator[ProviderEvent]:
        await asyncio.sleep(30)
        yield ProviderEvent(type="completed", awaiting_tools=False)


def test_an_eval_turn_past_its_deadline_is_cancelled_and_reported_as_a_504(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    dispatched: list[tuple[str, str, dict[str, object]]],
) -> None:
    """An eval turn past `_EVAL_TURN_DEADLINE_SECONDS` is cancelled and returned as a 504 `turn_timeout`."""
    soft_plus_margin = EVAL_BUDGET.max_wall_clock_seconds + EVAL_TURN_HARD_MARGIN_SECONDS
    assert soft_plus_margin == runtime_server._EVAL_TURN_DEADLINE_SECONDS
    monkeypatch.setattr(runtime_server, "_EVAL_TURN_DEADLINE_SECONDS", 0.05)
    _live_adapters(monkeypatch, anthropic=_HangingProvider())
    started = time.monotonic()
    response = client.post("/invocations", json=_request(provider_mode="live"))
    assert time.monotonic() - started < 5
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "turn_timeout"


class _DelegationStallsProvider(ModelProvider):
    """Supervisor delegates, the subagent calls one leaf and asks for more, then never answers."""

    def __init__(self) -> None:
        self.calls = 0

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        self.calls += 1
        return self._emit(self.calls)

    async def _emit(self, call: int) -> AsyncIterator[ProviderEvent]:
        if call == 1:
            yield ProviderEvent(
                type="tool_call", tool_name="research", tool_input={"intent": "acme"}, tool_id="t1"
            )
            yield ProviderEvent(type="completed", awaiting_tools=True)
            return
        if call in (2, 3):
            yield ProviderEvent(
                type="tool_call",
                tool_name="get_account",
                tool_input={"account_id": "acc-1"},
                tool_id=f"s{call}",
            )
            yield ProviderEvent(type="usage", input_tokens=40, output_tokens=10)
            yield ProviderEvent(type="completed", awaiting_tools=call == 2)
            return
        await asyncio.sleep(3600)
        yield ProviderEvent(type="completed", awaiting_tools=False)


def test_a_delegation_outlasting_the_soft_cap_is_a_200_budget_exhausted_observation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    dispatched: list[tuple[str, str, dict[str, object]]],
) -> None:
    """The soft cap ends a stalled delegation before the hard cancel, as a scored partial."""
    import eval.turn_observation as turn_observation

    soft = 0.3
    monkeypatch.setattr(
        turn_observation,
        "EVAL_BUDGET",
        TurnBudgetSpec(max_tool_calls=EVAL_BUDGET.max_tool_calls, max_wall_clock_seconds=soft),
    )
    monkeypatch.setattr(
        runtime_server, "_EVAL_TURN_DEADLINE_SECONDS", soft + EVAL_TURN_HARD_MARGIN_SECONDS
    )
    _live_adapters(monkeypatch, anthropic=_DelegationStallsProvider())
    started = time.monotonic()
    response = client.post("/invocations", json=_request(provider_mode="live"))
    assert time.monotonic() - started < EVAL_TURN_HARD_MARGIN_SECONDS
    assert response.status_code == 200, response.text
    turn = deserialise_turn(response.json())
    assert turn.outcome.budget_exhausted is True
    assert response.json()["usage_lower_bound"] is True
    assert turn.usage_lower_bound is True
    assert [call.tool_name for call in turn.tool_calls] == ["get_account"]
    assert [(tenant, name) for tenant, name, _ in dispatched] == [(EVAL_TENANT, "get_account")]
    assert {inv.tier for inv in turn.invocations} == {"subagent", "tool"}
    assert sum(inv.tokens.input_tokens for inv in turn.invocations) > 0


def test_eval_turn_logs_carry_the_runtime_session_and_the_tenant(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    dispatched: list[tuple[str, str, dict[str, object]]],
) -> None:
    """Success and failure lines name the per-arm runtime session and the run's tenant."""
    with caplog.at_level(logging.INFO, logger=runtime_server.__name__):
        client.post("/invocations", json=_request())
        _live_adapters(monkeypatch, anthropic=_FailingProvider())
        client.post("/invocations", json=_request(provider_mode="live"))

    lines = [
        json.loads(record.message)
        for record in caplog.records
        if record.message.startswith('{"event": "eval_turn')
    ]
    by_event = {line["event"]: line for line in lines}
    session = _request()[AGENT_IDENTITY_KEY]["session_id"]
    assert by_event["eval_turn"]["session_id"] == session
    assert by_event["eval_turn"]["correlation_id"] == EVAL_TENANT
    assert by_event["eval_turn"]["reason"] == "provider_mode=stub"
    assert by_event["eval_turn_failed"]["session_id"] == session
    assert by_event["eval_turn_failed"]["correlation_id"] == EVAL_TENANT
    assert by_event["eval_turn_failed"]["reason"].startswith("provider_unavailable")

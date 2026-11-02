# SPDX-License-Identifier: MIT
"""The eval-only fault is unreachable from the Runtime chat and delegation paths.

Against DynamoDB Local in a seeded ``eval-<run_id>`` tenant, with gt-16's fault declared in a
fixture registry: ``observe_turn`` and the Runtime ``eval_turn`` mode report ``tool_failure`` for
``list_activities`` (the positive control), while the chat turn path and the ``delegation`` mode,
for the same prompt, task id and session, reach the real handler and get ``data``. An
``eval_turn`` request in the ``demo`` tenant is refused with 403 before any dispatch.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
import pytest
from starlette.testclient import TestClient

import agents.runtime_server as runtime_server
import agents.tool_dispatch as tool_dispatch
import eval.fault_injection as fault_injection
from agents.runtime_entrypoint import RuntimeInvocation
from agents.supervisor import classify_tool_result
from agents.tool_dispatch import production_dispatch
from crm.envelope import (
    AGENT_IDENTITY_KEY,
    CLAIM_BOOK,
    CLAIM_FULL_VISIBILITY,
    CLAIM_REP_ID,
    CLAIM_TENANT,
    Identity,
)
from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLE_NAMES
from crm.store.store import Store
from crm.toolspec.definitions import ALL_TOOLS
from eval.harness import _DEFAULT_GOLDEN_TASKS_PATH, _load_golden_tasks
from eval.sweep_units import ArmSpec, arm_turn_tuning
from eval.task_ids import compose_ids_in_text, localize_tasks
from eval.tenancy import seed_tenant, tenant_for_run
from eval.turn_observation import deserialise_turn, observe_turn
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from providers.capabilities import load_capability_matrix
from providers.tier_tuning import WIRE_SCHEMA_VERSION, resolve_turn

MATRIX = load_capability_matrix()
#: DynamoDB Local on loopback, as in ``tests/integration/conftest.py``; the credential is a placeholder.
_ENDPOINT_URL = "http://127.0.0.1:8000"
_LOCAL_ENGINE_CRED = "local"
_GT16_PREFIX = "gt-16-"
_HANDLERS = {definition.name: definition.handler for definition in ALL_TOOLS}
_ARM_TUNING: dict[str, Any] = {
    "schema": WIRE_SCHEMA_VERSION,
    "source": "eval.arm",
    "tiers": {
        "supervisor": {"family": "anthropic"},
        "subagent": {"family": "anthropic"},
        "tool": {"family": "anthropic"},
    },
}


@dataclass(frozen=True)
class _Gt16:
    task_id: str
    prompt: str
    tenant: str
    rep_id: str
    book: tuple[str, ...]
    opportunity_id: str


@dataclass
class _LocalLambda:
    """Stands in for ``lambda:Invoke``: runs the named action handler in-process on ``store``."""

    store: Store
    invoked: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def __call__(
        self, function_name: str, event: dict[str, Any], *, client: Any = None
    ) -> dict[str, Any]:
        assert client is None, "the production path never supplies a client"
        module_path, _, attr = _HANDLERS[function_name].rpartition(".")
        handler = getattr(importlib.import_module(module_path), attr)
        output = dict(handler(event, store=self.store))
        self.invoked.append((function_name, output))
        return output


class _Scripted(ModelProvider):
    """Delegates to research, which calls ``list_activities``; then answers."""

    def __init__(self, opportunity_id: str) -> None:
        call: dict[str, object] = {"opportunity_id": opportunity_id}
        self._rounds = [
            _events("research", {"intent": f"activity history of {opportunity_id}"}, True),
            _events("list_activities", call, False),
            _events("list_activities", call, False),
            [
                ProviderEvent(type="text_delta", text="done"),
                ProviderEvent(type="usage", input_tokens=4, output_tokens=2),
                ProviderEvent(type="completed", text="done"),
            ],
        ]
        self.calls = 0

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        index = min(self.calls, len(self._rounds) - 1)
        self.calls += 1
        for event in self._rounds[index]:
            yield event


def _events(name: str, tool_input: dict[str, object], awaiting: bool) -> list[ProviderEvent]:
    return [
        ProviderEvent(type="tool_call", tool_name=name, tool_input=tool_input, tool_id="t1"),
        ProviderEvent(type="usage", input_tokens=4, output_tokens=2),
        ProviderEvent(type="completed", awaiting_tools=awaiting),
    ]


@pytest.fixture(scope="module")
def _seeded(_dynamodb_local_endpoint: None) -> Iterator[tuple[Store, str]]:
    """One freshly created table set and seeded eval tenant, shared by this module's read-only tests."""
    client = boto3.client(
        "dynamodb",
        endpoint_url=_ENDPOINT_URL,
        region_name="us-east-1",
        aws_access_key_id=_LOCAL_ENGINE_CRED,
        aws_secret_access_key=_LOCAL_ENGINE_CRED,
    )
    prefix = f"t{uuid.uuid4().hex[:12]}_"
    create_all_tables(client, table_prefix=prefix)
    try:
        store = Store(table_prefix=prefix, client=client)
        tenant = tenant_for_run(f"fi{uuid.uuid4().hex[:10]}")
        seed_tenant(store, tenant=tenant)
        yield store, tenant
    finally:
        for table in TABLE_NAMES:
            with contextlib.suppress(client.exceptions.ResourceNotFoundException):
                client.delete_table(TableName=f"{prefix}{table}")


@pytest.fixture
def gt16(
    _seeded: tuple[Store, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[_Gt16, _LocalLambda]]:
    """gt-16 in the seeded eval tenant, its fault declared, ``lambda:Invoke`` run locally."""
    store, tenant = _seeded
    (raw,) = [
        task
        for task in _load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH)
        if str(task["id"]).startswith(_GT16_PREFIX)
    ]
    (task,) = localize_tasks([raw], tenant=tenant)
    registry = tmp_path / "golden.yaml"
    registry.write_text(
        f"tasks:\n  - id: {task['id']}\n"
        "    fault_injection: {tool: list_activities, fault: transport}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fault_injection, "_default_registry_path", lambda: registry)
    monkeypatch.delenv("ACTION_LAMBDA_PREFIX", raising=False)
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    local = _LocalLambda(store)
    monkeypatch.setattr(tool_dispatch, "invoke_action", local)
    runtime_server._matrix = None
    runtime_server._stub_provider = None
    actor = task["actor"]
    yield (
        _Gt16(
            task_id=str(task["id"]),
            prompt=str(task["prompt"]),
            tenant=tenant,
            rep_id=str(actor["rep_id"]),
            book=tuple(str(account) for account in actor["book"]),
            opportunity_id=compose_ids_in_text("opp-0000", tenant=tenant),
        ),
        local,
    )
    runtime_server._stub_provider = None


def _identity(task: _Gt16) -> Identity:
    return Identity(
        rep_id=task.rep_id,
        book=frozenset(task.book),
        full_visibility=False,
        principal_type="user",
        session_id=task.task_id,
        source="agent",
        tenant=task.tenant,
    )


def _agent_block(task: _Gt16, *, tenant: str) -> dict[str, Any]:
    return {
        "claims": {
            CLAIM_REP_ID: task.rep_id,
            CLAIM_BOOK: ",".join(task.book),
            CLAIM_FULL_VISIBILITY: "false",
            CLAIM_TENANT: tenant,
        },
        "session_id": task.task_id,
    }


def _assert_list_activities_returned_data(local: _LocalLambda) -> None:
    assert [name for name, _ in local.invoked] == ["list_activities"]
    output = local.invoked[0][1]
    assert classify_tool_result(output).kind == "data"
    assert output["body"]["activities"], "the seeded opportunity has no activities to return"


def test_control_observe_turn_reports_tool_failure_for_gt16(
    gt16: tuple[_Gt16, _LocalLambda],
) -> None:
    task, local = gt16
    turn = asyncio.run(
        observe_turn(
            prompt=task.prompt,
            task_id=task.task_id,
            tenant=task.tenant,
            providers=lambda _family: _Scripted(task.opportunity_id),
            matrix=MATRIX,
            tuning=arm_turn_tuning(ArmSpec(family="anthropic")),
            dispatch=production_dispatch(_identity(task)),
        )
    )
    assert local.invoked == []
    assert [call.outcome_kind for call in turn.tool_calls] == ["tool_failure"]
    assert turn.outcome.denial_outcome == "tool_failure"


def test_control_runtime_eval_turn_mode_reports_tool_failure_for_gt16(
    gt16: tuple[_Gt16, _LocalLambda], monkeypatch: pytest.MonkeyPatch
) -> None:
    task, local = gt16
    provider = _Scripted(task.opportunity_id)
    monkeypatch.setattr(runtime_server, "_eval_turn_providers", lambda _mode: lambda _f: provider)
    response = TestClient(runtime_server.app).post(
        "/invocations",
        json={
            "mode": "eval_turn",
            AGENT_IDENTITY_KEY: _agent_block(task, tenant=task.tenant),
            "prompt": task.prompt,
            "task_id": task.task_id,
            "provider_mode": "stub",
            "tuning": _ARM_TUNING,
        },
    )
    assert response.status_code == 200, response.text
    turn = deserialise_turn(response.json())
    assert local.invoked == []
    assert turn.outcome.denial_outcome == "tool_failure"


def test_chat_turn_path_returns_data_from_list_activities(
    gt16: tuple[_Gt16, _LocalLambda],
) -> None:
    task, local = gt16
    from agents.turn_driver import session_turn_tuning
    from providers.base import Tuning

    tuning = session_turn_tuning(Tuning(source="test.fault", family="anthropic", effort=None))
    invocation = RuntimeInvocation(
        identity=_identity(task),
        session_id=task.task_id,
        prompt=task.prompt,
        tuning=tuning,
        resolved_tiers=resolve_turn(MATRIX, tuning),
    )
    provider = _Scripted(task.opportunity_id)

    async def _drain() -> list[str]:
        return [
            event.kind
            async for event in runtime_server._agent_events_for_turn(
                invocation, provider, matrix=MATRIX
            )
        ]

    kinds = asyncio.run(_drain())
    assert "completed" in kinds
    _assert_list_activities_returned_data(local)


def test_delegation_mode_returns_data_from_list_activities(
    gt16: tuple[_Gt16, _LocalLambda],
) -> None:
    task, local = gt16
    response = TestClient(runtime_server.app).post(
        "/invocations",
        json={
            "mode": "delegation",
            AGENT_IDENTITY_KEY: _agent_block(task, tenant=task.tenant),
            "task_id": task.task_id,
            "prompt": task.prompt,
            "tool_name": "list_activities",
            "tool_input": {"opportunity_id": task.opportunity_id},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"]["kind"] == "data"
    _assert_list_activities_returned_data(local)


def test_eval_turn_in_the_demo_tenant_is_refused_before_any_dispatch(
    gt16: tuple[_Gt16, _LocalLambda], monkeypatch: pytest.MonkeyPatch
) -> None:
    task, local = gt16
    provider = _Scripted(task.opportunity_id)
    monkeypatch.setattr(runtime_server, "_eval_turn_providers", lambda _mode: lambda _f: provider)
    response = TestClient(runtime_server.app).post(
        "/invocations",
        json={
            "mode": "eval_turn",
            AGENT_IDENTITY_KEY: _agent_block(task, tenant="demo"),
            "prompt": task.prompt,
            "task_id": task.task_id,
            "provider_mode": "stub",
            "tuning": _ARM_TUNING,
        },
    )
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "scope_denied"
    assert error["missing_scope"] == "tenant:eval"
    assert local.invoked == []
    assert provider.calls == 0

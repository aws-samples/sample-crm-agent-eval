# SPDX-License-Identifier: MIT
"""gt-15 runs as a service principal through both eval dispatch substrates, on DynamoDB Local.

The in-process substrate dispatches on the gateway ``_identity`` channel; the live-shaped one
builds the ``_agent_identity`` event the deployed Runtime sends and invokes the action handler
through :func:`crm.dispatch.invoke_action`, whose Lambda client is replaced by an in-process one.
Either way the listing handler decodes ``principal_type == "service"`` and refuses the empty book
with ``missing_scope: "account:*"``.
"""

from __future__ import annotations

import importlib
import io
import json
from collections.abc import Iterator
from typing import Any

import pytest

import crm.dispatch
import crm.handlers._common as handler_common
from agents.registry import full_registry
from agents.supervisor import classify_tool_result
from crm.authz import Identity
from crm.envelope import decode_identity
from crm.store.store import Store
from crm.toolspec.definitions import ALL_TOOLS
from eval import harness
from eval.harness import _DEFAULT_GOLDEN_TASKS_PATH, _load_golden_tasks
from eval.task_ids import localize_tasks
from eval.tenancy import seed_tenant, teardown_tenant, tenant_for_run

_GT15 = "gt-15-denial-service-principal-no-implicit-visibility"
_LISTING_TOOL = "search_opportunities"


class _InProcessLambda:
    """Serves ``lambda:InvokeFunction`` by calling the named action handler in-process."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._handlers = {tool.name: tool.handler for tool in ALL_TOOLS}

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        module_path, _, attribute = self._handlers[kwargs["FunctionName"]].rpartition(".")
        handler = getattr(importlib.import_module(module_path), attribute)
        result = handler(json.loads(kwargs["Payload"]), store=self._store)
        return {"Payload": io.BytesIO(json.dumps(result).encode("utf-8"))}


@pytest.fixture
def seeded(dynamodb_local_store: tuple[Store, str]) -> Iterator[tuple[Store, str]]:
    store, _prefix = dynamodb_local_store
    tenant = tenant_for_run("gt15-service")
    seed_tenant(store, tenant=tenant)
    try:
        yield store, tenant
    finally:
        teardown_tenant(store, tenant=tenant)


@pytest.fixture
def decoded(monkeypatch: pytest.MonkeyPatch) -> list[Identity]:
    """Every identity an action handler decodes, in order."""
    seen: list[Identity] = []
    real = decode_identity

    def _recording(event: dict[str, Any]) -> Identity:
        identity = real(event)
        seen.append(identity)
        return identity

    monkeypatch.setattr(handler_common, "decode_identity", _recording)
    return seen


def _gt15_actor(tenant: str) -> dict[str, object]:
    tasks = localize_tasks(_load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH), tenant=tenant)
    (task,) = [t for t in tasks if t["id"] == _GT15]
    actor = task["actor"]
    assert isinstance(actor, dict)
    return actor


def _assert_service_denied(output: dict[str, Any], decoded: list[Identity]) -> None:
    (identity,) = decoded
    assert identity.principal_type == "service"
    assert identity.full_visibility is False
    assert identity.book == frozenset()
    outcome = classify_tool_result(output)
    assert outcome.kind == "denied", output
    assert outcome.missing_scope == "account:*"


def test_gt15_on_the_in_process_gateway_channel_is_a_denied_service_principal(
    seeded: tuple[Store, str], decoded: list[Identity]
) -> None:
    store, tenant = seeded
    dispatch = harness.in_process_dispatch_factory(
        store=store, actor=_gt15_actor(tenant), tenant=tenant
    )
    output = dispatch(full_registry()[_LISTING_TOOL], {})
    _assert_service_denied(output, decoded)


def test_gt15_on_the_live_shaped_agent_channel_is_a_denied_service_principal(
    seeded: tuple[Store, str], decoded: list[Identity], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, tenant = seeded
    monkeypatch.delenv("ACTION_LAMBDA_PREFIX", raising=False)
    monkeypatch.setattr(crm.dispatch, "_lambda_client", _InProcessLambda(store))
    dispatch = harness.live_lambda_dispatch_factory(
        store=store, actor=_gt15_actor(tenant), tenant=tenant
    )
    output = dispatch(full_registry()[_LISTING_TOOL], {})
    _assert_service_denied(output, decoded)


def test_a_user_actor_with_a_book_is_not_denied_control(
    seeded: tuple[Store, str], decoded: list[Identity]
) -> None:
    """Control: the same dispatch for a user with a book returns data, so the denial is gt-15's."""
    store, tenant = seeded
    tasks = localize_tasks(_load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH), tenant=tenant)
    actor = next(
        t["actor"]
        for t in tasks
        if isinstance(t["actor"], dict) and t["actor"]["book"] and t["id"] != _GT15
    )
    assert isinstance(actor, dict)
    dispatch = harness.in_process_dispatch_factory(store=store, actor=actor, tenant=tenant)
    output = dispatch(full_registry()[_LISTING_TOOL], {})
    (identity,) = decoded
    assert identity.principal_type == "user"
    assert classify_tool_result(output).kind == "data", output

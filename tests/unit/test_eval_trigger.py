# SPDX-License-Identifier: MIT
"""Tests for `eval.trigger`'s authorization and job-row keys, and the `eval_trigger` handler.

* Live mode is reachable only through `submit_sweep`: no other public function takes a `mode`,
  `live`, or `substrate` parameter.
* The handler authorizes before any write and returns the flat or proxy shape for its channel.

The store is a real `moto`-backed `Store`.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.authz import Identity
from crm.envelope import (
    AGENT_IDENTITY_KEY,
    CLAIM_FULL_VISIBILITY,
    CLAIM_REP_ID,
    CLAIM_TENANT,
    identity_event,
)
from crm.errors import AuthenticationFailed, NotFound, ScopeDenied
from crm.handlers import eval_trigger as handler_module
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import trigger
from eval.runs import RunRecord
from eval.tenancy import (
    tenant_for_run,
)


@pytest.fixture
def dynamodb_client() -> Iterator[Any]:
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: Any) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


def _full_visibility_identity(tenant: str = "demo") -> Identity:
    return Identity(rep_id="rep-ashford", full_visibility=True, tenant=tenant)


def _agent_event(claims: dict[str, str], **arguments: Any) -> dict[str, Any]:
    """A flat direct-invoke event on the `agent` identity channel, built by
    `crm.envelope.identity_event`.
    """
    return identity_event(AGENT_IDENTITY_KEY, {"claims": claims, "session_id": "sess-1"}, arguments)


# -- live mode is unreachable ---------------------------------------------------------------------


def _public_module_functions() -> dict[str, object]:
    """Every public function defined in `eval.trigger` itself."""
    return {
        name: member
        for name, member in vars(trigger).items()
        if not name.startswith("_")
        and inspect.isfunction(member)
        and member.__module__ == trigger.__name__
    }


#: The state-machine submission path, whose `mode` is admin-gated and never runs a sweep in-process
#: (tested in `test_eval_trigger_authz.py`).
_MODE_SELECTING_EXEMPT = "submit_sweep"


@pytest.mark.parametrize("forbidden", ["mode", "live", "substrate"])
def test_no_public_function_accepts_a_provider_selecting_parameter(forbidden: str) -> None:
    """No public function but `submit_sweep` accepts a `mode`, `live`, or `substrate` parameter."""
    assert _MODE_SELECTING_EXEMPT in _public_module_functions()
    for name, member in _public_module_functions().items():
        if name == _MODE_SELECTING_EXEMPT:
            continue
        assert forbidden not in inspect.signature(member).parameters, (  # type: ignore[arg-type]
            f"eval.trigger.{name} accepts {forbidden!r}, which would make a billable sweep "
            "requestable from the UI"
        )


# -- the two eval_runs row kinds ------------------------------------------------------------------


def test_the_handler_recognises_exactly_its_arguments() -> None:
    """`mode` is for `submit` only, `repeats` for `submit` and `estimate` only, and `key` for
    `artefact_url` only (`test_eval_artefacts.py`)."""
    assert frozenset({"action", "run_id", "mode", "confirmed", "cursor", "key", "repeats"}) == (
        handler_module._RECOGNISED_ARGUMENTS
    )


def test_job_row_id_refuses_an_already_prefixed_id() -> None:
    with pytest.raises(ValueError, match="double-prefix"):
        trigger.job_row_id(trigger.job_row_id("x"))


def test_job_row_id_round_trips() -> None:
    assert trigger.run_id_of_job_row(trigger.job_row_id("abc")) == "abc"


def test_run_id_of_job_row_refuses_a_plain_run_id() -> None:
    with pytest.raises(ValueError, match="not a job row id"):
        trigger.run_id_of_job_row("abc")


# -- authorization: the denial, not only the grant -------------------------------------------------


def test_full_visibility_is_required_to_trigger() -> None:
    scoped = Identity(rep_id="rep-brixton", book=frozenset({"acct-1"}), tenant="demo")

    with pytest.raises(ScopeDenied) as excinfo:
        trigger.require_eval_trigger_scope(scoped)

    assert excinfo.value.missing_scope == trigger.EVAL_TRIGGER_SCOPE
    assert excinfo.value.http_status == 403


def test_a_full_visibility_identity_is_permitted() -> None:
    trigger.require_eval_trigger_scope(_full_visibility_identity())


def test_a_service_principal_can_never_be_permitted() -> None:
    """A service principal is denied eval-trigger scope and cannot carry full visibility."""
    service = Identity(rep_id="svc-gateway", principal_type="service", tenant="demo")

    with pytest.raises(ScopeDenied):
        trigger.require_eval_trigger_scope(service)

    with pytest.raises(ValueError):
        Identity(rep_id="svc-gateway", principal_type="service", full_visibility=True)


def test_an_unauthenticated_call_is_denied_at_the_handler(store: Store) -> None:
    """An unauthenticated call is denied at the handler."""
    response = handler_module.handler({"action": "submit"}, None, store=store)

    assert response["statusCode"] == AuthenticationFailed("x").http_status
    assert response["statusCode"] == 401


def test_an_unauthorized_call_is_denied_at_the_handler(store: Store) -> None:
    """An unauthorized user's trigger is denied at the handler."""
    event = _agent_event({CLAIM_REP_ID: "rep-brixton", CLAIM_TENANT: "demo"}, action="submit")

    response = handler_module.handler(event, None, store=store)

    assert response["statusCode"] == 403
    assert response["error"]["missing_scope"] == trigger.EVAL_TRIGGER_SCOPE


def test_an_unauthorized_call_starts_no_sweep(store: Store) -> None:
    """An unauthorized submit writes no job row."""
    event = _agent_event({CLAIM_REP_ID: "rep-brixton", CLAIM_TENANT: "demo"}, action="submit")

    handler_module.handler(event, None, store=store)

    rows, _ = store.query_gsi("eval_runs", "gsi_status", partition_value="job")
    assert rows == []


def test_a_caller_supplied_identity_in_the_body_cannot_escalate(store: Store) -> None:
    """The identity channel keys are stripped from arguments, so a forged `_identity` cannot win."""
    event = {
        "_agent_identity": {
            "claims": {CLAIM_REP_ID: "rep-brixton", CLAIM_TENANT: "demo"},
            "session_id": "s",
        },
        "_identity": {
            CLAIM_REP_ID: "rep-attacker",
            CLAIM_FULL_VISIBILITY: "true",
            CLAIM_TENANT: "demo",
        },
        "action": "submit",
    }

    response = handler_module.handler(event, None, store=store)

    assert response["statusCode"] in (401, 403)


# -- the handler's own input surface ---------------------------------------------------------------


def test_the_handler_requires_a_recognised_action(store: Store) -> None:
    event = _agent_event(_admin_claims(), action="destroy-everything")

    response = handler_module.handler(event, None, store=store)

    assert response["statusCode"] == 400


def test_the_handler_refuses_an_absent_action(store: Store) -> None:
    response = handler_module.handler(_agent_event(_admin_claims()), None, store=store)

    assert response["statusCode"] == 400


def test_the_handler_refuses_an_unrecognised_argument(store: Store) -> None:
    """The handler refuses an unrecognised argument."""
    event = _agent_event(_admin_claims(), action="submit", substrate="live-aws")

    response = handler_module.handler(event, None, store=store)

    assert response["statusCode"] == 400


def test_status_requires_a_run_id(store: Store) -> None:
    event = _agent_event(_admin_claims(), action="status")

    response = handler_module.handler(event, None, store=store)

    assert response["statusCode"] == 400


def test_status_refuses_an_internal_job_row_key(store: Store) -> None:
    event = _agent_event(_admin_claims(), action="status", run_id="job#abc")

    response = handler_module.handler(event, None, store=store)

    assert response["statusCode"] == 400


def test_status_for_an_unknown_run_id_is_a_not_found(store: Store) -> None:
    event = _agent_event(_admin_claims(), action="status", run_id="never-submitted")

    response = handler_module.handler(event, None, store=store)

    assert response["statusCode"] == NotFound("x").http_status
    assert response["statusCode"] == 404


def _admin_claims() -> dict[str, str]:
    return {
        CLAIM_REP_ID: "rep-ashford",
        CLAIM_FULL_VISIBILITY: "true",
        CLAIM_TENANT: "demo",
    }


def test_the_handler_never_forwards_a_caller_supplied_run_id_to_a_submission(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-supplied run id is never forwarded to a submission."""
    seen: list[dict[str, Any]] = []

    def _capture(*args: Any, **kwargs: Any) -> RunRecord:
        seen.append(kwargs)
        return _stub_submit()

    monkeypatch.setattr(trigger, "submit_sweep", _capture)

    handler_module.handler(
        _agent_event(_admin_claims(), action="submit", run_id="attacker-chosen"),
        None,
        store=store,
        sfn=object(),
    )

    assert all(kwargs.get("run_id") != "attacker-chosen" for kwargs in seen)


def test_the_flat_channel_returns_the_flat_shape(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trigger, "submit_sweep", _stub_submit)

    response = handler_module.handler(
        _agent_event(_admin_claims(), action="submit"), None, store=store, sfn=object()
    )

    assert response["statusCode"] == 200
    assert response["body"]["run_id"] == "allocated"
    assert response["body"]["status"] == "queued"


def test_the_proxy_channel_returns_a_json_string_body(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proxy channel returns a JSON string body."""
    monkeypatch.setattr(trigger, "submit_sweep", _stub_submit)
    event = {
        "version": "2.0",
        "routeKey": "POST /evals",
        "requestContext": {
            "authorizer": {"jwt": {"claims": _admin_claims()}},
            "http": {"method": "POST"},
        },
        "body": json.dumps({"action": "submit"}),
        "isBase64Encoded": False,
    }

    response = handler_module.handler(event, None, store=store, sfn=object())

    assert response["statusCode"] == 200
    assert isinstance(response["body"], str)
    assert json.loads(response["body"])["run_id"] == "allocated"
    assert response["headers"]["content-type"] == "application/json"


def test_a_proxy_denial_carries_the_errors_own_status(store: Store) -> None:
    event = {
        "version": "2.0",
        "routeKey": "POST /evals",
        "requestContext": {
            "authorizer": {"jwt": {"claims": {CLAIM_REP_ID: "rep-brixton", CLAIM_TENANT: "demo"}}},
            "http": {"method": "POST"},
        },
        "body": json.dumps({"action": "submit"}),
        "isBase64Encoded": False,
    }

    response = handler_module.handler(event, None, store=store)

    assert response["statusCode"] == 403
    assert isinstance(response["body"], str)
    assert json.loads(response["body"])["error"]["missing_scope"] == trigger.EVAL_TRIGGER_SCOPE


def _stub_submit(*args: Any, **kwargs: Any) -> RunRecord:
    """A submission that writes nothing, for response-shape assertions."""
    del args, kwargs
    return RunRecord(
        run_id="allocated",
        tenant=tenant_for_run("allocated"),
        mode="stub",
        status="queued",
        started_at="2026-01-01T00:00:00+00:00",
    )


# -- the handler is not an action tool -------------------------------------------------------------


def test_the_handler_is_not_published_as_an_mcp_tool() -> None:
    """The handler is in no `ALL_TOOLS` entry, and no rendered MCP tool name mentions eval."""
    from crm.toolspec.definitions import ALL_TOOLS, render_mcp_tools

    handler_paths = {tool.handler for tool in ALL_TOOLS}
    assert "crm.handlers.eval_trigger.handler" not in handler_paths
    assert all("eval" not in tool["name"] for tool in render_mcp_tools())


def test_the_handler_reaches_no_agent_registry() -> None:
    """`eval_trigger` is not a registered agent tool."""
    from agents.registry import REGISTERED_TOOL_NAMES

    assert "eval_trigger" not in REGISTERED_TOOL_NAMES
    assert not any("eval" in name for name in REGISTERED_TOOL_NAMES)

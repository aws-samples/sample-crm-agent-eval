# SPDX-License-Identifier: MIT
"""approve, reject and revert refuse the ``agent`` channel and service principals.

A person on the ``apigw`` (UI) or ``gateway`` (MCP) channel still decides proposals and reverts
audit records. Runs the real handlers against a ``moto``-mocked DynamoDB.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.authz import PROPOSAL_DECIDING_PRINCIPAL, PROPOSAL_DECIDING_SOURCES
from crm.envelope import AGENT_IDENTITY_KEY, IDENTITY_KEY, REQUEST_CONTEXT_KEY, identity_event
from crm.handlers import approve_proposal, reject_proposal, revert_audit_record, submit_proposal
from crm.handlers._common import idempotent_id
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from tests.conftest import TEST_API_CLIENT_ID

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"
_OPPORTUNITY = "opp-1"

_CLAIMS: dict[str, Any] = {
    "custom:rep_id": _REP,
    "custom:book": _ACCOUNT,
    "custom:tenant": _TENANT,
}


def _gateway_event(arguments: dict[str, Any], *, principal_type: str = "user") -> dict[str, Any]:
    payload = {
        "rep_id": _REP,
        "book": [_ACCOUNT],
        "full_visibility": False,
        "principal_type": principal_type,
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": _TENANT,
    }
    return identity_event(IDENTITY_KEY, payload, arguments)


def _apigw_event(arguments: dict[str, Any]) -> dict[str, Any]:
    claims = {**_CLAIMS, "token_use": "id", "aud": TEST_API_CLIENT_ID}
    context = {"authorizer": {"jwt": {"claims": claims}}, "requestId": "req-1"}
    return identity_event(REQUEST_CONTEXT_KEY, context, arguments)


def _agent_event(arguments: dict[str, Any]) -> dict[str, Any]:
    return identity_event(
        AGENT_IDENTITY_KEY, {"claims": dict(_CLAIMS), "session_id": "sess-1"}, arguments
    )


def _agent_service_event(arguments: dict[str, Any]) -> dict[str, Any]:
    payload = {"claims": dict(_CLAIMS), "session_id": "sess-1", "principal_type": "service"}
    return identity_event(AGENT_IDENTITY_KEY, payload, arguments)


def _gateway_service_event(arguments: dict[str, Any]) -> dict[str, Any]:
    return _gateway_event(arguments, principal_type="service")


_CHANNEL_EVENTS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "apigw": _apigw_event,
    "gateway": _gateway_event,
    "agent": _agent_event,
    "agent_service": _agent_service_event,
    "gateway_service": _gateway_service_event,
}

#: Each refused caller and the ``missing_scope`` its refusal names.
_REFUSED: dict[str, str] = {
    "agent": "channel:agent",
    "agent_service": "channel:agent",
    "gateway_service": "principal:service",
}


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _seed(store: Store) -> None:
    store.put_item(
        "accounts",
        {
            "account_id": _ACCOUNT,
            "tenant": _TENANT,
            "tenant_owner_rep_id": f"{_TENANT}#{_REP}",
            "owner_rep_id": _REP,
            "name": "Acme",
            "name_lower": "acme",
        },
    )
    store.put_item(
        "opportunities",
        {
            "opportunity_id": _OPPORTUNITY,
            "tenant": _TENANT,
            "tenant_account_id": f"{_TENANT}#{_ACCOUNT}",
            "tenant_owner_rep_id": f"{_TENANT}#{_REP}",
            "tenant_stage": f"{_TENANT}#Discovery",
            "account_id": _ACCOUNT,
            "owner_rep_id": _REP,
            "name": "Big Deal",
            "name_lower": "big deal",
            "stage": "Discovery",
            "value": 1000,
            "close_date": "2026-06-30",
            "primary_contact_id": None,
            "last_touch": None,
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )


def _submit(store: Store) -> str:
    result = submit_proposal.handler(
        _gateway_event(
            {
                "target_table": "opportunities",
                "target_id": _OPPORTUNITY,
                "operation": "update",
                "after_payload": {"stage": "Qualification"},
                "reason": "advance",
                "transaction_key": "txn-submit-1",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 200, result
    return str(result["body"]["proposal"]["proposal_id"])


def _decide(handler: Any, channel: str, proposal_id: str, store: Any) -> dict[str, Any]:
    event = _CHANNEL_EVENTS[channel](
        {"proposal_id": proposal_id, "transaction_key": f"txn-{channel}-decide"}
    )
    result: dict[str, Any] = handler.handler(event, store=store)
    return result


def _status(store: Store, proposal_id: str) -> str:
    proposal = store.get_item("proposals", {"proposal_id": proposal_id})
    assert proposal is not None
    return str(proposal["status"])


class _ExplodingStore:
    """Fails the test if the handler touches the store at all."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"store.{name} was reached for an agent-channel decision")


@pytest.mark.parametrize("caller", sorted(_REFUSED))
@pytest.mark.parametrize("handler", [approve_proposal, reject_proposal], ids=["approve", "reject"])
def test_a_non_person_decision_is_denied_and_commits_nothing(
    store: Store, handler: Any, caller: str
) -> None:
    _seed(store)
    proposal_id = _submit(store)
    before = store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})

    result = _decide(handler, caller, proposal_id, store)

    assert result["statusCode"] == 403, result
    assert result["error"]["code"] == "scope_denied"
    assert result["error"]["missing_scope"] == _REFUSED[caller]
    assert _status(store, proposal_id) == "pending"
    assert store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY}) == before


@pytest.mark.parametrize("caller", sorted(_REFUSED))
@pytest.mark.parametrize(
    ("handler", "arguments"),
    [
        (approve_proposal, {"proposal_id": "prop-x", "transaction_key": "txn-x"}),
        (reject_proposal, {"proposal_id": "prop-x", "transaction_key": "txn-x"}),
        (revert_audit_record, {"audit_id": "audit-x", "transaction_key": "txn-x"}),
    ],
    ids=["approve", "reject", "revert"],
)
def test_a_non_person_is_refused_before_the_store_is_read(
    handler: Any, arguments: dict[str, Any], caller: str
) -> None:
    result = handler.handler(_CHANNEL_EVENTS[caller](arguments), store=_ExplodingStore())
    assert result["statusCode"] == 403, result
    assert result["error"]["missing_scope"] == _REFUSED[caller]


def _approved_audit_id(store: Store) -> str:
    """Approve a fresh proposal as a person on the MCP channel; return its audit record's id."""
    _seed(store)
    proposal_id = _submit(store)
    transaction_key = "txn-approve-for-revert"
    result = approve_proposal.handler(
        _gateway_event({"proposal_id": proposal_id, "transaction_key": transaction_key}),
        store=store,
    )
    assert result["statusCode"] == 200, result
    return idempotent_id("audit", transaction_key)


def _revert(caller: str, audit_id: str, store: Store) -> dict[str, Any]:
    event = _CHANNEL_EVENTS[caller]({"audit_id": audit_id, "transaction_key": f"txn-{caller}-rv"})
    result: dict[str, Any] = revert_audit_record.handler(event, store=store)
    return result


@pytest.mark.parametrize("caller", sorted(_REFUSED))
def test_a_non_person_revert_is_denied_and_restores_nothing(store: Store, caller: str) -> None:
    audit_id = _approved_audit_id(store)
    approved = store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})

    result = _revert(caller, audit_id, store)

    assert result["statusCode"] == 403, result
    assert result["error"]["missing_scope"] == _REFUSED[caller]
    audit = store.get_item("audit_records", {"audit_id": audit_id})
    assert audit is not None and audit["status"] == "active"
    assert store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY}) == approved


@pytest.mark.parametrize("channel", ["apigw", "gateway"])
def test_a_person_on_ui_or_mcp_still_reverts(store: Store, channel: str) -> None:
    audit_id = _approved_audit_id(store)

    result = _revert(channel, audit_id, store)

    assert result["statusCode"] == 200, result
    audit = store.get_item("audit_records", {"audit_id": audit_id})
    assert audit is not None and audit["status"] == "reversed"
    restored = store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})
    assert restored is not None and restored["stage"] == "Discovery"


@pytest.mark.parametrize("channel", ["apigw", "gateway"])
def test_ui_and_mcp_channels_still_approve(store: Store, channel: str) -> None:
    _seed(store)
    proposal_id = _submit(store)

    result = _decide(approve_proposal, channel, proposal_id, store)

    assert result["statusCode"] == 200, result
    assert _status(store, proposal_id) == "approved"
    committed = store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})
    assert committed is not None and committed["stage"] == "Qualification"


@pytest.mark.parametrize("channel", ["apigw", "gateway"])
def test_ui_and_mcp_channels_still_reject(store: Store, channel: str) -> None:
    _seed(store)
    proposal_id = _submit(store)

    result = _decide(reject_proposal, channel, proposal_id, store)

    assert result["statusCode"] == 200, result
    assert _status(store, proposal_id) == "rejected"


def test_the_deciders_are_people_on_the_two_human_facing_channels() -> None:
    assert frozenset({"apigw", "gateway"}) == PROPOSAL_DECIDING_SOURCES
    assert PROPOSAL_DECIDING_PRINCIPAL == "user"

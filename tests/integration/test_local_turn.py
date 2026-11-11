# SPDX-License-Identifier: MIT
"""One supervisor turn composed end to end against DynamoDB Local.

Decodes a raw invocation payload with :mod:`agents.runtime_entrypoint`, dispatches a subagent tool to
its real handler module via `_dispatch_tool_call`, and checks the resulting
:class:`agents.events.AgentEvent` stream renders as one JSON object per SSE line.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from agents.budget import TurnBudget
from agents.events import AgentEvent
from agents.registry import RegisteredTool
from agents.runtime_entrypoint import (
    InMemorySessionStore,
    RuntimeInvocation,
    decode_invocation,
    format_sse_line,
    run_turn,
)
from agents.subagents import RESEARCH_SPEC
from agents.subagents.mutation import MUTATION_SPEC
from agents.supervisor import (
    SubagentOutcome,
    SupervisorTurn,
    classify_tool_result,
    proposal_event_for,
    synthesize_reply,
)
from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import create_account, create_opportunity
from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLE_NAMES
from crm.store.store import Store
from providers.capabilities import CapabilityMatrix, load_capability_matrix
from providers.stub import StubProvider

#: Unique per process, so concurrent xdist workers never share a table.
_TABLE_PREFIX = f"lt-{uuid.uuid4().hex[:12]}-"
_TENANT = "demo"
_REP = "rep-turn-1"


@pytest.fixture
def turn_store(dynamodb_local_client: Any) -> Iterator[Store]:
    """A tenant-`demo`-scoped :class:`Store` against DynamoDB Local, with a non-empty prefix.

    Drops every prefixed table on teardown.
    """
    create_all_tables(dynamodb_local_client, table_prefix=_TABLE_PREFIX)
    try:
        yield Store(table_prefix=_TABLE_PREFIX, client=dynamodb_local_client)
    finally:
        for table in TABLE_NAMES:
            with contextlib.suppress(dynamodb_local_client.exceptions.ResourceNotFoundException):
                dynamodb_local_client.delete_table(TableName=f"{_TABLE_PREFIX}{table}")


@pytest.fixture(scope="module")
def matrix() -> CapabilityMatrix:
    """The repository's real capability matrix, loaded once for this module."""
    return load_capability_matrix()


def _identity_payload(
    *, rep_id: str = _REP, book: list[str] | None = None, full_visibility: bool = False
) -> dict[str, Any]:
    """Build the `_identity` block `crm.envelope.decode_identity`'s gateway channel reads."""
    return {
        "rep_id": rep_id,
        "book": book or [],
        "full_visibility": full_visibility,
        "principal_type": "user",
        "session_id": "sess-local-turn",
        "source": "gateway",
        "tenant": _TENANT,
    }


def _invocation_payload(
    prompt: str,
    *,
    session_id: str = "sess-local-turn",
    rep_id: str = _REP,
    book: list[str] | None = None,
    full_visibility: bool = False,
) -> dict[str, Any]:
    """Build a raw AgentCore Runtime invocation payload for `decode_invocation`."""
    return {
        "prompt": prompt,
        "session_id": session_id,
        "_identity": _identity_payload(rep_id=rep_id, book=book, full_visibility=full_visibility),
    }


def _resolve_handler(tool: RegisteredTool) -> Any:
    """Import and return the real handler module's `handler` callable for `tool`.

    `tool.handler` is a dotted path such as `"crm.handlers.get_opportunity.handler"`.
    """
    module_path, _, attr = tool.handler.rpartition(".")
    module = importlib.import_module(module_path)
    return getattr(module, attr)


def _dispatch_tool_call(
    tool: RegisteredTool,
    tool_input: dict[str, object],
    *,
    store: Store,
    identity_payload: dict[str, Any],
) -> dict[str, Any]:
    """Invoke `tool`'s real Lambda handler exactly as the Lambda runtime would.

    The event carries the identity channel plus the tool's arguments as sibling keys, and is passed
    to the resolved `handler(event, store=...)` entry point.
    """
    handler = _resolve_handler(tool)
    event = identity_event(IDENTITY_KEY, identity_payload, tool_input)
    return dict(handler(event, store=store))


def _seed_account_and_opportunity(
    store: Store, *, rep_id: str = _REP
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create one real account and one real opportunity owned by `rep_id`, via the real handlers."""
    identity = _identity_payload(rep_id=rep_id, full_visibility=True)
    account_result = create_account.handler(
        {"_identity": identity, "name": "Local Turn Co", "transaction_key": "tx-lt-account-1"},
        store=store,
    )
    assert account_result["statusCode"] == 200
    account = dict(account_result["body"]["account"])

    opportunity_result = create_opportunity.handler(
        {
            "_identity": identity,
            "account_id": account["account_id"],
            "name": "Local Turn Deal",
            "transaction_key": "tx-lt-opp-1",
        },
        store=store,
    )
    assert opportunity_result["statusCode"] == 200
    opportunity = dict(opportunity_result["body"]["opportunity"])
    return account, opportunity


# -- Scenario 1: a plain read, routed through decode -> dispatch -> SSE framing ------------------


def test_a_read_only_turn_composes_decode_dispatch_and_sse_framing_end_to_end(
    turn_store: Store, matrix: CapabilityMatrix
) -> None:
    """A `get_opportunity` call flows from a raw invocation payload to a `data` outcome and one
    JSON object per SSE line."""
    _account, opportunity = _seed_account_and_opportunity(turn_store)

    payload = _invocation_payload(
        f"Tell me about opportunity {opportunity['opportunity_id']}.",
        book=[],
        full_visibility=True,
    )
    invocation: RuntimeInvocation = decode_invocation(payload, matrix)
    assert invocation.identity.rep_id == _REP
    assert invocation.identity.full_visibility is True

    get_opportunity_tool = next(
        tool for tool in RESEARCH_SPEC.tools if tool.name == "get_opportunity"
    )
    tool_output = _dispatch_tool_call(
        get_opportunity_tool,
        {"opportunity_id": opportunity["opportunity_id"]},
        store=turn_store,
        identity_payload=_identity_payload(full_visibility=True),
    )
    outcome = classify_tool_result(tool_output)
    assert outcome.kind == "data"
    assert outcome.data is not None
    found_opportunity = outcome.data["opportunity"]
    assert isinstance(found_opportunity, dict)
    assert found_opportunity["opportunity_id"] == opportunity["opportunity_id"]

    events: list[AgentEvent] = [
        AgentEvent(kind="tool_call", tool_name="get_opportunity", tool_input={}),
        AgentEvent(kind="tool_result", tool_output=tool_output),
        AgentEvent(
            kind="completed",
            text=synthesize_reply(outcome),
            input_tokens=100,
            output_tokens=20,
        ),
    ]

    store_sessions = InMemorySessionStore()
    lines = list(run_turn(invocation, iter(events), store=store_sessions))

    assert len(lines) == len(events)
    for line, event in zip(lines, events, strict=True):
        assert line == format_sse_line(event)
        assert line.startswith("data: ")
        assert line.endswith("\n\n")
        decoded = json.loads(line[len("data: ") : -2])
        assert decoded["kind"] == event.kind

    for line in lines:
        body = line[len("data: ") : -2]
        json.loads(body)  # raises if the single line is not exactly one JSON object


def test_a_full_delegation_hop_reaches_a_real_handler_against_the_real_engine(
    turn_store: Store,
) -> None:
    """Research's `list_activities` tool dispatches to the real handler and returns a `data`
    outcome with an empty activities list."""
    _account, opportunity = _seed_account_and_opportunity(turn_store)

    list_activities_tool = next(
        tool for tool in RESEARCH_SPEC.tools if tool.name == "list_activities"
    )

    tool_output = _dispatch_tool_call(
        list_activities_tool,
        {"opportunity_id": opportunity["opportunity_id"]},
        store=turn_store,
        identity_payload=_identity_payload(full_visibility=True),
    )
    outcome = classify_tool_result(tool_output)
    # `classify_tool_result` maps only an empty `items` (or empty `body`) to `"no_match"`, so an
    # empty `activities` list classifies as `"data"`.
    assert outcome.kind == "data"
    assert outcome.data is not None
    assert outcome.data["activities"] == []


# -- Scenario 2: a high-risk write surfaces as a structured proposal, never a direct commit ------


def test_a_high_risk_write_surfaces_as_a_structured_proposal_not_a_direct_commit(
    turn_store: Store,
) -> None:
    """`submit_proposal` writes a pending proposal row, renders as a `proposal` event, and leaves the
    opportunity's `stage` unchanged."""
    _account, opportunity = _seed_account_and_opportunity(turn_store)
    assert opportunity["stage"] != "Closed Won"

    submit_proposal_tool = next(
        tool for tool in MUTATION_SPEC.tools if tool.name == "submit_proposal"
    )
    tool_output = _dispatch_tool_call(
        submit_proposal_tool,
        {
            "target_table": "opportunities",
            "target_id": opportunity["opportunity_id"],
            "operation": "update",
            "reason": "Customer verbally committed on the call today.",
            "transaction_key": "tx-lt-proposal-1",  # gitleaks:allow  # nosemgrep: generic.secrets.gitleaks.generic-api-key
            "after_payload": {"stage": "Closed Won"},
        },
        store=turn_store,
        identity_payload=_identity_payload(full_visibility=True),
    )
    assert "error" not in tool_output

    outcome: SubagentOutcome = classify_tool_result(tool_output)
    assert outcome.kind == "data"

    proposal_event = proposal_event_for(tool_output)
    assert proposal_event.kind == "proposal"
    assert proposal_event.proposal_id
    assert proposal_event.proposal_summary is not None
    assert proposal_event.proposal_summary["after"] == {"stage": "Closed Won"}

    proposal_row = turn_store.get_item("proposals", {"proposal_id": proposal_event.proposal_id})
    assert proposal_row is not None
    assert proposal_row["status"] == "pending"
    assert proposal_row["target_id"] == opportunity["opportunity_id"]

    current_opportunity = turn_store.get_item(
        "opportunities", {"opportunity_id": opportunity["opportunity_id"]}
    )
    assert current_opportunity is not None
    assert current_opportunity["stage"] == opportunity["stage"]

    line = format_sse_line(proposal_event)
    decoded = json.loads(line[len("data: ") : -2])
    assert decoded["kind"] == "proposal"
    assert decoded["proposal_id"] == proposal_event.proposal_id


# -- Scenario 3: an out-of-scope target denies rather than fabricates ----------------------------


def test_an_out_of_scope_target_surfaces_denied_rather_than_fabricating(turn_store: Store) -> None:
    """A rep whose book excludes the target account gets a 403 `scope_denied` classified as
    `"denied"` with the missing scope named and no record data in the reply."""
    _account, opportunity = _seed_account_and_opportunity(turn_store, rep_id="rep-owner")

    out_of_scope_identity = _identity_payload(
        rep_id="rep-outsider", book=["acct-does-not-own-this-one"], full_visibility=False
    )

    get_opportunity_tool = next(
        tool for tool in RESEARCH_SPEC.tools if tool.name == "get_opportunity"
    )
    tool_output = _dispatch_tool_call(
        get_opportunity_tool,
        {"opportunity_id": opportunity["opportunity_id"]},
        store=turn_store,
        identity_payload=out_of_scope_identity,
    )

    assert tool_output.get("statusCode") == 403
    assert tool_output["error"]["code"] == "scope_denied"

    outcome = classify_tool_result(tool_output)
    assert outcome.kind == "denied"
    assert outcome.missing_scope is not None
    assert outcome.missing_scope.startswith("account:")
    assert outcome.data is None

    reply_text = synthesize_reply(outcome)
    assert "missing scope" in reply_text
    assert opportunity["name"] not in reply_text  # no fabricated content leaks into the denial

    denial_event = AgentEvent(kind="error", error_kind="tool_failure", error_message=reply_text)
    line = format_sse_line(denial_event)
    decoded = json.loads(line[len("data: ") : -2])
    assert decoded["kind"] == "error"
    assert "missing scope" in decoded["error_message"]


def test_a_supervisor_turn_direct_tool_call_also_composes_against_the_real_engine(
    turn_store: Store, matrix: CapabilityMatrix
) -> None:
    """`get_opportunity` is a supervisor direct tool and dispatches to the real handler as `data`."""
    _account, opportunity = _seed_account_and_opportunity(turn_store)

    turn = SupervisorTurn(
        provider=StubProvider(),
        matrix=matrix,
        budget=TurnBudget(),
    )
    assert "get_opportunity" in turn.direct_tool_names()

    direct_tool = next(tool for tool in RESEARCH_SPEC.tools if tool.name == "get_opportunity")
    tool_output = _dispatch_tool_call(
        direct_tool,
        {"opportunity_id": opportunity["opportunity_id"]},
        store=turn_store,
        identity_payload=_identity_payload(full_visibility=True),
    )
    outcome = classify_tool_result(tool_output)
    assert outcome.kind == "data"

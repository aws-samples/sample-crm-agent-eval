#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Export the real wire response shapes of the CRM action Lambdas the frontend consumes.

Runs each covered handler against a `moto`-mocked DynamoDB table and prints, as JSON, the
top-level key set of each response body (`crm.handlers._common.ok(body)["body"]`), plus one level
deeper where a key holds a record. `frontend/src/test/responseShapes.test.ts` runs this script and
asserts each tool's response carries the keys the frontend reads.

Run from the repository root with the project's pinned interpreter:

    .venv/bin/python3 frontend/scripts/export_response_shapes.py
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

from crm.envelope import IDENTITY_KEY, identity_event  # noqa: E402
from crm.store.provisioning import create_all_tables  # noqa: E402
from crm.store.store import Store  # noqa: E402
from crm.toolspec.definitions import ALL_TOOLS  # noqa: E402

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"
_OPPORTUNITY = "opp-1"
_CONTACT = "con-1"
_ACTIVITY = "act-1"
_AUDIT = "aud-1"
#: A dedicated target, so no other tool in the sweep can mutate it out from under the revert.
_AUDIT_OPPORTUNITY = "opp-audit"
_PROPOSAL_TO_APPROVE = "prop-approve"
_PROPOSAL_TO_REJECT = "prop-reject"
_PROPOSAL_TO_REBASE = "prop-rebase"


def _identity() -> dict[str, Any]:
    return {
        "rep_id": _REP,
        "book": [_ACCOUNT],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": _TENANT,
    }


def _event(args: dict[str, Any]) -> dict[str, Any]:
    return identity_event(IDENTITY_KEY, _identity(), args)


def _seed_account(store: Store) -> None:
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


def _seed_opportunity(store: Store, *, opportunity_id: str = _OPPORTUNITY) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": opportunity_id,
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
            "primary_contact_id": None,
            "last_touch": None,
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )


def _seed_contact(store: Store) -> None:
    store.put_item(
        "contacts",
        {
            "contact_id": _CONTACT,
            "tenant": _TENANT,
            "tenant_account_id": f"{_TENANT}#{_ACCOUNT}",
            "account_id": _ACCOUNT,
            "name": "Alice",
            "name_lower": "alice",
            "status": "active",
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )


def _seed_activity(store: Store) -> None:
    store.put_item(
        "activities",
        {
            "activity_id": _ACTIVITY,
            "tenant": _TENANT,
            "tenant_opportunity_id": f"{_TENANT}#{_OPPORTUNITY}",
            "opportunity_id": _OPPORTUNITY,
            # `require_record_scope` fails closed on an undeterminable account, so this is required.
            "account_id": _ACCOUNT,
            "kind": "call",
            "subject": "Kickoff",
            "occurred_at": "2024-01-01T00:00:00+00:00",
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )


def _seed_proposal(store: Store, *, proposal_id: str) -> None:
    """Seed a pending proposal whose before-snapshot is the target's **whole** current item.

    `crm.proposals.approve_proposal` compares the entire record against the stored snapshot and
    returns `Conflict` on any difference.
    """
    current = store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY}) or {}
    store.put_item(
        "proposals",
        {
            "proposal_id": proposal_id,
            "tenant": _TENANT,
            "tenant_status": f"{_TENANT}#pending",
            "tenant_target_id": f"{_TENANT}#{_OPPORTUNITY}",
            "tenant_actor_rep_id": f"{_TENANT}#{_REP}",
            "sort_key_attr": "proposal",
            "target_table": "opportunities",
            "target_id": _OPPORTUNITY,
            "operation": "update",
            "before_snapshot": dict(current),
            "after_payload": {"stage": "Qualification"},
            "reason": "advance",
            "status": "pending",
            "actor_rep_id": _REP,
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
        },
    )


def _seed_audit_record(store: Store) -> None:
    """Seed an active audit record whose `after_snapshot` is its target's **whole** current item.

    `crm.audit.revert` requires the target to currently match `after_snapshot`. It targets
    `_AUDIT_OPPORTUNITY`, which no earlier tool in the sweep mutates.
    """
    current = store.get_item("opportunities", {"opportunity_id": _AUDIT_OPPORTUNITY}) or {}
    store.put_item(
        "audit_records",
        {
            "audit_id": _AUDIT,
            "tenant": _TENANT,
            "tenant_target_id": f"{_TENANT}#{_AUDIT_OPPORTUNITY}",
            "tenant_actor_rep_id": f"{_TENANT}#{_REP}",
            "target_table": "opportunities",
            "target_id": _AUDIT_OPPORTUNITY,
            # `get_audit_record` applies the list's record-scope predicate to this attribute.
            "account_id": _ACCOUNT,
            "before_snapshot": {**current, "stage": "Discovery"},
            "after_snapshot": dict(current),
            "actor_rep_id": _REP,
            # `crm.audit.revert` refuses a record that is not `active`.
            "status": "active",
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )


#: Tools whose response shape the frontend never reads, with the reason. :func:`build_shapes`
#: raises on any tool that is in neither this set nor :data:`_TOOL_ARGS`.
_NOT_FRONTEND_CALLED: dict[str, str] = {
    "get_page_context": (
        "Agent context tool. Reached by the supervisor over the `agent` channel, never by "
        "`CrmApiClient`, so no frontend module reads its response body."
    ),
}

#: Arguments sufficient for each handler to reach a 2xx against the seeded fixture below.
#: `transaction_key` values must be unique per write, since the proposal/audit chain is idempotent.
_TOOL_ARGS: dict[str, dict[str, Any]] = {
    # Reads
    "search_accounts": {},
    "search_opportunities": {},
    "get_opportunity": {"opportunity_id": _OPPORTUNITY},
    "get_account": {"account_id": _ACCOUNT},
    "get_contact": {"contact_id": _CONTACT},
    "list_activities": {"opportunity_id": _OPPORTUNITY},
    "pipeline_aggregate": {},
    "list_proposals": {},
    "list_audit_records": {},
    "get_audit_record": {"audit_id": _AUDIT},
    "draft_message": {
        "contact_id": _CONTACT,
        "opportunity_id": _OPPORTUNITY,
        "intent": "follow_up",
    },
    # Low-risk writes
    "create_account": {"name": "New Co", "transaction_key": "tx-shapes-account"},
    "create_contact": {
        "account_id": _ACCOUNT,
        "name": "Bob",
        "transaction_key": "tx-shapes-contact",
    },
    "create_opportunity": {
        "account_id": _ACCOUNT,
        "name": "Deal",
        "transaction_key": "tx-shapes-opportunity",
    },
    "create_note": {
        "opportunity_id": _OPPORTUNITY,
        "body": "note body",
        "transaction_key": "tx-shapes-note",
    },
    "log_activity": {
        "opportunity_id": _OPPORTUNITY,
        "kind": "call",
        "subject": "Intro call",
        "occurred_at": "2024-01-02T00:00:00+00:00",
        "transaction_key": "tx-shapes-activity",
    },
    "update_activity": {
        "activity_id": _ACTIVITY,
        "fields": {"subject": "Renamed"},
        "transaction_key": "tx-shapes-activity-update",
    },
    "update_account": {
        "account_id": _ACCOUNT,
        "fields": {"name": "Acme Renamed"},
        "transaction_key": "tx-shapes-account-update",
    },
    "update_contact": {
        "contact_id": _CONTACT,
        "fields": {"title": "Head of Ops"},
        "transaction_key": "tx-shapes-contact-update",
    },
    "update_opportunity": {
        "opportunity_id": _OPPORTUNITY,
        # `value` and `stage` are not low-risk fields; a change to either goes through
        # `submit_proposal`.
        "fields": {"description": "Refreshed description"},
        "transaction_key": "tx-shapes-opportunity-update",
    },
    # Proposal chain. Each targets a distinct proposal so one decision cannot invalidate another.
    "submit_proposal": {
        "target_table": "opportunities",
        "target_id": _OPPORTUNITY,
        "operation": "update",
        "after_payload": {"stage": "Qualification"},
        "reason": "advance",
        "transaction_key": "tx-shapes-submit",
    },
    "approve_proposal": {
        "proposal_id": _PROPOSAL_TO_APPROVE,
        "transaction_key": "tx-shapes-approve",
    },
    "reject_proposal": {"proposal_id": _PROPOSAL_TO_REJECT, "transaction_key": "tx-shapes-reject"},
    "rebase_proposal": {"proposal_id": _PROPOSAL_TO_REBASE, "transaction_key": "tx-shapes-rebase"},
    "revert_audit_record": {"audit_id": _AUDIT, "transaction_key": "tx-shapes-revert"},
}


def _handler_for(tool_name: str) -> Any:
    """Import a handler module by tool name; the two are 1:1 by convention."""
    return importlib.import_module(f"crm.handlers.{tool_name}").handler


def _assert_every_tool_accounted_for() -> None:
    """Raise unless every tool in `ALL_TOOLS` is either exported or explicitly excluded."""
    known = set(_TOOL_ARGS) | set(_NOT_FRONTEND_CALLED)
    all_names = {tool.name for tool in ALL_TOOLS}

    missing = sorted(all_names - known)
    if missing:
        raise SystemExit(
            "export_response_shapes: no arguments and no exclusion reason for "
            f"{missing}. Add them to _TOOL_ARGS so the frontend's read of this response is "
            "asserted, or to _NOT_FRONTEND_CALLED with the reason it is never read."
        )

    stale = sorted(known - all_names)
    if stale:
        raise SystemExit(
            f"export_response_shapes: {stale} is no longer in ALL_TOOLS. Remove the entry so this "
            "script cannot claim coverage of a tool that does not exist."
        )


def build_shapes() -> dict[str, list[str]]:
    """Run every frontend-called handler and return `{tool_name: sorted(body.keys())}`.

    Raises when a handler does not reach a 2xx.
    """
    _assert_every_tool_accounted_for()

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        store = Store(client=client)

        _seed_account(store)
        _seed_opportunity(store)
        _seed_opportunity(store, opportunity_id=_AUDIT_OPPORTUNITY)
        _seed_contact(store)
        _seed_activity(store)
        _seed_audit_record(store)
        for proposal_id in (_PROPOSAL_TO_APPROVE, _PROPOSAL_TO_REJECT, _PROPOSAL_TO_REBASE):
            _seed_proposal(store, proposal_id=proposal_id)

        shapes: dict[str, list[str]] = {}
        for tool_name, args in sorted(_TOOL_ARGS.items()):
            result = _handler_for(tool_name)(_event(args), store=store)
            status = result.get("statusCode")
            if status is not None and int(status) >= 300:
                raise SystemExit(
                    f"export_response_shapes: {tool_name} returned {status} with body "
                    f"{result.get('body')!r}. Fix its _TOOL_ARGS — an un-exercised tool is an "
                    "unasserted response shape."
                )
            body = result["body"]
            shapes[tool_name] = sorted(body.keys())
            # One level deeper where the payload nests a record.
            for key, value in body.items():
                if isinstance(value, dict) and value:
                    shapes[f"{tool_name}.{key}"] = sorted(value.keys())

        return shapes


def main() -> int:
    json.dump(build_shapes(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

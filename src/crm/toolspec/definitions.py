# SPDX-License-Identifier: MIT
"""Single source of truth for every CRM action.

Three consumers derive from the definitions in this module:

1. The input validator each action Lambda applies to its event.
2. The AgentCore Gateway MCP tool definitions, transcribed into ``infra/main.tf``
   ``locals.tools`` / ``locals.tool_schema_properties`` and held to this module by
   ``scripts/check_toolspec_parity.py``.
3. The Strands agent tool registry.

``scripts/check_toolspec_parity.py`` checks all three and fails the build on divergence.

:class:`ToolDefinition` requires every input schema to set ``additionalProperties: false`` and to
declare no key in ``crm.envelope.IDENTITY_CHANNEL_KEYS``. There is no ``high_risk_write`` risk
class: high-risk changes go through ``submit_proposal``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Final, Literal

from crm.envelope import IDENTITY_CHANNEL_KEYS
from crm.toolspec.page_context import BY_KIND as _PAGE_CONTEXT_BY_KIND

#: ``read`` — no state change.
#: ``low_risk_write`` — commits directly.
#: ``proposal`` — creates a pending proposal for a high-risk change.
#: ``approval`` — decides, rebases, or reverses an already-recorded change.
RiskClass = Literal["read", "low_risk_write", "proposal", "approval"]

#: The closed set of entity table names every entity-keyed lookup in this module accepts.
TABLES: Final[frozenset[str]] = frozenset({"accounts", "contacts", "opportunities", "activities"})

#: Field-level allowlists governing what each entity permits on update, keyed by :data:`TABLES`.
UPDATE_ALLOWLISTS: Final[dict[str, frozenset[str]]] = {
    "accounts": frozenset({"name", "industry", "region", "website", "employee_count", "notes"}),
    "contacts": frozenset({"name", "title", "email", "phone", "status"}),
    # The high-risk fields (HIGH_RISK_OPPORTUNITY_FIELDS) change only through submit_proposal.
    "opportunities": frozenset({"name", "description", "source", "competitor"}),
    "activities": frozenset({"subject", "body", "occurred_at"}),
}

#: Opportunity fields whose modification requires a proposal.
HIGH_RISK_OPPORTUNITY_FIELDS: Final[frozenset[str]] = frozenset(
    {"stage", "value", "close_date", "owner_rep_id", "primary_contact_id"}
)


def high_risk_fields_for(table: str) -> frozenset[str]:
    """Return the fields on ``table`` whose change requires a proposal.

    Args:
        table: A plural table name from :data:`TABLES`.

    Raises:
        ValueError: When ``table`` is not a member of :data:`TABLES`.
    """
    if table not in TABLES:
        raise ValueError(f"unrecognised table {table!r}; expected one of {sorted(TABLES)}")
    return HIGH_RISK_OPPORTUNITY_FIELDS if table == "opportunities" else frozenset()


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One CRM action, in the single form every consumer is generated from.

    Attributes:
        name: Stable tool name. Surfaced to models and to MCP clients.
        display_label: Short human-facing action label for the assistant-panel step trace. UI-only:
            not part of the MCP tool definition.
        description: Human- and model-facing description. Surfaced verbatim over MCP.
        input_schema: JSON Schema for the arguments, excluding every identity channel key.
        risk: Risk class governing which agent may hold the tool and whether it commits directly.
        scope_arg: Argument naming the account to scope against, or ``None`` when the handler
            resolves scope from the target record it fetches.
        handler: Dotted path to the Lambda handler implementing the action.
        agents: Agent roles permitted to hold this tool.
    """

    name: str
    display_label: str
    description: str
    input_schema: dict[str, Any]
    risk: RiskClass
    scope_arg: str | None
    handler: str
    agents: frozenset[str]

    def __post_init__(self) -> None:
        """Reject a definition with an open or identity-declaring schema, or a mislabelled risk."""
        if not self.display_label.strip():
            raise ValueError(f"{self.name}: display_label must be a non-empty label")
        if self.input_schema.get("type") != "object":
            raise ValueError(f"{self.name}: input_schema must be an object schema")
        if self.input_schema.get("additionalProperties") is not False:
            raise ValueError(f"{self.name}: input_schema must set additionalProperties: false")
        declared_identity_keys = IDENTITY_CHANNEL_KEYS & set(
            self.input_schema.get("properties", {})
        )
        if declared_identity_keys:
            raise ValueError(
                f"{self.name}: {sorted(declared_identity_keys)!r} "
                "is injected by the Gateway interceptor and must never be declared as caller input"
            )
        if self.risk == "read" and self.name.startswith(("create_", "update_", "delete_")):
            raise ValueError(f"{self.name}: mutating name declared with risk 'read'")

    @property
    def mutates(self) -> bool:
        """Whether invoking this tool can change persisted state directly."""
        return self.risk in {"low_risk_write", "approval"}

    def to_mcp_tool(self) -> dict[str, Any]:
        """Render the AgentCore Gateway MCP tool definition."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


#: Longest value a long free-text field (:data:`LONG_TEXT_FIELDS`) accepts, in characters.
LONG_TEXT_MAX_LENGTH: Final[int] = 4000

#: Longest value any other free-text string accepts, in characters.
TEXT_MAX_LENGTH: Final[int] = 256

#: Longest pagination cursor accepted, in characters.
CURSOR_MAX_LENGTH: Final[int] = 4000

#: Longest ``revert_audit_record`` reason accepted; equals the limit :func:`crm.audit.revert` enforces.
REVERT_REASON_MAX_LENGTH: Final[int] = 1024

#: Field and argument names capped at :data:`LONG_TEXT_MAX_LENGTH`.
LONG_TEXT_FIELDS: Final[frozenset[str]] = frozenset({"description", "notes", "body", "reason"})


def max_length_for(name: str) -> int:
    """Return the ``maxLength`` a free-text field or argument named ``name`` carries."""
    if name in LONG_TEXT_FIELDS:
        return LONG_TEXT_MAX_LENGTH
    if name == "cursor":
        return CURSOR_MAX_LENGTH
    return TEXT_MAX_LENGTH


def _length_capped(name: str, prop: Any) -> Any:
    """Return ``prop`` with ``maxLength`` added when it is a free-text string without one."""
    if (
        not isinstance(prop, dict)
        or prop.get("type") != "string"
        or "enum" in prop
        or "maxLength" in prop
    ):
        return prop
    return {**prop, "maxLength": max_length_for(name)}


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Build a closed object schema, the only shape a tool input may take.

    Every free-text string property without an explicit ``maxLength`` gets
    :func:`max_length_for` its name.
    """
    return {
        "type": "object",
        "properties": {name: _length_capped(name, prop) for name, prop in properties.items()},
        "required": required,
        "additionalProperties": False,
    }


_STR = {"type": "string", "minLength": 1}
_OPT_STR = {"type": "string"}
_NUM = {"type": "number", "minimum": 0}
_TXN = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "description": (
        "Idempotency key. Use a new unique value (for example a random UUID) for each distinct "
        "change. Reuse a key only to retry the exact same change: a retry is never applied twice "
        "and returns the original result or a conflict. Never reuse a key for a different change; "
        "it can be refused as a conflict."
    ),
}
_QUERY = {
    "type": "string",
    "description": (
        "Words matched case-insensitively against the record name; every word must appear."
    ),
}
_CURSOR = {
    "type": "string",
    "description": (
        "The next_cursor from the previous call, with the same other arguments. Omit for the "
        "first page."
    ),
}
_SEARCH_LIMIT = {
    "type": "integer",
    "minimum": 1,
    "maximum": 200,
    "description": "Maximum records to return. Default: no limit beyond the examination budget.",
}
_CLOSE_BOUND = {"type": "string", "description": "Exclusive bound on close_date, YYYY-MM-DD."}
_REASON = {
    "type": "string",
    "minLength": 1,
    "description": "Why this change is being made. Recorded on the proposal and the audit record.",
}

STAGES: Final[tuple[str, ...]] = (
    "Discovery",
    "Qualification",
    "Security Review",
    "Negotiation",
    "Closed Won",
    "Closed Lost",
)
ACTIVITY_KINDS: Final[tuple[str, ...]] = (
    "call",
    "email",
    "meeting",
    "note",
    "stage_change",
    "finding",
)


def _fields_arg(table: str) -> dict[str, Any]:
    """Build the open ``fields`` argument of an ``update_*`` tool, naming its allowlisted keys."""
    allowed = ", ".join(sorted(UPDATE_ALLOWLISTS[table]))
    description = (
        f"Fields to change, keyed by field name. Allowed keys: {allowed}. "
        "Omit any field you are not changing."
    )
    high_risk = high_risk_fields_for(table)
    if high_risk:
        description += (
            f" {', '.join(sorted(high_risk))} are not accepted here; use submit_proposal."
        )
    return {"type": "object", "additionalProperties": True, "description": description}


_READERS = frozenset({"supervisor", "research", "analytics", "composer", "mutation"})
_RESEARCH = frozenset({"research", "supervisor"})
_ANALYTICS = frozenset({"analytics"})
_COMPOSER = frozenset({"composer"})
_MUTATION = frozenset({"mutation"})


# --------------------------------------------------------------------------------------
# Read operations
# --------------------------------------------------------------------------------------

_READS: Final[tuple[ToolDefinition, ...]] = (
    ToolDefinition(
        name="search_opportunities",
        display_label="Search opportunities",
        description=(
            "Find opportunities in your book by name words, stage, account, or owner. Use this to "
            "find ids; use get_opportunity for one record's full detail. If the result has "
            "truncated: true, pass its next_cursor as cursor to continue."
        ),
        input_schema=_schema(
            {
                "query": _QUERY,
                "stage": {"type": "string", "enum": list(STAGES)},
                "owner_rep_id": _STR,
                "account_id": _STR,
                "cursor": _CURSOR,
                "limit": _SEARCH_LIMIT,
            },
            [],
        ),
        risk="read",
        scope_arg=None,
        handler="crm.handlers.search_opportunities.handler",
        agents=_READERS,
    ),
    ToolDefinition(
        name="search_accounts",
        display_label="Search accounts",
        description=(
            "Find accounts in your book by name words or owner. Use this to find ids; use "
            "get_account for one account's contact list. If the result has truncated: true, pass "
            "its next_cursor as cursor to continue."
        ),
        input_schema=_schema(
            {
                "query": _QUERY,
                "owner_rep_id": _STR,
                "cursor": _CURSOR,
                "limit": _SEARCH_LIMIT,
            },
            [],
        ),
        risk="read",
        scope_arg=None,
        handler="crm.handlers.search_accounts.handler",
        agents=_READERS,
    ),
    ToolDefinition(
        name="get_opportunity",
        display_label="Get opportunity",
        description=(
            "Retrieve one opportunity by id, with its account's contacts, its notes, and its "
            "activity timeline. Use search_opportunities to find an id."
        ),
        input_schema=_schema({"opportunity_id": _STR}, ["opportunity_id"]),
        risk="read",
        scope_arg=None,
        handler="crm.handlers.get_opportunity.handler",
        agents=_READERS,
    ),
    ToolDefinition(
        name="get_account",
        display_label="Get account",
        description=(
            "Retrieve one account by id with its full contact list, including each contact's "
            "status. Use search_accounts first if you only have the account's name."
        ),
        input_schema=_schema({"account_id": _STR}, ["account_id"]),
        risk="read",
        scope_arg="account_id",
        handler="crm.handlers.get_account.handler",
        agents=_RESEARCH | _ANALYTICS | _COMPOSER | _MUTATION,
    ),
    ToolDefinition(
        name="get_contact",
        display_label="Get contact",
        description="Retrieve one contact by id, including its status.",
        input_schema=_schema({"contact_id": _STR}, ["contact_id"]),
        risk="read",
        scope_arg=None,
        handler="crm.handlers.get_contact.handler",
        agents=_RESEARCH | _COMPOSER | _MUTATION,
    ),
    ToolDefinition(
        name="list_activities",
        display_label="List activities",
        description=(
            "List one opportunity's activities, optionally filtered by kind, most recent first "
            "within each page, with next_cursor for the next page. Use for a complete or filtered "
            "history; get_opportunity already includes the timeline."
        ),
        input_schema=_schema(
            {
                "opportunity_id": _STR,
                "kind": {"type": "string", "enum": list(ACTIVITY_KINDS)},
                "cursor": _CURSOR,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Maximum activities per page. Default 50.",
                },
            },
            ["opportunity_id"],
        ),
        risk="read",
        scope_arg=None,
        handler="crm.handlers.list_activities.handler",
        agents=_RESEARCH | _ANALYTICS | _MUTATION,
    ),
    ToolDefinition(
        name="pipeline_aggregate",
        display_label="Aggregate pipeline",
        description=(
            "Totals across your book's opportunities: count per stage, total value, and value "
            "grouped by close date. Closed stages are excluded unless include_closed is true. No "
            "per-account breakdown; for one account, use search_opportunities with account_id."
        ),
        input_schema=_schema(
            {
                "owner_rep_id": _STR,
                "close_before": _CLOSE_BOUND,
                "close_after": _CLOSE_BOUND,
                "include_closed": {
                    "type": "boolean",
                    "description": "Include Closed Won and Closed Lost. Default false.",
                },
            },
            [],
        ),
        risk="read",
        scope_arg=None,
        handler="crm.handlers.pipeline_aggregate.handler",
        agents=_ANALYTICS | frozenset({"supervisor"}),
    ),
    ToolDefinition(
        name="list_proposals",
        display_label="List proposals",
        description=(
            "List proposals in your book. Set status to filter (pending = awaiting a decision); "
            "omit it to list every status. Returns up to 50 per call with next_cursor for the next "
            "page."
        ),
        input_schema=_schema(
            {
                "status": {
                    "type": "string",
                    "enum": ["pending", "approved", "rejected"],
                    "description": "pending: awaiting a decision; approved/rejected: decided.",
                },
                "cursor": _CURSOR,
            },
            [],
        ),
        risk="read",
        scope_arg=None,
        handler="crm.handlers.list_proposals.handler",
        agents=_MUTATION | frozenset({"supervisor"}),
    ),
    ToolDefinition(
        name="list_audit_records",
        display_label="List audit records",
        description="List audited changes within your book, most recent first, with revert state.",
        input_schema=_schema({"target_id": _STR, "cursor": _CURSOR}, []),
        risk="read",
        scope_arg=None,
        handler="crm.handlers.list_audit_records.handler",
        agents=_MUTATION | _RESEARCH,
    ),
    ToolDefinition(
        name="get_audit_record",
        display_label="Get audit record",
        description=(
            "Retrieve one audited change within your book: actor, timestamp, action, target, revert "
            "state, and the full before and after snapshots."
        ),
        input_schema=_schema({"audit_id": _STR}, ["audit_id"]),
        risk="read",
        scope_arg=None,
        handler="crm.handlers.get_audit_record.handler",
        agents=_MUTATION | _RESEARCH,
    ),
    ToolDefinition(
        name="draft_message",
        display_label="Draft message",
        description=(
            "Draft an email or message for a contact. Read-only: returns draft text and persists "
            "nothing."
        ),
        input_schema=_schema(
            {
                "contact_id": _STR,
                "opportunity_id": _OPT_STR,
                "intent": _STR,
                "tone": {"type": "string", "enum": ["formal", "neutral", "warm"]},
            },
            ["contact_id", "intent"],
        ),
        risk="read",
        scope_arg=None,
        handler="crm.handlers.draft_message.handler",
        agents=_COMPOSER,
    ),
)


# --------------------------------------------------------------------------------------
# Low-risk writes — commit directly
# --------------------------------------------------------------------------------------

_LOW_RISK_WRITES: Final[tuple[ToolDefinition, ...]] = (
    ToolDefinition(
        name="create_account",
        display_label="Create account",
        description="Create a new account. Commits immediately.",
        input_schema=_schema(
            {
                "name": _STR,
                "industry": _OPT_STR,
                "region": _OPT_STR,
                "website": _OPT_STR,
                "employee_count": {"type": "integer", "minimum": 0},
                "transaction_key": _TXN,
            },
            ["name", "transaction_key"],
        ),
        risk="low_risk_write",
        scope_arg=None,
        handler="crm.handlers.create_account.handler",
        agents=_MUTATION,
    ),
    ToolDefinition(
        name="update_account",
        display_label="Update account",
        description=("Update an account's fields. Commits immediately."),
        input_schema=_schema(
            {
                "account_id": _STR,
                "fields": _fields_arg("accounts"),
                "transaction_key": _TXN,
            },
            ["account_id", "fields", "transaction_key"],
        ),
        risk="low_risk_write",
        scope_arg="account_id",
        handler="crm.handlers.update_account.handler",
        agents=_MUTATION,
    ),
    ToolDefinition(
        name="create_contact",
        display_label="Create contact",
        description="Create a contact under an account. Commits immediately.",
        input_schema=_schema(
            {
                "account_id": _STR,
                "name": _STR,
                "title": _OPT_STR,
                "email": _OPT_STR,
                "phone": _OPT_STR,
                "status": {"type": "string", "enum": ["active", "departed"]},
                "transaction_key": _TXN,
            },
            ["account_id", "name", "transaction_key"],
        ),
        risk="low_risk_write",
        scope_arg="account_id",
        handler="crm.handlers.create_contact.handler",
        agents=_MUTATION,
    ),
    ToolDefinition(
        name="update_contact",
        display_label="Update contact",
        description=("Update a contact's fields, including its status. Commits immediately."),
        input_schema=_schema(
            {
                "contact_id": _STR,
                "fields": _fields_arg("contacts"),
                "transaction_key": _TXN,
            },
            ["contact_id", "fields", "transaction_key"],
        ),
        risk="low_risk_write",
        scope_arg=None,
        handler="crm.handlers.update_contact.handler",
        agents=_MUTATION,
    ),
    ToolDefinition(
        name="create_opportunity",
        display_label="Create opportunity",
        description=(
            "Create a new opportunity under an account. Commits immediately. Creating an "
            "opportunity is low risk; later changing its stage, value, close date, owner, or "
            "primary contact is not, and requires a proposal."
        ),
        input_schema=_schema(
            {
                "account_id": _STR,
                "name": _STR,
                "value": {**_NUM, "description": "Deal value in whole US dollars."},
                "close_date": {"type": "string", "description": "Expected close date, YYYY-MM-DD."},
                "stage": {"type": "string", "enum": list(STAGES)},
                "primary_contact_id": _OPT_STR,
                "description": _OPT_STR,
                "transaction_key": _TXN,
            },
            ["account_id", "name", "transaction_key"],
        ),
        risk="low_risk_write",
        scope_arg="account_id",
        handler="crm.handlers.create_opportunity.handler",
        agents=_MUTATION,
    ),
    ToolDefinition(
        name="update_opportunity",
        display_label="Update opportunity",
        description=(
            "Update low-risk opportunity fields such as name, description, source, and competitor. "
            "Stage, value, close date, owner, and primary contact are not changeable here — use "
            "submit_proposal for those."
        ),
        input_schema=_schema(
            {
                "opportunity_id": _STR,
                "fields": _fields_arg("opportunities"),
                "transaction_key": _TXN,
            },
            ["opportunity_id", "fields", "transaction_key"],
        ),
        risk="low_risk_write",
        scope_arg=None,
        handler="crm.handlers.update_opportunity.handler",
        agents=_MUTATION,
    ),
    ToolDefinition(
        name="log_activity",
        display_label="Log activity",
        description=(
            "Log an activity against an opportunity. Commits immediately and updates the "
            "opportunity's last-touch timestamp in the same operation."
        ),
        input_schema=_schema(
            {
                "opportunity_id": _STR,
                "kind": {"type": "string", "enum": list(ACTIVITY_KINDS)},
                "subject": _STR,
                "body": _OPT_STR,
                "occurred_at": {
                    "type": "string",
                    "description": (
                        "When it happened, ISO 8601 UTC (YYYY-MM-DDTHH:MM:SSZ). Default: now."
                    ),
                },
                "transaction_key": _TXN,
            },
            ["opportunity_id", "kind", "subject", "transaction_key"],
        ),
        risk="low_risk_write",
        scope_arg=None,
        handler="crm.handlers.log_activity.handler",
        agents=_MUTATION,
    ),
    ToolDefinition(
        name="update_activity",
        display_label="Update activity",
        description="Update a logged activity's subject, body, or timestamp. Commits immediately.",
        input_schema=_schema(
            {
                "activity_id": _STR,
                "fields": _fields_arg("activities"),
                "transaction_key": _TXN,
            },
            ["activity_id", "fields", "transaction_key"],
        ),
        risk="low_risk_write",
        scope_arg=None,
        handler="crm.handlers.update_activity.handler",
        agents=_MUTATION,
    ),
    ToolDefinition(
        name="create_note",
        display_label="Create note",
        description="Attach a note to an opportunity. Commits immediately.",
        input_schema=_schema(
            {"opportunity_id": _STR, "body": _STR, "transaction_key": _TXN},
            ["opportunity_id", "body", "transaction_key"],
        ),
        risk="low_risk_write",
        scope_arg=None,
        handler="crm.handlers.create_note.handler",
        agents=_MUTATION,
    ),
)


# --------------------------------------------------------------------------------------
# Proposal and approval surface
# --------------------------------------------------------------------------------------

_PROPOSAL_AND_APPROVAL: Final[tuple[ToolDefinition, ...]] = (
    ToolDefinition(
        name="submit_proposal",
        display_label="Submit proposal",
        description=(
            "Propose a high-risk change — an opportunity stage transition, value change, "
            "close-date change, owner reassignment, primary-contact reassignment, or any delete. "
            "Nothing changes until the proposal is approved. Use this, not update_*, for those "
            "changes."
        ),
        input_schema=_schema(
            {
                "target_table": {
                    "type": "string",
                    "enum": ["accounts", "contacts", "opportunities", "activities"],
                },
                "target_id": _STR,
                "operation": {"type": "string", "enum": ["update", "delete"]},
                "after_payload": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "Intended field values. Omitted for a delete.",
                },
                "reason": _REASON,
                "transaction_key": _TXN,
            },
            ["target_table", "target_id", "operation", "reason", "transaction_key"],
        ),
        risk="proposal",
        scope_arg=None,
        handler="crm.handlers.submit_proposal.handler",
        agents=_MUTATION,
    ),
    ToolDefinition(
        name="approve_proposal",
        display_label="Approve proposal",
        description=(
            "Approve a pending proposal. The stored before-snapshot is re-checked against current "
            "state; if the record has changed since the proposal was raised, approval is refused "
            "as a conflict rather than overwriting the newer value."
        ),
        input_schema=_schema(
            {"proposal_id": _STR, "transaction_key": _TXN}, ["proposal_id", "transaction_key"]
        ),
        risk="approval",
        scope_arg=None,
        handler="crm.handlers.approve_proposal.handler",
        agents=frozenset(),
    ),
    ToolDefinition(
        name="reject_proposal",
        display_label="Reject proposal",
        description="Reject a pending proposal. Terminal: a rejected proposal cannot be approved.",
        input_schema=_schema(
            {"proposal_id": _STR, "reason": _OPT_STR, "transaction_key": _TXN},
            ["proposal_id", "transaction_key"],
        ),
        risk="approval",
        scope_arg=None,
        handler="crm.handlers.reject_proposal.handler",
        agents=frozenset(),
    ),
    ToolDefinition(
        name="rebase_proposal",
        display_label="Rebase proposal",
        description=(
            "Re-capture a pending proposal's before-snapshot from the record's current state, so a "
            "proposal refused as a conflict can be reviewed against what the record holds now. "
            "Changes nothing on the record itself and does not approve the proposal."
        ),
        input_schema=_schema(
            {"proposal_id": _STR, "transaction_key": _TXN}, ["proposal_id", "transaction_key"]
        ),
        risk="approval",
        scope_arg=None,
        handler="crm.handlers.rebase_proposal.handler",
        agents=_MUTATION,
    ),
    ToolDefinition(
        name="revert_audit_record",
        display_label="Revert audited change",
        description=(
            "Reverse an audited change, restoring the recorded before-snapshot. Refused as a "
            "conflict if the record changed after the audit entry was written, or if this audit "
            "record has already been reverted."
        ),
        input_schema=_schema(
            {
                "audit_id": _STR,
                "reason": {**_OPT_STR, "maxLength": REVERT_REASON_MAX_LENGTH},
                "transaction_key": _TXN,
            },
            ["audit_id", "transaction_key"],
        ),
        risk="approval",
        scope_arg=None,
        handler="crm.handlers.revert_audit_record.handler",
        agents=frozenset(),
    ),
)


# --------------------------------------------------------------------------------------
# Page context — the assistant panel's view descriptor
# --------------------------------------------------------------------------------------


def _page_context_kind_guidance() -> str:
    """Describe every accepted ``page_context.kind`` and its required fields, for the model.

    Derived from :data:`crm.toolspec.page_context.BY_KIND`. Each ``kind`` is double-quoted and each
    field name backtick-quoted; ``tests/unit/test_toolspec.py`` parses that form.
    """
    clauses: list[str] = []
    for kind, variant in _PAGE_CONTEXT_BY_KIND.items():
        fields = dataclasses.fields(variant)
        required = [
            f.name
            for f in fields
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        ]
        optional = [
            f.name
            for f in fields
            if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING
        ]
        detail = "requires " + ", ".join(f"`{name}`" for name in required)
        if optional:
            detail += "; optional " + ", ".join(f"`{name}`" for name in optional)
        clauses.append(f'"{kind}" ({detail})')
    return "; ".join(clauses)


#: The ``page_context`` argument: an open object here, validated by
#: ``crm.toolspec.page_context.parse_page_context`` in the handler. The accepted ``kind`` values are
#: named in its description.
_PAGE_CONTEXT_ARGUMENT: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": True,
    "description": (
        "The PageContext descriptor for the view the user is currently looking at. Set `kind` to "
        "exactly one of the following, supplying that variant's fields: "
        + _page_context_kind_guidance()
        + ". An optional `query` carries `filters`/`sort`/`page`. Carries no retrieved rows."
    ),
}

_PAGE_CONTEXT: Final[tuple[ToolDefinition, ...]] = (
    ToolDefinition(
        name="get_page_context",
        display_label="Resolve current view",
        description=(
            "Resolve the page-context descriptor you were given into confirmed facts: the record "
            "in view, the query a list or board shows, or a statement that the view cannot be "
            "summarised. It returns no rows; run a list or board query yourself with the search "
            "tools."
        ),
        input_schema=_schema({"page_context": _PAGE_CONTEXT_ARGUMENT}, ["page_context"]),
        risk="read",
        scope_arg=None,
        handler="crm.handlers.get_page_context.handler",
        agents=frozenset({"supervisor"}),
    ),
)


ALL_TOOLS: Final[tuple[ToolDefinition, ...]] = (
    _READS + _LOW_RISK_WRITES + _PROPOSAL_AND_APPROVAL + _PAGE_CONTEXT
)

BY_NAME: Final[dict[str, ToolDefinition]] = {tool.name: tool for tool in ALL_TOOLS}


def tools_for_agent(role: str) -> tuple[ToolDefinition, ...]:
    """Return the tools an agent role may hold.

    The agent tool registry is built from this. A role that holds no tool gets an empty tuple.
    """
    return tuple(tool for tool in ALL_TOOLS if role in tool.agents)


def render_mcp_tools() -> list[dict[str, Any]]:
    """Render every action as an MCP tool definition for Gateway registration.

    Every action is registered, including approve, reject, and revert.
    """
    return [tool.to_mcp_tool() for tool in ALL_TOOLS]


def allowlist_for(entity: str) -> frozenset[str]:
    """Return the update allowlist for an entity.

    Args:
        entity: A plural table name from :data:`TABLES`.

    Raises:
        ValueError: When ``entity`` is not a member of :data:`TABLES`.
    """
    if entity not in TABLES:
        raise ValueError(f"unrecognised entity {entity!r}; expected one of {sorted(TABLES)}")
    return UPDATE_ALLOWLISTS[entity]

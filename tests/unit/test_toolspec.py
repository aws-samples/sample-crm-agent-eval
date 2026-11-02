# SPDX-License-Identifier: MIT
"""Assert the tool definitions satisfy the coverage, safety, and anti-spoofing invariants.

``test_no_agent_holds_a_tool_that_commits_a_high_risk_change`` asserts no agent role holds a tool
that commits a high-risk write directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from crm.envelope import IDENTITY_CHANNEL_KEYS, IDENTITY_KEY
from crm.toolspec.definitions import (
    ACTIVITY_KINDS,
    ALL_TOOLS,
    BY_NAME,
    HIGH_RISK_OPPORTUNITY_FIELDS,
    STAGES,
    TABLES,
    UPDATE_ALLOWLISTS,
    ToolDefinition,
    allowlist_for,
    high_risk_fields_for,
    render_mcp_tools,
    tools_for_agent,
)
from crm.toolspec.page_context import BY_KIND

# Read and write operations, plus approve, reject, and revert.
REQUIRED_TOOLS = {
    "search_opportunities",
    "get_opportunity",
    "get_account",
    "get_contact",
    "list_activities",
    "pipeline_aggregate",
    "create_account",
    "update_account",
    "create_contact",
    "update_contact",
    "create_opportunity",
    "update_opportunity",
    "log_activity",
    "update_activity",
    "create_note",
    "draft_message",
    "submit_proposal",
    "approve_proposal",
    "reject_proposal",
    "revert_audit_record",
}

AGENT_ROLES = ("supervisor", "research", "mutation", "analytics", "composer")


# --------------------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------------------


def test_every_required_operation_has_a_tool() -> None:
    """The central invariant: every UI-capable action exists as a tool."""
    missing = REQUIRED_TOOLS - set(BY_NAME)
    assert not missing, f"missing tool definitions: {sorted(missing)}"


def test_tool_names_are_unique() -> None:
    """A duplicated name would make one definition unreachable."""
    names = [tool.name for tool in ALL_TOOLS]
    assert len(names) == len(set(names))


def test_approve_reject_and_revert_are_ordinary_tools() -> None:
    """Approve, reject, and revert are available identically to every caller."""
    for name in ("approve_proposal", "reject_proposal", "revert_audit_record"):
        assert BY_NAME[name].risk == "approval"


# --------------------------------------------------------------------------------------
# Schema invariants — anti-spoofing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda tool: tool.name)
def test_every_schema_is_closed(tool: ToolDefinition) -> None:
    """An open schema would let a caller add arbitrary keys, including an identity claim."""
    assert tool.input_schema["type"] == "object"
    assert tool.input_schema["additionalProperties"] is False


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda tool: tool.name)
def test_no_schema_declares_the_reserved_identity_key(tool: ToolDefinition) -> None:
    """`_identity` is injected by the Gateway interceptor; declaring it as input invites spoofing."""
    assert IDENTITY_KEY not in tool.input_schema.get("properties", {})
    assert IDENTITY_KEY not in tool.input_schema.get("required", [])


def test_a_definition_with_an_open_schema_is_rejected_at_construction() -> None:
    """The invariant is enforced by the type, not only by this test suite."""
    with pytest.raises(ValueError, match="additionalProperties"):
        ToolDefinition(
            name="bad",
            display_label="Bad",
            description="d",
            input_schema={"type": "object", "properties": {}, "additionalProperties": True},
            risk="read",
            scope_arg=None,
            handler="h",
            agents=frozenset(),
        )


def test_a_definition_declaring_identity_as_input_is_rejected() -> None:
    """Closing the loop: the class refuses the dangerous shape outright."""
    with pytest.raises(ValueError, match=IDENTITY_KEY):
        ToolDefinition(
            name="bad",
            display_label="Bad",
            description="d",
            input_schema={
                "type": "object",
                "properties": {IDENTITY_KEY: {"type": "object"}},
                "additionalProperties": False,
            },
            risk="read",
            scope_arg=None,
            handler="h",
            agents=frozenset(),
        )


@pytest.mark.parametrize("channel_key", sorted(IDENTITY_CHANNEL_KEYS))
def test_a_definition_declaring_any_identity_channel_key_is_rejected(channel_key: str) -> None:
    """A definition declaring any key in `IDENTITY_CHANNEL_KEYS` as input is rejected."""
    with pytest.raises(ValueError, match=channel_key):
        ToolDefinition(
            name="bad",
            display_label="Bad",
            description="d",
            input_schema={
                "type": "object",
                "properties": {channel_key: {"type": "object"}},
                "additionalProperties": False,
            },
            risk="read",
            scope_arg=None,
            handler="h",
            agents=frozenset(),
        )


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda tool: tool.name)
def test_every_required_argument_is_declared_as_a_property(tool: ToolDefinition) -> None:
    """A required key absent from properties would be rejected by additionalProperties: false."""
    properties = set(tool.input_schema.get("properties", {}))
    assert set(tool.input_schema.get("required", [])) <= properties


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda tool: tool.name)
def test_every_tool_has_a_substantive_description(tool: ToolDefinition) -> None:
    """Descriptions are surfaced verbatim to models and MCP clients; a stub description misleads."""
    assert len(tool.description) >= 30


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda tool: tool.name)
def test_every_tool_names_a_handler(tool: ToolDefinition) -> None:
    """Every tool names a handler."""
    assert tool.handler.startswith("crm.handlers.")
    assert tool.handler.endswith(".handler")


def test_handlers_are_one_per_tool() -> None:
    """No two tools share a handler."""
    handlers = [tool.handler for tool in ALL_TOOLS]
    assert len(handlers) == len(set(handlers))


# --------------------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool", [t for t in ALL_TOOLS if t.risk != "read"], ids=lambda tool: tool.name
)
def test_every_state_changing_tool_requires_a_transaction_key(tool: ToolDefinition) -> None:
    """Every state-changing tool requires a caller-supplied transaction key."""
    assert "transaction_key" in tool.input_schema["required"], (
        f"{tool.name} changes state but does not require a transaction key"
    )


@pytest.mark.parametrize(
    "tool", [t for t in ALL_TOOLS if t.risk == "read"], ids=lambda tool: tool.name
)
def test_read_tools_do_not_require_a_transaction_key(tool: ToolDefinition) -> None:
    """Demanding an idempotency key for a read would be noise in the model's context."""
    assert "transaction_key" not in tool.input_schema.get("required", [])


# --------------------------------------------------------------------------------------
# Structural impossibility of a direct high-risk commit
# --------------------------------------------------------------------------------------


def test_no_tool_is_classified_as_a_direct_high_risk_write() -> None:
    """There is no `high_risk_write` risk class. High-risk changes go through submit_proposal."""
    assert {tool.risk for tool in ALL_TOOLS} <= {
        "read",
        "low_risk_write",
        "proposal",
        "approval",
    }


def test_no_delete_tool_exists() -> None:
    """No delete tool exists; deletes are high risk and go through a proposal."""
    assert not [tool for tool in ALL_TOOLS if tool.name.startswith("delete_")]


def test_opportunity_update_allowlist_excludes_every_high_risk_field() -> None:
    """The load-bearing allowlist. Any leak here makes update_opportunity a high-risk committer."""
    leaked = UPDATE_ALLOWLISTS["opportunities"] & HIGH_RISK_OPPORTUNITY_FIELDS
    assert not leaked, (
        f"update_opportunity would directly commit high-risk fields: {sorted(leaked)}"
    )


def test_high_risk_fields_cover_every_field_named_by_fr_4_3() -> None:
    """Stage, value, close date, owner, and primary contact are all high risk."""
    assert {
        "stage",
        "value",
        "close_date",
        "owner_rep_id",
        "primary_contact_id",
    } == HIGH_RISK_OPPORTUNITY_FIELDS


def test_submit_proposal_is_the_only_proposal_producing_tool() -> None:
    """A second route to a pending proposal would be a second place to get the safety wrong."""
    producers = [tool.name for tool in ALL_TOOLS if tool.risk == "proposal"]
    assert producers == ["submit_proposal"]


def test_submit_proposal_supports_both_update_and_delete() -> None:
    """Deletes are high risk, so they must be expressible as a proposal."""
    operations = BY_NAME["submit_proposal"].input_schema["properties"]["operation"]["enum"]
    assert set(operations) == {"update", "delete"}


def test_submit_proposal_requires_a_reason() -> None:
    """`submit_proposal` requires a reason."""
    assert "reason" in BY_NAME["submit_proposal"].input_schema["required"]


# --------------------------------------------------------------------------------------
# Agent surface
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["research", "analytics", "composer"])
def test_read_only_roles_hold_no_mutating_tool(role: str) -> None:
    """Research, analytics, and composer hold no mutating tool."""
    mutating = [tool.name for tool in tools_for_agent(role) if tool.risk != "read"]
    assert not mutating, f"{role} holds mutating tools: {mutating}"


#: Tools only a person may call: deciding a proposal and reverting an audit record.
_HUMAN_ONLY_TOOLS = frozenset({"approve_proposal", "reject_proposal", "revert_audit_record"})


def test_mutation_agent_holds_the_proposal_and_approval_surface() -> None:
    """The mutation subagent holds submit and rebase; deciding and reverting are not among them."""
    names = {tool.name for tool in tools_for_agent("mutation")}
    assert {"submit_proposal", "rebase_proposal"} <= names


def test_supervisor_holds_only_high_frequency_reads() -> None:
    """The supervisor's direct tools are high-frequency reads only."""
    supervisor_tools = tools_for_agent("supervisor")
    assert {tool.risk for tool in supervisor_tools} == {"read"}
    assert {"search_opportunities", "get_opportunity"} <= {t.name for t in supervisor_tools}


def test_no_agent_holds_a_tool_that_commits_a_high_risk_change() -> None:
    """Every mutating tool any role holds is low risk or an approval action."""
    for role in AGENT_ROLES:
        for tool in tools_for_agent(role):
            if not tool.mutates:
                continue
            assert tool.risk in {"low_risk_write", "approval"}, (
                f"{role} holds {tool.name!r} with risk {tool.risk!r}, which would commit a "
                "high-risk change directly"
            )


def test_every_agent_tool_has_a_ui_counterpart() -> None:
    """No agent holds a tool without a UI counterpart, i.e. an action Lambda."""
    for role in AGENT_ROLES:
        for tool in tools_for_agent(role):
            assert tool.name in BY_NAME
            assert tool.handler.startswith("crm.handlers.")


def test_every_tool_is_reachable_by_at_least_one_agent_role() -> None:
    """Every tool is reachable by at least one agent role, except the human-only decisions."""
    reachable = {tool.name for role in AGENT_ROLES for tool in tools_for_agent(role)}
    assert set(BY_NAME) - _HUMAN_ONLY_TOOLS == reachable


def test_unknown_role_yields_an_empty_surface_rather_than_raising() -> None:
    """A misconfigured role must fail closed, with no tools, not open with all of them."""
    assert tools_for_agent("nonexistent") == ()


# --------------------------------------------------------------------------------------
# MCP rendering
# --------------------------------------------------------------------------------------


def test_every_action_is_registered_as_an_mcp_tool() -> None:
    """Every action Lambda is rendered as a Gateway MCP tool, including approvals."""
    rendered = render_mcp_tools()
    assert len(rendered) == len(ALL_TOOLS)
    assert {entry["name"] for entry in rendered} == set(BY_NAME)


def test_mcp_rendering_carries_schema_and_description_verbatim() -> None:
    """MCP tool descriptions and schemas equal the shared definition's."""
    for entry in render_mcp_tools():
        tool = BY_NAME[entry["name"]]
        assert entry["description"] == tool.description
        assert entry["inputSchema"] == tool.input_schema


def test_mcp_rendering_never_exposes_the_identity_key() -> None:
    """An MCP client reading the schema must not learn `_identity` is an accepted argument."""
    for entry in render_mcp_tools():
        assert IDENTITY_KEY not in entry["inputSchema"].get("properties", {})


def test_mcp_rendering_is_deterministic() -> None:
    """Rendering the MCP tool JSON twice yields identical output."""
    assert render_mcp_tools() == render_mcp_tools()


# --------------------------------------------------------------------------------------
# Enums and allowlists
# --------------------------------------------------------------------------------------


def test_stage_enum_matches_fr_2_2_in_order() -> None:
    """The stage enum lists the stages in pipeline order."""
    assert STAGES == (
        "Discovery",
        "Qualification",
        "Security Review",
        "Negotiation",
        "Closed Won",
        "Closed Lost",
    )


def test_activity_kinds_match_fr_2_4() -> None:
    """The closed set of activity kinds."""
    assert set(ACTIVITY_KINDS) == {"call", "email", "meeting", "note", "stage_change", "finding"}


def test_stage_enum_is_used_everywhere_a_stage_is_accepted() -> None:
    """A free-text stage argument would let an invalid stage reach the store."""
    for tool in ALL_TOOLS:
        stage = tool.input_schema.get("properties", {}).get("stage")
        if stage is not None:
            assert stage.get("enum") == list(STAGES), f"{tool.name} accepts an unconstrained stage"


def test_allowlists_exist_for_every_updatable_entity() -> None:
    """Every updatable entity has a field-level update allowlist."""
    assert set(UPDATE_ALLOWLISTS) == TABLES
    for entity in UPDATE_ALLOWLISTS:
        assert allowlist_for(entity), f"{entity} has an empty update allowlist"


def test_unknown_entity_allowlist_raises_rather_than_returning_empty() -> None:
    """An unrecognised entity must be a loud error, never a silent 'nothing is permitted'."""
    with pytest.raises(ValueError, match="unrecognised entity"):
        allowlist_for("nonexistent")


def test_high_risk_fields_are_reported_only_for_opportunities() -> None:
    """Only opportunity fields are reported as high risk."""
    assert high_risk_fields_for("opportunities") == HIGH_RISK_OPPORTUNITY_FIELDS
    assert high_risk_fields_for("accounts") == frozenset()


def test_unknown_table_high_risk_fields_raises_rather_than_returning_empty() -> None:
    """An unrecognised table name must never read as 'nothing is high risk'."""
    with pytest.raises(ValueError, match="unrecognised table"):
        high_risk_fields_for("nonexistent")


@pytest.mark.parametrize("table", sorted(TABLES))
def test_allowlist_for_and_high_risk_fields_for_share_one_vocabulary(table: str) -> None:
    """Every name in `TABLES` gets a non-empty allowlist and a correct high-risk-fields answer."""
    assert allowlist_for(table), f"{table} has an empty update allowlist"
    if table == "opportunities":
        assert high_risk_fields_for(table) == HIGH_RISK_OPPORTUNITY_FIELDS
    else:
        assert high_risk_fields_for(table) == frozenset()


def test_submit_proposal_target_table_enum_matches_tables() -> None:
    """`submit_proposal.target_table` must accept exactly the vocabulary the allowlist helpers use."""
    target_table_enum = set(
        BY_NAME["submit_proposal"].input_schema["properties"]["target_table"]["enum"]
    )
    assert target_table_enum == TABLES


# --------------------------------------------------------------------------------------
# The get_page_context schema advertises exactly the handler-accepted `kind` values
# --------------------------------------------------------------------------------------


def _advertised_page_context_kinds() -> set[str]:
    """The double-quoted lowercase `kind` tokens in `get_page_context`'s schema description.

    Field names in that description are backtick-quoted, so they are not captured.
    """
    description = BY_NAME["get_page_context"].input_schema["properties"]["page_context"][
        "description"
    ]
    return set(re.findall(r'"([a-z]+)"', description))


def test_get_page_context_schema_advertises_exactly_the_handler_accepted_kinds() -> None:
    """The `kind` values advertised in the schema description equal the `BY_KIND` set."""
    advertised = _advertised_page_context_kinds()
    handler_accepted = set(BY_KIND)
    assert handler_accepted <= advertised, (
        f"kinds accepted by parse_page_context but not advertised to the model: "
        f"{sorted(handler_accepted - advertised)}"
    )
    assert advertised <= handler_accepted, (
        f"kinds advertised to the model that the handler rejects: "
        f"{sorted(advertised - handler_accepted)}"
    )


def test_get_page_context_schema_names_each_kinds_required_fields() -> None:
    """Every accepted `kind` is advertised alongside at least its mandatory `route` field."""
    description = BY_NAME["get_page_context"].input_schema["properties"]["page_context"][
        "description"
    ]
    for kind in BY_KIND:
        assert f'"{kind}"' in description
    assert "`route`" in description


@pytest.mark.parametrize(
    "tool", [t for t in ALL_TOOLS if t.risk != "read"], ids=lambda tool: tool.name
)
def test_every_transaction_key_is_bounded_at_128_characters(tool: ToolDefinition) -> None:
    """A 128-character key conforms and a 129-character key is refused by the validator."""
    from crm.toolspec.validation import validator_for

    assert tool.input_schema["properties"]["transaction_key"]["maxLength"] == 128
    subschema = tool.input_schema["properties"]["transaction_key"]
    validator = type(validator_for(tool))(subschema)
    assert validator.is_valid("k" * 128)
    assert not validator.is_valid("k" * 129)


def test_no_agent_tool_set_holds_approve_reject_or_revert() -> None:
    """No agent role may decide a proposal or revert; a person does both."""
    roles = {role for tool in ALL_TOOLS for role in tool.agents}
    assert roles, "no tool names any agent role, so this asserts nothing"
    for role in sorted(roles):
        names = {tool.name for tool in tools_for_agent(role)}
        assert not names & _HUMAN_ONLY_TOOLS, (role, sorted(names))
    for name in sorted(_HUMAN_ONLY_TOOLS):
        assert BY_NAME[name].agents == frozenset(), name


@pytest.mark.parametrize("value", ["", "acct-1"], ids=["empty", "present"])
def test_search_opportunities_account_id_is_at_least_one_character(value: str) -> None:
    """An empty account_id fails the toolspec validator rather than reaching the store."""
    from crm.toolspec.validation import arguments_conform

    assert arguments_conform("search_opportunities", {"account_id": value}) is bool(value)


@pytest.mark.parametrize(
    ("tool_name", "field"),
    [
        ("search_opportunities", "owner_rep_id"),
        ("search_accounts", "owner_rep_id"),
        ("list_audit_records", "target_id"),
    ],
)
@pytest.mark.parametrize("value", ["", "x"], ids=["empty", "present"])
def test_optional_partition_filters_are_at_least_one_character(
    tool_name: str, field: str, value: str
) -> None:
    """An empty partition filter fails the validator rather than reaching the store."""
    from crm.toolspec.validation import arguments_conform

    assert arguments_conform(tool_name, {field: value}) is bool(value)


def test_pipeline_aggregate_rejects_an_empty_owner_rep_id() -> None:
    """An empty owner_rep_id is a validation error, like the other owner_rep_id fields."""
    from crm.errors import ValidationFailed
    from crm.toolspec.validation import validate_arguments

    tool = BY_NAME["pipeline_aggregate"]
    validate_arguments(tool, {"owner_rep_id": "rep-1"})
    with pytest.raises(ValidationFailed) as excinfo:
        validate_arguments(tool, {"owner_rep_id": ""})
    assert "owner_rep_id" in str(excinfo.value.source)


# --------------------------------------------------------------------------------------
# Model-facing descriptions carry no golden-task steering and name every allowlisted field
# --------------------------------------------------------------------------------------

_GOLDEN_TASKS = Path(__file__).resolve().parents[2] / "src" / "eval" / "golden_tasks.yaml"

#: Words that name a golden task's scenario rather than a tool's behaviour.
_SCENARIO_WORDS: tuple[str, ...] = ("successor", "departed", "stale", "regressed", "abandoned")

_ENTITY_ID = re.compile(r"\b(?:opp|cont|acct|rep|act|note|prop|aud|svc)-[a-z0-9]+\b")


def _golden_task_terms() -> frozenset[str]:
    """Every golden-task id, entity id named in the task file, and scenario word."""
    text = _GOLDEN_TASKS.read_text(encoding="utf-8")
    task_ids = {str(task["id"]) for task in yaml.safe_load(text)["tasks"]}
    return frozenset(task_ids | set(_ENTITY_ID.findall(text)) | set(_SCENARIO_WORDS))


def _descriptions(node: object) -> list[str]:
    """Every ``description`` string anywhere inside a JSON Schema node."""
    if isinstance(node, dict):
        found = [node["description"]] if isinstance(node.get("description"), str) else []
        return found + [text for value in node.values() for text in _descriptions(value)]
    if isinstance(node, list):
        return [text for value in node for text in _descriptions(value)]
    return []


def _steering_terms_in(text: str, terms: frozenset[str]) -> list[str]:
    """The golden-task terms ``text`` names, matched as whole words, case-insensitively."""
    return sorted(
        term for term in terms if re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text, re.I)
    )


def test_the_golden_task_term_set_is_non_vacuous() -> None:
    """The scanned term set holds task ids and entity ids as well as the scenario words."""
    terms = _golden_task_terms()
    assert any(term.startswith("gt-") for term in terms)
    assert {"opp-0000", "cont-0000", "acct-0000"} <= terms


def test_the_steering_scan_flags_a_scenario_hint() -> None:
    """Control: a description pointing at a golden task's answer path is detected."""
    hint = (
        "The contact list is the intended path for identifying a successor when an "
        "opportunity's primary contact has departed."
    )
    assert _steering_terms_in(hint, _golden_task_terms()) == ["departed", "successor"]


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda tool: tool.name)
def test_no_description_names_a_golden_task_entity(tool: ToolDefinition) -> None:
    """No tool or property description names a golden task, its entities, or its scenario."""
    terms = _golden_task_terms()
    for text in [tool.description, *_descriptions(tool.input_schema)]:
        assert _steering_terms_in(text, terms) == [], f"{tool.name}: {text!r}"


_UPDATE_TOOL_TABLES: dict[str, str] = {
    "update_account": "accounts",
    "update_contact": "contacts",
    "update_opportunity": "opportunities",
    "update_activity": "activities",
}


def test_every_updatable_table_has_an_update_tool() -> None:
    """The update-tool mapping below covers exactly the allowlisted tables."""
    assert set(_UPDATE_TOOL_TABLES.values()) == set(UPDATE_ALLOWLISTS)
    assert set(_UPDATE_TOOL_TABLES) <= set(BY_NAME)


@pytest.mark.parametrize(("tool_name", "table"), sorted(_UPDATE_TOOL_TABLES.items()))
def test_every_allowlist_key_appears_in_its_fields_description(tool_name: str, table: str) -> None:
    """The open ``fields`` object names each allowlisted key, and each high-risk key as refused."""
    fields = BY_NAME[tool_name].input_schema["properties"]["fields"]
    assert fields["additionalProperties"] is True
    sentences = [sentence.strip() for sentence in fields["description"].split(". ")]
    allowed = next(sentence for sentence in sentences if sentence.startswith("Allowed keys:"))
    for key in UPDATE_ALLOWLISTS[table]:
        assert re.search(rf"\b{key}\b", allowed), f"{tool_name}: {key!r} not named as allowed"
    refused = [sentence for sentence in sentences if "not accepted here" in sentence]
    for key in high_risk_fields_for(table):
        assert re.search(rf"\b{key}\b", refused[0]), f"{tool_name}: {key!r} not named as refused"
        assert "submit_proposal" in refused[0]
        assert not re.search(rf"\b{key}\b", allowed)

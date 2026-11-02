# SPDX-License-Identifier: MIT
"""Tests for `subagent_tool_schemas()`: one well-formed, family-neutral tool schema per subagent role.

Also checks the names are the role names, disjoint from and leaving `DIRECT_TOOL_NAMES` unchanged.
"""

from __future__ import annotations

from agents.registry import tool_registry_for
from agents.subagents import ALL_SUBAGENT_SPECS, SUBAGENT_SPECS_BY_ROLE
from agents.supervisor import (
    DIRECT_TOOL_NAMES,
    SUPERVISOR_ROLE,
    subagent_tool_schemas,
)

_EXPECTED_ROLES = ("research", "mutation", "analytics", "composer")


# --- one tool per role, deterministic order ---------------------------------------------------


def test_returns_one_schema_per_subagent_role() -> None:
    schemas = subagent_tool_schemas()
    assert len(schemas) == len(SUBAGENT_SPECS_BY_ROLE) == 4
    names = [schema["name"] for schema in schemas]
    assert set(names) == set(SUBAGENT_SPECS_BY_ROLE)


def test_order_follows_all_subagent_specs_deterministically() -> None:
    names = [schema["name"] for schema in subagent_tool_schemas()]
    assert names == [spec.role for spec in ALL_SUBAGENT_SPECS]
    # The four roles, in canonical order.
    assert names == list(_EXPECTED_ROLES)


def test_is_deterministic_across_calls() -> None:
    assert subagent_tool_schemas() == subagent_tool_schemas()


# --- each schema is well-formed and family-neutral --------------------------------------------


def test_each_schema_has_name_description_and_an_intent_input_schema() -> None:
    for schema in subagent_tool_schemas():
        assert set(schema) == {"name", "description", "input_schema"}
        assert isinstance(schema["name"], str) and schema["name"]
        assert isinstance(schema["description"], str) and schema["description"]
        input_schema = schema["input_schema"]
        assert isinstance(input_schema, dict)
        assert input_schema["type"] == "object"
        assert input_schema["required"] == ["intent"]
        assert input_schema["additionalProperties"] is False
        properties = input_schema["properties"]
        assert isinstance(properties, dict)
        assert set(properties) == {"intent"}
        assert properties["intent"]["type"] == "string"


def test_matches_the_provider_seam_tool_shape() -> None:
    """Every subagent tool carries the `name`, `description` and `input_schema` the seam reads."""
    for schema in subagent_tool_schemas():
        assert "name" in schema and "input_schema" in schema


def test_description_is_derived_from_the_roles_own_leaf_tools() -> None:
    """The description names the role's own leaf tools, sorted."""
    for schema in subagent_tool_schemas():
        role = str(schema["name"])
        leaf = ", ".join(sorted(tool.name for tool in SUBAGENT_SPECS_BY_ROLE[role].tools))
        description = str(schema["description"])
        assert role in description
        assert leaf in description
        assert "intent" in description


def test_no_schema_names_a_model_family() -> None:
    for schema in subagent_tool_schemas():
        blob = f"{schema['name']} {schema['description']}".lower()
        for vendor in ("claude", "anthropic", "gpt", "openai", "sonnet", "opus", "haiku"):
            assert vendor not in blob


# --- additive: names are the role names, disjoint from the direct tools -----------------------


def test_schema_names_are_the_dispatchable_role_names() -> None:
    names = {str(schema["name"]) for schema in subagent_tool_schemas()}
    assert names == set(SUBAGENT_SPECS_BY_ROLE)


def test_subagent_tool_names_are_disjoint_from_direct_tool_names() -> None:
    """No subagent tool name shadows a direct CRM read tool."""
    names = {str(schema["name"]) for schema in subagent_tool_schemas()}
    assert names.isdisjoint(DIRECT_TOOL_NAMES)


# --- the additive guarantee: nothing existing changed -----------------------------------------


def test_direct_tool_names_is_unchanged_and_still_the_registry_derived_set() -> None:
    """`DIRECT_TOOL_NAMES` is exactly the supervisor registry's tool set."""
    assert frozenset(tool.name for tool in tool_registry_for(SUPERVISOR_ROLE)) == DIRECT_TOOL_NAMES
    # No subagent role leaked into the direct set.
    assert DIRECT_TOOL_NAMES.isdisjoint(set(SUBAGENT_SPECS_BY_ROLE))


def test_each_description_is_the_role_its_purpose_and_its_isolation_contract() -> None:
    """The description is ``role: purpose`` plus the fixed isolation sentence and leaf tools."""
    for spec in ALL_SUBAGENT_SPECS:
        schema = next(s for s in subagent_tool_schemas() if s["name"] == spec.role)
        leaf_tools = ", ".join(sorted(tool.name for tool in spec.tools))
        assert schema["description"] == (
            f"{spec.role}: {spec.purpose} It sees only 'intent', not this conversation or the "
            "user's page, and returns the results of its tool calls, or the first error it could "
            f"not recover from. Its tools: {leaf_tools}."
        )


def test_the_intent_field_asks_for_a_self_contained_task() -> None:
    for schema in subagent_tool_schemas():
        input_schema = schema["input_schema"]
        assert isinstance(input_schema, dict)
        assert input_schema["properties"]["intent"]["description"] == (
            "The complete, self-contained task, including every record id, name and value it needs."
        )


def test_every_role_has_a_distinct_non_empty_purpose() -> None:
    purposes = [spec.purpose for spec in ALL_SUBAGENT_SPECS]
    assert all(purpose.strip() for purpose in purposes)
    assert len(set(purposes)) == len(purposes)
    assert SUBAGENT_SPECS_BY_ROLE["mutation"].purpose.startswith("Every change to CRM data")

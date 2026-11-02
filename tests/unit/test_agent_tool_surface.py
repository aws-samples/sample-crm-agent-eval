# SPDX-License-Identifier: MIT
"""Per-role sweep proving the four subagents' tool surfaces are structurally high-risk-free.

Checks each subagent's minimum tool surface, that every subagent tool resolves to the Lambda
handler backing the UI, and that no subagent can commit a high-risk write directly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agents.registry import RegisteredTool, tool_registry_for
from agents.subagents import ALL_SUBAGENT_SPECS, SUBAGENT_SPECS_BY_ROLE
from agents.subagents.analytics import ANALYTICS_SPEC
from agents.subagents.analytics import ROLE as ANALYTICS_ROLE
from agents.subagents.base import SUBAGENT_TIER, SubagentSpec, build_subagent_spec
from agents.subagents.composer import COMPOSER_SPEC
from agents.subagents.composer import ROLE as COMPOSER_ROLE
from agents.subagents.mutation import MUTATION_SPEC
from agents.subagents.mutation import ROLE as MUTATION_ROLE
from agents.subagents.research import RESEARCH_SPEC
from agents.subagents.research import ROLE as RESEARCH_ROLE
from crm.toolspec.definitions import (
    ALL_TOOLS,
    HIGH_RISK_OPPORTUNITY_FIELDS,
    UPDATE_ALLOWLISTS,
    RiskClass,
    ToolDefinition,
    high_risk_fields_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The four subagent roles this file sweeps, paired with each subagent module's exported spec.
_ROLE_SPECS: tuple[tuple[str, SubagentSpec], ...] = (
    (RESEARCH_ROLE, RESEARCH_SPEC),
    (MUTATION_ROLE, MUTATION_SPEC),
    (ANALYTICS_ROLE, ANALYTICS_SPEC),
    (COMPOSER_ROLE, COMPOSER_SPEC),
)

_READ_ONLY_ROLES = (RESEARCH_ROLE, ANALYTICS_ROLE, COMPOSER_ROLE)

#: The closed set of risk classes that may appear on a subagent's tool.
_NON_HIGH_RISK_RISK_CLASSES: frozenset[RiskClass] = frozenset(
    {"read", "low_risk_write", "proposal", "approval"}
)


# --------------------------------------------------------------------------------------
# Each subagent's spec is derived from the generated registry
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("role,spec", _ROLE_SPECS)
def test_subagent_spec_tools_equal_registry_for_its_role(role: str, spec: SubagentSpec) -> None:
    """Each subagent's ``tools`` is exactly ``tool_registry_for(role)`` — nothing added or hidden."""
    assert spec.role == role
    assert spec.tools == tool_registry_for(role)


@pytest.mark.parametrize("role,spec", _ROLE_SPECS)
def test_subagent_spec_runs_on_the_subagent_tier(role: str, spec: SubagentSpec) -> None:
    """Every one of the four subagents runs on the subagent tier."""
    assert spec.tier == SUBAGENT_TIER == "subagent"


def test_build_subagent_spec_is_the_only_construction_path_for_a_role() -> None:
    """A spec built directly with the same role produces an identical tool set."""
    for role, spec in _ROLE_SPECS:
        assert build_subagent_spec(role, spec.purpose) == spec


def test_research_subagent_covers_the_fr_6_4_minimum_surface() -> None:
    """Research holds at least: search opportunities, get opportunity, get account, get
    contact, list activities."""
    minimum = {
        "search_opportunities",
        "get_opportunity",
        "get_account",
        "get_contact",
        "list_activities",
    }
    assert minimum <= RESEARCH_SPEC.tool_names


def test_mutation_subagent_covers_the_fr_6_4_minimum_surface() -> None:
    """Mutation holds the low-risk writers plus submit; a person decides and reverts."""
    minimum = {
        "submit_proposal",
        "create_account",
        "update_account",
        "create_contact",
        "update_contact",
        "create_opportunity",
        "update_opportunity",
        "log_activity",
        "update_activity",
        "create_note",
    }
    assert minimum <= MUTATION_SPEC.tool_names


def test_analytics_subagent_covers_the_fr_6_4_minimum_surface() -> None:
    """Analytics holds pipeline aggregation."""
    assert "pipeline_aggregate" in ANALYTICS_SPEC.tool_names


def test_composer_subagent_covers_the_fr_6_4_minimum_surface() -> None:
    """Composer holds message and email drafting."""
    assert "draft_message" in COMPOSER_SPEC.tool_names


# --------------------------------------------------------------------------------------
# No subagent holds a tool that can commit a high-risk write directly
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("role,spec", _ROLE_SPECS)
def test_no_subagent_holds_a_high_risk_writer(role: str, spec: SubagentSpec) -> None:
    """Every tool a subagent holds carries a risk class other than a high-risk one."""
    for tool in spec.tools:
        definition = _definition_for(tool)
        assert definition.risk in _NON_HIGH_RISK_RISK_CLASSES


@pytest.mark.parametrize("role,spec", _ROLE_SPECS)
def test_no_subagent_holds_a_delete_tool(role: str, spec: SubagentSpec) -> None:
    """No subagent holds a tool named ``delete_*``."""
    assert not any(tool.name.startswith("delete_") for tool in spec.tools)


def test_no_delete_tool_exists_anywhere_in_the_toolspec() -> None:
    """No ``delete_*`` tool exists anywhere in the toolspec."""
    assert not any(tool.name.startswith("delete_") for tool in ALL_TOOLS)


@pytest.mark.parametrize("role,spec", _ROLE_SPECS)
def test_submit_proposal_is_the_sole_proposal_producer_any_subagent_could_reach(
    role: str, spec: SubagentSpec
) -> None:
    """The only ``proposal``-class tool a subagent may hold is ``submit_proposal``."""
    proposal_tools = [tool.name for tool in spec.tools if _definition_for(tool).risk == "proposal"]
    assert set(proposal_tools) <= {"submit_proposal"}


def test_exactly_one_tool_in_the_whole_toolspec_has_risk_proposal() -> None:
    """``submit_proposal`` is the only tool in the toolspec with risk class ``proposal``."""
    proposal_definitions = [tool for tool in ALL_TOOLS if tool.risk == "proposal"]
    assert [tool.name for tool in proposal_definitions] == ["submit_proposal"]


def test_opportunity_update_allowlist_is_disjoint_from_the_five_high_risk_fields() -> None:
    """The opportunity update allowlist shares no field with the five high-risk fields."""
    allowlist = UPDATE_ALLOWLISTS["opportunities"]
    high_risk_fields = high_risk_fields_for("opportunities")
    assert high_risk_fields == HIGH_RISK_OPPORTUNITY_FIELDS
    assert allowlist.isdisjoint(high_risk_fields)


@pytest.mark.parametrize("role,spec", _ROLE_SPECS)
def test_mutation_specific_low_risk_writer_never_names_a_high_risk_field_in_its_schema(
    role: str, spec: SubagentSpec
) -> None:
    """A held ``update_opportunity`` schema never enumerates a high-risk field in ``fields``."""
    high_risk_fields = high_risk_fields_for("opportunities")
    for tool in spec.tools:
        definition = _definition_for(tool)
        if definition.name != "update_opportunity":
            continue
        properties = definition.input_schema.get("properties", {})
        fields_property = properties.get("fields", {})
        enumerated = set(fields_property.get("enum", []))
        assert enumerated.isdisjoint(high_risk_fields)


# --------------------------------------------------------------------------------------
# Research, analytics, and composer are read-only; mutation holds no direct high-risk
# mutator
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", _READ_ONLY_ROLES)
def test_read_only_subagent_holds_no_mutating_tool(role: str) -> None:
    """Research, Analytics, and Composer hold no tool that can change persisted state."""
    spec = SUBAGENT_SPECS_BY_ROLE[role]
    for tool in spec.tools:
        assert not tool.mutates


def test_mutation_subagent_holds_no_direct_high_risk_mutator() -> None:
    """Mutation holds no tool whose invocation alone commits a change to an opportunity's stage,
    value, close date, owner, or primary contact."""
    direct_high_risk_mutator_names = {
        f"update_{table}_{field}"
        for table in ("opportunity",)
        for field in HIGH_RISK_OPPORTUNITY_FIELDS
    } | {"set_stage", "set_owner", "set_value", "set_close_date", "set_primary_contact"}
    assert direct_high_risk_mutator_names.isdisjoint(MUTATION_SPEC.tool_names)
    assert "update_opportunity" in MUTATION_SPEC.tool_names


def test_mutation_subagent_tools_are_exactly_the_toolspecs_mutation_agents_grant() -> None:
    """The toolspec tools granted to ``mutation`` are exactly :data:`MUTATION_SPEC`'s tools."""
    granted_to_mutation = {tool.name for tool in ALL_TOOLS if "mutation" in tool.agents}
    assert granted_to_mutation == MUTATION_SPEC.tool_names


# --------------------------------------------------------------------------------------
# Every subagent tool resolves to the same Lambda backing the UI
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("role,spec", _ROLE_SPECS)
def test_every_subagent_tool_has_a_handler_and_therefore_a_ui_counterpart(
    role: str, spec: SubagentSpec
) -> None:
    """Every tool a subagent holds carries its toolspec definition's ``crm.handlers.`` path."""
    for tool in spec.tools:
        assert tool.handler
        assert tool.handler.startswith("crm.handlers.")
        definition = _definition_for(tool)
        assert tool.handler == definition.handler


def test_no_tool_in_the_full_registry_lacks_a_handler() -> None:
    """Every tool in the toolspec has a ``crm.handlers.`` handler path."""
    for definition in ALL_TOOLS:
        assert definition.handler
        assert definition.handler.startswith("crm.handlers.")


def test_every_subagent_tool_handler_resolves_to_an_existing_lambda_handler_module() -> None:
    """Each subagent tool's handler path names a module file that exists under ``src/``."""
    for _, spec in _ROLE_SPECS:
        for tool in spec.tools:
            module_path = tool.handler.rsplit(".", 1)[0]
            relative = Path(*module_path.split(".")).with_suffix(".py")
            assert (REPO_ROOT / "src" / relative).is_file(), (
                f"{tool.name}: handler module {module_path} not found under src/crm/handlers/"
            )


# --------------------------------------------------------------------------------------
# Cross-subagent invariants
# --------------------------------------------------------------------------------------


def test_every_registered_role_appears_in_all_subagent_specs_exactly_once() -> None:
    """The four specs cover the four subagent roles, each exactly once, in a stable order."""
    roles = [spec.role for spec in ALL_SUBAGENT_SPECS]
    assert roles == [RESEARCH_ROLE, MUTATION_ROLE, ANALYTICS_ROLE, COMPOSER_ROLE]
    assert len(set(roles)) == len(roles)


def test_subagent_specs_by_role_matches_all_subagent_specs() -> None:
    """The role-keyed mapping the supervisor consumes agrees with the ordered tuple."""
    assert set(SUBAGENT_SPECS_BY_ROLE) == {spec.role for spec in ALL_SUBAGENT_SPECS}
    for role, spec in SUBAGENT_SPECS_BY_ROLE.items():
        assert spec.role == role


def test_no_two_subagents_share_the_supervisors_direct_tool_set_by_accident() -> None:
    """Every subagent's tool set is non-empty and the four sets are pairwise distinct."""
    tool_sets = [spec.tool_names for spec in ALL_SUBAGENT_SPECS]
    assert all(tool_set for tool_set in tool_sets)
    assert len({frozenset(s) for s in tool_sets}) == len(tool_sets)


# --------------------------------------------------------------------------------------
# No vendor SDK import anywhere under src/agents/subagents/
# --------------------------------------------------------------------------------------

_FORBIDDEN_VENDOR_IMPORT_ROOTS = ("anthropic", "openai", "boto3", "botocore")


def test_no_vendor_sdk_import_under_subagents_package() -> None:
    """No file under ``src/agents/subagents/`` imports a vendor SDK."""
    root = REPO_ROOT / "src" / "agents" / "subagents"
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = _imported_module_roots(tree) & set(_FORBIDDEN_VENDOR_IMPORT_ROOTS)
        if found:
            violations.append(f"{path.relative_to(REPO_ROOT)}: imports {sorted(found)}")
    assert not violations, "vendor SDK import(s) found:\n" + "\n".join(violations)


def _imported_module_roots(tree: ast.Module) -> set[str]:
    """Collect the top-level module name of every import statement in a parsed module."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


def _definition_for(tool: RegisteredTool) -> ToolDefinition:
    """Look up the source :class:`ToolDefinition` a :class:`RegisteredTool` was derived from."""
    for definition in ALL_TOOLS:
        if definition.name == tool.name:
            return definition
    raise AssertionError(f"no ToolDefinition named {tool.name!r} in ALL_TOOLS")

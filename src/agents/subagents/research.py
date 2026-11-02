# SPDX-License-Identifier: MIT
"""The Research subagent: read-only lookups over accounts, contacts, and opportunities.

Holds no writer of any kind. Its tool surface is exactly what
:func:`agents.registry.tool_registry_for` returns for role ``"research"``.
"""

from __future__ import annotations

from agents.subagents.base import SubagentSpec, build_subagent_spec

#: The Research subagent's role name, matched against
#: :attr:`crm.toolspec.definitions.ToolDefinition.agents`.
ROLE = "research"

#: What the Research subagent is for; see :attr:`SubagentSpec.purpose`.
PURPOSE = (
    "Read-only investigation that needs several dependent reads across accounts, contacts, "
    "opportunities, activities and audit records."
)

#: The Research subagent's spec: search and get opportunities, accounts and contacts, list
#: activities, and ``list_audit_records``; every tool is read-only.
RESEARCH_SPEC: SubagentSpec = build_subagent_spec(ROLE, PURPOSE)


__all__ = ["PURPOSE", "ROLE", "RESEARCH_SPEC"]

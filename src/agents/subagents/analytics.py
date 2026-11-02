# SPDX-License-Identifier: MIT
"""The Analytics subagent: read-only pipeline aggregation.

Holds no writer of any kind. Its tool surface is exactly what
:func:`agents.registry.tool_registry_for` returns for role ``"analytics"``.
"""

from __future__ import annotations

from agents.subagents.base import SubagentSpec, build_subagent_spec

#: The Analytics subagent's role name, matched against
#: :attr:`crm.toolspec.definitions.ToolDefinition.agents`.
ROLE = "analytics"

#: What the Analytics subagent is for; see :attr:`SubagentSpec.purpose`.
PURPOSE = "Read-only rollups and comparisons across many opportunities."

#: The Analytics subagent's spec: pipeline aggregation (stage funnel counts, value rollup,
#: forecast by close date) through the read-only ``pipeline_aggregate`` tool.
ANALYTICS_SPEC: SubagentSpec = build_subagent_spec(ROLE, PURPOSE)


__all__ = ["PURPOSE", "ROLE", "ANALYTICS_SPEC"]

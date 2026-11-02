# SPDX-License-Identifier: MIT
"""Shared subagent definition shape.

Every subagent — Research, Mutation, Analytics, Composer — is one :class:`SubagentSpec`: a role
name, its one-sentence purpose, the tools that role holds, and the model tier it runs on. The tool
set is read at import time from :func:`agents.registry.tool_registry_for`, which derives from
:data:`crm.toolspec.definitions.ALL_TOOLS`, so each subagent tool carries the same handler path
as the Lambda the UI calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.registry import RegisteredTool, tool_registry_for
from providers.capabilities import Tier

#: The model tier every subagent runs on; its models and effort caps are in
#: ``config/model_capabilities.yaml``.
SUBAGENT_TIER: Tier = "subagent"


@dataclass(frozen=True, slots=True)
class SubagentSpec:
    """One subagent's role, purpose, tool surface, and model tier.

    Attributes:
        role: The agent role name matched against
            :attr:`crm.toolspec.definitions.ToolDefinition.agents`; it determines the tool surface.
        purpose: What the subagent is for, in one or two sentences. The single source for both its
            system prompt (``config/agent_prompts/subagent.yaml``) and the supervisor-facing tool
            description (:func:`agents.supervisor.subagent_tool_schemas`).
        tools: Every tool this role holds, derived exclusively from
            :func:`agents.registry.tool_registry_for`.
        tier: The model tier this subagent runs on. Always :data:`SUBAGENT_TIER`.
    """

    role: str
    purpose: str
    tools: tuple[RegisteredTool, ...]
    tier: Tier = SUBAGENT_TIER

    @property
    def tool_names(self) -> frozenset[str]:
        """The names of every tool this subagent holds, as a set for membership checks."""
        return frozenset(tool.name for tool in self.tools)


def build_subagent_spec(role: str, purpose: str) -> SubagentSpec:
    """Build a :class:`SubagentSpec` for ``role`` from the generated tool registry alone.

    Args:
        role: An agent role name recognised by
            :func:`crm.toolspec.definitions.tools_for_agent`.
        purpose: The subagent's purpose sentence (:attr:`SubagentSpec.purpose`).

    Returns:
        A :class:`SubagentSpec` whose ``tools`` is exactly ``tool_registry_for(role)`` — nothing
        added, nothing withheld.
    """
    return SubagentSpec(role=role, purpose=purpose, tools=tool_registry_for(role))


__all__ = [
    "SUBAGENT_TIER",
    "SubagentSpec",
    "build_subagent_spec",
]

# SPDX-License-Identifier: MIT
"""The Composer subagent: read-only message and email drafting.

Holds no writer of any kind; ``draft_message`` returns draft text and persists nothing. Its tool
surface is exactly what :func:`agents.registry.tool_registry_for` returns for role ``"composer"``.
"""

from __future__ import annotations

from agents.subagents.base import SubagentSpec, build_subagent_spec

#: The Composer subagent's role name, matched against
#: :attr:`crm.toolspec.definitions.ToolDefinition.agents`.
ROLE = "composer"

#: What the Composer subagent is for; see :attr:`SubagentSpec.purpose`.
PURPOSE = "Drafts an email or message to a contact; sends and saves nothing."

#: The Composer subagent's spec: message and email drafting through ``draft_message``.
COMPOSER_SPEC: SubagentSpec = build_subagent_spec(ROLE, PURPOSE)


__all__ = ["PURPOSE", "ROLE", "COMPOSER_SPEC"]

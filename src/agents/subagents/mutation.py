# SPDX-License-Identifier: MIT
"""The Mutation subagent: every state change, gated through proposal for high-risk fields.

Holds the low-risk writers (create/update account, create/update contact, create/update
opportunity, log/update activity, create note), the proposal surface (``submit_proposal``,
``rebase_proposal``), and the reads it needs to look up a target before writing it. It holds
**no direct high-risk mutator** — there is no tool in its surface, or in any surface, that can
change an opportunity's stage, value, close date, owner, or primary contact except by first
calling ``submit_proposal``. It holds none of ``approve_proposal``, ``reject_proposal`` or
``revert_audit_record``: a person decides and reverts, through the UI or MCP with a human
identity, and those handlers refuse the agent channel (:func:`crm.authz.require_proposal_decider`).
Its tool surface is exactly what :func:`agents.registry.tool_registry_for` returns for role
``"mutation"``.
"""

from __future__ import annotations

from agents.subagents.base import SubagentSpec, build_subagent_spec

#: The Mutation subagent's role name, matched against
#: :attr:`crm.toolspec.definitions.ToolDefinition.agents`.
ROLE = "mutation"

#: What the Mutation subagent is for; see :attr:`SubagentSpec.purpose`.
PURPOSE = (
    "Every change to CRM data: creates and updates records, logs activities and notes, and submits "
    "proposals for the changes a person must approve."
)

#: The Mutation subagent's spec: every low-risk writer and the proposal/approval surface.
MUTATION_SPEC: SubagentSpec = build_subagent_spec(ROLE, PURPOSE)


__all__ = ["PURPOSE", "ROLE", "MUTATION_SPEC"]

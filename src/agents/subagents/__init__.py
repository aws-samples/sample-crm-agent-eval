# SPDX-License-Identifier: MIT
"""The four subagents the supervisor holds as tools.

:data:`ALL_SUBAGENT_SPECS` and :data:`SUBAGENT_SPECS_BY_ROLE` expose one
:class:`~agents.subagents.base.SubagentSpec` per role: its role name, its tool surface (from
:func:`agents.registry.tool_registry_for`), and its model tier.
"""

from __future__ import annotations

from typing import Final

from agents.subagents.analytics import ANALYTICS_SPEC
from agents.subagents.analytics import ROLE as ANALYTICS_ROLE
from agents.subagents.base import SubagentSpec
from agents.subagents.composer import COMPOSER_SPEC
from agents.subagents.composer import ROLE as COMPOSER_ROLE
from agents.subagents.mutation import MUTATION_SPEC
from agents.subagents.mutation import ROLE as MUTATION_ROLE
from agents.subagents.research import RESEARCH_SPEC
from agents.subagents.research import ROLE as RESEARCH_ROLE

#: Every subagent spec the supervisor may delegate to, in a fixed order.
ALL_SUBAGENT_SPECS: Final[tuple[SubagentSpec, ...]] = (
    RESEARCH_SPEC,
    MUTATION_SPEC,
    ANALYTICS_SPEC,
    COMPOSER_SPEC,
)

#: The same specs, keyed by role name.
SUBAGENT_SPECS_BY_ROLE: Final[dict[str, SubagentSpec]] = {
    spec.role: spec for spec in ALL_SUBAGENT_SPECS
}

__all__ = [
    "ALL_SUBAGENT_SPECS",
    "ANALYTICS_ROLE",
    "ANALYTICS_SPEC",
    "COMPOSER_ROLE",
    "COMPOSER_SPEC",
    "MUTATION_ROLE",
    "MUTATION_SPEC",
    "RESEARCH_ROLE",
    "RESEARCH_SPEC",
    "SUBAGENT_SPECS_BY_ROLE",
    "SubagentSpec",
]

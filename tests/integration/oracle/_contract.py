# SPDX-License-Identifier: MIT
"""The oracle script contract: the known-correct model steps a :class:`ScriptedProvider` replays.

Each ``scripts_*.py`` module in this package exposes :data:`SCRIPTS_REGISTRY_ATTR`, a mapping from
golden task id to one or more :data:`OracleScript` values. Ids inside ``args`` are tenant-relative;
the provider composes them against the turn's tenant. Offline test code only, never published.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

#: Every role a step may name: the supervisor and the four subagents.
ROLES: Final[tuple[str, ...]] = ("supervisor", "research", "mutation", "analytics", "composer")


@dataclass(frozen=True, slots=True)
class Call:
    """One model tool call in one role's round.

    Attributes:
        role: One of :data:`ROLES`.
        tool: A CRM tool name, or a subagent tool name when ``role`` is ``"supervisor"``.
        args: The call's arguments, with tenant-relative ids.
    """

    role: str
    tool: str
    args: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Answer:
    """One role's final text for its turn or delegation.

    Attributes:
        role: One of :data:`ROLES`.
        text: The answer text, with tenant-relative ids.
    """

    role: str
    text: str


Step = Call | Answer
OracleScript = tuple[Step, ...]

#: The attribute each ``scripts_*.py`` module exposes: ``Mapping[task_id, tuple[OracleScript, ...]]``.
SCRIPTS_REGISTRY_ATTR: Final[str] = "SCRIPTS"

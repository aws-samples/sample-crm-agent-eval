# SPDX-License-Identifier: MIT
"""The Strands agent tool registry, generated from :mod:`crm.toolspec.definitions`.

Every :class:`RegisteredTool` this module returns is built from one entry of
:data:`crm.toolspec.definitions.ALL_TOOLS`; there is no hand-maintained tool table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from crm.toolspec.definitions import ALL_TOOLS, ToolDefinition, tools_for_agent


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """One tool as it is exposed to an agent, derived entirely from a :class:`ToolDefinition`.

    Attributes:
        name: The tool's stable name, taken from the source definition.
        description: The tool's model-facing description, taken from the source definition.
        input_schema: The JSON Schema for the tool's arguments, taken from the source definition.
        handler: Dotted path to the Lambda handler this tool invokes, taken from the source
            definition.
        mutates: Whether invoking this tool can change persisted state, taken from the source
            definition's :attr:`~crm.toolspec.definitions.ToolDefinition.mutates`.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: str
    mutates: bool

    @classmethod
    def _from_definition(cls, definition: ToolDefinition) -> RegisteredTool:
        """Construct a :class:`RegisteredTool` from exactly one source-of-truth definition."""
        return cls(
            name=definition.name,
            description=definition.description,
            input_schema=definition.input_schema,
            handler=definition.handler,
            mutates=definition.mutates,
        )


def tool_registry_for(role: str) -> tuple[RegisteredTool, ...]:
    """Return the tools agent role ``role`` may hold, derived from ``ALL_TOOLS``.

    Args:
        role: An agent role name, matched against
            :attr:`crm.toolspec.definitions.ToolDefinition.agents`.

    Returns:
        Every :class:`RegisteredTool` whose source definition names ``role`` in ``agents``, in
        ``ALL_TOOLS`` order; an empty tuple for a role holding no tool.
    """
    return tuple(
        RegisteredTool._from_definition(definition) for definition in tools_for_agent(role)
    )


def full_registry() -> dict[str, RegisteredTool]:
    """Return every tool definition rendered as a :class:`RegisteredTool`, keyed by name.

    Returns:
        A mapping from tool name to :class:`RegisteredTool`, one entry per member of
        :data:`crm.toolspec.definitions.ALL_TOOLS`.
    """
    return {
        definition.name: RegisteredTool._from_definition(definition) for definition in ALL_TOOLS
    }


def mutating_tool_names_for(role: str) -> frozenset[str]:
    """Return the subset of ``role``'s tools that can change persisted state.

    Derived from each source definition's
    :attr:`~crm.toolspec.definitions.ToolDefinition.mutates`.

    Args:
        role: An agent role name, matched as :func:`tool_registry_for` matches it.

    Returns:
        The mutating tool names granted to ``role``, empty for a read-only role.
    """
    return frozenset(tool.name for tool in tool_registry_for(role) if tool.mutates)


#: Every tool name this module's registry can expose, computed once from ``ALL_TOOLS``.
REGISTERED_TOOL_NAMES: Final[frozenset[str]] = frozenset(tool.name for tool in ALL_TOOLS)

#: Every mutating tool name in the whole toolspec, computed once from ``ALL_TOOLS``.
MUTATING_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    tool.name for tool in ALL_TOOLS if tool.mutates
)


__all__ = [
    "MUTATING_TOOL_NAMES",
    "REGISTERED_TOOL_NAMES",
    "RegisteredTool",
    "full_registry",
    "mutating_tool_names_for",
    "tool_registry_for",
]

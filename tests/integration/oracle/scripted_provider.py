# SPDX-License-Identifier: MIT
"""``ScriptedProvider``: a :class:`providers.base.ModelProvider` that replays one oracle script.

Each ``stream`` call infers the calling role from the offered tool names and emits that role's next
step. A tool-tier formulation call (one offered tool, under the tool actor's directive) is answered
by echoing the subagent's draft arguments as the tool call, and consumes no step. Offline test code
only: no ``src/`` module and no harness option may reference it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Final

from agents.subagents import ALL_SUBAGENT_SPECS
from agents.tool_actor import _ACTOR_DIRECTIVE
from eval.task_ids import compose_ids_in_text
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation
from tests.integration.oracle._contract import ROLES, Answer, Call, OracleScript, Step

#: The role name :func:`infer_role` returns for a tool-tier formulation call.
TOOL_TIER_ROLE: Final[str] = "tool"

#: The supervisor's role name.
SUPERVISOR_ROLE: Final[str] = "supervisor"

#: Token counts reported on every emitted ``usage`` and ``completed`` event.
_INPUT_TOKENS: Final[int] = 64
_OUTPUT_TOKENS: Final[int] = 16

_SUBAGENT_SURFACES: Final[dict[str, frozenset[str]]] = {
    spec.role: frozenset(tool.name for tool in spec.tools) for spec in ALL_SUBAGENT_SPECS
}


class ScriptExhausted(RuntimeError):  # noqa: N818
    """A role was asked for a step past the end of its script, or a step was left unconsumed."""


class UnknownToolSurfaceError(ValueError):
    """The offered tools match no role, or more than one."""


def infer_role(tools: Sequence[Mapping[str, object]], messages: Sequence[object] = ()) -> str:
    """Return the role whose tool surface ``tools`` is.

    Any subagent tool name means ``"supervisor"``. A single tool under the tool actor's directive
    means :data:`TOOL_TIER_ROLE`. Otherwise the names must equal exactly one subagent's surface.

    Raises:
        UnknownToolSurfaceError: The surface is empty, unknown, or ambiguous.
    """
    names = frozenset(str(tool.get("name")) for tool in tools)
    if names & _SUBAGENT_SURFACES.keys():
        return SUPERVISOR_ROLE
    if len(tools) == 1 and _actor_intent(messages) is not None:
        return TOOL_TIER_ROLE
    matches = [role for role, surface in _SUBAGENT_SURFACES.items() if surface == names]
    if len(matches) != 1:
        raise UnknownToolSurfaceError(f"tool surface {sorted(names)} matches roles {matches}")
    return matches[0]


def _actor_intent(messages: Sequence[object]) -> str | None:
    """The tool actor's intent text, or ``None`` when ``messages`` is not a tool-actor request."""
    if len(messages) != 1 or not isinstance(messages[0], Mapping):
        return None
    content = messages[0].get("content")
    prefix = f"{_ACTOR_DIRECTIVE}\n\n"
    if not isinstance(content, str) or not content.startswith(prefix):
        return None
    return content[len(prefix) :]


def _compose(value: object, tenant: str) -> object:
    """Compose every tenant-relative id in every string inside ``value``."""
    if isinstance(value, str):
        return compose_ids_in_text(value, tenant=tenant)
    if isinstance(value, Mapping):
        return {str(key): _compose(item, tenant) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_compose(item, tenant) for item in value]
    return value


class ScriptedProvider(ModelProvider):
    """Replay one :data:`OracleScript` for a turn in ``tenant``, one step per role per round.

    Args:
        scripts: The steps, in order; each role consumes its own steps in sequence.
        tenant: The tenant the script's ids are composed against.

    Raises:
        ValueError: A step names a role outside :data:`ROLES`.
    """

    def __init__(self, scripts: OracleScript, tenant: str) -> None:
        unknown = sorted({step.role for step in scripts} - set(ROLES))
        if unknown:
            raise ValueError(f"oracle script names unknown role(s) {unknown}")
        self._tenant = tenant
        self._steps: dict[str, list[Step]] = {role: [] for role in ROLES}
        for step in scripts:
            self._steps[step.role].append(step)
        self._cursors: dict[str, int] = dict.fromkeys(ROLES, 0)
        self._overruns: list[str] = []
        self._call_count = 0

    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        """Emit the inferred role's next step, or echo a tool-tier formulation.

        Raises:
            UnknownToolSurfaceError: ``tools`` matches no role.
            ScriptExhausted: The role has no step left.
        """
        del invocation
        role = infer_role(tools, messages)
        if role == TOOL_TIER_ROLE:
            return self._emit(self._formulation(str(tools[0].get("name")), messages))
        cursor = self._cursors[role]
        if cursor >= len(self._steps[role]):
            self._overruns.append(role)
            raise ScriptExhausted(
                f"role {role!r} asked for step {cursor + 1} of a script of {cursor}"
            )
        self._cursors[role] = cursor + 1
        return self._emit(self._events_for(role, self._steps[role][cursor]))

    def remaining(self) -> dict[str, int]:
        """Unconsumed steps per role, for roles with any left."""
        return {
            role: len(steps) - self._cursors[role]
            for role, steps in self._steps.items()
            if len(steps) > self._cursors[role]
        }

    def assert_fully_consumed(self) -> None:
        """Raise :class:`ScriptExhausted` if any step is unconsumed or any role overran its script."""
        if self._overruns:
            raise ScriptExhausted(f"roles overran their scripts: {self._overruns}")
        remaining = self.remaining()
        if remaining:
            raise ScriptExhausted(f"unconsumed oracle steps per role: {remaining}")

    def _next_tool_id(self, role: str) -> str:
        self._call_count += 1
        return f"oracle-{role}-{self._call_count}"

    def _events_for(self, role: str, step: Step) -> list[ProviderEvent]:
        if isinstance(step, Call):
            tool_input = _compose(step.args, self._tenant)
            assert isinstance(tool_input, dict)
            return [
                ProviderEvent(
                    type="tool_call",
                    tool_name=step.tool,
                    tool_input=tool_input,
                    tool_id=self._next_tool_id(role),
                ),
                self._usage(),
                ProviderEvent(type="completed", awaiting_tools=True),
            ]
        assert isinstance(step, Answer)
        return [
            ProviderEvent(
                type="text_delta", text=compose_ids_in_text(step.text, tenant=self._tenant)
            ),
            self._usage(),
            ProviderEvent(
                type="completed", input_tokens=_INPUT_TOKENS, output_tokens=_OUTPUT_TOKENS
            ),
        ]

    def _formulation(self, tool_name: str, messages: Sequence[object]) -> list[ProviderEvent]:
        intent = _actor_intent(messages) or ""
        try:
            draft = json.loads(intent)
        except json.JSONDecodeError:
            draft = {}
        tool_input = dict(draft) if isinstance(draft, dict) else {}
        return [
            ProviderEvent(
                type="tool_call",
                tool_name=tool_name,
                tool_input=tool_input,
                tool_id=self._next_tool_id(TOOL_TIER_ROLE),
            ),
            self._usage(),
            ProviderEvent(
                type="completed", input_tokens=_INPUT_TOKENS, output_tokens=_OUTPUT_TOKENS
            ),
        ]

    @staticmethod
    def _usage() -> ProviderEvent:
        return ProviderEvent(
            type="usage",
            input_tokens=_INPUT_TOKENS,
            output_tokens=_OUTPUT_TOKENS,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )

    @staticmethod
    async def _emit(events: list[ProviderEvent]) -> AsyncIterator[ProviderEvent]:
        for event in events:
            yield event

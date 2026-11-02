# SPDX-License-Identifier: MIT
"""The AgentCore Runtime HTTP entrypoint: identity, fail-fast tuning, and resumable SSE.

1. **Identity.** :func:`resolve_identity` decodes the payload through
   :func:`crm.envelope.decode_identity`; the relay-verified claims arrive under
   :data:`crm.envelope.AGENT_IDENTITY_KEY` on the ``agent`` channel.
2. **Tuning.** :func:`resolve_tuning` decodes the schema-3 per-tier ``tuning`` block and resolves
   every tier, raising :class:`crm.errors.ValidationFailed` naming the offending field before any
   turn starts. Each tier runs its own family.
3. **Session resumption.** A :class:`SessionStore` records every emitted event under the
   ``session_id``; :func:`run_turn` with ``resume_from`` replays only later events. ``heartbeat``
   events are streamed but never recorded, so they do not shift resume indices.
4. **SSE framing.** :func:`format_sse_line` renders one :class:`agents.events.AgentEvent` as one
   JSON object on one line.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from agents.budget import CHAT_BUDGET, TurnBudget, TurnBudgetSpec
from agents.events import AgentEvent
from agents.supervisor import SUPERVISOR_TIER, SupervisorTurn
from crm.envelope import Identity, decode_identity
from crm.errors import ValidationFailed
from crm.toolspec.page_context import PageContext, parse_page_context
from providers.base import ModelProvider, ResolvedInvocation, Tuning
from providers.capabilities import CapabilityMatrix, Tier
from providers.tier_tuning import TurnTuning, parse_wire_tuning, resolve_turn

#: The ``source`` recorded on a chat turn's tuning when the payload names none.
_TUNING_SOURCE_UI: Final[str] = "agent.invocation.tuning"

#: Source named when the inbound invocation carries no usable ``prompt``.
_PROMPT_SOURCE_UI: Final[str] = "agent.invocation.prompt"


def resolve_identity(payload: dict[str, Any]) -> Identity:
    """Decode the invoking user's identity from an invocation payload.

    Delegates entirely to :func:`crm.envelope.decode_identity`.

    Args:
        payload: The raw invocation event, carrying one identity channel
            :func:`crm.envelope.decode_identity` recognises.

    Returns:
        The authenticated caller's identity and scope.

    Raises:
        crm.errors.AuthenticationFailed: When no recognised identity channel is present, or the
            matching channel's payload is absent, malformed, or missing a rep identifier.
    """
    return decode_identity(payload)


def resolve_tuning(
    matrix: CapabilityMatrix, *, raw_tuning: object, default_source: str = _TUNING_SOURCE_UI
) -> tuple[TurnTuning, Mapping[Tier, ResolvedInvocation]]:
    """Decode a schema-3 wire ``tuning`` block and resolve every tier before the turn starts.

    Args:
        matrix: The loaded capability matrix.
        raw_tuning: The inbound payload's ``tuning`` value. ``None`` (absent) means every tier
            takes its configured default.
        default_source: The ``source`` recorded when the block names none.

    Returns:
        The decoded :class:`providers.tier_tuning.TurnTuning` and each tier's resolved invocation,
        from :func:`providers.tier_tuning.resolve_turn`.

    Raises:
        crm.errors.ValidationFailed: A tier carries a ``temperature`` key, the block is not schema 3,
            or any tier names a family, model or effort the matrix rejects. ``source``
            names the offending field, e.g. ``tuning.schema`` or ``tuning.tiers.<tier>.<field>``.
    """
    if raw_tuning is None:
        tuning = TurnTuning(source=default_source, tiers={})
    else:
        tuning = parse_wire_tuning(raw_tuning, default_source=default_source)
    return tuning, resolve_turn(matrix, tuning)


def event_payload(event: AgentEvent) -> dict[str, Any]:
    """Render one :class:`agents.events.AgentEvent` as the JSON object the wire carries.

    :func:`format_sse_line` frames this object for the SSE path, and a non-streaming response
    embeds the same object.

    Args:
        event: The closed-set event to render.

    Returns:
        A JSON-serialisable object carrying every field of the closed event schema, with the
        fields this ``kind`` does not use present and ``None``.
    """
    return {
        "kind": event.kind,
        "tool_name": event.tool_name,
        "tool_input": event.tool_input,
        "tool_output": event.tool_output,
        "truncated": event.truncated,
        "proposal_id": event.proposal_id,
        "proposal_summary": event.proposal_summary,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "cache_read_tokens": event.cache_read_tokens,
        "cache_write_tokens": event.cache_write_tokens,
        "text": event.text,
        "event_id": event.event_id,
        "partial": event.partial,
        "error_kind": event.error_kind,
        "error_message": event.error_message,
        "partial_work": event.partial_work,
        "cost_lines": (
            None if event.cost_lines is None else [dict(line) for line in event.cost_lines]
        ),
        "cost_usd": event.cost_usd,
        "cost_withheld": event.cost_withheld,
        "resolved_tiers": (
            None if event.resolved_tiers is None else [dict(tier) for tier in event.resolved_tiers]
        ),
    }


def format_sse_line(event: AgentEvent) -> str:
    """Render one :class:`agents.events.AgentEvent` as one JSON object on one SSE data line.

    Args:
        event: The closed-set event to render.

    Returns:
        A single ``data: <json>\\n\\n`` SSE frame carrying exactly one JSON object.
    """
    return f"data: {json.dumps(event_payload(event), allow_nan=False)}\n\n"


@dataclass(slots=True)
class _RecordedEvent:
    """One event recorded in a session, tagged with its sequence number.

    Attributes:
        sequence: Monotonically increasing position of this event within its session, starting
            at 1.
        event: The recorded event itself.
    """

    sequence: int
    event: AgentEvent


class SessionStore(Protocol):
    """The resumption contract: events recorded per session id, replayable after a sequence number."""

    def append(self, session_id: str, event: AgentEvent) -> int:
        """Record ``event`` for ``session_id`` and return its assigned sequence number."""
        raise NotImplementedError

    def events_after(self, session_id: str, *, sequence: int) -> tuple[AgentEvent, ...]:
        """Return every event recorded for ``session_id`` with a sequence number above ``sequence``."""
        raise NotImplementedError

    def last_sequence(self, session_id: str) -> int:
        """Return the highest sequence number recorded for ``session_id``, or ``0`` if none."""
        raise NotImplementedError


@dataclass(slots=True)
class InMemorySessionStore:
    """A process-local :class:`SessionStore`, keyed by session id.

    Sessions are held for the lifetime of the store instance and never evicted.

    Attributes:
        _sessions: Recorded events per session id, in append order.
    """

    _sessions: dict[str, list[_RecordedEvent]] = field(default_factory=dict)

    def append(self, session_id: str, event: AgentEvent) -> int:
        """Record ``event`` for ``session_id`` and return its assigned sequence number."""
        history = self._sessions.setdefault(session_id, [])
        sequence = len(history) + 1
        history.append(_RecordedEvent(sequence=sequence, event=event))
        return sequence

    def events_after(self, session_id: str, *, sequence: int) -> tuple[AgentEvent, ...]:
        """Return every event recorded for ``session_id`` with a sequence number above ``sequence``."""
        history = self._sessions.get(session_id, [])
        return tuple(recorded.event for recorded in history if recorded.sequence > sequence)

    def last_sequence(self, session_id: str) -> int:
        """Return the highest sequence number recorded for ``session_id``, or ``0`` if none."""
        history = self._sessions.get(session_id, [])
        return history[-1].sequence if history else 0


@dataclass(slots=True)
class RuntimeInvocation:
    """One decoded AgentCore Runtime invocation, ready to drive a supervisor turn.

    Attributes:
        identity: The invoking user's decoded identity and scope.
        session_id: Correlation id for this conversation: the payload's ``session_id`` when
            supplied, else ``identity.session_id``. The resumption key for :class:`SessionStore`.
        prompt: The user's chat message for this turn.
        tuning: The turn's :class:`providers.tier_tuning.TurnTuning` exactly as decoded, before
            any tier resolution; :func:`agents.turn_driver.drive_turn` resolves every tier from it.
        resolved_tiers: Each tier's model and parameters, as :func:`resolve_tuning` validated them.
        page_context: The view the user was looking at when the turn started, or ``None``. A
            descriptor, never rows.
    """

    identity: Identity
    session_id: str
    prompt: str
    tuning: TurnTuning
    resolved_tiers: Mapping[Tier, ResolvedInvocation]
    page_context: PageContext | None = None

    @property
    def resolved_invocation(self) -> ResolvedInvocation:
        """The supervisor tier's resolved invocation."""
        return self.resolved_tiers[SUPERVISOR_TIER]


#: Payload key carrying the page-context descriptor; the relay (``src/relay/src/runtime.ts``)
#: writes it.
PAGE_CONTEXT_KEY: Final[str] = "page_context"


def decode_page_context(payload: dict[str, Any]) -> PageContext | None:
    """Decode the optional page-context descriptor an invocation payload may carry.

    Delegates to :func:`crm.toolspec.page_context.parse_page_context`. An absent key or a ``None``
    value returns ``None``.

    Args:
        payload: The raw invocation body.

    Returns:
        The parsed descriptor, or ``None`` when the payload carries none.

    Raises:
        crm.errors.ValidationFailed: When the descriptor is present but not a valid
            :class:`PageContext`.
    """
    raw = payload.get(PAGE_CONTEXT_KEY)
    if raw is None:
        return None
    return parse_page_context(raw)


def decode_invocation(payload: dict[str, Any], matrix: CapabilityMatrix) -> RuntimeInvocation:
    """Decode and validate one inbound invocation payload before any turn starts.

    Performs, in order: identity decoding, tuning validation, and prompt presence. Any step
    raising means no turn is constructed and no model call is made.

    Args:
        payload: The raw invocation body, carrying ``prompt``, an optional ``session_id``, an
            optional schema-3 ``tuning`` block, an optional :data:`PAGE_CONTEXT_KEY` descriptor, and the
            identity channel :func:`resolve_identity` reads.
        matrix: The loaded capability matrix used to validate ``payload["tuning"]``.

    Returns:
        A :class:`RuntimeInvocation` carrying a decoded identity and a validated invocation.

    Raises:
        crm.errors.AuthenticationFailed: When identity cannot be established.
        crm.errors.ValidationFailed: When the tuning block is not schema 3 or any tier's request is
            invalid for its resolved model, when ``prompt`` is absent, empty, or whitespace only, or when a supplied
            :data:`PAGE_CONTEXT_KEY` descriptor is malformed. Each names the offending source.
    """
    identity = resolve_identity(payload)
    tuning, resolved_tiers = resolve_tuning(matrix, raw_tuning=payload.get("tuning"))
    prompt = str(payload.get("prompt") or "")
    if not prompt.strip():
        raise ValidationFailed(
            "A message is required to start a turn.",
            source=_PROMPT_SOURCE_UI,
            detail="invocation payload carries no non-empty prompt",
        )
    session_id = str(payload.get("session_id") or identity.session_id or "")
    return RuntimeInvocation(
        identity=identity,
        session_id=session_id,
        prompt=prompt,
        tuning=tuning,
        resolved_tiers=resolved_tiers,
        page_context=decode_page_context(payload),
    )


def run_turn(
    invocation: RuntimeInvocation,
    events: Iterator[AgentEvent],
    *,
    store: SessionStore,
    resume_from: int = 0,
) -> Iterator[str]:
    """Drive one turn's already-produced event stream into resumable SSE lines.

    Every event from ``events`` except a ``heartbeat`` is recorded in ``store`` before being
    rendered; a heartbeat is rendered only. When
    ``resume_from`` is below the session's last recorded sequence, only the recorded events after
    it are yielded and ``events`` is not consumed.

    Args:
        invocation: The decoded, tuning-validated invocation driving this turn.
        events: The turn's own event stream, e.g. from repeated calls to
            :func:`agents.supervisor.provider_event_to_agent_event` over a provider's stream.
            Consumed only when ``resume_from`` is at or beyond the last recorded sequence.
        store: Where events are recorded and replayed from.
        resume_from: The highest sequence number the caller already received for this session.
            ``0`` means "from the start" — a fresh turn.

    Yields:
        One SSE data line per event, in :func:`format_sse_line`'s framing.
    """
    last_recorded = store.last_sequence(invocation.session_id)
    if resume_from < last_recorded:
        for replayed in store.events_after(invocation.session_id, sequence=resume_from):
            yield format_sse_line(replayed)
        return

    for event in events:
        if event.kind != "heartbeat":
            store.append(invocation.session_id, event)
        yield format_sse_line(event)


def build_supervisor_turn(
    invocation: RuntimeInvocation,
    provider: ModelProvider,
    matrix: CapabilityMatrix,
    *,
    budget: TurnBudgetSpec = CHAT_BUDGET,
) -> SupervisorTurn:
    """Construct the :class:`agents.supervisor.SupervisorTurn` for one decoded invocation.

    The turn's tuning override pins the family and effort already validated in
    ``invocation.resolved_invocation``.

    Args:
        invocation: The decoded, tuning-validated invocation.
        provider: The model provider seam this turn invokes through.
        matrix: The loaded capability matrix.
        budget: The turn's caps, :data:`agents.budget.CHAT_BUDGET` by default.

    Returns:
        A ready-to-drive :class:`agents.supervisor.SupervisorTurn` scoped to this invocation's
        identity and validated tuning.
    """
    resolved = invocation.resolved_invocation
    tuning_override = Tuning(
        source=_TUNING_SOURCE_UI,
        family=resolved.model.family,
        effort=resolved.effort,
    )
    return SupervisorTurn(
        provider=provider,
        matrix=matrix,
        budget=TurnBudget.from_spec(budget),
        tuning_override=tuning_override,
        page_context=invocation.page_context,
    )


__all__ = [
    "PAGE_CONTEXT_KEY",
    "InMemorySessionStore",
    "RuntimeInvocation",
    "SessionStore",
    "build_supervisor_turn",
    "decode_invocation",
    "event_payload",
    "format_sse_line",
    "resolve_identity",
    "resolve_tuning",
    "run_turn",
]

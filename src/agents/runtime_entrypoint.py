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
   :func:`run_turn` starts each fresh turn with :meth:`SessionStore.begin_turn`, so a session
   holds only its latest turn's events; :class:`InMemorySessionStore` also caps that at
   :data:`MAX_EVENTS_PER_SESSION`, enough for any one chat turn, dropping the oldest first.
4. **SSE framing.** :func:`format_sse_line` renders one :class:`agents.events.AgentEvent` as one
   JSON object on one line.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from agents.budget import (
    CHAT_BUDGET,
    CHAT_MAX_TOOL_CALLS,
    TurnBudget,
    TurnBudgetSpec,
)
from agents.events import AgentEvent
from agents.subagent_runner import DEFAULT_MAX_SUBAGENT_ROUNDS
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

    def begin_turn(self, session_id: str) -> None:
        """Forget the events of ``session_id``'s earlier turns; sequence numbers keep counting."""
        raise NotImplementedError


#: The largest per-round output-token cap of any model in ``config/model_capabilities.yaml``.
MAX_ROUND_OUTPUT_TOKENS: Final[int] = 131_072

#: Events one tool call can record: its ``tool_call``, its ``tool_result`` and a ``proposal``.
_EVENTS_PER_TOOL_CALL: Final[int] = 3

#: The terminal events of one turn: ``usage`` plus ``completed``, or one ``error``.
_TERMINAL_EVENTS_PER_TURN: Final[int] = 2

#: Supervisor output tokens one chat turn's ``text_delta`` retention allows for, before the round
#: that crosses it.
_TEXT_DELTA_TOKEN_ALLOWANCE: Final[int] = 200_000

#: The most ``text_delta`` frames one chat turn can record, at most one per supervisor output token.
_MAX_TEXT_DELTAS_PER_TURN: Final[int] = _TEXT_DELTA_TOKEN_ALLOWANCE + MAX_ROUND_OUTPUT_TOKENS

#: The most tool calls one chat turn can record: the supervisor's, every charged leaf call, and the
#: leaf calls of the one delegation that crosses the budget, at most one per output token.
_MAX_TOOL_CALLS_PER_TURN: Final[int] = (
    2 * CHAT_MAX_TOOL_CALLS + DEFAULT_MAX_SUBAGENT_ROUNDS * MAX_ROUND_OUTPUT_TOKENS
)

#: The most events :class:`InMemorySessionStore` keeps per session within one turn: one whole chat
#: turn's worth.
MAX_EVENTS_PER_SESSION: Final[int] = (
    _MAX_TEXT_DELTAS_PER_TURN
    + _EVENTS_PER_TOOL_CALL * _MAX_TOOL_CALLS_PER_TURN
    + _TERMINAL_EVENTS_PER_TURN
)


@dataclass(slots=True)
class _SessionLog:
    """One session's retained events, oldest first, and the last sequence number it assigned."""

    events: deque[_RecordedEvent]
    last_sequence: int = 0


@dataclass(slots=True)
class InMemorySessionStore:
    """A process-local :class:`SessionStore`, keyed by session id.

    Each session keeps the events appended since its last :meth:`begin_turn`, at most the most
    recent ``max_events`` of them; appending past that drops the oldest. Sequence numbers keep
    counting across drops and turns, so :meth:`events_after` is exact whenever no event
    above the requested sequence has been dropped; otherwise it returns only the retained ones.

    Attributes:
        max_events: Events retained per session; defaults to :data:`MAX_EVENTS_PER_SESSION`.
        _sessions: The retained log per session id.
    """

    max_events: int = MAX_EVENTS_PER_SESSION
    _sessions: dict[str, _SessionLog] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject a non-positive ``max_events``."""
        if isinstance(self.max_events, bool) or self.max_events <= 0:
            raise ValueError(f"max_events must be a positive integer, got {self.max_events!r}")

    def append(self, session_id: str, event: AgentEvent) -> int:
        """Record ``event`` for ``session_id`` and return its assigned sequence number."""
        log = self._sessions.get(session_id)
        if log is None:
            log = _SessionLog(events=deque(maxlen=self.max_events))
            self._sessions[session_id] = log
        log.last_sequence += 1
        log.events.append(_RecordedEvent(sequence=log.last_sequence, event=event))
        return log.last_sequence

    def events_after(self, session_id: str, *, sequence: int) -> tuple[AgentEvent, ...]:
        """Return every retained event for ``session_id`` with a sequence number above ``sequence``."""
        log = self._sessions.get(session_id)
        if log is None:
            return ()
        return tuple(recorded.event for recorded in log.events if recorded.sequence > sequence)

    def last_sequence(self, session_id: str) -> int:
        """Return the highest sequence number assigned for ``session_id``, or ``0`` if none."""
        log = self._sessions.get(session_id)
        return log.last_sequence if log is not None else 0

    def begin_turn(self, session_id: str) -> None:
        """Drop every event retained for ``session_id``, keeping its sequence counter."""
        log = self._sessions.get(session_id)
        if log is not None:
            log.events.clear()


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


#: The longest chat prompt, in characters, a turn accepts. Equal to the relay's and the chat
#: Gateway target's caps.
PROMPT_MAX_CHARS: Final[int] = 8000


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
            invalid for its resolved model, when ``prompt`` is absent, empty, whitespace only, or
            longer than :data:`PROMPT_MAX_CHARS`, or when a supplied :data:`PAGE_CONTEXT_KEY`
            descriptor is malformed. Each names the offending source.
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
    if len(prompt) > PROMPT_MAX_CHARS:
        raise ValidationFailed(
            f"The message is longer than {PROMPT_MAX_CHARS} characters.",
            source=_PROMPT_SOURCE_UI,
            detail=f"prompt length {len(prompt)} exceeds {PROMPT_MAX_CHARS}",
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

    A fresh turn first calls :meth:`SessionStore.begin_turn`, dropping the session's earlier
    turns, then records every event from ``events`` except a ``heartbeat`` in ``store`` before
    rendering it; a heartbeat is rendered only. When
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

    store.begin_turn(invocation.session_id)
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
    "PROMPT_MAX_CHARS",
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

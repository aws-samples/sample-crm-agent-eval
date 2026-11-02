# SPDX-License-Identifier: MIT
"""Server-side, per-session conversation history for the production chat path.

Each prior turn's user prompt and final answer (never tool payloads) is kept per ``session_id`` and
prepended to a follow-up turn's model messages. The store records the owning ``(rep_id, tenant)`` at
write time and refuses a read or write by any other principal with a generic
:class:`crm.errors.ScopeDenied`; an unknown session reads empty. The oldest turns beyond
``max_turns`` are dropped, and each field is redacted with :func:`crm.metering.redact` token-wise
and then capped at ``max_field_chars``. Process-local and lost on restart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, Protocol

from crm.authz import Identity
from crm.errors import ScopeDenied, ValidationFailed
from crm.metering import redact

#: Scope name reported on a refused history read/write; it embeds no session id.
HISTORY_SCOPE: Final[str] = "session:history:own"

#: Generic refusal message, identical for a foreign-owner read and a foreign-owner write.
_DENIAL_MESSAGE: Final[str] = "That conversation's history is not available from this session."

#: Default number of prior turns kept per session; older turns are dropped.
DEFAULT_MAX_TURNS: Final[int] = 10

#: Default per-field character cap, applied after redaction.
DEFAULT_MAX_FIELD_CHARS: Final[int] = 4000

#: Appended to a field truncated at the character cap, so a reader can see it was shortened.
TRUNCATION_SUFFIX: Final[str] = "…[truncated]"

#: Matches each run of non-whitespace; redaction is applied per token and whitespace is preserved.
_NON_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\S+")


@dataclass(frozen=True, slots=True)
class _Owner:
    """The ``(rep_id, tenant)`` pair a session's history read or write is authorized against."""

    rep_id: str
    tenant: str

    @classmethod
    def of(cls, identity: Identity) -> _Owner:
        """Return the owner pair for ``identity``."""
        return cls(rep_id=identity.rep_id, tenant=identity.tenant)


@dataclass(frozen=True, slots=True)
class HistoryTurn:
    """One prior turn's redacted, bounded user prompt and final answer.

    Attributes:
        prompt: The turn's user prompt, redacted and length-capped.
        answer: The turn's synthesized final answer, redacted and length-capped.
    """

    prompt: str
    answer: str


@dataclass(slots=True)
class _SessionHistory:
    """One session's owner and its bounded list of prior turns (oldest first)."""

    owner: _Owner
    turns: list[HistoryTurn] = field(default_factory=list)


class ConversationHistoryStore(Protocol):
    """The server-side conversation-history contract the production turn path reads and writes."""

    def record_turn(self, session_id: str, *, identity: Identity, prompt: str, answer: str) -> None:
        """Record ``(prompt, answer)`` for ``session_id`` on behalf of ``identity``."""
        raise NotImplementedError

    def history_for(self, session_id: str, *, identity: Identity) -> tuple[HistoryTurn, ...]:
        """Return the prior turns for ``session_id`` visible to ``identity`` (oldest first)."""
        raise NotImplementedError

    def clear(self, session_id: str) -> None:
        """Forget ``session_id``'s history entirely."""
        raise NotImplementedError


@dataclass(slots=True)
class InMemoryConversationHistory:
    """A process-local :class:`ConversationHistoryStore` that owns its identity+tenant scope.

    Attributes:
        max_turns: How many prior turns to keep per session before dropping the oldest.
            Must be positive.
        max_field_chars: Per-field character cap applied after redaction. Must be positive.
        _sessions: One :class:`_SessionHistory` per session id.
    """

    max_turns: int = DEFAULT_MAX_TURNS
    max_field_chars: int = DEFAULT_MAX_FIELD_CHARS
    _sessions: dict[str, _SessionHistory] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_turns < 1:
            raise ValueError(f"max_turns must be positive, got {self.max_turns}")
        if self.max_field_chars < 1:
            raise ValueError(f"max_field_chars must be positive, got {self.max_field_chars}")

    def record_turn(self, session_id: str, *, identity: Identity, prompt: str, answer: str) -> None:
        """Record a redacted, bounded ``(prompt, answer)`` for ``session_id``.

        Args:
            session_id: The conversation's session id. Must be non-blank.
            identity: The authenticated caller, whose ``(rep_id, tenant)`` owns the session.
            prompt: The turn's user prompt.
            answer: The turn's synthesized final answer.

        Raises:
            crm.errors.ValidationFailed: ``session_id`` is blank.
            crm.errors.ScopeDenied: ``session_id`` already belongs to a different principal.
        """
        key = _require_session_id(session_id)
        owner = _Owner.of(identity)
        existing = self._sessions.get(key)
        if existing is not None and existing.owner != owner:
            raise _denied("write to a session the caller does not own")
        session = existing if existing is not None else _SessionHistory(owner=owner)
        session.turns.append(
            HistoryTurn(prompt=self._sanitise(prompt), answer=self._sanitise(answer))
        )
        while len(session.turns) > self.max_turns:
            session.turns.pop(0)
        self._sessions[key] = session

    def history_for(self, session_id: str, *, identity: Identity) -> tuple[HistoryTurn, ...]:
        """Return ``session_id``'s prior turns visible to ``identity`` (oldest first).

        An unknown session returns an empty tuple; a session owned by a different
        ``(rep_id, tenant)`` is refused.

        Raises:
            crm.errors.ValidationFailed: ``session_id`` is blank.
            crm.errors.ScopeDenied: ``session_id`` exists but belongs to a different principal.
        """
        key = _require_session_id(session_id)
        owner = _Owner.of(identity)
        session = self._sessions.get(key)
        if session is None:
            return ()
        if session.owner != owner:
            raise _denied("read of a session the caller does not own")
        return tuple(session.turns)

    def clear(self, session_id: str) -> None:
        """Forget ``session_id``'s history. A no-op when it is not present."""
        self._sessions.pop(session_id, None)

    def _sanitise(self, text: str) -> str:
        """Redact secret-shaped tokens, then cap length."""
        redacted = _NON_WHITESPACE.sub(lambda match: str(redact(match.group())), text)
        if len(redacted) > self.max_field_chars:
            return redacted[: self.max_field_chars] + TRUNCATION_SUFFIX
        return redacted


def _denied(detail: str) -> ScopeDenied:
    """Build the generic history :class:`ScopeDenied`; the detail is for logs, never the caller."""
    return ScopeDenied(_DENIAL_MESSAGE, missing_scope=HISTORY_SCOPE, detail=detail)


def _require_session_id(session_id: str) -> str:
    """Return ``session_id`` unchanged, rejecting a blank value (used verbatim as the store key).

    Raises:
        crm.errors.ValidationFailed: ``session_id`` is empty or whitespace only.
    """
    if not session_id.strip():
        raise ValidationFailed(
            "A conversation id is required to read history.",
            source="agent.history.session_id",
            detail="history requested with an empty session_id",
        )
    return session_id


__all__ = [
    "DEFAULT_MAX_FIELD_CHARS",
    "DEFAULT_MAX_TURNS",
    "HISTORY_SCOPE",
    "ConversationHistoryStore",
    "HistoryTurn",
    "InMemoryConversationHistory",
    "TRUNCATION_SUFFIX",
]

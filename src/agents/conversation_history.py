# SPDX-License-Identifier: MIT
"""Server-side, per-session conversation history for the production chat path.

Each prior turn's user prompt and final answer (never tool payloads) is kept per ``session_id`` and
prepended to a follow-up turn's model messages. The store records the owning ``(rep_id, tenant)`` at
write time and refuses a read or write by any other principal with a generic
:class:`crm.errors.ScopeDenied`; an unknown session reads empty. The oldest turns beyond
``max_turns`` are dropped, and each field is redacted with :func:`crm.metering.redact` token-wise
and then capped at ``max_field_chars``.

:class:`InMemoryConversationHistory` is process-local and lost on restart.
:class:`DynamoDbConversationHistory` keeps one ``reps`` item per ``(tenant, session_id)`` through
:class:`crm.store.store.Store`, expiring ``ttl_seconds`` after its last write, and serialises
concurrent writers with an optimistic version check. That check orders turns by when their writes
commit, not by when the turns began: a turn whose write lands after a later turn's is appended
after it, and no turn sequence exists to reorder them.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol

from crm.authz import Identity
from crm.errors import Conflict, CrmError, ScopeDenied, ValidationFailed
from crm.metering import redact
from crm.store.keys import history_key

if TYPE_CHECKING:
    from crm.store.store import Store

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

#: Default lifetime of a durable history item after its last write.
DEFAULT_TTL_SECONDS: Final[int] = 86_400

#: Logical table holding durable history items.
HISTORY_TABLE: Final[str] = "reps"

#: The ``kind`` attribute value of a durable history item.
HISTORY_KIND: Final[str] = "history"

#: Read-modify-write attempts :meth:`DynamoDbConversationHistory.record_turn` makes before
#: raising :class:`HistoryWriteConflict`.
MAX_WRITE_ATTEMPTS: Final[int] = 3

#: Bounds, in seconds, of the jittered pause before each retried history write.
RETRY_BACKOFF_SECONDS: Final[tuple[float, float]] = (0.02, 0.08)

_PARTITION_KEY: Final[str] = "rep_id"

#: Every attribute a durable history item carries; no other attribute is ever written.
HISTORY_ITEM_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        _PARTITION_KEY,
        "kind",
        "owner_rep_id",
        "owner_tenant",
        "turns",
        "version",
        "updated_at",
        "expires_at",
    }
)


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
        """Record ``(prompt, answer)`` for ``session_id`` on behalf of ``identity``.

        Raises:
            crm.errors.ValidationFailed: ``session_id`` is blank.
        """
        raise NotImplementedError

    def history_for(self, session_id: str, *, identity: Identity) -> tuple[HistoryTurn, ...]:
        """Return the prior turns for ``session_id`` visible to ``identity`` (oldest first).

        Raises:
            crm.errors.ValidationFailed: ``session_id`` is blank.
        """
        raise NotImplementedError

    def clear(self, session_id: str, *, identity: Identity) -> None:
        """Forget ``session_id``'s history on behalf of its owner ``identity``.

        Raises:
            crm.errors.ValidationFailed: ``session_id`` is blank.
        """
        raise NotImplementedError


class HistoryWriteConflict(Conflict):
    """Concurrent writers kept winning the race for one session's history item."""


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
        _validate_bounds(self.max_turns, self.max_field_chars)

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
            HistoryTurn(
                prompt=_sanitise(prompt, self.max_field_chars),
                answer=_sanitise(answer, self.max_field_chars),
            )
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

    def clear(self, session_id: str, *, identity: Identity) -> None:
        """Forget ``session_id``'s history. A no-op when it is not present.

        Raises:
            crm.errors.ValidationFailed: ``session_id`` is blank.
            crm.errors.ScopeDenied: ``session_id`` belongs to a different principal.
        """
        key = _require_session_id(session_id)
        session = self._sessions.get(key)
        if session is not None and session.owner != _Owner.of(identity):
            raise _denied("clear of a session the caller does not own")
        self._sessions.pop(key, None)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DynamoDbConversationHistory:
    """A durable :class:`ConversationHistoryStore` on the ``reps`` table, one item per session.

    The item key embeds the caller's tenant, so another tenant's session reads empty. An item past
    its ``expires_at`` reads empty, whether or not a TTL has deleted it yet, but only its owner may
    write or clear it until it is deleted. Every read is strongly
    consistent. Item attributes are exactly :data:`HISTORY_ITEM_ATTRIBUTES`; there is no
    ``tenant``, so no index lists the item.

    Args:
        store: The store every read and write goes through.
        max_turns: Prior turns kept per session before dropping the oldest. Must be positive.
        max_field_chars: Per-field character cap applied after redaction. Must be positive.
        ttl_seconds: Item lifetime after its last write, in seconds. Must be positive.
        clock: Returns the current time as a timezone-aware datetime.
        sleep: Pauses between retried writes, taking seconds.
    """

    def __init__(
        self,
        store: Store,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        _validate_bounds(max_turns, max_field_chars)
        if ttl_seconds < 1:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        self._store = store
        self.max_turns = max_turns
        self.max_field_chars = max_field_chars
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._sleep = sleep

    def record_turn(self, session_id: str, *, identity: Identity, prompt: str, answer: str) -> None:
        """Append a redacted, bounded ``(prompt, answer)`` to ``session_id``'s item.

        An expired item still belongs to its owner until it is deleted; the owner's write replaces
        its turns with the new one.

        Raises:
            crm.errors.ValidationFailed: ``session_id`` is blank.
            crm.errors.ScopeDenied: ``session_id``'s item, expired or not, belongs to a different
                principal.
            HistoryWriteConflict: Every one of :data:`MAX_WRITE_ATTEMPTS` writes lost a race.
            crm.errors.CrmError: The store failed, or the stored item is malformed.
        """
        key = self._key(session_id, identity)
        owner = _Owner.of(identity)
        new_turn = {
            "prompt": _sanitise(prompt, self.max_field_chars),
            "answer": _sanitise(answer, self.max_field_chars),
        }
        for attempt in range(MAX_WRITE_ATTEMPTS):
            if attempt:
                self._sleep(random.uniform(*RETRY_BACKOFF_SECONDS))  # noqa: S311
            now = self._clock()
            item = self._store.get_item(HISTORY_TABLE, {_PARTITION_KEY: key}, consistent=True)
            expected_version = None if item is None else _version_of(item)
            prior: list[dict[str, str]] = []
            if item is not None:
                if _owner_of(item) != owner:
                    raise _denied("write to a session the caller does not own")
                if not _expired(item, now):
                    prior = [
                        {"prompt": turn.prompt, "answer": turn.answer} for turn in _turns_of(item)
                    ]
            turns = [*prior, new_turn][-self.max_turns :]
            written = self._store.put_conditional(
                HISTORY_TABLE,
                {
                    _PARTITION_KEY: key,
                    "kind": HISTORY_KIND,
                    "owner_rep_id": owner.rep_id,
                    "owner_tenant": owner.tenant,
                    "turns": turns,
                    "version": 1 if expected_version is None else expected_version + 1,
                    "updated_at": now.isoformat(),
                    "expires_at": int(now.timestamp()) + self.ttl_seconds,
                },
                expected_version=expected_version,
            )
            if written:
                return
        raise HistoryWriteConflict(
            "This conversation was updated concurrently; the turn was not added to its history.",
            detail=f"history write lost {MAX_WRITE_ATTEMPTS} optimistic-version races",
        )

    def history_for(self, session_id: str, *, identity: Identity) -> tuple[HistoryTurn, ...]:
        """Return ``session_id``'s prior turns visible to ``identity`` (oldest first).

        A missing or expired item returns an empty tuple.

        Raises:
            crm.errors.ValidationFailed: ``session_id`` is blank.
            crm.errors.ScopeDenied: The item belongs to a different principal.
            crm.errors.CrmError: The store failed, or the stored item is malformed.
        """
        key = self._key(session_id, identity)
        item = self._store.get_item(HISTORY_TABLE, {_PARTITION_KEY: key}, consistent=True)
        if item is None or _expired(item, self._clock()):
            return ()
        if _owner_of(item) != _Owner.of(identity):
            raise _denied("read of a session the caller does not own")
        return _turns_of(item)

    def clear(self, session_id: str, *, identity: Identity) -> None:
        """Delete ``session_id``'s item, expired or not. A no-op when it is missing.

        Raises:
            crm.errors.ValidationFailed: ``session_id`` is blank.
            crm.errors.ScopeDenied: The item, expired or not, belongs to a different principal.
            crm.errors.CrmError: The store failed, or the stored item is malformed.
        """
        key = self._key(session_id, identity)
        item = self._store.get_item(HISTORY_TABLE, {_PARTITION_KEY: key}, consistent=True)
        if item is None:
            return
        if _owner_of(item) != _Owner.of(identity):
            raise _denied("clear of a session the caller does not own")
        self._store.delete_item(HISTORY_TABLE, {_PARTITION_KEY: key})

    @staticmethod
    def _key(session_id: str, identity: Identity) -> str:
        return history_key(identity.tenant, _require_session_id(session_id))


def _validate_bounds(max_turns: int, max_field_chars: int) -> None:
    if max_turns < 1:
        raise ValueError(f"max_turns must be positive, got {max_turns}")
    if max_field_chars < 1:
        raise ValueError(f"max_field_chars must be positive, got {max_field_chars}")


def _sanitise(text: str, max_field_chars: int) -> str:
    """Redact secret-shaped tokens, then cap length."""
    redacted = _NON_WHITESPACE.sub(lambda match: str(redact(match.group())), text)
    if len(redacted) > max_field_chars:
        return redacted[:max_field_chars] + TRUNCATION_SUFFIX
    return redacted


def _malformed(detail: str) -> CrmError:
    return CrmError("Conversation history could not be read.", detail=f"history item: {detail}")


def _owner_of(item: Mapping[str, object]) -> _Owner:
    rep_id, tenant = item.get("owner_rep_id"), item.get("owner_tenant")
    if not isinstance(rep_id, str) or not isinstance(tenant, str):
        raise _malformed("owner attributes are not strings")
    return _Owner(rep_id=rep_id, tenant=tenant)


def _version_of(item: Mapping[str, object]) -> int:
    version = item.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _malformed("version is not an integer")
    return version


def _expired(item: Mapping[str, object], now: datetime) -> bool:
    expires_at = item.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        raise _malformed("expires_at is not an integer")
    return expires_at <= now.timestamp()


def _turns_of(item: Mapping[str, object]) -> tuple[HistoryTurn, ...]:
    raw = item.get("turns")
    if not isinstance(raw, list):
        raise _malformed("turns is not a list")
    turns: list[HistoryTurn] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise _malformed("a turn is not a map")
        prompt, answer = entry.get("prompt"), entry.get("answer")
        if not isinstance(prompt, str) or not isinstance(answer, str):
            raise _malformed("a turn's prompt or answer is not a string")
        turns.append(HistoryTurn(prompt=prompt, answer=answer))
    return tuple(turns)


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
            "A conversation id is required for conversation history.",
            source="agent.history.session_id",
            detail="history requested with an empty session_id",
        )
    return session_id


__all__ = [
    "DEFAULT_MAX_FIELD_CHARS",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_TTL_SECONDS",
    "HISTORY_ITEM_ATTRIBUTES",
    "HISTORY_KIND",
    "HISTORY_SCOPE",
    "HISTORY_TABLE",
    "MAX_WRITE_ATTEMPTS",
    "RETRY_BACKOFF_SECONDS",
    "ConversationHistoryStore",
    "DynamoDbConversationHistory",
    "HistoryTurn",
    "HistoryWriteConflict",
    "InMemoryConversationHistory",
    "TRUNCATION_SUFFIX",
]

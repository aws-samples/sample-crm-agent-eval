# SPDX-License-Identifier: MIT
"""Cooperative cancellation markers, keyed by session id.

A turn is cancelled by a marker the agent loop reads between steps, written through the ``cancel``
mode of :mod:`agents.runtime_server`'s ``POST /invocations`` and consulted by
:class:`agents.supervisor.TurnGuard`. A cancellation is accepted only from the owner the turn was
registered with; an unknown session id and a wrong owner are refused with the same
:class:`crm.errors.ScopeDenied`. A cancel after a terminal event is ``already_terminal`` and emits
nothing; a tool call already dispatched completes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from crm.authz import Identity
from crm.errors import ScopeDenied, ValidationFailed

#: Scope name reported on a refused cancellation; it does not embed the session id.
CANCEL_SCOPE: Final[str] = "session:own"

#: User-safe message for every refused cancellation, unknown session and wrong owner alike.
_DENIAL_MESSAGE: Final[str] = "That conversation cannot be cancelled from this session."

#: What a cancellation request did. ``accepted`` wrote a new marker; ``already_requested`` found one
#: already present; ``already_terminal`` found the turn already finished and wrote nothing.
CancelDisposition = Literal["accepted", "already_requested", "already_terminal"]


@dataclass(frozen=True, slots=True)
class SessionOwner:
    """The principal a session belongs to, as the pair a cancellation is authorized against.

    Attributes:
        rep_id: The owning rep, from the identity that started the turn.
        tenant: The owning tenant; the same rep id in another tenant is a different owner.
    """

    rep_id: str
    tenant: str

    @classmethod
    def of(cls, identity: Identity) -> SessionOwner:
        """Return the owner pair for ``identity``.

        Args:
            identity: An identity :func:`crm.envelope.decode_identity` produced.
        """
        return cls(rep_id=identity.rep_id, tenant=identity.tenant)


@dataclass(slots=True)
class _SessionMarker:
    """One session's cancellation state.

    Attributes:
        owner: The principal permitted to cancel this session.
        cancel_requested: Whether a cancellation has been accepted for this session.
        terminal: Whether the turn already emitted a terminal event, after which a cancellation is
            a no-op.
    """

    owner: SessionOwner
    cancel_requested: bool = False
    terminal: bool = False


class CancellationStore(Protocol):
    """The cooperative-cancellation contract the agent loop reads between steps."""

    def register_turn(self, session_id: str, *, owner: SessionOwner) -> None:
        """Bind ``session_id`` to ``owner`` for the duration of a turn."""
        raise NotImplementedError

    def request_cancellation(
        self, session_id: str, *, requester: SessionOwner
    ) -> CancelDisposition:
        """Record a cancellation for ``session_id`` on behalf of ``requester``."""
        raise NotImplementedError

    def is_cancelled(self, session_id: str) -> bool:
        """Return whether a cancellation is pending for ``session_id``."""
        raise NotImplementedError

    def mark_terminal(self, session_id: str) -> None:
        """Record that ``session_id``'s turn reached a terminal event."""
        raise NotImplementedError

    def clear(self, session_id: str) -> None:
        """Forget ``session_id`` entirely."""
        raise NotImplementedError


@dataclass(slots=True)
class InMemoryCancellationStore:
    """A process-local :class:`CancellationStore`.

    Registering the same owner twice, requesting a cancellation twice, marking terminal twice, and
    clearing an unknown session are all idempotent no-ops.

    Attributes:
        _markers: One :class:`_SessionMarker` per registered session id.
    """

    _markers: dict[str, _SessionMarker] = field(default_factory=dict)

    def register_turn(self, session_id: str, *, owner: SessionOwner) -> None:
        """Bind ``session_id`` to ``owner`` for a turn, resetting any finished turn's state.

        Called again during a turn it is a no-op, preserving a requested cancellation; called for a
        session whose previous turn is terminal it starts a fresh marker.

        Args:
            session_id: The turn's session id. Must be non-blank.
            owner: The principal permitted to cancel this session.

        Raises:
            crm.errors.ValidationFailed: ``session_id`` is blank.
            crm.errors.ScopeDenied: ``session_id`` is already bound to a different owner.
        """
        key = _require_session_id(session_id)
        existing = self._markers.get(key)
        if existing is None:
            self._markers[key] = _SessionMarker(owner=owner)
            return
        if existing.owner != owner:
            raise ScopeDenied(
                _DENIAL_MESSAGE,
                missing_scope=CANCEL_SCOPE,
                detail="session id already bound to a different owner",
            )
        if existing.terminal:
            self._markers[key] = _SessionMarker(owner=owner)

    def request_cancellation(
        self, session_id: str, *, requester: SessionOwner
    ) -> CancelDisposition:
        """Record a cancellation for ``session_id``, if ``requester`` owns it.

        Args:
            session_id: The session to cancel.
            requester: The authenticated caller asking for the cancellation.

        Returns:
            ``accepted`` on the first successful request, ``already_requested`` on a repeat, and
            ``already_terminal`` when the turn had already finished, in which case no marker is
            written.

        Raises:
            crm.errors.ValidationFailed: ``session_id`` is empty.
            crm.errors.ScopeDenied: ``session_id`` is unregistered or owned by another principal;
                both cases raise the identical error.
        """
        key = _require_session_id(session_id)
        marker = self._markers.get(key)
        if marker is None or marker.owner != requester:
            raise ScopeDenied(
                _DENIAL_MESSAGE,
                missing_scope=CANCEL_SCOPE,
                detail="cancellation requested for a session the caller does not own",
            )
        if marker.terminal:
            return "already_terminal"
        if marker.cancel_requested:
            return "already_requested"
        marker.cancel_requested = True
        return "accepted"

    def is_cancelled(self, session_id: str) -> bool:
        """Return whether a cancellation is pending for ``session_id``.

        An unregistered or cleared session reports ``False``.
        """
        marker = self._markers.get(session_id)
        return marker is not None and marker.cancel_requested

    def mark_terminal(self, session_id: str) -> None:
        """Record that ``session_id``'s turn reached a terminal event.

        A no-op for an unregistered session.
        """
        marker = self._markers.get(session_id)
        if marker is not None:
            marker.terminal = True

    def clear(self, session_id: str) -> None:
        """Forget ``session_id``. A no-op when it is not registered."""
        self._markers.pop(session_id, None)


def _require_session_id(session_id: str) -> str:
    """Return ``session_id`` unchanged (not normalised), rejecting a blank value.

    Raises:
        crm.errors.ValidationFailed: ``session_id`` is empty or whitespace only.
    """
    if not session_id.strip():
        raise ValidationFailed(
            "A conversation id is required to cancel a turn.",
            source="agent.cancel.session_id",
            detail="cancellation requested with an empty session_id",
        )
    return session_id


__all__ = [
    "CANCEL_SCOPE",
    "CancelDisposition",
    "CancellationStore",
    "InMemoryCancellationStore",
    "SessionOwner",
]

# SPDX-License-Identifier: MIT
"""Conversation ownership for the MCP chat surface: one ``reps`` item per conversation.

A conversation id is server-generated (:func:`new_conversation_id`) and doubles as the AgentCore
Runtime session id. :func:`claim_or_verify` creates the ownership item for a new conversation with
a conditional put, or checks that an existing one belongs to the caller. The item carries no
``tenant`` attribute, so it never appears in ``reps.gsi_tenant``.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Final

from crm.authz import Identity
from crm.errors import Conflict, NotFound, ScopeDenied, ValidationFailed
from crm.store import keys
from crm.store.store import Store

CONVERSATIONS_TABLE: Final[str] = "reps"
CONVERSATION_KIND: Final[str] = "conversation"

#: A conversation id: ``conv-`` and 32 lowercase hex digits (37 characters, a valid
#: ``runtimeSessionId``).
CONVERSATION_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^conv-[0-9a-f]{32}$")

_CREATE_ATTEMPTS: Final[int] = 3


def new_conversation_id() -> str:
    """Return a fresh conversation id matching :data:`CONVERSATION_ID_PATTERN`."""
    return f"conv-{uuid.uuid4().hex}"


def _key(conversation_id: str) -> dict[str, str]:
    return {"rep_id": keys.conversation_key(conversation_id)}


def _owned_by(item: dict[str, object], identity: Identity) -> bool:
    return (
        item.get("kind") == CONVERSATION_KIND
        and item.get("owner_principal_type") == identity.principal_type
        and item.get("owner_rep_id") == identity.rep_id
        and item.get("owner_tenant") == identity.tenant
    )


def _create(store: Store, identity: Identity) -> str:
    for _ in range(_CREATE_ATTEMPTS):
        conversation_id = new_conversation_id()
        created = store.put_if_absent(
            CONVERSATIONS_TABLE,
            {
                **_key(conversation_id),
                "kind": CONVERSATION_KIND,
                "owner_principal_type": identity.principal_type,
                "owner_rep_id": identity.rep_id,
                "owner_tenant": identity.tenant,
                "created_at": datetime.now(UTC).isoformat(),
                "runtime_session_id": conversation_id,
            },
        )
        if created:
            return conversation_id
    raise Conflict(
        "A new conversation could not be started. Try again.",
        detail=f"{_CREATE_ATTEMPTS} generated conversation ids already existed",
    )


def claim_or_verify(store: Store, conversation_id: str | None, identity: Identity) -> str:
    """Return the conversation id the caller may continue, creating one when none is given.

    Args:
        store: The persistence layer.
        conversation_id: An id from an earlier reply, or ``None`` to start a new conversation.
        identity: The authenticated caller; the owner is its principal type, rep id and tenant.

    Returns:
        ``conversation_id`` when it exists and the caller owns it, or a newly created id.

    Raises:
        ValidationFailed: ``conversation_id`` does not match :data:`CONVERSATION_ID_PATTERN`.
        NotFound: No conversation exists with that id.
        ScopeDenied: The conversation belongs to another principal
            (``missing_scope="conversation:owner"``).
        Conflict: A new id could not be claimed after repeated collisions.
    """
    if conversation_id is None:
        return _create(store, identity)
    if not CONVERSATION_ID_PATTERN.fullmatch(conversation_id):
        raise ValidationFailed(
            "conversation_id is not a conversation id this service issued.",
            source="conversation_id",
        )
    item = store.get_item(CONVERSATIONS_TABLE, _key(conversation_id))
    if item is None:
        raise NotFound("That conversation has expired or is unknown. Start a new conversation.")
    if not _owned_by(item, identity):
        raise ScopeDenied(
            "That conversation belongs to someone else.",
            missing_scope="conversation:owner",
            detail=(
                f"conversation={conversation_id} principal_type={identity.principal_type} "
                f"{identity.scope_description()}"
            ),
        )
    return conversation_id


__all__ = [
    "CONVERSATIONS_TABLE",
    "CONVERSATION_ID_PATTERN",
    "CONVERSATION_KIND",
    "claim_or_verify",
    "new_conversation_id",
]

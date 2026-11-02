# SPDX-License-Identifier: MIT
"""Conversation ownership against DynamoDB Local: conditional create, sparse item, owner-only reuse.

The real engine evaluates the create condition, so two racing claims of one id produce
exactly one owner, and `reps.gsi_tenant` never lists a conversation item.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from crm.authz import Identity
from crm.conversations import CONVERSATIONS_TABLE, claim_or_verify, new_conversation_id
from crm.envelope import IDENTITY_KEY, decode_identity
from crm.errors import NotFound, ScopeDenied
from crm.store.store import Store

_RACERS = 8


def _gateway(rep_id: str, *, tenant: str = "demo") -> Identity:
    payload: dict[str, Any] = {
        "rep_id": rep_id,
        "book": ["A-1"],
        "principal_type": "user",
        "source": "gateway",
        "tenant": tenant,
    }
    return decode_identity({IDENTITY_KEY: payload})


def test_conversation_item_is_absent_from_gsi_tenant(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    store, _ = dynamodb_local_store
    store.put_item("reps", {"rep_id": "rep-a", "tenant": "demo", "created_at": "2026-10-07"})
    conversation_id = claim_or_verify(store, None, _gateway("rep-a"))
    assert store.get_item(CONVERSATIONS_TABLE, {"rep_id": f"conversation#{conversation_id}"})
    items, _ = store.query_gsi("reps", "gsi_tenant", partition_value="demo")
    assert [item["rep_id"] for item in items] == ["rep-a"]


def test_owner_reuse_and_foreign_denial_on_the_real_engine(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    store, _ = dynamodb_local_store
    conversation_id = claim_or_verify(store, None, _gateway("rep-a"))
    assert claim_or_verify(store, conversation_id, _gateway("rep-a")) == conversation_id
    with pytest.raises(ScopeDenied):
        claim_or_verify(store, conversation_id, _gateway("rep-b"))
    with pytest.raises(ScopeDenied):
        claim_or_verify(store, conversation_id, _gateway("rep-a", tenant="eval-x"))
    with pytest.raises(NotFound):
        claim_or_verify(store, new_conversation_id(), _gateway("rep-a"))


def test_racing_creates_of_one_id_yield_exactly_one_owner(
    dynamodb_local_store: tuple[Store, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With every racer drawing the same id, the engine's condition admits one creator only."""
    from crm import conversations

    store, _ = dynamodb_local_store
    contested = new_conversation_id()
    monkeypatch.setattr(conversations, "new_conversation_id", lambda: contested)

    def claim(index: int) -> str:
        try:
            return claim_or_verify(store, None, _gateway(f"rep-{index}"))
        except Exception as exc:  # noqa: BLE001 - each loser's error type is the observable.
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=_RACERS) as pool:
        outcomes = list(pool.map(claim, range(_RACERS)))

    assert outcomes.count(contested) == 1
    assert outcomes.count("Conflict") == _RACERS - 1
    winner = outcomes.index(contested)
    item = store.get_item(CONVERSATIONS_TABLE, {"rep_id": f"conversation#{contested}"})
    assert item is not None and item["owner_rep_id"] == f"rep-{winner}"

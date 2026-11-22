# SPDX-License-Identifier: MIT
"""The DynamoDB conversation-history store against DynamoDB Local.

The real engine evaluates the optimistic-version condition, enforces the 400 KB item limit and
maintains `reps.gsi_tenant`, so these tests prove the round trip, the expiry bound, tenant and owner
isolation, concurrent writers, index absence and the size bound at the caps.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agents.conversation_history import (
    DEFAULT_MAX_FIELD_CHARS,
    DEFAULT_MAX_TURNS,
    DEFAULT_TTL_SECONDS,
    HISTORY_TABLE,
    TRUNCATION_SUFFIX,
    DynamoDbConversationHistory,
    HistoryTurn,
)
from crm.authz import Identity
from crm.errors import ScopeDenied
from crm.store import keys
from crm.store.store import Store

_SESSION = "session-ddb-1"

#: DynamoDB's maximum item size.
_ITEM_SIZE_LIMIT_BYTES = 400 * 1024


def _identity(*, rep_id: str = "rep-1", tenant: str = "demo") -> Identity:
    return Identity(
        rep_id=rep_id,
        book=frozenset({"acc-1"}),
        full_visibility=False,
        principal_type="user",
        session_id=_SESSION,
        source="agent",
        tenant=tenant,
    )


def _raw_item(client: Any, prefix: str, tenant: str = "demo") -> dict[str, Any]:
    response = client.get_item(
        TableName=f"{prefix}{HISTORY_TABLE}",
        Key={"rep_id": {"S": keys.history_key(tenant, _SESSION)}},
        ConsistentRead=True,
    )
    item: dict[str, Any] = response["Item"]
    return item


def _encoded_size(value: Any) -> int:
    """Upper-bound estimate of an encoded attribute value's stored size, in bytes."""
    if "S" in value:
        return len(value["S"].encode("utf-8"))
    if "N" in value:
        return len(value["N"]) + 1
    if "M" in value:
        return 3 + sum(len(k.encode("utf-8")) + 1 + _encoded_size(v) for k, v in value["M"].items())
    if "L" in value:
        return 3 + sum(1 + _encoded_size(v) for v in value["L"])
    raise AssertionError(f"unexpected attribute shape {sorted(value)}")


def test_round_trip_keeps_turns_in_order(dynamodb_local_store: tuple[Store, str]) -> None:
    store, _ = dynamodb_local_store
    history = DynamoDbConversationHistory(store)
    history.record_turn(_SESSION, identity=_identity(), prompt="first", answer="one")
    history.record_turn(_SESSION, identity=_identity(), prompt="second", answer="two")
    assert history.history_for(_SESSION, identity=_identity()) == (
        HistoryTurn(prompt="first", answer="one"),
        HistoryTurn(prompt="second", answer="two"),
    )


def test_expires_at_is_bounded_by_the_ttl(
    dynamodb_local_store: tuple[Store, str], dynamodb_local_client: Any
) -> None:
    store, prefix = dynamodb_local_store
    before = int(datetime.now(UTC).timestamp())
    DynamoDbConversationHistory(store).record_turn(
        _SESSION, identity=_identity(), prompt="p", answer="a"
    )
    after = int(datetime.now(UTC).timestamp())
    expires_at = int(_raw_item(dynamodb_local_client, prefix)["expires_at"]["N"])
    assert before + DEFAULT_TTL_SECONDS <= expires_at <= after + DEFAULT_TTL_SECONDS


def test_an_item_past_expiry_reads_empty_before_ttl_deletion(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    store, _ = dynamodb_local_store
    now = [datetime.now(UTC)]
    history = DynamoDbConversationHistory(store, ttl_seconds=60, clock=lambda: now[0])
    history.record_turn(_SESSION, identity=_identity(), prompt="p", answer="a")
    now[0] += timedelta(seconds=61)
    assert store.get_item(HISTORY_TABLE, {"rep_id": keys.history_key("demo", _SESSION)})
    assert history.history_for(_SESSION, identity=_identity()) == ()


def test_a_foreign_tenant_reads_empty(dynamodb_local_store: tuple[Store, str]) -> None:
    store, _ = dynamodb_local_store
    history = DynamoDbConversationHistory(store)
    history.record_turn(_SESSION, identity=_identity(), prompt="secret", answer="a")
    assert history.history_for(_SESSION, identity=_identity(tenant="eval-run-1")) == ()


def test_a_foreign_rep_is_refused_on_read_and_write(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    store, _ = dynamodb_local_store
    history = DynamoDbConversationHistory(store)
    history.record_turn(_SESSION, identity=_identity(), prompt="p", answer="a")
    foreign = _identity(rep_id="rep-attacker")
    with pytest.raises(ScopeDenied):
        history.history_for(_SESSION, identity=foreign)
    with pytest.raises(ScopeDenied):
        history.record_turn(_SESSION, identity=foreign, prompt="inject", answer="x")
    assert history.history_for(_SESSION, identity=_identity()) == (
        HistoryTurn(prompt="p", answer="a"),
    )


def test_an_expired_undeleted_item_is_neither_seizable_nor_left_behind_by_clear(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    """Past expiry and before TTL deletion: a stranger's write and clear are refused, the owner's
    clear deletes the item."""
    store, _ = dynamodb_local_store
    now = [datetime.now(UTC)]
    history = DynamoDbConversationHistory(store, ttl_seconds=60, clock=lambda: now[0])
    history.record_turn(_SESSION, identity=_identity(), prompt="p", answer="a")
    now[0] += timedelta(seconds=61)
    key = {"rep_id": keys.history_key("demo", _SESSION)}
    stranger = _identity(rep_id="rep-attacker")

    with pytest.raises(ScopeDenied):
        history.record_turn(_SESSION, identity=stranger, prompt="seize", answer="x")
    with pytest.raises(ScopeDenied):
        history.clear(_SESSION, identity=stranger)
    item = store.get_item(HISTORY_TABLE, key)
    assert item is not None and item["owner_rep_id"] == "rep-1"

    history.clear(_SESSION, identity=_identity())
    assert store.get_item(HISTORY_TABLE, key) is None


class _BarrierStore(Store):
    """A `Store` whose first conditional write per thread waits until both writers have read."""

    def __init__(self, inner: Store, barrier: threading.Barrier) -> None:
        self.__dict__.update(inner.__dict__)
        self._barrier = barrier
        self._waited = threading.local()

    def put_conditional(
        self, table: str, item: Mapping[str, object], *, expected_version: int | None
    ) -> bool:
        if not getattr(self._waited, "done", False):
            self._waited.done = True
            self._barrier.wait(timeout=10)
        return super().put_conditional(table, item, expected_version=expected_version)


def test_two_concurrent_writers_both_land(dynamodb_local_store: tuple[Store, str]) -> None:
    """Both writers read the same version before either writes; the loser retries and lands."""
    store, _ = dynamodb_local_store
    history = DynamoDbConversationHistory(store)
    history.record_turn(_SESSION, identity=_identity(), prompt="seed", answer="s")
    racing = DynamoDbConversationHistory(_BarrierStore(store, threading.Barrier(2)))
    errors: list[Exception] = []

    def write(prompt: str) -> None:
        try:
            racing.record_turn(_SESSION, identity=_identity(), prompt=prompt, answer="x")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(name,)) for name in ("left", "right")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    prompts = [turn.prompt for turn in history.history_for(_SESSION, identity=_identity())]
    assert prompts[0] == "seed"
    assert sorted(prompts[1:]) == ["left", "right"]
    item = store.get_item(HISTORY_TABLE, {"rep_id": keys.history_key("demo", _SESSION)})
    assert item is not None and item["version"] == 3


def test_history_items_are_absent_from_gsi_tenant(dynamodb_local_store: tuple[Store, str]) -> None:
    store, _ = dynamodb_local_store
    store.put_item("reps", {"rep_id": "rep-1", "tenant": "demo", "created_at": "2026-11-15"})
    DynamoDbConversationHistory(store).record_turn(
        _SESSION, identity=_identity(), prompt="p", answer="a"
    )
    items, _ = store.query_gsi("reps", "gsi_tenant", partition_value="demo")
    assert [item["rep_id"] for item in items] == ["rep-1"]


def test_an_item_at_the_caps_is_well_under_the_item_size_limit(
    dynamodb_local_store: tuple[Store, str], dynamodb_local_client: Any
) -> None:
    store, prefix = dynamodb_local_store
    history = DynamoDbConversationHistory(store)
    oversized = ("wörd " * DEFAULT_MAX_FIELD_CHARS)[: DEFAULT_MAX_FIELD_CHARS * 2]
    for _ in range(DEFAULT_MAX_TURNS + 2):
        history.record_turn(_SESSION, identity=_identity(), prompt=oversized, answer=oversized)
    turns = history.history_for(_SESSION, identity=_identity())
    assert len(turns) == DEFAULT_MAX_TURNS
    capped = oversized[:DEFAULT_MAX_FIELD_CHARS] + TRUNCATION_SUFFIX
    assert all(turn.prompt == capped and turn.answer == capped for turn in turns)
    item = _raw_item(dynamodb_local_client, prefix)
    assert set(item) == {
        "rep_id",
        "kind",
        "owner_rep_id",
        "owner_tenant",
        "turns",
        "version",
        "updated_at",
        "expires_at",
    }
    size = sum(len(name.encode("utf-8")) + _encoded_size(value) for name, value in item.items())
    assert size < _ITEM_SIZE_LIMIT_BYTES / 4

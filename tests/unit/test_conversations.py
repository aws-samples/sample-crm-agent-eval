# SPDX-License-Identifier: MIT
"""Conversation ownership: server-issued ids, conditional create, owner-only reuse.

Runs against a `moto`-mocked DynamoDB client. Identities are decoded from Gateway `_identity`
payloads, the only channel that reaches the chat target.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm import conversations
from crm.authz import Identity
from crm.conversations import (
    CONVERSATION_ID_PATTERN,
    CONVERSATION_KIND,
    CONVERSATIONS_TABLE,
    claim_or_verify,
    new_conversation_id,
)
from crm.envelope import IDENTITY_KEY, decode_identity
from crm.errors import Conflict, NotFound, ScopeDenied, ValidationFailed
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def store() -> Iterator[Store]:
    """A Store backed by a moto-mocked client, with every declared table created."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _gateway(rep_id: str, *, tenant: str = "demo", principal_type: str = "user") -> Identity:
    payload: dict[str, Any] = {
        "rep_id": rep_id,
        "book": ["A-1"],
        "full_visibility": False,
        "principal_type": principal_type,
        "source": "gateway",
        "tenant": tenant,
    }
    return decode_identity({IDENTITY_KEY: payload})


def test_new_ids_match_the_pattern_and_are_valid_runtime_session_ids() -> None:
    """`conv-` + 32 hex: 37 characters, above the Runtime's 33-character minimum."""
    ids = {new_conversation_id() for _ in range(50)}
    assert len(ids) == 50
    for conversation_id in ids:
        assert CONVERSATION_ID_PATTERN.fullmatch(conversation_id)
        assert len(conversation_id) >= 33


def test_none_creates_an_owned_item_with_no_tenant_attribute(store: Store) -> None:
    """A new conversation is recorded with its owner triple and stays out of `gsi_tenant`."""
    identity = _gateway("rep-a")
    conversation_id = claim_or_verify(store, None, identity)
    item = store.get_item(CONVERSATIONS_TABLE, {"rep_id": f"conversation#{conversation_id}"})
    assert item is not None
    assert "tenant" not in item
    assert item["kind"] == CONVERSATION_KIND
    assert item["owner_principal_type"] == "user"
    assert item["owner_rep_id"] == "rep-a"
    assert item["owner_tenant"] == "demo"
    assert item["runtime_session_id"] == conversation_id
    assert isinstance(item["created_at"], str)


def test_owner_may_continue_its_conversation(store: Store) -> None:
    identity = _gateway("rep-a")
    conversation_id = claim_or_verify(store, None, identity)
    assert claim_or_verify(store, conversation_id, _gateway("rep-a")) == conversation_id


@pytest.mark.parametrize(
    "intruder",
    [
        {"rep_id": "rep-b"},
        {"rep_id": "rep-a", "tenant": "eval-other"},
        {"rep_id": "rep-a", "principal_type": "service"},
    ],
    ids=["other-rep", "same-rep-other-tenant", "same-rep-as-service"],
)
def test_any_owner_field_mismatch_is_denied(store: Store, intruder: dict[str, str]) -> None:
    """Owner equality is the full (principal type, rep id, tenant) triple."""
    conversation_id = claim_or_verify(store, None, _gateway("rep-a"))
    rep_id = intruder.pop("rep_id")
    with pytest.raises(ScopeDenied) as denied:
        claim_or_verify(store, conversation_id, _gateway(rep_id, **intruder))
    assert denied.value.missing_scope == "conversation:owner"


def test_unknown_conversation_id_is_not_found_and_not_claimed(store: Store) -> None:
    """A well-formed id this service never issued is refused, never adopted."""
    unknown = new_conversation_id()
    with pytest.raises(NotFound):
        claim_or_verify(store, unknown, _gateway("rep-a"))
    assert store.get_item(CONVERSATIONS_TABLE, {"rep_id": f"conversation#{unknown}"}) is None


@pytest.mark.parametrize(
    "bad",
    ["", "conv-", "conv-XYZ", "conv-" + "0" * 31, "conv-" + "0" * 33, "rep-a", "conv-" + "A" * 32],
)
def test_malformed_conversation_id_is_a_validation_failure(store: Store, bad: str) -> None:
    with pytest.raises(ValidationFailed) as excinfo:
        claim_or_verify(store, bad, _gateway("rep-a"))
    assert excinfo.value.source == "conversation_id"


def test_an_item_of_another_kind_at_the_key_is_denied(store: Store) -> None:
    """Only a `kind="conversation"` item proves ownership."""
    conversation_id = new_conversation_id()
    store.put_item(
        CONVERSATIONS_TABLE,
        {
            "rep_id": f"conversation#{conversation_id}",
            "owner_principal_type": "user",
            "owner_rep_id": "rep-a",
            "owner_tenant": "demo",
        },
    )
    with pytest.raises(ScopeDenied):
        claim_or_verify(store, conversation_id, _gateway("rep-a"))


def test_create_retries_a_colliding_id_then_gives_up(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generated id that already exists is never reused; persistent collision is `Conflict`."""
    taken = claim_or_verify(store, None, _gateway("rep-b"))
    monkeypatch.setattr(conversations, "new_conversation_id", lambda: taken)
    with pytest.raises(Conflict):
        claim_or_verify(store, None, _gateway("rep-a"))
    item = store.get_item(CONVERSATIONS_TABLE, {"rep_id": f"conversation#{taken}"})
    assert item is not None and item["owner_rep_id"] == "rep-b"

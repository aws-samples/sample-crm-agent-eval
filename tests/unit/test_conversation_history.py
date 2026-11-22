# SPDX-License-Identifier: MIT
"""Server-side conversation history: identity+tenant scope, size bound, redaction, no cross-session.

The store enforces its own `(rep_id, tenant)` scope, so a foreign-principal read is denied with no
cancellation marker involved. The contract tests run against both the in-memory store and the
DynamoDB store (over a `moto`-mocked client); the DynamoDB-only tests cover expiry, the item
shape and the optimistic-version retry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from moto import mock_aws

from agents.conversation_history import (
    DEFAULT_MAX_FIELD_CHARS,
    DEFAULT_TTL_SECONDS,
    HISTORY_ITEM_ATTRIBUTES,
    HISTORY_KIND,
    HISTORY_SCOPE,
    HISTORY_TABLE,
    MAX_WRITE_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    TRUNCATION_SUFFIX,
    ConversationHistoryStore,
    DynamoDbConversationHistory,
    HistoryTurn,
    HistoryWriteConflict,
    InMemoryConversationHistory,
)
from crm.authz import Identity
from crm.errors import Conflict, CrmError, ScopeDenied, ValidationFailed
from crm.store import keys
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_SESSION = "session-abc"

_T0 = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)

#: Builds a store under test from `(max_turns, max_field_chars)`.
StoreFactory = Callable[..., ConversationHistoryStore]


class _Clock:
    """A settable clock for the DynamoDB store."""

    def __init__(self, now: datetime = _T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def ddb_store() -> Iterator[Store]:
    """A `Store` over a moto-mocked DynamoDB client with every declared table created."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


@pytest.fixture(params=["memory", "dynamodb"])
def make_store(request: pytest.FixtureRequest) -> Iterator[StoreFactory]:
    """A factory for the store under test, parametrised over both implementations."""
    if request.param == "memory":
        yield lambda **kwargs: InMemoryConversationHistory(**kwargs)
        return
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        backing = Store(client=client)
        yield lambda **kwargs: DynamoDbConversationHistory(backing, clock=_Clock(), **kwargs)


def _identity(*, rep_id: str = "rep-1", tenant: str = "demo") -> Identity:
    """The identity shape `decode_identity` produces for a Hosted-UI caller."""
    return Identity(
        rep_id=rep_id,
        book=frozenset({"acc-1"}),
        full_visibility=False,
        principal_type="user",
        session_id=_SESSION,
        source="apigw",
        tenant=tenant,
    )


# --- basic record / read ---------------------------------------------------------------------


def test_records_and_returns_prior_turns_oldest_first(make_store: StoreFactory) -> None:
    store = make_store()
    ident = _identity()
    store.record_turn(_SESSION, identity=ident, prompt="hello", answer="hi there")
    store.record_turn(_SESSION, identity=ident, prompt="and again", answer="second answer")

    history = store.history_for(_SESSION, identity=ident)
    assert history == (
        HistoryTurn(prompt="hello", answer="hi there"),
        HistoryTurn(prompt="and again", answer="second answer"),
    )


def test_a_new_or_unknown_session_reads_empty_not_denied(make_store: StoreFactory) -> None:
    # The production path reads before recording, so a brand-new session must not raise.
    store = make_store()
    assert store.history_for("never-seen", identity=_identity()) == ()


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"], ids=["empty", "spaces", "whitespace"])
def test_blank_session_id_is_rejected_on_read_write_and_clear(
    make_store: StoreFactory, blank: str
) -> None:
    """Both implementations refuse a blank session id on every operation, `clear` included."""
    store = make_store()
    ident = _identity()
    with pytest.raises(ValidationFailed):
        store.history_for(blank, identity=ident)
    with pytest.raises(ValidationFailed):
        store.record_turn(blank, identity=ident, prompt="p", answer="a")
    with pytest.raises(ValidationFailed):
        store.clear(blank, identity=ident)


# --- the store owns its (rep_id, tenant) scope ------------------------------------------------


def test_foreign_rep_cannot_read_another_reps_history_with_no_cancellation_marker(
    make_store: StoreFactory,
) -> None:
    # The cancellation store is never touched, so only the history store's scope check can deny.
    store = make_store()
    owner = _identity(rep_id="rep-owner")
    store.record_turn(_SESSION, identity=owner, prompt="secret plan", answer="the answer")

    foreign = _identity(rep_id="rep-attacker")
    with pytest.raises(ScopeDenied) as excinfo:
        store.history_for(_SESSION, identity=foreign)
    assert excinfo.value.missing_scope == HISTORY_SCOPE
    # the refusal names no session id, so it is not an existence oracle
    assert _SESSION not in str(excinfo.value)


def test_foreign_tenant_cannot_read_history_even_with_same_rep_id() -> None:
    store = InMemoryConversationHistory()
    store.record_turn(_SESSION, identity=_identity(tenant="demo"), prompt="p", answer="a")
    with pytest.raises(ScopeDenied):
        store.history_for(_SESSION, identity=_identity(tenant="eval-run-1"))


def test_foreign_principal_cannot_inject_into_another_reps_history(
    make_store: StoreFactory,
) -> None:
    store = make_store()
    store.record_turn(_SESSION, identity=_identity(rep_id="rep-owner"), prompt="p", answer="a")
    with pytest.raises(ScopeDenied):
        store.record_turn(
            _SESSION, identity=_identity(rep_id="rep-attacker"), prompt="inject", answer="x"
        )
    # the owner's history is unchanged by the refused write
    assert store.history_for(_SESSION, identity=_identity(rep_id="rep-owner")) == (
        HistoryTurn(prompt="p", answer="a"),
    )


def test_the_owner_still_reads_after_a_foreign_read_is_refused(make_store: StoreFactory) -> None:
    store = make_store()
    owner = _identity(rep_id="rep-owner")
    store.record_turn(_SESSION, identity=owner, prompt="p", answer="a")
    with pytest.raises(ScopeDenied):
        store.history_for(_SESSION, identity=_identity(rep_id="rep-attacker"))
    assert store.history_for(_SESSION, identity=owner) == (HistoryTurn(prompt="p", answer="a"),)


# --- size bound (drop oldest) ----------------------------------------------------------------


def test_drops_oldest_turns_beyond_the_budget(make_store: StoreFactory) -> None:
    store = make_store(max_turns=2)
    ident = _identity()
    for i in range(4):
        store.record_turn(_SESSION, identity=ident, prompt=f"p{i}", answer=f"a{i}")

    history = store.history_for(_SESSION, identity=ident)
    assert len(history) == 2
    assert [t.prompt for t in history] == ["p2", "p3"]  # oldest two dropped


def test_caps_each_field_length(make_store: StoreFactory) -> None:
    store = make_store(max_field_chars=10)
    ident = _identity()
    # spaced words, so no single token matches the opaque-secret shape and redaction is a no-op
    long_prose = "ab cd ef gh ij kl mn op"  # 23 chars, ordinary prose
    store.record_turn(_SESSION, identity=ident, prompt=long_prose, answer="short")
    turn = store.history_for(_SESSION, identity=ident)[0]
    assert turn.prompt == long_prose[:10] + TRUNCATION_SUFFIX
    assert turn.answer == "short"


def test_invalid_bounds_are_rejected(make_store: StoreFactory) -> None:
    with pytest.raises(ValueError, match="max_turns"):
        make_store(max_turns=0)
    with pytest.raises(ValueError, match="max_field_chars"):
        make_store(max_field_chars=0)


# --- log-grade redaction ---------------------------------------------------------------------


#: A 40-char opaque credential shape that matches :func:`crm.metering._looks_like_secret`'s
#: opaque-secret pattern but none of gitleaks' rules.
_OPAQUE_CREDENTIAL = "aB3dE6gH9jK2mN5pQ8sT" + "1vW4xZ7bC0dF3gH6jK9m"  # gitleaks:allow


def test_redacts_a_credential_shaped_token_embedded_in_prose(make_store: StoreFactory) -> None:
    store = make_store()
    ident = _identity()
    # an opaque credential embedded mid-sentence must be scrubbed while the prose survives
    store.record_turn(
        _SESSION,
        identity=ident,
        prompt=f"my key is {_OPAQUE_CREDENTIAL} please use it",
        answer=f"the value {_OPAQUE_CREDENTIAL} was rotated",
    )
    turn = store.history_for(_SESSION, identity=ident)[0]
    assert _OPAQUE_CREDENTIAL not in turn.prompt
    assert _OPAQUE_CREDENTIAL not in turn.answer
    assert "[REDACTED]" in turn.prompt
    # ordinary words are preserved
    assert turn.prompt == "my key is [REDACTED] please use it"
    assert turn.answer == "the value [REDACTED] was rotated"


def test_ordinary_prose_is_not_over_redacted(make_store: StoreFactory) -> None:
    store = make_store()
    ident = _identity()
    store.record_turn(
        _SESSION, identity=ident, prompt="what are my top accounts?", answer="Acme and Globex."
    )
    turn = store.history_for(_SESSION, identity=ident)[0]
    assert turn.prompt == "what are my top accounts?"
    assert turn.answer == "Acme and Globex."
    assert "[REDACTED]" not in turn.prompt + turn.answer


def test_a_secret_is_redacted_before_truncation_so_no_partial_leaks(
    make_store: StoreFactory,
) -> None:
    # Redaction runs before truncation, so no 15-char partial of the secret is stored.
    store = make_store(max_field_chars=15)
    ident = _identity()
    secret = "A" * 40  # matches the opaque-secret shape (>=32 chars)
    store.record_turn(_SESSION, identity=ident, prompt=secret, answer="ok")
    turn = store.history_for(_SESSION, identity=ident)[0]
    assert "A" * 15 not in turn.prompt  # no truncated-secret partial leaked
    assert turn.prompt == "[REDACTED]"  # <=15 chars after redaction, so not truncated


# --- clear -----------------------------------------------------------------------------------


def test_clear_forgets_a_session_and_is_idempotent(make_store: StoreFactory) -> None:
    store = make_store()
    ident = _identity()
    store.record_turn(_SESSION, identity=ident, prompt="p", answer="a")
    store.clear(_SESSION, identity=ident)
    assert store.history_for(_SESSION, identity=ident) == ()
    store.clear(_SESSION, identity=ident)  # no-op, no raise


def test_a_foreign_principal_cannot_clear_another_reps_history(make_store: StoreFactory) -> None:
    store = make_store()
    owner = _identity(rep_id="rep-owner")
    store.record_turn(_SESSION, identity=owner, prompt="p", answer="a")
    with pytest.raises(ScopeDenied):
        store.clear(_SESSION, identity=_identity(rep_id="rep-attacker"))
    assert store.history_for(_SESSION, identity=owner) == (HistoryTurn(prompt="p", answer="a"),)


def test_history_turn_is_frozen() -> None:
    import dataclasses

    turn = HistoryTurn(prompt="p", answer="a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        turn.prompt = "x"  # type: ignore[misc]


def test_default_field_cap_is_generous_enough_for_ordinary_answers() -> None:
    # a guard that the default is a real bound, not a near-zero that would truncate normal prose
    assert DEFAULT_MAX_FIELD_CHARS >= 1000


# --- DynamoDB store: tenant in the key, expiry, item shape, optimistic retry -----------------


def _raw_item(backing: Store, tenant: str = "demo") -> dict[str, object] | None:
    return backing.get_item(HISTORY_TABLE, {"rep_id": keys.history_key(tenant, _SESSION)})


def test_dynamodb_foreign_tenant_reads_empty_and_writes_its_own_item(ddb_store: Store) -> None:
    store = DynamoDbConversationHistory(ddb_store, clock=_Clock())
    store.record_turn(_SESSION, identity=_identity(tenant="demo"), prompt="p", answer="a")
    foreign = _identity(tenant="eval-run-1")
    assert store.history_for(_SESSION, identity=foreign) == ()
    store.record_turn(_SESSION, identity=foreign, prompt="q", answer="b")
    assert store.history_for(_SESSION, identity=_identity(tenant="demo")) == (
        HistoryTurn(prompt="p", answer="a"),
    )
    assert store.history_for(_SESSION, identity=foreign) == (HistoryTurn(prompt="q", answer="b"),)


def test_dynamodb_item_carries_exactly_the_declared_attributes(ddb_store: Store) -> None:
    clock = _Clock()
    store = DynamoDbConversationHistory(ddb_store, clock=clock)
    store.record_turn(_SESSION, identity=_identity(), prompt="p", answer="a")
    item = _raw_item(ddb_store)
    assert item == {
        "rep_id": "history#demo#session-abc",
        "kind": HISTORY_KIND,
        "owner_rep_id": "rep-1",
        "owner_tenant": "demo",
        "turns": [{"prompt": "p", "answer": "a"}],
        "version": 1,
        "updated_at": _T0.isoformat(),
        "expires_at": int(_T0.timestamp()) + DEFAULT_TTL_SECONDS,
    }
    assert set(item) == HISTORY_ITEM_ATTRIBUTES
    assert "tenant" not in item


def test_dynamodb_each_write_bumps_the_version_and_refreshes_expiry(ddb_store: Store) -> None:
    clock = _Clock()
    store = DynamoDbConversationHistory(ddb_store, clock=clock, ttl_seconds=600)
    store.record_turn(_SESSION, identity=_identity(), prompt="p1", answer="a1")
    clock.now = _T0 + timedelta(seconds=300)
    store.record_turn(_SESSION, identity=_identity(), prompt="p2", answer="a2")
    item = _raw_item(ddb_store)
    assert item is not None
    assert item["version"] == 2
    assert item["expires_at"] == int(clock.now.timestamp()) + 600


def test_dynamodb_an_expired_item_reads_empty(ddb_store: Store) -> None:
    clock = _Clock()
    store = DynamoDbConversationHistory(ddb_store, clock=clock, ttl_seconds=60)
    store.record_turn(_SESSION, identity=_identity(), prompt="p", answer="a")
    clock.now = _T0 + timedelta(seconds=59)
    assert store.history_for(_SESSION, identity=_identity()) != ()
    clock.now = _T0 + timedelta(seconds=60)
    assert store.history_for(_SESSION, identity=_identity()) == ()


def test_dynamodb_a_write_after_expiry_starts_a_fresh_history(ddb_store: Store) -> None:
    clock = _Clock()
    store = DynamoDbConversationHistory(ddb_store, clock=clock, ttl_seconds=60)
    store.record_turn(_SESSION, identity=_identity(), prompt="old", answer="a")
    clock.now = _T0 + timedelta(seconds=61)
    store.record_turn(_SESSION, identity=_identity(), prompt="new", answer="b")
    assert store.history_for(_SESSION, identity=_identity()) == (
        HistoryTurn(prompt="new", answer="b"),
    )
    item = _raw_item(ddb_store)
    assert item is not None and item["version"] == 2


def test_dynamodb_rejects_a_non_positive_ttl(ddb_store: Store) -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        DynamoDbConversationHistory(ddb_store, ttl_seconds=0)


class _RacingStore(Store):
    """A `Store` that lets a competing writer land just before each of the first `races` writes."""

    def __init__(self, inner: Store, compete: Callable[[], None], races: int) -> None:
        self.__dict__.update(inner.__dict__)
        self._compete = compete
        self._races = races
        self.attempts = 0

    def put_conditional(
        self, table: str, item: Mapping[str, object], *, expected_version: int | None
    ) -> bool:
        self.attempts += 1
        if self.attempts <= self._races:
            self._compete()
        return super().put_conditional(table, item, expected_version=expected_version)


def test_dynamodb_a_lost_race_is_retried_and_both_turns_land(ddb_store: Store) -> None:
    rival = DynamoDbConversationHistory(ddb_store, clock=_Clock())
    racing = _RacingStore(
        ddb_store,
        lambda: rival.record_turn(_SESSION, identity=_identity(), prompt="rival", answer="r"),
        races=1,
    )
    store = DynamoDbConversationHistory(racing, clock=_Clock())
    store.record_turn(_SESSION, identity=_identity(), prompt="mine", answer="m")
    assert racing.attempts == 2
    assert [t.prompt for t in store.history_for(_SESSION, identity=_identity())] == [
        "rival",
        "mine",
    ]


def test_dynamodb_raises_history_write_conflict_after_the_last_attempt(ddb_store: Store) -> None:
    rival = DynamoDbConversationHistory(ddb_store, clock=_Clock())
    racing = _RacingStore(
        ddb_store,
        lambda: rival.record_turn(_SESSION, identity=_identity(), prompt="rival", answer="r"),
        races=MAX_WRITE_ATTEMPTS,
    )
    pauses: list[float] = []
    store = DynamoDbConversationHistory(racing, clock=_Clock(), sleep=pauses.append)
    with pytest.raises(HistoryWriteConflict) as excinfo:
        store.record_turn(_SESSION, identity=_identity(), prompt="mine", answer="m")
    assert isinstance(excinfo.value, Conflict)
    assert racing.attempts == MAX_WRITE_ATTEMPTS == 3
    low, high = RETRY_BACKOFF_SECONDS
    assert len(pauses) == MAX_WRITE_ATTEMPTS - 1
    assert all(low <= pause <= high for pause in pauses)
    prompts = [t.prompt for t in store.history_for(_SESSION, identity=_identity())]
    assert "mine" not in prompts and prompts == ["rival"] * MAX_WRITE_ATTEMPTS


def test_dynamodb_a_first_write_that_lands_does_not_pause(ddb_store: Store) -> None:
    pauses: list[float] = []
    store = DynamoDbConversationHistory(ddb_store, clock=_Clock(), sleep=pauses.append)
    store.record_turn(_SESSION, identity=_identity(), prompt="p", answer="a")
    assert pauses == []


class _CapturingClient:
    """Forwards every call to a real client and records each `get_item` call's keyword arguments."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.get_item_calls: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> Any:
        self.get_item_calls.append(kwargs)
        return self._inner.get_item(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_dynamodb_every_history_read_is_strongly_consistent(ddb_store: Store) -> None:
    capturing = _CapturingClient(ddb_store._client)
    store = DynamoDbConversationHistory(Store(client=capturing), clock=_Clock())
    store.record_turn(_SESSION, identity=_identity(), prompt="p", answer="a")
    store.history_for(_SESSION, identity=_identity())
    store.clear(_SESSION, identity=_identity())
    assert len(capturing.get_item_calls) == 3
    assert all(call.get("ConsistentRead") is True for call in capturing.get_item_calls)


def _expired_owned_item(ddb_store: Store) -> DynamoDbConversationHistory:
    """Record one turn for `rep-owner` and move the clock past its expiry, before any TTL delete."""
    clock = _Clock()
    store = DynamoDbConversationHistory(ddb_store, clock=clock, ttl_seconds=60)
    store.record_turn(_SESSION, identity=_identity(rep_id="rep-owner"), prompt="p", answer="a")
    clock.now = _T0 + timedelta(seconds=61)
    return store


def test_dynamodb_an_expired_item_reads_empty_for_its_owner_and_a_stranger(
    ddb_store: Store,
) -> None:
    store = _expired_owned_item(ddb_store)
    assert store.history_for(_SESSION, identity=_identity(rep_id="rep-owner")) == ()
    assert store.history_for(_SESSION, identity=_identity(rep_id="rep-other")) == ()


def test_dynamodb_a_stranger_cannot_seize_an_expired_undeleted_item(ddb_store: Store) -> None:
    """A different principal's write to an expired item is refused and leaves the item untouched."""
    store = _expired_owned_item(ddb_store)
    before = _raw_item(ddb_store)

    with pytest.raises(ScopeDenied) as excinfo:
        store.record_turn(_SESSION, identity=_identity(rep_id="rep-other"), prompt="x", answer="y")

    assert excinfo.value.missing_scope == HISTORY_SCOPE
    assert _raw_item(ddb_store) == before


def test_dynamodb_the_owner_clears_an_expired_undeleted_item(ddb_store: Store) -> None:
    store = _expired_owned_item(ddb_store)
    store.clear(_SESSION, identity=_identity(rep_id="rep-owner"))
    assert _raw_item(ddb_store) is None


def test_dynamodb_a_stranger_cannot_clear_an_expired_undeleted_item(ddb_store: Store) -> None:
    store = _expired_owned_item(ddb_store)
    with pytest.raises(ScopeDenied):
        store.clear(_SESSION, identity=_identity(rep_id="rep-other"))
    assert _raw_item(ddb_store) is not None


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("turns", "not-a-list"),
        ("turns", [{"prompt": "p"}]),
        ("turns", ["not-a-map"]),
        ("owner_rep_id", 7),
        ("expires_at", "soon"),
        ("version", "one"),
    ],
)
def test_dynamodb_a_malformed_item_is_an_error_not_silent_history(
    ddb_store: Store, attribute: str, value: object
) -> None:
    store = DynamoDbConversationHistory(ddb_store, clock=_Clock())
    store.record_turn(_SESSION, identity=_identity(), prompt="p", answer="a")
    item = _raw_item(ddb_store)
    assert item is not None
    ddb_store.put_item(HISTORY_TABLE, {**item, attribute: value})
    with pytest.raises(CrmError, match="could not be read"):
        if attribute == "version":
            store.record_turn(_SESSION, identity=_identity(), prompt="p2", answer="a2")
        else:
            store.history_for(_SESSION, identity=_identity())


def test_dynamodb_a_store_failure_propagates(ddb_store: Store) -> None:
    store = DynamoDbConversationHistory(Store(table_prefix="missing-", client=ddb_store._client))
    with pytest.raises(CrmError):
        store.history_for(_SESSION, identity=_identity())
    with pytest.raises(CrmError):
        store.record_turn(_SESSION, identity=_identity(), prompt="p", answer="a")

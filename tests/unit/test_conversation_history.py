# SPDX-License-Identifier: MIT
"""Server-side conversation history: identity+tenant scope, size bound, redaction, no cross-session.

The store enforces its own `(rep_id, tenant)` scope, so a foreign-principal read is denied with no
cancellation marker involved.
"""

from __future__ import annotations

import pytest

from agents.conversation_history import (
    DEFAULT_MAX_FIELD_CHARS,
    HISTORY_SCOPE,
    TRUNCATION_SUFFIX,
    HistoryTurn,
    InMemoryConversationHistory,
)
from crm.authz import Identity
from crm.errors import ScopeDenied, ValidationFailed

_SESSION = "session-abc"


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


def test_records_and_returns_prior_turns_oldest_first() -> None:
    store = InMemoryConversationHistory()
    ident = _identity()
    store.record_turn(_SESSION, identity=ident, prompt="hello", answer="hi there")
    store.record_turn(_SESSION, identity=ident, prompt="and again", answer="second answer")

    history = store.history_for(_SESSION, identity=ident)
    assert history == (
        HistoryTurn(prompt="hello", answer="hi there"),
        HistoryTurn(prompt="and again", answer="second answer"),
    )


def test_a_new_or_unknown_session_reads_empty_not_denied() -> None:
    # The production path reads before recording, so a brand-new session must not raise.
    store = InMemoryConversationHistory()
    assert store.history_for("never-seen", identity=_identity()) == ()


def test_blank_session_id_is_rejected_on_read_and_write() -> None:
    store = InMemoryConversationHistory()
    ident = _identity()
    with pytest.raises(ValidationFailed):
        store.history_for("   ", identity=ident)
    with pytest.raises(ValidationFailed):
        store.record_turn("", identity=ident, prompt="p", answer="a")


# --- the store owns its (rep_id, tenant) scope ------------------------------------------------


def test_foreign_rep_cannot_read_another_reps_history_with_no_cancellation_marker() -> None:
    # The cancellation store is never touched, so only the history store's scope check can deny.
    store = InMemoryConversationHistory()
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


def test_foreign_principal_cannot_inject_into_another_reps_history() -> None:
    store = InMemoryConversationHistory()
    store.record_turn(_SESSION, identity=_identity(rep_id="rep-owner"), prompt="p", answer="a")
    with pytest.raises(ScopeDenied):
        store.record_turn(
            _SESSION, identity=_identity(rep_id="rep-attacker"), prompt="inject", answer="x"
        )
    # the owner's history is unchanged by the refused write
    assert store.history_for(_SESSION, identity=_identity(rep_id="rep-owner")) == (
        HistoryTurn(prompt="p", answer="a"),
    )


def test_the_owner_still_reads_after_a_foreign_read_is_refused() -> None:
    store = InMemoryConversationHistory()
    owner = _identity(rep_id="rep-owner")
    store.record_turn(_SESSION, identity=owner, prompt="p", answer="a")
    with pytest.raises(ScopeDenied):
        store.history_for(_SESSION, identity=_identity(rep_id="rep-attacker"))
    assert store.history_for(_SESSION, identity=owner) == (HistoryTurn(prompt="p", answer="a"),)


# --- size bound (drop oldest) ----------------------------------------------------------------


def test_drops_oldest_turns_beyond_the_budget() -> None:
    store = InMemoryConversationHistory(max_turns=2)
    ident = _identity()
    for i in range(4):
        store.record_turn(_SESSION, identity=ident, prompt=f"p{i}", answer=f"a{i}")

    history = store.history_for(_SESSION, identity=ident)
    assert len(history) == 2
    assert [t.prompt for t in history] == ["p2", "p3"]  # oldest two dropped


def test_caps_each_field_length() -> None:
    store = InMemoryConversationHistory(max_field_chars=10)
    ident = _identity()
    # spaced words, so no single token matches the opaque-secret shape and redaction is a no-op
    long_prose = "ab cd ef gh ij kl mn op"  # 23 chars, ordinary prose
    store.record_turn(_SESSION, identity=ident, prompt=long_prose, answer="short")
    turn = store.history_for(_SESSION, identity=ident)[0]
    assert turn.prompt == long_prose[:10] + TRUNCATION_SUFFIX
    assert turn.answer == "short"


def test_invalid_bounds_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_turns"):
        InMemoryConversationHistory(max_turns=0)
    with pytest.raises(ValueError, match="max_field_chars"):
        InMemoryConversationHistory(max_field_chars=0)


# --- log-grade redaction ---------------------------------------------------------------------


#: A 40-char opaque credential shape that matches :func:`crm.metering._looks_like_secret`'s
#: opaque-secret pattern but none of gitleaks' rules.
_OPAQUE_CREDENTIAL = "aB3dE6gH9jK2mN5pQ8sT" + "1vW4xZ7bC0dF3gH6jK9m"  # gitleaks:allow


def test_redacts_a_credential_shaped_token_embedded_in_prose() -> None:
    store = InMemoryConversationHistory()
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


def test_ordinary_prose_is_not_over_redacted() -> None:
    store = InMemoryConversationHistory()
    ident = _identity()
    store.record_turn(
        _SESSION, identity=ident, prompt="what are my top accounts?", answer="Acme and Globex."
    )
    turn = store.history_for(_SESSION, identity=ident)[0]
    assert turn.prompt == "what are my top accounts?"
    assert turn.answer == "Acme and Globex."
    assert "[REDACTED]" not in turn.prompt + turn.answer


def test_a_secret_is_redacted_before_truncation_so_no_partial_leaks() -> None:
    # Redaction runs before truncation, so no 15-char partial of the secret is stored.
    store = InMemoryConversationHistory(max_field_chars=15)
    ident = _identity()
    secret = "A" * 40  # matches the opaque-secret shape (>=32 chars)
    store.record_turn(_SESSION, identity=ident, prompt=secret, answer="ok")
    turn = store.history_for(_SESSION, identity=ident)[0]
    assert "A" * 15 not in turn.prompt  # no truncated-secret partial leaked
    assert turn.prompt == "[REDACTED]"  # <=15 chars after redaction, so not truncated


# --- clear -----------------------------------------------------------------------------------


def test_clear_forgets_a_session_and_is_idempotent() -> None:
    store = InMemoryConversationHistory()
    ident = _identity()
    store.record_turn(_SESSION, identity=ident, prompt="p", answer="a")
    store.clear(_SESSION)
    assert store.history_for(_SESSION, identity=ident) == ()
    store.clear(_SESSION)  # no-op, no raise


def test_history_turn_is_frozen() -> None:
    import dataclasses

    turn = HistoryTurn(prompt="p", answer="a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        turn.prompt = "x"  # type: ignore[misc]


def test_default_field_cap_is_generous_enough_for_ordinary_answers() -> None:
    # a guard that the default is a real bound, not a near-zero that would truncate normal prose
    assert DEFAULT_MAX_FIELD_CHARS >= 1000

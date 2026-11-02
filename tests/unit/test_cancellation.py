# SPDX-License-Identifier: MIT
"""Tests for the cooperative cancellation marker store.

Covers idempotent write/read/clear, denial of a foreign caller, and cancel-after-terminal.
"""

from __future__ import annotations

import pytest

from agents.cancellation import (
    CANCEL_SCOPE,
    InMemoryCancellationStore,
    SessionOwner,
)
from crm.authz import Identity
from crm.errors import ScopeDenied, ValidationFailed

_SESSION = "session-abc"


def _identity(*, rep_id: str = "rep-1", tenant: str = "demo") -> Identity:
    """Build the identity shape `decode_identity` produces for a Hosted-UI caller."""
    return Identity(
        rep_id=rep_id,
        book=frozenset({"acc-1"}),
        full_visibility=False,
        principal_type="user",
        session_id=_SESSION,
        source="apigw",
        tenant=tenant,
    )


def _owner(*, rep_id: str = "rep-1", tenant: str = "demo") -> SessionOwner:
    return SessionOwner.of(_identity(rep_id=rep_id, tenant=tenant))


# --------------------------------------------------------------------------------------
# The granted case, and idempotence of write / read / clear
# --------------------------------------------------------------------------------------


def test_owner_pair_is_derived_from_an_identity_never_constructed_here() -> None:
    owner = SessionOwner.of(_identity(rep_id="rep-9", tenant="eval-42"))
    assert owner == SessionOwner(rep_id="rep-9", tenant="eval-42")


def test_the_registering_owner_can_cancel_its_own_session() -> None:
    store = InMemoryCancellationStore()
    store.register_turn(_SESSION, owner=_owner())

    assert store.is_cancelled(_SESSION) is False
    assert store.request_cancellation(_SESSION, requester=_owner()) == "accepted"
    assert store.is_cancelled(_SESSION) is True


def test_registering_the_same_owner_twice_is_idempotent_and_preserves_the_marker() -> None:
    store = InMemoryCancellationStore()
    store.register_turn(_SESSION, owner=_owner())
    store.request_cancellation(_SESSION, requester=_owner())

    store.register_turn(_SESSION, owner=_owner())

    assert store.is_cancelled(_SESSION) is True


def test_a_new_turn_on_a_finished_session_starts_with_no_stale_marker() -> None:
    """Re-registering a finished session clears the previous turn's cancellation marker."""
    store = InMemoryCancellationStore()
    store.register_turn(_SESSION, owner=_owner())
    store.request_cancellation(_SESSION, requester=_owner())
    store.mark_terminal(_SESSION)

    store.register_turn(_SESSION, owner=_owner())

    assert store.is_cancelled(_SESSION) is False
    assert store.request_cancellation(_SESSION, requester=_owner()) == "accepted"


def test_a_finished_session_cannot_be_taken_over_by_re_registering_it() -> None:
    """A foreign owner cannot re-register a finished session."""
    store = InMemoryCancellationStore()
    store.register_turn(_SESSION, owner=_owner(rep_id="rep-1"))
    store.mark_terminal(_SESSION)

    with pytest.raises(ScopeDenied):
        store.register_turn(_SESSION, owner=_owner(rep_id="rep-2"))


def test_a_repeated_cancellation_is_reported_as_already_requested_not_re_accepted() -> None:
    store = InMemoryCancellationStore()
    store.register_turn(_SESSION, owner=_owner())

    assert store.request_cancellation(_SESSION, requester=_owner()) == "accepted"
    assert store.request_cancellation(_SESSION, requester=_owner()) == "already_requested"
    assert store.is_cancelled(_SESSION) is True


def test_clear_is_idempotent_and_forgets_the_marker() -> None:
    store = InMemoryCancellationStore()
    store.register_turn(_SESSION, owner=_owner())
    store.request_cancellation(_SESSION, requester=_owner())

    store.clear(_SESSION)
    store.clear(_SESSION)
    store.clear("never-registered")

    assert store.is_cancelled(_SESSION) is False


def test_mark_terminal_is_idempotent_and_tolerates_an_unregistered_session() -> None:
    store = InMemoryCancellationStore()
    store.mark_terminal("never-registered")

    store.register_turn(_SESSION, owner=_owner())
    store.mark_terminal(_SESSION)
    store.mark_terminal(_SESSION)

    assert store.request_cancellation(_SESSION, requester=_owner()) == "already_terminal"


def test_an_unregistered_session_reports_no_pending_cancellation() -> None:
    """An unregistered session reports no pending cancellation."""
    assert InMemoryCancellationStore().is_cancelled("never-registered") is False


def test_two_sessions_do_not_interfere() -> None:
    store = InMemoryCancellationStore()
    store.register_turn("session-one", owner=_owner())
    store.register_turn("session-two", owner=_owner())

    store.request_cancellation("session-one", requester=_owner())

    assert store.is_cancelled("session-one") is True
    assert store.is_cancelled("session-two") is False


# --------------------------------------------------------------------------------------
# The denied cases: a caller cannot address another user's session_id
# --------------------------------------------------------------------------------------


def test_a_different_rep_cannot_cancel_another_users_session() -> None:
    store = InMemoryCancellationStore()
    store.register_turn(_SESSION, owner=_owner(rep_id="rep-1"))

    with pytest.raises(ScopeDenied) as denial:
        store.request_cancellation(_SESSION, requester=_owner(rep_id="rep-2"))

    assert denial.value.missing_scope == CANCEL_SCOPE
    assert store.is_cancelled(_SESSION) is False


def test_the_same_rep_id_in_another_tenant_is_a_different_owner() -> None:
    """The same rep id in another tenant is denied as a different owner."""
    store = InMemoryCancellationStore()
    store.register_turn(_SESSION, owner=_owner(tenant="demo"))

    with pytest.raises(ScopeDenied):
        store.request_cancellation(_SESSION, requester=_owner(tenant="canary"))

    assert store.is_cancelled(_SESSION) is False


def test_cancelling_an_unregistered_session_is_denied_identically_to_a_foreign_one() -> None:
    """Cancelling an unknown session and a foreign session raise identical denials."""
    store = InMemoryCancellationStore()
    store.register_turn(_SESSION, owner=_owner(rep_id="rep-1"))

    with pytest.raises(ScopeDenied) as unknown:
        store.request_cancellation("session-never-seen", requester=_owner(rep_id="rep-1"))
    with pytest.raises(ScopeDenied) as foreign:
        store.request_cancellation(_SESSION, requester=_owner(rep_id="rep-2"))

    assert unknown.value.message == foreign.value.message
    assert unknown.value.missing_scope == foreign.value.missing_scope
    assert unknown.value.http_status == foreign.value.http_status == 403


def test_a_second_owner_cannot_take_over_an_existing_session_id_by_registering_it() -> None:
    store = InMemoryCancellationStore()
    store.register_turn(_SESSION, owner=_owner(rep_id="rep-1"))

    with pytest.raises(ScopeDenied):
        store.register_turn(_SESSION, owner=_owner(rep_id="rep-2"))

    assert store.request_cancellation(_SESSION, requester=_owner(rep_id="rep-1")) == "accepted"


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_session_id_is_rejected_on_both_write_paths(blank: str) -> None:
    store = InMemoryCancellationStore()
    with pytest.raises(ValidationFailed):
        store.register_turn(blank, owner=_owner())
    with pytest.raises(ValidationFailed):
        store.request_cancellation(blank, requester=_owner())


# --------------------------------------------------------------------------------------
# A cancel after a terminal event is a no-op, not a second terminal event
# --------------------------------------------------------------------------------------


def test_a_cancel_after_a_terminal_event_writes_no_marker() -> None:
    store = InMemoryCancellationStore()
    store.register_turn(_SESSION, owner=_owner())
    store.mark_terminal(_SESSION)

    assert store.request_cancellation(_SESSION, requester=_owner()) == "already_terminal"
    assert store.is_cancelled(_SESSION) is False


def test_a_cancel_that_landed_before_the_terminal_event_still_reads_as_cancelled() -> None:
    """A cancellation accepted before the terminal mark still reads as cancelled."""
    store = InMemoryCancellationStore()
    store.register_turn(_SESSION, owner=_owner())
    assert store.request_cancellation(_SESSION, requester=_owner()) == "accepted"

    store.mark_terminal(_SESSION)

    assert store.is_cancelled(_SESSION) is True


def test_a_terminal_session_stays_terminal_for_a_foreign_caller_who_is_still_denied() -> None:
    """A foreign caller's cancel on a terminal session is still denied."""
    store = InMemoryCancellationStore()
    store.register_turn(_SESSION, owner=_owner(rep_id="rep-1"))
    store.mark_terminal(_SESSION)

    with pytest.raises(ScopeDenied):
        store.request_cancellation(_SESSION, requester=_owner(rep_id="rep-2"))

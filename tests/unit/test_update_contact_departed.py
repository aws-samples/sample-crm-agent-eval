# SPDX-License-Identifier: MIT
"""`update_contact` refuses a non-reactivating update to a departed contact.

Runs the real handler entry point against a `moto`-mocked DynamoDB. The refusal precedes any write,
an update moving status off `departed` still applies, and `not_found` and `scope_denied` keep their precedence.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import CONTACT_DEPARTED_MESSAGE
from crm.handlers import update_contact
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_ACCOUNT = "acct-1"
_OTHER_ACCOUNT = "acct-2"
_CONTACT = "cont-1"


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


class _TransactWriteSpy:
    """Counts `transact_write` calls on a real store while delegating to it."""

    def __init__(self, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real = store.transact_write

        def _spy(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(store, "transact_write", _spy)


def _event(args: dict[str, Any], *, book: list[str] | None = None) -> dict[str, Any]:
    identity = {
        "rep_id": "rep-1",
        "book": book if book is not None else [_ACCOUNT],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": _TENANT,
    }
    return identity_event(IDENTITY_KEY, identity, args)


def _seed_contact(store: Store, *, account_id: str = _ACCOUNT, status: str = "departed") -> None:
    store.put_item(
        "contacts",
        {
            "contact_id": _CONTACT,
            "tenant": _TENANT,
            "tenant_account_id": f"{_TENANT}#{account_id}",
            "account_id": account_id,
            "name": "Jane Doe",
            "name_lower": "jane doe",
            "status": status,
            "phone": "+1-555-010-0000",
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )


def _audit_count(store: Store) -> int:
    items, _ = store.query_tenant_scan("audit_records", tenant=_TENANT)
    return len(items)


def _update(store: Store, fields: dict[str, Any], **event_kwargs: Any) -> dict[str, Any]:
    result: dict[str, Any] = update_contact.handler(
        _event(
            {"contact_id": _CONTACT, "fields": fields, "transaction_key": "tx-departed-1"},
            **event_kwargs,
        ),
        store=store,
    )
    return result


def test_phone_change_on_departed_contact_is_refused_before_any_write(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A departed contact's phone update gives `contact_departed`/409, and nothing is written."""
    _seed_contact(store)
    before = store.get_item("contacts", {"contact_id": _CONTACT})
    spy = _TransactWriteSpy(store, monkeypatch)

    result = _update(store, {"phone": "+1-555-010-9999"})

    assert result["statusCode"] == 409
    assert result["error"] == {
        "code": "contact_departed",
        "message": CONTACT_DEPARTED_MESSAGE,
        "next_step": "tell_user",
        "missing_scope": "status:active",
    }
    assert spy.calls == 0
    assert store.get_item("contacts", {"contact_id": _CONTACT}) == before
    assert _audit_count(store) == 0


def test_update_keeping_status_departed_is_refused_before_any_write(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`{status: departed, phone}` does not change status, so it is refused like a bare phone edit."""
    _seed_contact(store)
    before = store.get_item("contacts", {"contact_id": _CONTACT})
    spy = _TransactWriteSpy(store, monkeypatch)

    result = _update(store, {"status": "departed", "phone": "+1-555-010-9999"})

    assert result["statusCode"] == 409
    assert result["error"]["code"] == "contact_departed"
    assert result["error"]["missing_scope"] == "status:active"
    assert spy.calls == 0
    assert store.get_item("contacts", {"contact_id": _CONTACT}) == before
    assert _audit_count(store) == 0


def test_marking_an_active_contact_departed_still_applies(store: Store) -> None:
    """The refusal keys on the stored status: an active contact can still be marked departed."""
    _seed_contact(store, status="active")

    result = _update(store, {"status": "departed"})

    assert result["statusCode"] == 200, result
    assert result["body"]["contact"]["status"] == "departed"
    assert _audit_count(store) == 1


def test_reactivating_update_on_departed_contact_applies_with_one_audit_record(
    store: Store,
) -> None:
    """`{status: active, phone}` on a departed contact succeeds and writes exactly one audit record."""
    _seed_contact(store)

    result = _update(store, {"status": "active", "phone": "+1-555-010-9999"})

    assert result["statusCode"] == 200, result
    contact = result["body"]["contact"]
    assert (contact["status"], contact["phone"]) == ("active", "+1-555-010-9999")
    stored = store.get_item("contacts", {"contact_id": _CONTACT})
    assert stored is not None
    assert (stored["status"], stored["phone"]) == ("active", "+1-555-010-9999")
    assert _audit_count(store) == 1


def test_departed_contact_outside_the_book_is_scope_denied_not_contact_departed(
    store: Store,
) -> None:
    """Scope precedes the departed check, so an out-of-book departed contact stays `scope_denied`."""
    _seed_contact(store, account_id=_OTHER_ACCOUNT)

    result = _update(store, {"phone": "+1-555-010-9999"})

    assert result["statusCode"] == 403
    assert result["error"]["code"] == "scope_denied"
    assert _audit_count(store) == 0


def test_missing_contact_is_not_found(store: Store) -> None:
    """`not_found` precedes both scope and the departed check."""
    result = _update(store, {"phone": "+1-555-010-9999"})

    assert result["statusCode"] == 404
    assert result["error"]["code"] == "not_found"


def test_active_contact_phone_change_is_unaffected(store: Store) -> None:
    """The refusal is keyed on `departed`: an active contact's phone update still applies."""
    _seed_contact(store, status="active")

    result = _update(store, {"phone": "+1-555-010-9999"})

    assert result["statusCode"] == 200, result
    assert result["body"]["contact"]["phone"] == "+1-555-010-9999"
    assert _audit_count(store) == 1


def test_refusal_payload_carries_no_detail(store: Store) -> None:
    """The contact id recorded in `detail` never crosses the boundary."""
    _seed_contact(store)

    result = _update(store, {"phone": "+1-555-010-9999"})

    assert "detail" not in result["error"]
    assert "contact_id=" not in repr(result)

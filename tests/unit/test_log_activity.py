# SPDX-License-Identifier: MIT
"""`log_activity` touches only `last_touch`/`updated_at` on the opportunity, under `moto`.

The opportunity write is a transactional `Update` beside the activity `Put`, so a concurrent write to
any other opportunity field is never reverted, and a vanished or re-tenanted opportunity cancels the
whole transaction.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import log_activity
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"
_OPPORTUNITY = "opp-1"
_OCCURRED = "2026-03-01T09:00:00+00:00"


@pytest.fixture
def client() -> Iterator[Any]:
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(dynamodb)
        yield dynamodb


def _event(**overrides: object) -> dict[str, Any]:
    args: dict[str, object] = {
        "opportunity_id": _OPPORTUNITY,
        "kind": "call",
        "subject": "Check-in",
        "occurred_at": _OCCURRED,
        "transaction_key": "tx-log",
        **overrides,
    }
    return identity_event(
        IDENTITY_KEY,
        {
            "rep_id": _REP,
            "book": [_ACCOUNT],
            "full_visibility": False,
            "principal_type": "user",
            "session_id": "sess-1",
            "source": "gateway",
            "tenant": _TENANT,
        },
        args,
    )


def _seed(store: Store) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": _OPPORTUNITY,
            "tenant": _TENANT,
            "account_id": _ACCOUNT,
            "name": "Acme Renewal",
            "stage": "Discovery",
            "close_date": "2026-06-30",
            "owner_rep_id": _REP,
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
    )


def _opportunity(store: Store) -> dict[str, object] | None:
    return store.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})


def _activities(store: Store) -> list[dict[str, object]]:
    items, _ = store.query_tenant_scan("activities", tenant=_TENANT, limit=100)
    return items


class _WriteBeforeTransact(Store):
    """A store that rewrites the opportunity right before its next transaction."""

    def __init__(self, *, client: Any, fields: dict[str, object] | None) -> None:
        super().__init__(client=client)
        self._fields = fields

    def transact_write(self, operations: Any, *, transaction_key: str | None = None) -> None:
        current = self.get_item("opportunities", {"opportunity_id": _OPPORTUNITY})
        assert current is not None
        if self._fields is None:
            self.delete_item("opportunities", {"opportunity_id": _OPPORTUNITY})
        else:
            self.put_item("opportunities", {**current, **self._fields})
        super().transact_write(operations, transaction_key=transaction_key)


def test_only_last_touch_and_updated_at_change_on_the_opportunity(client: Any) -> None:
    store = Store(client=client)
    _seed(store)
    before = _opportunity(store)
    assert before is not None

    result = log_activity.handler(_event(), store=store)

    assert result["statusCode"] == 200, result
    after = _opportunity(store)
    assert after is not None
    assert after["last_touch"] == _OCCURRED
    assert after["updated_at"] != before["updated_at"]
    unchanged = {k: v for k, v in after.items() if k not in {"last_touch", "updated_at"}}
    assert unchanged == {k: v for k, v in before.items() if k != "updated_at"}
    assert result["body"]["opportunity"] == after
    assert len(_activities(store)) == 1


def test_a_concurrent_write_to_another_field_is_not_reverted(client: Any) -> None:
    """The lost update this prevents: an approve landing mid-flight keeps its `stage`."""
    plain = Store(client=client)
    _seed(plain)

    result = log_activity.handler(
        _event(), store=_WriteBeforeTransact(client=client, fields={"stage": "Closed Won"})
    )

    assert result["statusCode"] == 200, result
    after = _opportunity(plain)
    assert after is not None
    assert (after["stage"], after["tenant_stage"]) == ("Closed Won", "demo#Closed Won")
    assert after["last_touch"] == _OCCURRED


def test_an_opportunity_deleted_mid_flight_cancels_the_activity_too(client: Any) -> None:
    plain = Store(client=client)
    _seed(plain)

    result = log_activity.handler(_event(), store=_WriteBeforeTransact(client=client, fields=None))

    assert result["statusCode"] == 409
    assert _opportunity(plain) is None
    assert _activities(plain) == []


def test_an_opportunity_moved_to_another_tenant_mid_flight_is_not_touched(client: Any) -> None:
    plain = Store(client=client)
    _seed(plain)

    result = log_activity.handler(
        _event(), store=_WriteBeforeTransact(client=client, fields={"tenant": "eval-other"})
    )

    assert result["statusCode"] == 409
    after = _opportunity(plain)
    assert after is not None and "last_touch" not in after
    assert _activities(plain) == []


def test_an_unknown_opportunity_is_not_found(client: Any) -> None:
    result = log_activity.handler(_event(), store=Store(client=client))

    assert result["statusCode"] == 404


def test_a_retry_with_the_same_key_never_writes_a_second_activity(client: Any) -> None:
    """The activity id is derived from `transaction_key` and inserted only if absent."""
    from crm.handlers._common import idempotent_id

    store = Store(client=client)
    _seed(store)

    first = log_activity.handler(_event(), store=store)
    second = log_activity.handler(_event(), store=store)

    assert first["statusCode"] == 200, first
    assert first["body"]["activity"]["activity_id"] == idempotent_id("act", "tx-log")
    assert second["statusCode"] == 409
    assert [item["activity_id"] for item in _activities(store)] == [idempotent_id("act", "tx-log")]

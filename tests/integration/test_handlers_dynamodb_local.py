# SPDX-License-Identifier: MIT
"""Integration tests: action Lambda handlers against DynamoDB Local through their real entry point.

Covers `log_activity`'s atomic activity + `last_touch` write, caller-transaction-key idempotency,
and `Store.transact_write`'s table-prefix resolution under a non-empty `table_prefix`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import Conflict
from crm.handlers import create_account, create_opportunity, log_activity
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from tests.integration.conftest import _drop_prefixed_tables

_TENANT = "demo"
_REP = "rep-1"
_ACCOUNT = "acct-1"


def _run_scoped_key(name: str) -> str:
    """Return a random 36-character ``transaction_key``, unique per call.

    DynamoDB Local retains `ClientRequestToken`s across sessions, so a fixed token can collide with
    an earlier run's different request; 36 characters is the token's maximum length.
    """
    del name  # kept as a call-site self-documentation aid only; the token itself is opaque.
    return uuid.uuid4().hex + uuid.uuid4().hex[:4]


#: Unique per process, so concurrent xdist workers never share a table.
_TABLE_PREFIX = f"int-{uuid.uuid4().hex[:12]}-"


@pytest.fixture
def handler_store(dynamodb_local_client: Any) -> Iterator[Store]:
    """A :class:`Store` on DynamoDB Local with a non-empty ``table_prefix``, its own tables
    dropped after each test.
    """
    create_all_tables(dynamodb_local_client, table_prefix=_TABLE_PREFIX)
    try:
        yield Store(table_prefix=_TABLE_PREFIX, client=dynamodb_local_client)
    finally:
        _drop_prefixed_tables(dynamodb_local_client, _TABLE_PREFIX)


def _identity(*, rep_id: str = _REP, tenant: str = _TENANT) -> dict[str, Any]:
    # Account ids are generated per test, so the identity uses full visibility instead of a book.
    return {
        "rep_id": rep_id,
        "book": [],
        "full_visibility": True,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": tenant,
    }


def _event(args: dict[str, Any], **identity_overrides: Any) -> dict[str, Any]:
    return identity_event(IDENTITY_KEY, _identity(**identity_overrides), args)


def _create_account(store: Store, *, transaction_key: str | None = None) -> dict[str, Any]:
    result = create_account.handler(
        _event(
            {"name": "Acme", "transaction_key": transaction_key or _run_scoped_key("tx-account")}
        ),
        store=store,
    )
    assert result["statusCode"] == 200
    return dict(result["body"]["account"])


def _create_opportunity(
    store: Store, *, account_id: str, transaction_key: str | None = None
) -> dict[str, Any]:
    result = create_opportunity.handler(
        _event(
            {
                "account_id": account_id,
                "name": "Big Deal",
                "transaction_key": transaction_key or _run_scoped_key("tx-opp"),
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 200
    return dict(result["body"]["opportunity"])


# -- activity insert + parent last_touch update is atomic ----------------------------------------


def test_log_activity_commits_activity_and_last_touch_together_on_the_real_engine(
    handler_store: Store,
) -> None:
    """A successful `log_activity` persists the activity row and the opportunity's `last_touch`."""
    account = _create_account(handler_store)
    opportunity = _create_opportunity(handler_store, account_id=account["account_id"])
    assert opportunity["last_touch"] is None

    result = log_activity.handler(
        _event(
            {
                "opportunity_id": opportunity["opportunity_id"],
                "kind": "call",
                "subject": "Intro call",
                "occurred_at": "2026-01-01T00:00:00+00:00",
                "transaction_key": _run_scoped_key("tx-activity"),
            }
        ),
        store=handler_store,
    )
    assert result["statusCode"] == 200, result
    activity_id = result["body"]["activity"]["activity_id"]

    reread_activity = handler_store.get_item("activities", {"activity_id": activity_id})
    reread_opportunity = handler_store.get_item(
        "opportunities", {"opportunity_id": opportunity["opportunity_id"]}
    )
    assert reread_activity is not None
    assert reread_opportunity is not None
    assert reread_opportunity["last_touch"] == "2026-01-01T00:00:00+00:00"


def test_log_activity_writes_no_activity_row_when_its_target_opportunity_is_absent(
    handler_store: Store,
) -> None:
    """A `log_activity` on an absent opportunity returns 404 and writes no activity row."""
    account = _create_account(handler_store)
    opportunity = _create_opportunity(handler_store, account_id=account["account_id"])
    handler_store.delete_item("opportunities", {"opportunity_id": opportunity["opportunity_id"]})

    result = log_activity.handler(
        _event(
            {
                "opportunity_id": opportunity["opportunity_id"],
                "kind": "call",
                "subject": "Intro call",
                "transaction_key": _run_scoped_key("tx-activity-notfound"),
            }
        ),
        store=handler_store,
    )
    assert result["statusCode"] == 404

    scan = handler_store._client.scan(TableName=handler_store.physical_name("activities"))  # noqa: SLF001
    assert scan["Count"] == 0


def test_log_activity_transaction_is_genuinely_cancelled_by_a_real_condition_check_race(
    handler_store: Store,
) -> None:
    """`log_activity`'s transaction shape is cancelled whole when the opportunity is deleted first.

    The opportunity `Put` is conditioned on `attribute_exists(opportunity_id)`, and the engine's
    rejection leaves the activity row absent.
    """
    account = _create_account(handler_store)
    opportunity = _create_opportunity(handler_store, account_id=account["account_id"])

    from crm.handlers.wire import put_operation

    activity_item = {
        "activity_id": f"act-race-{uuid.uuid4()}",
        "tenant": _TENANT,
        "opportunity_id": opportunity["opportunity_id"],
        "account_id": account["account_id"],
        "kind": "call",
        "subject": "Racing call",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
        "created_by": _REP,
    }
    stale_opportunity = dict(opportunity)
    stale_opportunity["last_touch"] = "2026-01-01T00:00:00+00:00"

    # The opportunity is deleted between log_activity's read and its transaction.
    handler_store.delete_item("opportunities", {"opportunity_id": opportunity["opportunity_id"]})

    with pytest.raises(Conflict):
        handler_store.transact_write(
            [
                put_operation("activities", activity_item),
                {
                    "Put": {
                        "TableName": "opportunities",
                        "Item": {
                            k: {"S": str(v)} if not isinstance(v, bool) else {"BOOL": v}
                            for k, v in stale_opportunity.items()
                            if v is not None
                        },
                        "ConditionExpression": "attribute_exists(opportunity_id)",
                    }
                },
            ],
            transaction_key=_run_scoped_key("tx-race"),
        )

    assert (
        handler_store.get_item("activities", {"activity_id": activity_item["activity_id"]}) is None
    )


# -- caller-transaction-key idempotency holds under replay ----------------------------------------


def test_create_account_replay_with_same_transaction_key_is_a_real_no_op(
    handler_store: Store,
) -> None:
    """Two identical `create_account` calls with one transaction key persist exactly one account."""
    key = _run_scoped_key("tx-replay-noop")
    first = _create_account(handler_store, transaction_key=key)
    second = _create_account(handler_store, transaction_key=key)
    assert first["account_id"] == second["account_id"]

    scan = handler_store._client.scan(TableName=handler_store.physical_name("accounts"))  # noqa: SLF001
    assert scan["Count"] == 1


def test_create_account_replay_with_same_key_different_payload_is_refused_with_conflict(
    handler_store: Store,
) -> None:
    """A replayed ``transaction_key`` with a different payload is refused with `Conflict`/409."""
    key = _run_scoped_key("tx-replay-mismatch")
    first = _create_account(handler_store, transaction_key=key)
    result = create_account.handler(
        _event({"name": "A Different Name", "transaction_key": key}),
        store=handler_store,
    )
    assert result["statusCode"] == 409, result
    assert "error" in result

    # The original account is unchanged and no second row exists.
    unchanged = handler_store.get_item("accounts", {"account_id": first["account_id"]})
    assert unchanged is not None
    assert unchanged["name"] == "Acme"

    scan = handler_store._client.scan(TableName=handler_store.physical_name("accounts"))  # noqa: SLF001
    assert scan["Count"] == 1

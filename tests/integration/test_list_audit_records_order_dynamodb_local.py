# SPDX-License-Identifier: MIT
"""``list_audit_records`` pages newest-first, on DynamoDB Local.

With more audit records than one page holds, page 1 is the newest page's worth in descending
``created_at`` order and the cursor continues to the older remainder, both for the tenant-wide
listing and for one ``target_id``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import list_audit_records
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from tests.integration.conftest import _drop_prefixed_tables

_TENANT = "eval-audit-order"
#: Unique per process, so concurrent xdist workers never share a table.
_TABLE_PREFIX = f"int-audit-order-{uuid.uuid4().hex[:12]}-"
_RECORDS = 60
_PAGE = 50


@pytest.fixture
def store(dynamodb_local_client: Any) -> Iterator[Store]:
    """A prefixed :class:`Store` on DynamoDB Local with its own tables, dropped after each test."""
    create_all_tables(dynamodb_local_client, table_prefix=_TABLE_PREFIX)
    try:
        yield Store(table_prefix=_TABLE_PREFIX, client=dynamodb_local_client)
    finally:
        _drop_prefixed_tables(dynamodb_local_client, _TABLE_PREFIX)


def _seed(store: Store) -> list[str]:
    """Write :data:`_RECORDS` audit records one second apart; return their ids oldest-first."""
    ids = [f"aud-{n:03d}" for n in range(_RECORDS)]
    for n, audit_id in enumerate(ids):
        store.put_item(
            "audit_records",
            {
                "audit_id": audit_id,
                "tenant": _TENANT,
                "target_table": "accounts",
                "target_id": "acc-order",
                "actor_rep_id": "rep-order",
                "created_at": f"2026-10-14T00:{n // 60:02d}:{n % 60:02d}+00:00",
                "status": "active",
                "before_snapshot": {},
                "after_snapshot": {"account_id": "acc-order"},
                "account_id": "acc-order",
            },
        )
    return ids


def _page(store: Store, args: dict[str, Any]) -> dict[str, Any]:
    identity = {
        "rep_id": "rep-order",
        "book": [],
        "full_visibility": True,
        "principal_type": "user",
        "session_id": "sess-audit-order",
        "source": "gateway",
        "tenant": _TENANT,
    }
    result = list_audit_records.handler(identity_event(IDENTITY_KEY, identity, args), store=store)
    assert result["statusCode"] == 200, result
    return dict(result["body"])


@pytest.mark.parametrize("args", [{}, {"target_id": "acc-order"}], ids=["tenant", "target"])
def test_page_one_is_the_newest_records_and_the_cursor_pages_to_the_rest(
    store: Store, args: dict[str, Any]
) -> None:
    newest_first = list(reversed(_seed(store)))

    first = _page(store, args)
    assert [r["audit_id"] for r in first["audit_records"]] == newest_first[:_PAGE]
    assert first["next_cursor"] is not None

    second = _page(store, {**args, "cursor": first["next_cursor"]})
    assert [r["audit_id"] for r in second["audit_records"]] == newest_first[_PAGE:]

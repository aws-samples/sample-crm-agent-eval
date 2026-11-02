# SPDX-License-Identifier: MIT
"""Integration test: the `update_contact` departed refusal writes nothing on DynamoDB Local.

The handler runs through its real entry point against the real engine; the contact row's raw
attribute map and the tenant's `audit_records` count are compared before and after the refusal.
"""

from __future__ import annotations

import uuid
from typing import Any

from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import update_contact
from crm.store.store import Store

_TENANT = "demo"
_ACCOUNT = "acct-1"


def _event(args: dict[str, Any]) -> dict[str, Any]:
    identity = {
        "rep_id": "rep-1",
        "book": [_ACCOUNT],
        "full_visibility": False,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": _TENANT,
    }
    return identity_event(IDENTITY_KEY, identity, args)


def _raw_contact(client: Any, prefix: str, contact_id: str) -> dict[str, Any]:
    response = client.get_item(
        TableName=f"{prefix}contacts",
        Key={"contact_id": {"S": contact_id}},
        ConsistentRead=True,
    )
    item: dict[str, Any] = response["Item"]
    return item


def _audit_count(client: Any, prefix: str) -> int:
    count = 0
    kwargs: dict[str, Any] = {"TableName": f"{prefix}audit_records", "Select": "COUNT"}
    while True:
        page = client.scan(**kwargs)
        count += int(page["Count"])
        if "LastEvaluatedKey" not in page:
            return count
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _update(store: Store, contact_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = update_contact.handler(
        _event(
            {
                "contact_id": contact_id,
                "fields": fields,
                "transaction_key": uuid.uuid4().hex + uuid.uuid4().hex[:4],
            }
        ),
        store=store,
    )
    return result


def test_departed_refusal_leaves_contact_row_and_audit_count_unchanged(
    dynamodb_local_store: tuple[Store, str], dynamodb_local_client: Any
) -> None:
    """After `contact_departed` (no status, or status kept `departed`), the raw item and audit count
    are unchanged; reactivation then writes."""
    store, prefix = dynamodb_local_store
    contact_id = f"cont-{uuid.uuid4().hex[:8]}"
    store.put_item(
        "contacts",
        {
            "contact_id": contact_id,
            "tenant": _TENANT,
            "tenant_account_id": f"{_TENANT}#{_ACCOUNT}",
            "account_id": _ACCOUNT,
            "name": "Jane Doe",
            "name_lower": "jane doe",
            "status": "departed",
            "phone": "+1-555-010-0000",
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )
    row_before = _raw_contact(dynamodb_local_client, prefix, contact_id)
    audits_before = _audit_count(dynamodb_local_client, prefix)

    result = _update(store, contact_id, {"phone": "+1-555-010-9999"})

    assert result["statusCode"] == 409
    assert result["error"]["code"] == "contact_departed"
    assert result["error"]["missing_scope"] == "status:active"
    assert "detail" not in result["error"]
    assert _raw_contact(dynamodb_local_client, prefix, contact_id) == row_before
    assert _audit_count(dynamodb_local_client, prefix) == audits_before

    kept_departed = _update(store, contact_id, {"status": "departed", "phone": "+1-555-010-9999"})

    assert kept_departed["statusCode"] == 409
    assert kept_departed["error"]["code"] == "contact_departed"
    assert _raw_contact(dynamodb_local_client, prefix, contact_id) == row_before
    assert _audit_count(dynamodb_local_client, prefix) == audits_before

    reactivated = _update(store, contact_id, {"status": "active", "phone": "+1-555-010-9999"})

    assert reactivated["statusCode"] == 200, reactivated
    row_after = _raw_contact(dynamodb_local_client, prefix, contact_id)
    assert row_after["status"] == {"S": "active"}
    assert row_after["phone"] == {"S": "+1-555-010-9999"}
    assert _audit_count(dynamodb_local_client, prefix) == audits_before + 1

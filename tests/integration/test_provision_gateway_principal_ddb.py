# SPDX-License-Identifier: MIT
"""`scripts/provision_gateway_principal.py` against DynamoDB Local.

The book is compared with the accounts the real ``demo`` seed writes, and the item is resolved
through the Gateway interceptor's own default ``reps`` lookup (a `crm.store.Store` it builds itself).
"""

from __future__ import annotations

from typing import Any

import pytest
from scripts import provision_gateway_principal as gp

from crm.authz import DEMO_TENANT
from crm.store.store import Store
from eval.tenancy import seed_demo_tenant
from gateway.identity_interceptor import GatewayInterceptorConfig, _build_verified_identity

_ENDPOINT_URL = "http://127.0.0.1:8000"
_LOCAL_ENGINE_CRED = "local"


def _tenant_account_ids(store: Store) -> set[str]:
    ids: set[str] = set()
    cursor: dict[str, object] | None = None
    while True:
        items, cursor = store.query_tenant_scan(
            "accounts", tenant=DEMO_TENANT, exclusive_start_key=cursor
        )
        ids |= {str(item["account_id"]) for item in items}
        if cursor is None:
            return ids


def _config(prefix: str) -> GatewayInterceptorConfig:
    return GatewayInterceptorConfig(
        discovery_url="https://cognito-idp.us-east-1.amazonaws.com/pool/.well-known/openid-configuration",
        jwks_url="https://cognito-idp.us-east-1.amazonaws.com/pool/.well-known/jwks.json",
        allowed_client_ids=frozenset({"client"}),
        service_principal_rep_id=gp.GATEWAY_SERVICE_PRINCIPAL_REP_ID,
        table_prefix=prefix,
    )


@pytest.fixture
def local_default_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point a default `boto3.client("dynamodb")` at DynamoDB Local, with no ambient profile."""
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ENDPOINT_URL_DYNAMODB", _ENDPOINT_URL)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", _LOCAL_ENGINE_CRED)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", _LOCAL_ENGINE_CRED)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def test_the_book_equals_the_accounts_the_demo_seed_writes(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    store, _prefix = dynamodb_local_store
    seed_demo_tenant(store)
    seeded = _tenant_account_ids(store)
    assert seeded, "the demo seed wrote no account, so the comparison below would be vacuous"

    gp.provision_gateway_principal(store)

    item = store.get_item("reps", {"rep_id": gp.GATEWAY_SERVICE_PRINCIPAL_REP_ID}, consistent=True)
    assert item is not None
    assert item["book"] == sorted(seeded)
    assert item["tenant"] == DEMO_TENANT
    assert item["full_visibility"] is False
    assert item["status"] == "active"


def test_the_row_is_named_as_the_gateway_service_principal(
    dynamodb_local_store: tuple[Store, str], dynamodb_local_client: Any
) -> None:
    store, prefix = dynamodb_local_store
    gp.provision_gateway_principal(store)

    raw = dynamodb_local_client.get_item(
        TableName=f"{prefix}reps",
        Key={"rep_id": {"S": gp.GATEWAY_SERVICE_PRINCIPAL_REP_ID}},
        ConsistentRead=True,
    )["Item"]
    assert raw["name"] == {"S": "MCP gateway service principal"}


def test_a_rerun_leaves_the_stored_item_unchanged(
    dynamodb_local_store: tuple[Store, str], dynamodb_local_client: Any
) -> None:
    store, prefix = dynamodb_local_store
    gp.provision_gateway_principal(store)
    before = dynamodb_local_client.scan(TableName=f"{prefix}reps", ConsistentRead=True)["Items"]
    gp.provision_gateway_principal(store)
    after = dynamodb_local_client.scan(TableName=f"{prefix}reps", ConsistentRead=True)["Items"]
    assert after == before
    assert len(after) == 1


@pytest.mark.usefixtures("local_default_client")
def test_the_interceptor_default_lookup_resolves_the_written_book(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    store, prefix = dynamodb_local_store
    gp.provision_gateway_principal(store)

    identity = _build_verified_identity({}, session_id="s-1", config=_config(prefix))

    assert identity["rep_id"] == gp.GATEWAY_SERVICE_PRINCIPAL_REP_ID
    assert identity["principal_type"] == "service"
    assert identity["tenant"] == DEMO_TENANT
    assert identity["book"] == gp.demo_book()
    assert identity["full_visibility"] is False


@pytest.mark.usefixtures("local_default_client")
def test_non_vacuity_without_the_item_the_interceptor_establishes_no_identity(
    dynamodb_local_store: tuple[Store, str],
) -> None:
    _store, prefix = dynamodb_local_store

    identity = _build_verified_identity({}, session_id="s-1", config=_config(prefix))

    assert identity["rep_id"] == ""
    assert identity["book"] == []

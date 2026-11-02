# SPDX-License-Identifier: MIT
"""Shared fixtures for the DynamoDB Local integration tests.

Provides a `boto3` DynamoDB client against DynamoDB Local on loopback, with every table in
:mod:`crm.store.schema` created by :func:`crm.store.provisioning.create_all_tables`. If the engine
is unreachable, every test is skipped with a reason naming how to start it. The automatic start
goes through :func:`tests._dynamodb_local.ensure_started`, serialized across xdist workers.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import pytest

from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLE_NAMES
from crm.store.store import Store
from tests._dynamodb_local import ENDPOINT_URL, START_SCRIPT, ensure_started, start_lock_dir

_ENDPOINT_URL = ENDPOINT_URL
#: Placeholder credential DynamoDB Local accepts; not a secret.
_LOCAL_ENGINE_CRED = "local"

#: When set to any non-empty value, an unreachable DynamoDB Local engine fails instead of skipping.
_REQUIRE_ENGINE_ENV_VAR = "CRM_REQUIRE_DYNAMODB_LOCAL"


def _dynamodb_local_available(tmp_path_factory: pytest.TempPathFactory) -> tuple[bool, str]:
    """Return ``(available, reason)``. ``reason`` explains a ``False`` result for the skip message."""
    if ensure_started(start_lock_dir(tmp_path_factory)):
        return True, ""
    return False, (
        "DynamoDB Local is not reachable at "
        f"{_ENDPOINT_URL} and could not be started automatically. Run "
        f"'{START_SCRIPT} start' (requires Docker) before running these tests, or start "
        "DynamoDB Local yourself on this host/port."
    )


@pytest.fixture(scope="session")
def _dynamodb_local_endpoint(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Session-scoped gate: skip (or, under ``CRM_REQUIRE_DYNAMODB_LOCAL``, fail) every test in
    this package if the engine is unavailable.
    """
    available, reason = _dynamodb_local_available(tmp_path_factory)
    if not available:
        if os.environ.get(_REQUIRE_ENGINE_ENV_VAR):
            pytest.fail(
                f"{_REQUIRE_ENGINE_ENV_VAR} is set and {reason} "
                "The aggregate offline gate treats an absent engine as a failure, not a skip, so "
                "this layer's contracts (GSI projections, condition-check cancellation, "
                "transaction atomicity, table-prefix resolution) cannot be silently unverified."
            )
        pytest.skip(reason, allow_module_level=False)
    yield


@pytest.fixture
def dynamodb_local_client(_dynamodb_local_endpoint: None) -> Any:
    """A `boto3` DynamoDB client pointed at the real local engine, with no AWS credential.

    DynamoDB Local performs no IAM check, so the access key pair below is a fixed placeholder.
    """
    return boto3.client(
        "dynamodb",
        endpoint_url=_ENDPOINT_URL,
        region_name="us-east-1",
        aws_access_key_id=_LOCAL_ENGINE_CRED,
        aws_secret_access_key=_LOCAL_ENGINE_CRED,
    )


@pytest.fixture
def dynamodb_local_store(dynamodb_local_client: Any) -> Iterator[tuple[Store, str]]:
    """A :class:`Store` against DynamoDB Local, with every declared table freshly created.

    Each test gets an isolated table prefix, and every table created here is dropped afterward.

    Yields:
        A ``(store, table_prefix)`` pair; the prefix lets tests name physical tables in raw
        ``TransactWriteItems`` bodies.
    """
    prefix = f"t{uuid.uuid4().hex[:12]}_"
    create_all_tables(dynamodb_local_client, table_prefix=prefix)
    try:
        yield Store(table_prefix=prefix, client=dynamodb_local_client), prefix
    finally:
        _drop_prefixed_tables(dynamodb_local_client, prefix)


def _drop_prefixed_tables(client: Any, prefix: str) -> None:
    """Delete every table this fixture created, identified by its prefix."""
    for table in TABLE_NAMES:
        with contextlib.suppress(client.exceptions.ResourceNotFoundException):
            client.delete_table(TableName=f"{prefix}{table}")

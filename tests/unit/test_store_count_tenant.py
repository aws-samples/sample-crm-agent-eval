# SPDX-License-Identifier: MIT
"""``Store.count_tenant`` and the count-only ``demo`` snapshot path built on it.

The demo baseline and its ``demo_tenant_untouched`` re-read must reach DynamoDB only through
``Query`` with ``Select="COUNT"``, so no ``demo`` row attribute is ever returned to the sweep. One
test runs against DynamoDB Local (skipped without it, failed under ``CRM_REQUIRE_DYNAMODB_LOCAL``).
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator
from typing import Any, Final

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from crm.authz import DEMO_TENANT
from crm.errors import CrmError
from crm.store.provisioning import create_all_tables
from crm.store.schema import TABLE_NAMES
from crm.store.store import Store
from eval.harness import _fr93_delta_checks, _read_demo_baseline
from eval.tenant_guard import (
    WATCHED_TABLES,
    TenantSnapshot,
    demo_tenant_untouched_check,
    read_tenant_counts,
    store_count_reader,
)
from tests._dynamodb_local import ENDPOINT_URL, START_SCRIPT, ensure_started, start_lock_dir

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_OTHER_TENANT: Final[str] = "eval-count-other"
_NOW: Final[str] = "2026-01-01T00:00:00Z"
_REQUIRE_ENGINE_ENV_VAR: Final[str] = "CRM_REQUIRE_DYNAMODB_LOCAL"
#: Placeholder credential DynamoDB Local accepts; not a secret.
_LOCAL_ENGINE_CRED: Final[str] = "local"


class _ScriptedQueryClient:
    """A client whose ``query`` returns scripted pages and records every request."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = list(pages)
        self.requests: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return self._pages.pop(0)


class _RecordingClient:
    """A real client wrapped to record every ``query`` and ``scan`` request it serves."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.queries: list[dict[str, Any]] = []
        self.scans: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> Any:
        self.queries.append(kwargs)
        return self.inner.query(**kwargs)

    def scan(self, **kwargs: Any) -> Any:
        self.scans.append(kwargs)
        return self.inner.scan(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _note(note_id: str, tenant: str) -> dict[str, object]:
    return {
        "note_id": note_id,
        "tenant": tenant,
        "tenant_opportunity_id": f"{tenant}#opp-1",
        "opportunity_id": "opp-1",
        "body": "private text",
        "created_at": _NOW,
        "created_by": "rep-1",
        "schema_version": 1,
    }


def _queries_for(client: _RecordingClient, tenant: str) -> list[dict[str, Any]]:
    return [
        request
        for request in client.queries
        if request.get("ExpressionAttributeValues", {}).get(":pk") == {"S": tenant}
    ]


@pytest.fixture
def recording() -> Iterator[tuple[Store, _RecordingClient]]:
    """A Store over a moto client with every table, wrapped to record queries."""
    with mock_aws():
        inner = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(inner)
        client = _RecordingClient(inner)
        yield Store(client=client), client


def test_count_tenant_sums_count_across_pages_with_select_count() -> None:
    """Every page is a ``Select=COUNT`` query on ``gsi_tenant``; the cursor is forwarded."""
    cursor = {"note_id": {"S": "note-2"}}
    client = _ScriptedQueryClient(
        [
            {"Count": 2, "LastEvaluatedKey": cursor},
            {"Count": 3, "LastEvaluatedKey": cursor},
            {"Count": 1},
        ]
    )
    store = Store(client=client)

    assert store.count_tenant("notes", tenant=DEMO_TENANT) == 6
    assert len(client.requests) == 3
    assert {request["Select"] for request in client.requests} == {"COUNT"}
    assert {request["IndexName"] for request in client.requests} == {"gsi_tenant"}
    assert "ExclusiveStartKey" not in client.requests[0]
    assert all(request["ExclusiveStartKey"] == cursor for request in client.requests[1:])
    assert all(":pk" in request["ExpressionAttributeValues"] for request in client.requests)


def test_count_tenant_wraps_a_client_error_in_the_taxonomy() -> None:
    """An ``AccessDeniedException`` surfaces as a taxonomy member, never a raw botocore error."""

    class _Denying:
        def query(self, **kwargs: Any) -> Any:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "Query"
            )

    with pytest.raises(CrmError):
        Store(client=_Denying()).count_tenant("notes", tenant=DEMO_TENANT)


def test_count_tenant_refuses_a_table_without_a_tenant_index() -> None:
    """``eval_runs`` has no ``gsi_tenant``; asking to count it is a programming error."""
    with pytest.raises(ValueError, match="no tenant-wide scan index"):
        Store(client=_ScriptedQueryClient([])).count_tenant("eval_runs", tenant=DEMO_TENANT)


def test_count_tenant_counts_only_the_named_tenant(
    recording: tuple[Store, _RecordingClient],
) -> None:
    """Rows under another tenant are not counted."""
    store, _ = recording
    for index in range(3):
        store.put_item("notes", _note(f"note-d{index}", DEMO_TENANT))
    store.put_item("notes", _note("note-o", _OTHER_TENANT))
    assert store.count_tenant("notes", tenant=DEMO_TENANT) == 3
    assert store.count_tenant("notes", tenant=_OTHER_TENANT) == 1


def test_demo_baseline_and_recheck_issue_only_count_queries(
    recording: tuple[Store, _RecordingClient],
) -> None:
    """The demo snapshot and its untouched re-read query every watched table, each ``Select=COUNT``."""
    store, client = recording
    store.put_item("notes", _note("note-d", DEMO_TENANT))
    store.put_item("notes", _note("note-o", _OTHER_TENANT))

    before = _read_demo_baseline(store)
    baseline_queries = _queries_for(client, DEMO_TENANT)
    checks = _fr93_delta_checks(store, tenant=_OTHER_TENANT, before=before)

    assert before.counts is not None and before.counts["notes"] == 1
    demo_check = next(check for check in checks if check.check == "demo_tenant_untouched")
    assert demo_check.status == "pass", demo_check.detail
    demo_queries = _queries_for(client, DEMO_TENANT)
    assert len(baseline_queries) == len(WATCHED_TABLES)
    assert len(demo_queries) == 2 * len(WATCHED_TABLES)
    assert [request.get("Select") for request in demo_queries] == ["COUNT"] * len(demo_queries)
    assert client.scans == []


def test_demo_recheck_by_count_fails_on_a_changed_count(
    recording: tuple[Store, _RecordingClient],
) -> None:
    """A demo row written after the baseline turns the count-based check red."""
    store, _ = recording
    before = read_tenant_counts(store_count_reader(store), DEMO_TENANT)
    store.put_item("notes", _note("note-late", DEMO_TENANT))
    result = demo_tenant_untouched_check(
        resource="test", before=before, counter=store_count_reader(store)
    )
    assert result.status == "fail"
    assert "notes" in result.detail


def test_demo_recheck_by_count_fails_without_a_baseline(
    recording: tuple[Store, _RecordingClient],
) -> None:
    """A never-taken before-snapshot fails, even though the demo tenant is unchanged."""
    store, _ = recording
    result = demo_tenant_untouched_check(
        resource="test",
        before=TenantSnapshot(counts=None, error="baseline unreadable"),
        counter=store_count_reader(store),
    )
    assert result.status == "fail"
    assert "never read" in result.detail


def test_count_reader_failure_is_recorded_not_raised() -> None:
    """A count read that raises yields an unobserved snapshot naming the exception."""

    def _failing(table: str, tenant: str) -> int:
        raise RuntimeError("denied")

    snapshot = read_tenant_counts(_failing, DEMO_TENANT)
    assert snapshot.observed is False
    assert "RuntimeError: denied" in snapshot.error


def test_demo_check_requires_exactly_one_reader() -> None:
    """Neither, or both, of ``reader`` and ``counter`` is a programming error."""
    before = TenantSnapshot(counts={})
    with pytest.raises(ValueError, match="one of reader or counter"):
        demo_tenant_untouched_check(resource="test", before=before)
    with pytest.raises(ValueError, match="not both"):
        demo_tenant_untouched_check(
            resource="test", before=before, reader=lambda _t, _n: [], counter=lambda _t, _n: 0
        )


@pytest.fixture
def local_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Store]:
    """A Store against DynamoDB Local with every table created under a fresh prefix."""
    if not ensure_started(start_lock_dir(tmp_path_factory)):
        reason = f"DynamoDB Local is not reachable at {ENDPOINT_URL}; run '{START_SCRIPT} start'"
        if os.environ.get(_REQUIRE_ENGINE_ENV_VAR):
            pytest.fail(f"{_REQUIRE_ENGINE_ENV_VAR} is set and {reason}")
        pytest.skip(reason)
    client = boto3.client(
        "dynamodb",
        endpoint_url=ENDPOINT_URL,
        region_name="us-east-1",
        aws_access_key_id=_LOCAL_ENGINE_CRED,
        aws_secret_access_key=_LOCAL_ENGINE_CRED,
    )
    prefix = f"t{uuid.uuid4().hex[:12]}_"
    create_all_tables(client, table_prefix=prefix)
    try:
        yield Store(table_prefix=prefix, client=client)
    finally:
        for table in TABLE_NAMES:
            with contextlib.suppress(client.exceptions.ResourceNotFoundException):
                client.delete_table(TableName=f"{prefix}{table}")


def test_count_equals_row_count_on_dynamodb_local(local_store: Store) -> None:
    """Against the real engine, the count equals the number of rows the row query returns."""
    for index in range(7):
        local_store.put_item("notes", _note(f"note-d{index}", DEMO_TENANT))
    local_store.put_item("notes", _note("note-o", _OTHER_TENANT))

    rows: list[dict[str, object]] = []
    cursor: dict[str, object] | None = None
    while True:
        page, cursor = local_store.query_tenant_scan(
            "notes", tenant=DEMO_TENANT, exclusive_start_key=cursor
        )
        rows.extend(page)
        if cursor is None:
            break

    assert local_store.count_tenant("notes", tenant=DEMO_TENANT) == len(rows) == 7
    assert local_store.count_tenant("notes", tenant=_OTHER_TENANT) == 1
    assert local_store.count_tenant("activities", tenant=DEMO_TENANT) == 0

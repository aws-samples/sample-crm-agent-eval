# SPDX-License-Identifier: MIT
"""``pipeline_aggregate`` examines at most the search budget and batch-fetches bodies.

Runs the handler against a ``moto``-mocked DynamoDB store. The demo-tenant cases pin the totals
the handler produced before the budget and batching were introduced, as sha256 digests of the
canonical JSON body without ``truncated``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import boto3
import pytest
from fixtures.generate import seed_tenant
from moto import mock_aws

from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import pipeline_aggregate
from crm.search import EXAMINATION_BUDGET
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_DEMO_BOOK = [f"acct-demo-{index:04d}" for index in range(7)]

#: Digest of each demo-tenant case's body (``truncated`` removed) as produced before the change.
_PRE_CHANGE_BODY_SHA256 = {
    "book": "2f16da3b968fcc006bc4e2537002357671541aff059f87b3bf27b38a4bbb8466",
    "full": "77c814dec39c21f38a2a28881ca7df1cb4086be8268514572db9b370d84f5021",
    "full_closed": "dadb8885813d27d7b36d43506930ed0a40ddd7036a75381c7177391ee5d31529",
    "full_window": "a39e44462a339ead5eef671cc49940f712228023ee83e8dcd32791003964285e",
}

_CASES: dict[str, tuple[bool, list[str], dict[str, Any]]] = {
    "book": (False, _DEMO_BOOK, {}),
    "full": (True, [], {}),
    "full_closed": (True, [], {"include_closed": True}),
    "full_window": (True, [], {"close_after": "2026-01-01", "close_before": "2026-06-30"}),
}


class RecordingStore(Store):
    """A real :class:`Store` that records tenant-scan page sizes and batch-get key counts, and
    refuses any single-item read."""

    def __init__(self, client: Any) -> None:
        super().__init__(client=client)
        self.scan_pages: list[int] = []
        self.batch_key_counts: list[int] = []

    def query_tenant_scan(
        self, table: str, **kwargs: Any
    ) -> tuple[list[dict[str, object]], dict[str, object] | None]:
        page, cursor = super().query_tenant_scan(table, **kwargs)
        self.scan_pages.append(len(page))
        return page, cursor

    def batch_get_items(
        self, table: str, keys: Sequence[Mapping[str, str]]
    ) -> list[dict[str, object]]:
        self.batch_key_counts.append(len(keys))
        return super().batch_get_items(table, keys)

    def get_item(self, *args: Any, **kwargs: Any) -> dict[str, object] | None:
        raise AssertionError("pipeline_aggregate must not read opportunities one at a time")


@pytest.fixture
def store() -> Iterator[RecordingStore]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield RecordingStore(client)


def _event(*, full: bool, book: list[str], args: dict[str, Any]) -> dict[str, Any]:
    identity = {
        "rep_id": "rep-x",
        "book": book,
        "full_visibility": full,
        "principal_type": "user",
        "session_id": "s",
        "source": "gateway",
        "tenant": _TENANT,
    }
    return identity_event(IDENTITY_KEY, identity, args)


def _seed_opportunities(store: Store, count: int) -> None:
    for index in range(count):
        store.put_item(
            "opportunities",
            {
                "opportunity_id": f"opp-{index:03d}",
                "tenant": _TENANT,
                "tenant_account_id": f"{_TENANT}#acct-1",
                "tenant_owner_rep_id": f"{_TENANT}#rep-1",
                "tenant_stage": f"{_TENANT}#Discovery",
                "account_id": "acct-1",
                "owner_rep_id": "rep-1",
                "name": "Deal",
                "name_lower": "deal",
                "stage": "Discovery",
                "value": 100,
                "close_date": "2026-03-01",
            },
        )


def _call(store: Store, **kwargs: Any) -> dict[str, Any]:
    result = pipeline_aggregate.handler(_event(**kwargs), store=store)
    assert result["statusCode"] == 200, result
    body: dict[str, Any] = result["body"]
    return body


def test_demo_tenant_totals_equal_the_pre_change_totals(store: RecordingStore) -> None:
    seed_tenant(store, tenant=_TENANT)
    for name, (full, book, args) in _CASES.items():
        body = _call(store, full=full, book=book, args=args)
        assert body.pop("truncated") is False, name
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
        assert digest == _PRE_CHANGE_BODY_SHA256[name], name


def test_bodies_are_batch_fetched_never_read_one_at_a_time(store: RecordingStore) -> None:
    _seed_opportunities(store, 30)
    body = _call(store, full=True, book=[], args={})
    assert body["stage_funnel"]["Discovery"] == 30
    assert body["value_rollup"] == 3000
    assert store.batch_key_counts == [30]


def test_examination_stops_at_the_budget_and_reports_truncation(
    store: RecordingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_opportunities(store, 8)
    monkeypatch.setattr(pipeline_aggregate, "EXAMINATION_BUDGET", 5)
    body = _call(store, full=True, book=[], args={})
    assert body["truncated"] is True
    assert sum(store.scan_pages) == 5
    assert body["stage_funnel"]["Discovery"] == 5
    assert body["value_rollup"] == 500


def test_an_examination_within_the_budget_is_not_truncated(
    store: RecordingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_opportunities(store, 8)
    monkeypatch.setattr(pipeline_aggregate, "EXAMINATION_BUDGET", 9)
    body = _call(store, full=True, book=[], args={})
    assert body["truncated"] is False
    assert body["stage_funnel"]["Discovery"] == 8


def test_out_of_scope_rows_count_against_the_budget_but_are_not_fetched(
    store: RecordingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_opportunities(store, 6)
    monkeypatch.setattr(pipeline_aggregate, "EXAMINATION_BUDGET", 4)
    body = _call(store, full=False, book=["acct-other"], args={})
    assert body["truncated"] is True
    assert sum(store.scan_pages) == 4
    assert body["value_rollup"] == 0
    assert store.batch_key_counts == [0]


def test_the_handler_uses_the_search_budget() -> None:
    assert vars(pipeline_aggregate)["EXAMINATION_BUDGET"] == EXAMINATION_BUDGET == 2000

# SPDX-License-Identifier: MIT
"""Unit tests for ``Store.batch_put_items`` and the batched ``fixtures.generate.seed_tenant``.

A recording stub client stands in for DynamoDB, so each test reads exactly which requests were
issued.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from fixtures.generate import generate_dataset, seed_tenant

import crm.store.store as store_module
from crm.errors import CrmError, ServiceBusy
from crm.store.keys import derive, derived_attribute_names
from crm.store.store import Store

_TENANT = "eval-batchput-0001"
_DESERIALIZER = TypeDeserializer()


class _RecordingClient:
    """Records every ``batch_write_item`` call; leaves the first ``unprocessed_rounds`` partly undone.

    ``put_item`` fails the test outright, so a seeding path that falls back to it is caught.
    """

    def __init__(self, *, unprocessed_rounds: int = 0) -> None:
        self.batch_calls: list[dict[str, list[dict[str, Any]]]] = []
        self._unprocessed_rounds = unprocessed_rounds

    def batch_write_item(self, *, RequestItems: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:  # noqa: N803
        self.batch_calls.append(RequestItems)
        if self._unprocessed_rounds > 0:
            self._unprocessed_rounds -= 1
            table, entries = next(iter(RequestItems.items()))
            return {"UnprocessedItems": {table: entries[:1]}}
        return {"UnprocessedItems": {}}

    def put_item(self, **_: object) -> None:
        raise AssertionError("seeding must not issue put_item")


class _FailingClient:
    def batch_write_item(self, **_: object) -> dict[str, Any]:
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}},
            "BatchWriteItem",
        )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr("crm.store.store.time.sleep", delays.append)
    return delays


def _store(client: object) -> Store:
    return Store(table_prefix="t-", client=client)


def _written_items(client: _RecordingClient, physical: str) -> list[dict[str, Any]]:
    """Every item put into ``physical``, decoded, in request order."""
    return [
        {
            key: _DESERIALIZER.deserialize(value)
            for key, value in entry["PutRequest"]["Item"].items()
        }
        for call in client.batch_calls
        for entry in call.get(physical, [])
    ]


def _call_multiset(client: _RecordingClient) -> list[str]:
    """Every ``batch_write_item`` request, canonicalised and sorted, so call order is ignored."""
    return sorted(json.dumps(call, sort_keys=True) for call in client.batch_calls)


def _account(account_id: str) -> dict[str, object]:
    return {"tenant": _TENANT, "account_id": account_id, "owner_rep_id": "rep-1", "name": "A"}


def test_seed_tenant_issues_one_batch_call_per_25_items_per_table_and_no_put_item() -> None:
    client = _RecordingClient()
    seed_tenant(_store(client), tenant=_TENANT)

    corpus = generate_dataset(_TENANT).as_mapping()
    assert len(client.batch_calls) == sum(math.ceil(len(items) / 25) for items in corpus.values())
    for call in client.batch_calls:
        ((_table, entries),) = call.items()
        assert 1 <= len(entries) <= 25
        assert all(set(entry) == {"PutRequest"} for entry in entries)
    for table, items in corpus.items():
        assert len(_written_items(client, f"t-{table}")) == len(items), table


def test_seed_tenant_writes_every_item_with_its_derived_index_attributes() -> None:
    client = _RecordingClient()
    seed_tenant(_store(client), tenant=_TENANT)

    for table, items in generate_dataset(_TENANT).as_mapping().items():
        written = _written_items(client, f"t-{table}")
        assert len(written) == len(items), table
        for sent in written:
            recomputed = derive(table, sent)
            present = derived_attribute_names(table) & set(recomputed)
            assert present, f"{table}: no derived attribute was written"
            assert {name: sent.get(name) for name in present} == {
                name: recomputed[name] for name in present
            }, table


def test_a_second_seed_sends_identical_items() -> None:
    first, second = _RecordingClient(), _RecordingClient()
    seed_tenant(_store(first), tenant=_TENANT)
    seed_tenant(_store(second), tenant=_TENANT)
    assert _call_multiset(first) == _call_multiset(second)
    assert first.batch_calls


def test_batch_put_items_returns_the_count_and_chunks_at_25() -> None:
    client = _RecordingClient()
    items = [_account(f"acc-{n:03d}") for n in range(51)]
    assert _store(client).batch_put_items("accounts", items) == 51
    assert [len(call["t-accounts"]) for call in client.batch_calls] == [25, 25, 1]


def test_batch_put_items_applies_derive_to_each_item() -> None:
    client = _RecordingClient()
    _store(client).batch_put_items("accounts", [_account("acc-1")])
    (sent,) = _written_items(client, "t-accounts")
    expected = derive("accounts", _account("acc-1"))
    assert sent == expected
    assert derived_attribute_names("accounts") & set(sent), "accounts declares no derived field"


def test_an_empty_batch_issues_no_call() -> None:
    client = _RecordingClient()
    assert _store(client).batch_put_items("accounts", []) == 0
    assert client.batch_calls == []


def test_unprocessed_items_are_resubmitted_with_growing_backoff(_no_sleep: list[float]) -> None:
    client = _RecordingClient(unprocessed_rounds=2)
    assert _store(client).batch_put_items("accounts", [_account("a"), _account("b")]) == 2
    assert len(client.batch_calls) == 3
    assert len(client.batch_calls[1]["t-accounts"]) == 1
    assert len(_no_sleep) == 2
    assert _no_sleep[1] > _no_sleep[0] > 0


def test_items_still_unprocessed_after_the_last_attempt_raise_service_busy(
    _no_sleep: list[float],
) -> None:
    client = _RecordingClient(unprocessed_rounds=10_000)
    with pytest.raises(ServiceBusy) as exc_info:
        _store(client).batch_put_items("accounts", [_account("a")])
    assert len(client.batch_calls) == store_module._BATCH_PUT_MAX_ATTEMPTS
    assert len(_no_sleep) == store_module._BATCH_PUT_MAX_ATTEMPTS - 1
    assert "batch_put_items:accounts" in (exc_info.value.detail or "")


def test_a_duplicate_primary_key_raises_before_any_write() -> None:
    client = _RecordingClient()
    with pytest.raises(ValueError, match="duplicate primary key"):
        _store(client).batch_put_items("accounts", [_account("dup"), _account("dup")])
    assert client.batch_calls == []


def test_an_item_without_its_primary_key_raises_before_any_write() -> None:
    client = _RecordingClient()
    item: Mapping[str, object] = {"tenant": _TENANT, "name": "no id"}
    with pytest.raises(ValueError, match="lacks key attribute"):
        _store(client).batch_put_items("accounts", [item])
    assert client.batch_calls == []


def test_a_client_error_becomes_a_taxonomy_error() -> None:
    with pytest.raises(CrmError) as exc_info:
        _store(_FailingClient()).batch_put_items("accounts", [_account("a")])
    assert not isinstance(exc_info.value, ClientError)
    assert isinstance(exc_info.value.__cause__, ClientError)


def test_an_unknown_table_is_refused() -> None:
    with pytest.raises(ValueError, match="unrecognised table"):
        _store(_RecordingClient()).batch_put_items("nope", [_account("a")])

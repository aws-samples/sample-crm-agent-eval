# SPDX-License-Identifier: MIT
"""Client-side SDK failures on every Store write path, and BatchGetItem no-progress, against moto.

A transport fault (unreachable endpoint, connect or read timeout) becomes a retryable
:class:`crm.errors.ServiceBusy`; any other SDK-side failure becomes an internal ``CrmError``. The
exception text never reaches the user-facing message.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import (
    BotoCoreError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ParamValidationError,
    ReadTimeoutError,
)
from moto import mock_aws

from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import CrmError, ServiceBusy
from crm.handlers import update_account
from crm.store.keys import derive
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_ENDPOINT = "https://dynamodb.us-east-1.amazonaws.com"
_SECRET_TEXT = "internal-endpoint-detail"  # nosec B105  # a redaction sentinel, not a credential
_TENANT = "demo"
_ACCOUNT = "acct-1"

_TRANSPORT_FAULTS: dict[str, Callable[[], BotoCoreError]] = {
    "endpoint_connection": lambda: EndpointConnectionError(endpoint_url=_ENDPOINT),
    "connect_timeout": lambda: ConnectTimeoutError(endpoint_url=_ENDPOINT),
    "read_timeout": lambda: ReadTimeoutError(endpoint_url=_ENDPOINT),
}


class _FaultyClient:
    """Delegates to a real client, raising ``fault()`` from the named operations."""

    def __init__(self, inner: Any, failing: set[str], fault: Callable[[], BotoCoreError]) -> None:
        self._inner = inner
        self._failing = failing
        self._fault = fault

    def __getattr__(self, name: str) -> Any:
        if name in self._failing:

            def _raise(**_kwargs: Any) -> Any:
                raise self._fault()

            return _raise
        return getattr(self._inner, name)


@pytest.fixture
def client() -> Iterator[Any]:
    with mock_aws():
        real = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(real)
        Store(client=real).put_item(
            "accounts",
            derive(
                "accounts",
                {"account_id": _ACCOUNT, "tenant": _TENANT, "owner_rep_id": "rep-1", "name": "A"},
            ),
        )
        yield real


def _faulty(client: Any, operations: set[str], fault: Callable[[], BotoCoreError]) -> Store:
    return Store(client=_FaultyClient(client, operations, fault))


def _account(**extra: object) -> dict[str, object]:
    return {
        "account_id": _ACCOUNT,
        "tenant": _TENANT,
        "owner_rep_id": "rep-1",
        "name": "A",
        **extra,
    }


_WRITE_CALLS: dict[str, tuple[set[str], Callable[[Store], object]]] = {
    "put_item": ({"put_item"}, lambda s: s.put_item("accounts", _account())),
    "put_if_absent": ({"put_item"}, lambda s: s.put_if_absent("accounts", _account())),
    "put_idempotent": (
        {"put_item"},
        lambda s: s.put_idempotent("accounts", _account(name="B"), transaction_key="tx-1"),
    ),
    "delete_item": ({"delete_item"}, lambda s: s.delete_item("accounts", {"account_id": _ACCOUNT})),
    "update_item": (
        {"update_item"},
        lambda s: s.update_item(
            "accounts", {"account_id": _ACCOUNT}, set_values={"industry": "Retail"}, condition=None
        ),
    ),
    "transact_write": (
        {"transact_write_items"},
        lambda s: s.transact_write(
            [{"Put": {"TableName": "accounts", "Item": {"account_id": {"S": "acct-9"}}}}]
        ),
    ),
    "batch_put_items": (
        {"batch_write_item"},
        lambda s: s.batch_put_items("accounts", [_account(account_id="acct-2")]),
    ),
    "delete_tenant": ({"batch_write_item"}, lambda s: s.delete_tenant(_TENANT)),
}


@pytest.mark.parametrize("fault_name", sorted(_TRANSPORT_FAULTS))
@pytest.mark.parametrize("call_name", sorted(_WRITE_CALLS))
def test_a_transport_fault_on_a_write_path_is_service_busy(
    client: Any, call_name: str, fault_name: str
) -> None:
    operations, call = _WRITE_CALLS[call_name]
    store = _faulty(client, operations, _TRANSPORT_FAULTS[fault_name])

    with pytest.raises(ServiceBusy) as raised:
        call(store)

    assert raised.value.next_step == "retry_later"
    assert _ENDPOINT not in str(raised.value)
    assert raised.value.detail is not None and call_name.split("_")[0] in raised.value.detail


@pytest.mark.parametrize("call_name", sorted(_WRITE_CALLS))
def test_a_non_transport_sdk_failure_on_a_write_path_is_internal(
    client: Any, call_name: str
) -> None:
    operations, call = _WRITE_CALLS[call_name]
    store = _faulty(client, operations, lambda: ParamValidationError(report=_SECRET_TEXT))

    with pytest.raises(CrmError) as raised:
        call(store)

    assert not isinstance(raised.value, ServiceBusy)
    assert raised.value.code == "internal_error"
    assert _SECRET_TEXT not in str(raised.value)


def test_an_update_handler_returns_a_retryable_error_on_a_connection_fault(client: Any) -> None:
    store = _faulty(
        client,
        {"put_item", "update_item", "transact_write_items"},
        _TRANSPORT_FAULTS["endpoint_connection"],
    )
    event = identity_event(
        IDENTITY_KEY,
        {
            "rep_id": "rep-1",
            "book": [_ACCOUNT],
            "full_visibility": False,
            "principal_type": "user",
            "session_id": "sess-1",
            "source": "gateway",
            "tenant": _TENANT,
        },
        {"account_id": _ACCOUNT, "fields": {"industry": "Retail"}, "transaction_key": "tx-h"},
    )

    result = update_account.handler(event, store=store)

    assert result["statusCode"] == ServiceBusy.http_status, result
    assert result["error"]["next_step"] == "retry_later", result
    assert _ENDPOINT not in str(result)


# --- BatchGetItem no-progress ---------------------------------------------------------------


class _UnprocessedBatchGet:
    """Returns ``unprocessed_per_call[i]`` of the requested keys as unprocessed on call ``i``."""

    def __init__(self, inner: Any, unprocessed_per_call: list[int | None]) -> None:
        self._inner = inner
        self._plan = list(unprocessed_per_call)
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def batch_get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        ((physical, request),) = kwargs["RequestItems"].items()
        keys = request["Keys"]
        hold = self._plan.pop(0) if self._plan else 0
        hold = len(keys) if hold is None else hold
        served = (
            self._inner.batch_get_item(RequestItems={physical: {"Keys": keys[hold:]}})
            if (hold < len(keys))
            else {"Responses": {physical: []}}
        )
        unprocessed = {physical: {"Keys": keys[:hold]}} if hold else {}
        return {"Responses": served.get("Responses", {}), "UnprocessedKeys": unprocessed}


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr("crm.store.store.time.sleep", recorded.append)
    return recorded


def _seed_accounts(client: Any, count: int) -> list[dict[str, str]]:
    store = Store(client=client)
    ids = [f"acct-b{index}" for index in range(count)]
    store.batch_put_items("accounts", [_account(account_id=i) for i in ids])
    return [{"account_id": i} for i in ids]


def test_batch_get_with_no_progress_is_service_busy_after_backoff(
    client: Any, sleeps: list[float]
) -> None:
    keys = _seed_accounts(client, 3)
    fake = _UnprocessedBatchGet(client, [None] * 50)

    with pytest.raises(ServiceBusy) as raised:
        Store(client=fake).batch_get_items("accounts", keys)

    assert "batch_get_items" not in str(raised.value)
    assert raised.value.detail is not None and "batch_get_items:accounts" in raised.value.detail
    assert fake.calls > 1
    assert sleeps and sleeps == sorted(sleeps) and sleeps[-1] > sleeps[0]


def test_batch_get_that_stalls_then_progresses_returns_every_item(
    client: Any, sleeps: list[float]
) -> None:
    keys = _seed_accounts(client, 4)
    fake = _UnprocessedBatchGet(client, [None, None, 2, 0])

    items = Store(client=fake).batch_get_items("accounts", keys)

    assert sorted(str(item["account_id"]) for item in items) == sorted(
        k["account_id"] for k in keys
    )
    assert len(sleeps) == 2


def test_batch_get_making_partial_progress_never_sleeps(client: Any, sleeps: list[float]) -> None:
    keys = _seed_accounts(client, 4)
    fake = _UnprocessedBatchGet(client, [3, 2, 1, 0])

    items = Store(client=fake).batch_get_items("accounts", keys)

    assert len(items) == 4
    assert sleeps == []

# SPDX-License-Identifier: MIT
"""What the audited create paths send to the store, recorded against a ``moto`` DynamoDB.

``create_idempotent`` sends no ``ClientRequestToken`` and re-reads consistently after a cancelled
write; ``log_activity`` reads the opportunity whose ``updated_at`` it conditions on consistently.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.envelope import IDENTITY_KEY, identity_event
from crm.errors import Conflict
from crm.handlers import create_account, log_activity
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TENANT = "demo"
_REP = "rep-1"


class _RecordingStore(Store):
    """A real store that records each ``get_item``'s ``consistent`` flag and each transaction's key."""

    def __init__(self, client: Any) -> None:
        super().__init__(client=client)
        self.reads: list[tuple[str, bool]] = []
        self.transaction_keys: list[str | None] = []
        self.cancel_next_transaction = False

    def get_item(
        self, table: str, key: Mapping[str, str], *, consistent: bool = False
    ) -> dict[str, object] | None:
        self.reads.append((table, consistent))
        return super().get_item(table, key, consistent=consistent)

    def transact_write(self, operations: Any, *, transaction_key: str | None = None) -> None:
        self.transaction_keys.append(transaction_key)
        if self.cancel_next_transaction:
            self.cancel_next_transaction = False
            raise Conflict("The operation conflicted with a concurrent change.")
        super().transact_write(operations, transaction_key=transaction_key)


@pytest.fixture
def store() -> Iterator[_RecordingStore]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield _RecordingStore(client)


def _event(args: dict[str, Any]) -> dict[str, Any]:
    identity = {
        "rep_id": _REP,
        "book": [],
        "full_visibility": True,
        "principal_type": "user",
        "session_id": "sess-1",
        "source": "gateway",
        "tenant": _TENANT,
    }
    return identity_event(IDENTITY_KEY, identity, args)


def _create_account(store: Store, key: str) -> dict[str, Any]:
    result = create_account.handler(_event({"name": "Acme", "transaction_key": key}), store=store)
    assert result["statusCode"] == 200, result
    return dict(result["body"]["account"])


def test_create_idempotent_sends_no_client_request_token(store: _RecordingStore) -> None:
    _create_account(store, "tx-no-token-1")
    assert store.transaction_keys == [None]


def test_the_re_read_after_a_cancelled_create_is_consistent(store: _RecordingStore) -> None:
    store.cancel_next_transaction = True
    result = create_account.handler(
        _event({"name": "Acme", "transaction_key": "tx-cancelled-1"}), store=store
    )
    assert result["statusCode"] == 409, result
    assert store.reads == [("accounts", False), ("accounts", True)]


def test_log_activity_reads_the_opportunity_it_conditions_on_consistently(
    store: _RecordingStore,
) -> None:
    store.put_item(
        "opportunities",
        {
            "opportunity_id": "opp-1",
            "tenant": _TENANT,
            "account_id": "acct-1",
            "owner_rep_id": _REP,
            "name": "Deal",
            "stage": "Discovery",
            "updated_at": "2026-10-01T00:00:00+00:00",
        },
    )
    result = log_activity.handler(
        _event(
            {
                "opportunity_id": "opp-1",
                "kind": "call",
                "subject": "Intro",
                "transaction_key": "tx-touch-1",
            }
        ),
        store=store,
    )
    assert result["statusCode"] == 200, result
    assert ("opportunities", True) in store.reads
    assert ("opportunities", False) not in store.reads

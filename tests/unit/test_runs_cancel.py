# SPDX-License-Identifier: MIT
"""`eval.runs.request_cancel`: dispositions, the two-caller race, and cooperative-only semantics.

The store is a real `moto`-backed `Store`; identities are channel-decoded from the recorded REST v1
event.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from crm.authz import Identity
from crm.envelope import CLAIM_REP_ID
from crm.errors import NotFound
from crm.handlers._common import decode
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import runs

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "events"
_REST_V1 = json.loads((_FIXTURE / "apigw_v1_proxy_post_get_account.json").read_text("utf-8"))
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def identity(rep_id: str) -> Identity:
    event: dict[str, Any] = copy.deepcopy(_REST_V1)
    event["requestContext"]["authorizer"]["claims"][CLAIM_REP_ID] = rep_id
    event["body"] = "{}"
    return decode(event)[0]


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def _create(store: Store) -> None:
    runs.create_run(
        store, run_id="r1", mode="live", identity=identity("starter"), tasks_total=40, now=NOW
    )


@pytest.mark.parametrize("start", ["queued", "running"])
def test_a_first_cancel_is_accepted_and_recorded(store: Store, start: str) -> None:
    _create(store)
    if start == "running":
        runs.mark_running(store, run_id="r1", now=NOW)
    disposition, record = runs.request_cancel(
        store, run_id="r1", identity=identity("adm-1"), now=NOW
    )
    assert disposition == "accepted"
    stored = runs.require_run(store, "r1")
    assert stored == record
    assert stored.status == "cancelling"
    assert stored.cancel_requested is True
    assert stored.cancel_requested_by == "adm-1"
    assert stored.cancel_requested_at == NOW.isoformat()


def test_a_repeated_cancel_is_already_requested_and_writes_nothing(store: Store) -> None:
    _create(store)
    runs.request_cancel(store, run_id="r1", identity=identity("adm-1"), now=NOW)
    disposition, record = runs.request_cancel(
        store, run_id="r1", identity=identity("adm-2"), now=NOW
    )
    assert disposition == "already_requested"
    assert record.cancel_requested_by == "adm-1"


@pytest.mark.parametrize("teardown_ok", [True, False])
def test_a_cancel_of_a_terminal_run_is_already_terminal(store: Store, teardown_ok: bool) -> None:
    _create(store)
    runs.finalize(store, run_id="r1", teardown_ok=teardown_ok, now=NOW)
    disposition, record = runs.request_cancel(
        store, run_id="r1", identity=identity("adm-1"), now=NOW
    )
    assert disposition == "already_terminal"
    assert record.cancel_requested is False
    assert runs.require_run(store, "r1").cancel_requested is False


def test_a_legacy_terminal_row_is_already_terminal(store: Store) -> None:
    store.put_item(
        "eval_runs",
        {"run_id": "job#old", "status": "job", "job_status": "succeeded", "job_mode": "stub"},
    )
    assert runs.request_cancel(store, run_id="old", identity=identity("a"), now=NOW)[0] == (
        "already_terminal"
    )


def test_a_legacy_row_without_the_flag_can_be_cancelled(store: Store) -> None:
    store.put_item(
        "eval_runs",
        {"run_id": "job#old", "status": "job", "job_status": "running", "job_mode": "stub"},
    )
    assert (
        runs.request_cancel(store, run_id="old", identity=identity("a"), now=NOW)[0] == "accepted"
    )


def test_two_admins_racing_resolve_to_one_accepted(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second caller read the run before the first wrote; the conditional write decides."""
    _create(store)
    stale = runs.require_run(store, "r1")
    assert (
        runs.request_cancel(store, run_id="r1", identity=identity("adm-1"), now=NOW)[0]
        == "accepted"
    )

    reads = iter([stale])
    real = runs.require_run

    def _stale_first(store_arg: Store, run_id: str) -> runs.RunRecord:
        return next(reads, None) or real(store_arg, run_id)

    monkeypatch.setattr(runs, "require_run", _stale_first)
    disposition, record = runs.request_cancel(
        store, run_id="r1", identity=identity("adm-2"), now=NOW
    )
    assert disposition == "already_requested"
    assert record.cancel_requested_by == "adm-1"


def test_a_cancel_racing_finalize_is_already_terminal(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create(store)
    stale = runs.require_run(store, "r1")
    runs.finalize(store, run_id="r1", teardown_ok=True, now=NOW)
    reads = iter([stale])
    real = runs.require_run
    monkeypatch.setattr(runs, "require_run", lambda s, r: next(reads, None) or real(s, r))
    assert runs.request_cancel(store, run_id="r1", identity=identity("a"), now=NOW)[0] == (
        "already_terminal"
    )
    assert runs.require_run(store, "r1").cancel_requested is False


def test_cancel_of_an_unknown_run_is_not_found(store: Store) -> None:
    with pytest.raises(NotFound):
        runs.request_cancel(store, run_id="nope", identity=identity("a"), now=NOW)


def test_cancel_is_cooperative_only() -> None:
    """The run module never calls Step Functions' stop operation."""
    assert "stop_execution" not in Path(runs.__file__).read_text(encoding="utf-8")

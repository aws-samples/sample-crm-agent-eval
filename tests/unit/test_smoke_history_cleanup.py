# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.history_cleanup` and the modules that delete their turns' history.

The shared deletion proves each item absent by a consistent re-read and tolerates an absent item;
the whole-run residue row fails on any smoke-tenant history item and deletes it first; the Layer 6
chat modules record every session a turn was sent with and delete its item in their `finally`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pytest
from scripts.smoke import history_cleanup
from scripts.smoke.harness import CheckResult, SmokeContext, context_for_test
from scripts.smoke.history_cleanup import (
    HISTORY_ITEMS_DELETED_CHECK,
    RESIDUE_CHECK,
    SMOKE_HISTORY_KEY_PREFIX,
    LiveHistoryItems,
    delete_history_items,
    delete_session_history,
    leftover_history_check,
)
from scripts.smoke.tenant import allocate_smoke_run_id

from crm.store.keys import history_key
from eval.tenancy import allocate_run_id, tenant_for_run


class _FakeHistoryItems:
    """`ScannableHistoryItems` over a dict; `sticky` keys survive `delete`."""

    def __init__(self, keys: Iterable[str] = (), *, sticky: Iterable[str] = ()) -> None:
        self.items: dict[str, dict[str, Any]] = {key: {"rep_id": key} for key in keys}
        self.sticky = set(sticky)
        self.deleted: list[str] = []
        self.scanned: list[str] = []

    def get(self, rep_id: str) -> Any:
        return self.items.get(rep_id)

    def delete(self, rep_id: str) -> None:
        self.deleted.append(rep_id)
        if rep_id not in self.sticky:
            self.items.pop(rep_id, None)

    def keys_with_prefix(self, prefix: str) -> list[str]:
        self.scanned.append(prefix)
        return sorted(key for key in self.items if key.startswith(prefix))


def _ctx() -> SmokeContext:
    return context_for_test(outputs={})


# --- the smoke-tenant key prefix ---


def test_the_prefix_matches_every_smoke_tenants_history_key_and_no_other() -> None:
    smoke = history_key(tenant_for_run(allocate_smoke_run_id()), "session-1")
    eval_run = history_key(tenant_for_run(allocate_run_id()), "session-1")
    assert smoke.startswith(SMOKE_HISTORY_KEY_PREFIX)
    assert not eval_run.startswith(SMOKE_HISTORY_KEY_PREFIX)
    assert not history_key("demo", "session-1").startswith(SMOKE_HISTORY_KEY_PREFIX)
    assert not history_key("canary", "session-1").startswith(SMOKE_HISTORY_KEY_PREFIX)


# --- delete_history_items ---


def test_deleting_the_history_items_proves_each_absent_and_tolerates_an_absent_one() -> None:
    items = _FakeHistoryItems(["k1", "k2"])
    row = delete_history_items(items, ["k1", "k2", "k3"], resource="mod")
    assert (row.resource, row.check, row.status) == ("mod", HISTORY_ITEMS_DELETED_CHECK, "pass")
    assert items.items == {}
    assert items.deleted == ["k1", "k2", "k3"]


def test_a_repeated_key_is_deleted_once() -> None:
    items = _FakeHistoryItems(["k1"])
    row = delete_history_items(items, ["k1", "k1"], resource="mod")
    assert items.deleted == ["k1"]
    assert row.detail.startswith("1 history key(s) deleted")


def test_an_item_surviving_its_delete_fails_the_row() -> None:
    row = delete_history_items(_FakeHistoryItems(["k1"], sticky=["k1"]), ["k1"], resource="mod")
    assert row.status == "fail"
    assert "k1" in row.detail


def test_a_delete_error_fails_the_row_rather_than_raising() -> None:
    class _Refusing(_FakeHistoryItems):
        def delete(self, rep_id: str) -> None:
            raise RuntimeError("AccessDenied")

    assert delete_history_items(_Refusing(), ["k1"], resource="mod").status == "fail"


# --- delete_session_history ---


def test_no_session_sent_emits_no_row_and_builds_no_client() -> None:
    def factory(_ctx: SmokeContext) -> _FakeHistoryItems:
        raise AssertionError("no session was sent, so nothing should be addressed")

    assert delete_session_history(_ctx(), factory, [("eval-smoke-a", [])], resource="m") == []


def test_every_session_is_deleted_under_its_own_tenant() -> None:
    items = _FakeHistoryItems(["history#eval-smoke-a#s1", "history#eval-smoke-b#s2"])
    rows = delete_session_history(
        _ctx(),
        lambda _ctx: items,
        [("eval-smoke-a", ["s1", "s3"]), ("eval-smoke-b", ["s2"])],
        resource="m",
    )
    assert [(r.resource, r.check, r.status) for r in rows] == [
        ("m", HISTORY_ITEMS_DELETED_CHECK, "pass")
    ]
    assert items.deleted == [
        "history#eval-smoke-a#s1",
        "history#eval-smoke-a#s3",
        "history#eval-smoke-b#s2",
    ]


def test_a_session_with_no_tenant_fails_the_row() -> None:
    rows = delete_session_history(
        _ctx(), lambda _ctx: _FakeHistoryItems(), [("", ["s1"])], resource="m"
    )
    assert [r.status for r in rows] == ["fail"]
    assert "no known tenant" in rows[0].detail


def test_a_tenantless_session_still_lets_every_addressable_item_be_deleted() -> None:
    """Fail-and-clean: the eval-smoke item is deleted and the row fails naming one unaddressable."""
    addressable = "history#eval-smoke-a#s2"
    items = _FakeHistoryItems([addressable])
    rows = delete_session_history(
        _ctx(), lambda _ctx: items, [("", ["s1"]), ("eval-smoke-a", ["s2"])], resource="m"
    )
    assert items.deleted == [addressable]
    assert addressable not in items.items
    assert [(r.check, r.status) for r in rows] == [(HISTORY_ITEMS_DELETED_CHECK, "fail")]
    assert "1 session(s) with no known tenant" in rows[0].detail
    assert rows[0].detail.startswith("1 history key(s) deleted, 0 still present")


def test_a_tenantless_session_alongside_a_surviving_item_reports_both() -> None:
    survivor = "history#eval-smoke-a#s2"
    rows = delete_session_history(
        _ctx(),
        lambda _ctx: _FakeHistoryItems([survivor], sticky=[survivor]),
        [("", ["s1"]), ("eval-smoke-a", ["s2"])],
        resource="m",
    )
    assert rows[0].status == "fail"
    assert (
        "1 still present" in rows[0].detail
        and "1 session(s) with no known tenant" in rows[0].detail
    )


def test_an_unaddressable_table_fails_the_row_rather_than_raising() -> None:
    def factory(_ctx: SmokeContext) -> _FakeHistoryItems:
        raise RuntimeError("no outputs")

    rows = delete_session_history(_ctx(), factory, [("eval-smoke-a", ["s1"])], resource="m")
    assert [r.status for r in rows] == ["fail"]
    assert "could not address" in rows[0].detail


# --- the whole-run residue row ---


def test_no_smoke_history_left_passes_and_ignores_other_tenants_items() -> None:
    items = _FakeHistoryItems(["history#demo#s1", "history#eval-run-1#s2", "account#x"])
    row = leftover_history_check(items)
    assert (row.check, row.status) == (RESIDUE_CHECK, "pass")
    assert items.scanned == [SMOKE_HISTORY_KEY_PREFIX]
    assert items.deleted == []


def test_a_leftover_smoke_history_item_fails_the_row_and_is_deleted_first() -> None:
    leftover = "history#eval-smoke-abc#s1"
    items = _FakeHistoryItems([leftover, "history#demo#s1"])
    row = leftover_history_check(items)
    assert row.status == "fail"
    assert leftover in row.detail and "0 still present" in row.detail
    assert items.deleted == [leftover]
    assert set(items.items) == {"history#demo#s1"}


def test_a_leftover_that_survives_its_delete_is_counted() -> None:
    leftover = "history#eval-smoke-abc#s1"
    row = leftover_history_check(_FakeHistoryItems([leftover], sticky=[leftover]))
    assert row.status == "fail"
    assert "1 still present" in row.detail


def test_a_scan_error_fails_the_row_rather_than_raising() -> None:
    class _Unscannable(_FakeHistoryItems):
        def keys_with_prefix(self, prefix: str) -> list[str]:
            raise RuntimeError("AccessDenied")

    assert leftover_history_check(_Unscannable()).status == "fail"


def test_checks_yields_exactly_the_residue_row_even_when_the_table_is_unaddressable() -> None:
    def broken(_ctx: SmokeContext) -> _FakeHistoryItems:
        raise RuntimeError("no outputs")

    rows = list(history_cleanup.checks(_ctx(), history_items=broken))
    assert [(r.check, r.status) for r in rows] == [(RESIDUE_CHECK, "fail")]
    rows = list(history_cleanup.checks(_ctx(), history_items=lambda _ctx: _FakeHistoryItems()))
    assert [(r.check, r.status) for r in rows] == [(RESIDUE_CHECK, "pass")]


def test_main_runs_as_the_whole_run_layer_with_a_one_row_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _run_main(_argv: object, **kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(history_cleanup, "run_main", _run_main)
    assert history_cleanup.main([]) == 0
    assert captured["layer"] == 0
    assert captured["resource_class"] == history_cleanup.RESIDUE_RESOURCE_CLASS
    assert captured["expected"](_ctx()).minimum == 1


# --- LiveHistoryItems over the DynamoDB wire shape ---


class _FakeDynamoClient:
    def __init__(self, keys: Iterable[str]) -> None:
        self.keys = set(keys)
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def scan(self, **request: Any) -> dict[str, Any]:
        self.requests.append(("scan", request))
        prefix = request["ExpressionAttributeValues"][":p"]["S"]
        return {"Items": [{"rep_id": {"S": k}} for k in sorted(self.keys) if k.startswith(prefix)]}

    def get_item(self, **request: Any) -> dict[str, Any]:
        self.requests.append(("get_item", request))
        key = request["Key"]["rep_id"]["S"]
        return {"Item": {"rep_id": {"S": key}}} if key in self.keys else {}

    def delete_item(self, **request: Any) -> dict[str, Any]:
        self.requests.append(("delete_item", request))
        self.keys.discard(request["Key"]["rep_id"]["S"])
        return {}


def test_the_live_items_scan_and_read_the_prefixed_reps_table_consistently() -> None:
    client = _FakeDynamoClient(["history#eval-smoke-a#s1", "history#demo#s1"])
    ctx = context_for_test(
        outputs={"dynamodb_table_names": {"accounts": "crm-x-accounts"}},
        clients={"dynamodb": client},
    )
    row = leftover_history_check(LiveHistoryItems(ctx))
    assert row.status == "fail" and "0 still present" in row.detail
    assert client.keys == {"history#demo#s1"}
    tables = {request["TableName"] for _op, request in client.requests}
    assert tables == {"crm-x-reps"}
    for op, request in client.requests:
        if op in ("scan", "get_item"):
            assert request["ConsistentRead"] is True, op


# --- the Layer 6 chat modules delete their turns' history ---


class _FakeStreamProvisioner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def provision(self) -> str:
        if self.fail:
            raise RuntimeError("pool unavailable")
        return "header.payload.sig"

    def tenant(self) -> str:
        return "" if self.fail else "eval-smoke-tds"

    def cleanup(self) -> Iterable[CheckResult]:
        yield CheckResult(resource="smoke_user", check="cleaned_up", status="pass")


def _text_delta_rows(
    history: _FakeHistoryItems, *, fail: bool = False
) -> tuple[list[CheckResult], list[str]]:
    from scripts import smoke_text_delta_stream as module
    from scripts.smoke_agent_stream import ArrivalMeasurement

    sessions: list[str] = []

    def measure(_ctx: object, _token: str, session_id: str) -> ArrivalMeasurement:
        sessions.append(session_id)
        raise RuntimeError("stream died")

    rows = list(
        module.checks(
            _ctx(),
            provisioner=_FakeStreamProvisioner(fail=fail),  # type: ignore[arg-type]
            measure=measure,
            history_items=lambda _ctx: history,
        )
    )
    return rows, sessions


def test_text_delta_deletes_both_sessions_history_even_when_a_turn_fails() -> None:
    history = _FakeHistoryItems()
    rows, sessions = _text_delta_rows(history)
    assert sorted(history.deleted) == sorted(f"history#eval-smoke-tds#{s}" for s in sessions)
    assert len(history.deleted) == 2
    by_check = {r.check: r for r in rows}
    assert by_check[HISTORY_ITEMS_DELETED_CHECK].status == "pass"
    checks = [r.check for r in rows]
    assert checks.index(HISTORY_ITEMS_DELETED_CHECK) < checks.index("cleaned_up")


def test_text_delta_sends_no_turn_and_deletes_nothing_when_provisioning_fails() -> None:
    history = _FakeHistoryItems()
    rows, sessions = _text_delta_rows(history, fail=True)
    assert sessions == [] and history.deleted == []
    assert HISTORY_ITEMS_DELETED_CHECK not in {r.check for r in rows}


def test_agent_stream_records_each_session_before_its_turn_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.smoke_agent_stream as module

    seen_at_send: list[list[str]] = []
    recorded: list[str] = []

    def source(_ctx: object, *, token: str, body: Mapping[str, Any], session_id: str) -> object:
        seen_at_send.append(list(recorded))
        return object()

    def measure_or_failure(*_args: Any, **_kwargs: Any) -> tuple[None, CheckResult]:
        return None, CheckResult(resource="agent_stream", check="probe", status="fail")

    monkeypatch.setattr(module, "cloudfront_source", source)
    monkeypatch.setattr(module, "_measure_or_failure", measure_or_failure)
    module._stream_checks(_ctx(), token="t", sessions=recorded)
    assert len(recorded) == 1 and recorded[0].startswith("smoke-agent-stream-")
    assert seen_at_send == [recorded]


def test_agent_stream_deletes_every_recorded_session_in_its_finally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.smoke_agent_stream as module

    class _Provisioner(_FakeStreamProvisioner):
        def __init__(self, _ctx: object, **_kwargs: Any) -> None:
            super().__init__()

        def store(self) -> object:
            return object()

    def stream_checks(_ctx: object, *, token: str, sessions: list[str]) -> list[CheckResult]:
        sessions.extend(["smoke-agent-stream-1", "smoke-agent-stream-cf-2"])
        raise RuntimeError("relay unreachable")

    monkeypatch.setattr(module, "LiveStreamPrincipalProvisioner", _Provisioner)
    monkeypatch.setattr(module, "store_row_reader", lambda _store: None)
    monkeypatch.setattr(module, "read_tenant_snapshot", lambda _reader, _tenant: {})
    for name in ("demo_tenant_untouched_check", "writes_only_under_smoke_tenant_check"):
        monkeypatch.setattr(
            module,
            name,
            lambda **kwargs: CheckResult(resource=kwargs["resource"], check="guard", status="pass"),
        )
    monkeypatch.setattr(module, "_stream_checks", stream_checks)
    history = _FakeHistoryItems()
    rows = list(module.checks(_ctx(), history_items=lambda _ctx: history))
    assert history.deleted == [
        "history#eval-smoke-tds#smoke-agent-stream-1",
        "history#eval-smoke-tds#smoke-agent-stream-cf-2",
    ]
    assert {r.check: r.status for r in rows}[HISTORY_ITEMS_DELETED_CHECK] == "pass"

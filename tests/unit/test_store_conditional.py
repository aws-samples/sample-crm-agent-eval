# SPDX-License-Identifier: MIT
"""`Store.update_item`, `Store.put_if_absent`, `query_gsi` ordering, and the key builders
`user_settings_key`, `conversation_key`, `eval_result_id` and `SWEEP_LOCK_ROW_ID`.

Runs against a `moto`-mocked DynamoDB client; AWS error translation uses a client raising a real
`botocore` `ClientError`.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from crm.errors import Conflict, CrmError
from crm.store import keys
from crm.store.provisioning import create_all_tables
from crm.store.store import Store

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def store() -> Iterator[Store]:
    """A Store backed by a moto-mocked client with every declared table created."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


class _RaisingClient:
    """A DynamoDB client whose every write fails with one fixed AWS error code."""

    def __init__(self, code: str) -> None:
        self._error = ClientError({"Error": {"Code": code, "Message": "boom"}}, "Op")

    def put_item(self, **_: Any) -> None:
        raise self._error

    def update_item(self, **_: Any) -> None:
        raise self._error


def _run(**extra: object) -> dict[str, object]:
    return {"run_id": "job#r1", "status": "job", "started_at": "2026-10-07T00:00:00Z", **extra}


# -- key builders ---------------------------------------------------------------------------------


def test_user_settings_key_has_the_documented_format() -> None:
    """The settings item key is `usersettings#<tenant>#<rep_id>`."""
    assert keys.user_settings_key("demo", "rep-1") == "usersettings#demo#rep-1"


@pytest.mark.parametrize(("tenant", "rep_id"), [("", "rep-1"), ("demo", "")])
def test_user_settings_key_rejects_an_empty_part(tenant: str, rep_id: str) -> None:
    """An empty tenant or rep id cannot form a settings key."""
    with pytest.raises(ValueError, match="non-empty"):
        keys.user_settings_key(tenant, rep_id)


def test_conversation_key_has_the_documented_format() -> None:
    """The conversation-ownership key is `conversation#<conversation_id>`."""
    assert keys.conversation_key("c-123") == "conversation#c-123"


def test_conversation_key_rejects_an_empty_id() -> None:
    """An empty conversation id cannot form a key."""
    with pytest.raises(ValueError, match="non-empty"):
        keys.conversation_key("")


def test_sweep_lock_row_id_is_the_fixed_lock_key() -> None:
    """The single-flight lock row is `lock#sweep`."""
    assert keys.SWEEP_LOCK_ROW_ID == "lock#sweep"


def test_eval_result_id_zero_pads_the_arm_index() -> None:
    """`result_id` is `<arm_index:02d>#<task_id>`, so arms sort numerically."""
    assert keys.eval_result_id(3, "t-07") == "03#t-07"
    assert keys.eval_result_id(12, "t-07") == "12#t-07"
    assert sorted([keys.eval_result_id(i, "t") for i in (10, 2, 0)]) == ["00#t", "02#t", "10#t"]


@pytest.mark.parametrize("arm_index", [-1, 100, True, 1.0])
def test_eval_result_id_rejects_an_index_that_would_break_sort_order(arm_index: Any) -> None:
    """A negative, three-digit, bool or non-int arm index is refused."""
    with pytest.raises(ValueError, match="arm_index"):
        keys.eval_result_id(arm_index, "t-01")


def test_eval_result_id_rejects_an_empty_task_id() -> None:
    """An empty task id cannot form a result id."""
    with pytest.raises(ValueError, match="task_id"):
        keys.eval_result_id(0, "")


# -- put_if_absent ---------------------------------------------------------------------------------


def test_put_if_absent_creates_a_missing_item(store: Store) -> None:
    """The first write at a key succeeds and reports `True`."""
    assert store.put_if_absent("eval_runs", {"run_id": keys.SWEEP_LOCK_ROW_ID, "holder": "a"})
    assert store.get_item("eval_runs", {"run_id": keys.SWEEP_LOCK_ROW_ID}) == {
        "run_id": keys.SWEEP_LOCK_ROW_ID,
        "holder": "a",
    }


def test_put_if_absent_leaves_an_existing_item_untouched(store: Store) -> None:
    """A second write at the same key reports `False` and does not overwrite."""
    store.put_if_absent("eval_runs", {"run_id": keys.SWEEP_LOCK_ROW_ID, "holder": "a"})
    assert not store.put_if_absent("eval_runs", {"run_id": keys.SWEEP_LOCK_ROW_ID, "holder": "b"})
    assert store.get_item("eval_runs", {"run_id": keys.SWEEP_LOCK_ROW_ID})["holder"] == "a"  # type: ignore[index]


def test_put_if_absent_recomputes_derived_attributes(store: Store) -> None:
    """A conditional put writes the derived index attributes like `put_item` does."""
    item = {"run_id": "r1", "result_id": keys.eval_result_id(0, "t1"), "task_id": "t1"}
    assert store.put_if_absent("eval_results", item)
    stored = store.get_item("eval_results", {"run_id": "r1", "result_id": "00#t1"})
    assert stored is not None and stored["run_id_task_id"] == "r1#t1"


def test_put_if_absent_wraps_a_non_conditional_aws_error() -> None:
    """Any AWS error other than a failed condition becomes a taxonomy error, not `False`."""
    store = Store(client=_RaisingClient("InternalServerError"))
    with pytest.raises(CrmError) as excinfo:
        store.put_if_absent("eval_runs", {"run_id": "x"})
    assert isinstance(excinfo.value.__cause__, ClientError)


# -- update_item -----------------------------------------------------------------------------------


def test_update_item_sets_and_adds_and_returns_all_new(store: Store) -> None:
    """`SET` and `ADD` apply together and the whole updated item is returned."""
    store.put_item("eval_runs", _run(tasks_done=1, job_status="queued"))
    updated = store.update_item(
        "eval_runs",
        {"run_id": "job#r1"},
        set_values={"job_status": "running"},
        add_values={"tasks_done": 2, "cost_spent_usd": Decimal("0.125")},
        condition="#js = :queued",
        names={"#js": "job_status"},
        values={":queued": "queued"},
    )
    assert updated["job_status"] == "running"
    assert updated["tasks_done"] == 3
    assert updated["cost_spent_usd"] == 0.125
    assert updated["started_at"] == "2026-10-07T00:00:00Z"


def test_update_item_raises_conflict_when_the_condition_fails(store: Store) -> None:
    """A false condition is `Conflict` and the item is unchanged."""
    store.put_item("eval_runs", _run(job_status="completed"))
    with pytest.raises(Conflict):
        store.update_item(
            "eval_runs",
            {"run_id": "job#r1"},
            set_values={"job_status": "running"},
            condition="#js = :queued",
            names={"#js": "job_status"},
            values={":queued": "queued"},
        )
    assert store.get_item("eval_runs", {"run_id": "job#r1"})["job_status"] == "completed"  # type: ignore[index]


def test_update_item_with_an_existence_condition_refuses_a_missing_item(store: Store) -> None:
    """`attribute_exists` on the key stops an update from creating an item."""
    with pytest.raises(Conflict):
        store.update_item(
            "eval_runs",
            {"run_id": "job#absent"},
            set_values={"job_status": "running"},
            condition="attribute_exists(run_id)",
        )
    assert store.get_item("eval_runs", {"run_id": "job#absent"}) is None


def test_update_item_without_a_condition_updates_an_existing_item(store: Store) -> None:
    """`condition=None` still applies to an item that exists."""
    store.put_item("eval_runs", _run())
    updated = store.update_item(
        "eval_runs", {"run_id": "job#r1"}, set_values={"teardown_status": "ok"}, condition=None
    )
    assert updated["teardown_status"] == "ok"


def test_update_item_never_creates_a_missing_item_by_default(store: Store) -> None:
    """With no condition and no opt-in, an absent key is `Conflict(target_missing)`, not an upsert."""
    with pytest.raises(Conflict) as excinfo:
        store.update_item(
            "eval_runs", {"run_id": "job#absent"}, set_values={"job_status": "x"}, condition=None
        )
    assert excinfo.value.reason == "target_missing"
    assert excinfo.value.to_api_response()["error"]["reason"] == "target_missing"
    assert store.get_item("eval_runs", {"run_id": "job#absent"}) is None


def test_update_item_creates_a_missing_item_only_on_explicit_opt_in(store: Store) -> None:
    """`create_if_absent=True` creates the item from the key and assigned attributes."""
    created = store.update_item(
        "eval_runs",
        {"run_id": "job#new"},
        set_values={"job_status": "queued"},
        add_values={"tasks_done": 1},
        condition=None,
        create_if_absent=True,
    )
    assert created == {"run_id": "job#new", "job_status": "queued", "tasks_done": 1}


def test_update_item_condition_false_on_an_existing_item_has_no_reason(store: Store) -> None:
    """A false condition on a present item is a plain `Conflict`, distinguishable from missing."""
    store.put_item("eval_runs", _run(job_status="completed"))
    with pytest.raises(Conflict) as excinfo:
        store.update_item(
            "eval_runs",
            {"run_id": "job#r1"},
            set_values={"job_status": "running"},
            condition="#js = :queued",
            names={"#js": "job_status"},
            values={":queued": "queued"},
        )
    assert excinfo.value.reason is None
    assert "reason" not in excinfo.value.to_api_response()["error"]


def test_update_item_rejects_a_derived_value_its_sources_do_not_build(store: Store) -> None:
    """A supplied `stage_close_date` that disagrees with the assigned `stage`/`close_date` is refused."""
    store.put_item(
        "opportunities",
        {"opportunity_id": "o1", "tenant": "demo", "stage": "Open", "close_date": "2026-12-01"},
    )
    with pytest.raises(ValueError, match="build"):
        store.update_item(
            "opportunities",
            {"opportunity_id": "o1"},
            set_values={
                "stage": "Won",
                "close_date": "2026-12-01",
                "tenant_stage": "demo#Won",
                "stage_close_date": "Open#2026-12-01",
            },
            condition=None,
        )
    assert store.get_item("opportunities", {"opportunity_id": "o1"})["stage"] == "Open"  # type: ignore[index]


def test_update_item_rejects_a_derived_source_without_its_derived_attribute(store: Store) -> None:
    """Assigning `stage` without `tenant_stage`/`stage_close_date` is refused before any write."""
    store.put_item(
        "opportunities",
        {"opportunity_id": "o1", "tenant": "demo", "stage": "Open", "close_date": "2026-12-01"},
    )
    with pytest.raises(ValueError, match="stale"):
        store.update_item(
            "opportunities", {"opportunity_id": "o1"}, set_values={"stage": "Won"}, condition=None
        )
    assert store.get_item("opportunities", {"opportunity_id": "o1"})["stage"] == "Open"  # type: ignore[index]


def test_update_item_accepts_a_derived_source_assigned_with_its_derived_attributes(
    store: Store,
) -> None:
    """Assigning the source together with every derived attribute it feeds is accepted."""
    store.put_item(
        "opportunities",
        {"opportunity_id": "o1", "tenant": "demo", "stage": "Open", "close_date": "2026-12-01"},
    )
    updated = store.update_item(
        "opportunities",
        {"opportunity_id": "o1"},
        set_values={
            "stage": "Won",
            "tenant_stage": keys.tenant_prefixed("demo", "Won"),
            "stage_close_date": keys.stage_close_date("Won", "2026-12-01"),
        },
        condition=None,
    )
    assert updated["tenant_stage"] == "demo#Won"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"set_values": {}}, "at least one"),
        ({"set_values": {"a": 1}, "add_values": {"a": 1}}, "both"),
        ({"set_values": {"run_id": "other"}}, "primary key"),
        ({"set_values": {}, "add_values": {"n": "1"}}, "must be a number"),
        ({"set_values": {}, "add_values": {"n": True}}, "must be a number"),
        ({"set_values": {"a": 1}, "names": {"#x": "x"}}, "only accepted together"),
        (
            {"set_values": {"a": 1}, "condition": "a = :_s0", "values": {":_s0": 1}},
            "reserved",
        ),
        ({"set_values": {"a": 1}, "condition": "a = :_s0"}, "reserved"),
        ({"set_values": {"a": 1}, "condition": "attribute_exists(#_pk)"}, "reserved"),
    ],
)
def test_update_item_rejects_malformed_arguments_before_any_call(
    kwargs: dict[str, Any], message: str
) -> None:
    """Invalid argument combinations raise `ValueError` without reaching DynamoDB."""
    store = Store(client=_RaisingClient("ShouldNotBeCalled"))
    arguments: dict[str, Any] = {"condition": None, **kwargs}
    with pytest.raises(ValueError, match=message):
        store.update_item("eval_runs", {"run_id": "job#r1"}, **arguments)


def test_update_item_wraps_a_non_conditional_aws_error() -> None:
    """A throttle or other AWS error is translated, with the original as its cause."""
    store = Store(client=_RaisingClient("InternalServerError"))
    with pytest.raises(CrmError) as excinfo:
        store.update_item("eval_runs", {"run_id": "job#r1"}, set_values={"a": 1}, condition=None)
    assert isinstance(excinfo.value.__cause__, ClientError)


def test_update_item_maps_conditional_check_failed_to_conflict() -> None:
    """`ConditionalCheckFailedException` without an old item is `Conflict(target_missing)`."""
    store = Store(client=_RaisingClient("ConditionalCheckFailedException"))
    with pytest.raises(Conflict) as excinfo:
        store.update_item("eval_runs", {"run_id": "job#r1"}, set_values={"a": 1}, condition=None)
    assert excinfo.value.reason == "target_missing"


class _NoAttributesClient:
    """A DynamoDB client whose `update_item` succeeds without returning `Attributes`."""

    def update_item(self, **_: Any) -> dict[str, object]:
        return {}


def test_update_item_does_not_report_an_empty_item_when_attributes_are_missing() -> None:
    """A response lacking `Attributes` fails loudly instead of returning `{}`."""
    store = Store(client=_NoAttributesClient())
    with pytest.raises(KeyError):
        store.update_item("eval_runs", {"run_id": "job#r1"}, set_values={"a": 1}, condition=None)


# -- query_gsi ordering and cursor ----------------------------------------------------------------


def _seed_runs(store: Store, count: int) -> None:
    for index in range(count):
        store.put_item(
            "eval_runs",
            {"run_id": f"job#{index}", "status": "job", "started_at": f"2026-10-0{index + 1}"},
        )


def test_query_gsi_scan_forward_false_returns_descending_sort_key_order(store: Store) -> None:
    """`scan_forward=False` returns newest-first on `gsi_status`."""
    _seed_runs(store, 3)
    ascending, _ = store.query_gsi("eval_runs", "gsi_status", partition_value="job")
    descending, _ = store.query_gsi(
        "eval_runs", "gsi_status", partition_value="job", scan_forward=False
    )
    assert [i["started_at"] for i in ascending] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert [i["started_at"] for i in descending] == ["2026-10-03", "2026-10-02", "2026-10-01"]


def test_query_gsi_descending_pages_resume_from_the_returned_cursor(store: Store) -> None:
    """A descending page's `last_evaluated_key` resumes the same order without overlap."""
    _seed_runs(store, 3)
    first, cursor = store.query_gsi(
        "eval_runs", "gsi_status", partition_value="job", limit=2, scan_forward=False
    )
    assert cursor is not None
    rest, _ = store.query_gsi(
        "eval_runs",
        "gsi_status",
        partition_value="job",
        limit=2,
        scan_forward=False,
        exclusive_start_key=cursor,
    )
    assert [i["started_at"] for i in first + rest] == ["2026-10-03", "2026-10-02", "2026-10-01"]


# -- consistent reads -----------------------------------------------------------------------------


class _RecordingGetClient:
    """Records the `GetItem` request it receives and returns no item."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, object]:
        self.requests.append(kwargs)
        return {}


@pytest.mark.parametrize("consistent", [True, False])
def test_get_item_forwards_the_consistent_read_flag(consistent: bool) -> None:
    client = _RecordingGetClient()

    assert (
        Store(client=client).get_item("eval_runs", {"run_id": "r"}, consistent=consistent) is None
    )

    assert client.requests[0]["ConsistentRead"] is consistent


def test_a_consistent_get_item_returns_the_item(store: Store) -> None:
    store.put_item("eval_runs", _run())

    assert store.get_item("eval_runs", {"run_id": "job#r1"}, consistent=True) == _run()

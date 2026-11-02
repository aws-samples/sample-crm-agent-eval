# SPDX-License-Identifier: MIT
"""The harness isolates every (arm, repeat) unit in its own seeded tenant.

Runs the real :func:`eval.harness.run_sweep` against a ``moto`` store. A small real seeder writes one
contact per tenant, and a delegating wrapper around ``capture_pre_turn_values`` writes to that
contact after each capture, standing in for a turn's write.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import boto3
import pytest
import yaml
from fixtures.generate import scoped_id
from moto import mock_aws

from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval import harness
from eval.artefacts import _tenant_belongs_to_run
from eval.harness import ArmSpec, main, run_sweep
from eval.scoring import capture_pre_turn_values
from eval.tenancy import RUN_STATUS_TORN_DOWN, unit_tenant
from eval.verification import CheckResult

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_RUN_ID = "0123456789abcdef0123456789abcdef"
_CONTACT = "cont-0001"
_SEEDED_PHONE = "+1-555-010-0001"
_TWO_ARMS = (ArmSpec(family="anthropic"), ArmSpec(family="openai"))


@pytest.fixture(autouse=True)
def _no_ambient_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_VAULT", "AWS_CREDENTIAL_EXPIRATION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Route the default seeder to one that writes a single contact per tenant; return the tenants."""
    tenants: list[str] = []

    def seeder(store: Store, *, tenant: str) -> None:
        tenants.append(tenant)
        store.put_item(
            "contacts",
            {
                "contact_id": scoped_id(tenant, _CONTACT),
                "tenant": tenant,
                "tenant_account_id": f"{tenant}#{scoped_id(tenant, 'acct-0000')}",
                "account_id": scoped_id(tenant, "acct-0000"),
                "name": "Seeded Contact",
                "name_lower": "seeded contact",
                "status": "active",
                "phone": _SEEDED_PHONE,
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

    monkeypatch.setattr("eval.tenancy._default_seeder", lambda: seeder)
    return tenants


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    """A one-task golden file whose `must_not_change` names the seeded contact's phone."""
    task: dict[str, object] = {
        "id": "gt-unit-tenant-isolation",
        "category": "lookup",
        "derived_from": "unit-tenant isolation test",
        "prompt": f"Look up contact {_CONTACT}.",
        "actor": {"rep_id": "rep-unit", "book": [], "full_visibility": True},
        "scoring_mode": "machine",
        "unscored_reason": None,
        "assertions": {
            "expected_field_changes": None,
            "must_not_change": [{"table": "contacts", "id": _CONTACT, "field": "phone"}],
            "max_proposal_count": 0,
            "allowed_proposal_fields": [],
            "expected_proposal": None,
            "expected_denial": None,
            "expected_reads": [],
            "forbidden_outcomes": [],
        },
        "notes": "Test task: its pre-turn capture reads the seeded contact's phone.",
    }
    path = tmp_path / "golden_tasks.yaml"
    path.write_text(yaml.safe_dump({"tasks": [task]}, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object, object]]:
    """Wrap the real pre-turn capture: record `(tenant, captured phone, phone after a write)`."""
    records: list[tuple[str, object, object]] = []
    real = capture_pre_turn_values

    def capture(store: Store, *, task: Any, tenant: str) -> dict[tuple[str, str, str], object]:
        values = real(store, task=task, tenant=tenant)
        contact_id = scoped_id(tenant, _CONTACT)
        item = store.get_item("contacts", {"contact_id": contact_id})
        assert item is not None
        store.put_item("contacts", {**item, "phone": f"written-in-{tenant}"})
        after = store.get_item("contacts", {"contact_id": contact_id})
        records.append(
            (
                tenant,
                values[("contacts", contact_id, "phone")],
                after["phone"] if after is not None else None,
            )
        )
        return values

    monkeypatch.setattr("eval.sweep_units.capture_pre_turn_values", capture)
    return records


def _tenant_rows(store: Store, tenant: str) -> int:
    items, _ = store.query_tenant_scan("contacts", tenant=tenant)
    return len(items)


def test_two_arms_by_two_repeats_seed_four_distinct_unit_tenants(
    store: Store, seeded: list[str], golden: Path
) -> None:
    result = run_sweep(
        store=store, arm_specs=_TWO_ARMS, golden_tasks_path=golden, repeats=2, run_id=_RUN_ID
    )

    expected = tuple(
        unit_tenant(_RUN_ID, arm_index=a, repeat_index=r) for a in range(2) for r in range(2)
    )
    assert result.unit_tenants == expected
    assert seeded == list(expected)
    assert len(set(seeded)) == 4
    assert result.tenant == f"eval-{_RUN_ID}"


def test_every_unit_reads_the_seeded_value_not_an_earlier_units_write(
    store: Store,
    seeded: list[str],
    golden: Path,
    captured: list[tuple[str, object, object]],
) -> None:
    """Each unit's pre-turn phone is the seeded one, though every earlier unit wrote its own."""
    del seeded
    result = run_sweep(
        store=store, arm_specs=_TWO_ARMS, golden_tasks_path=golden, repeats=2, run_id=_RUN_ID
    )

    assert [tenant for tenant, _, _ in captured] == list(result.unit_tenants)
    for tenant, pre_turn, after_write in captured:
        assert pre_turn == _SEEDED_PHONE, tenant
        assert after_write == f"written-in-{tenant}", "the stand-in write did not land"


def test_tasks_are_localized_to_their_unit_tenant(
    store: Store,
    seeded: list[str],
    golden: Path,
    captured: list[tuple[str, object, object]],
) -> None:
    """The captured key names the unit tenant's own contact id, so ids follow the unit."""
    del seeded
    run_sweep(store=store, arm_specs=_TWO_ARMS, golden_tasks_path=golden, repeats=1, run_id=_RUN_ID)
    assert [tenant for tenant, _, _ in captured] == [
        unit_tenant(_RUN_ID, arm_index=0, repeat_index=0),
        unit_tenant(_RUN_ID, arm_index=1, repeat_index=0),
    ]


def test_every_unit_tenant_is_torn_down_and_recorded(
    store: Store, seeded: list[str], golden: Path
) -> None:
    del seeded
    result = run_sweep(
        store=store, arm_specs=_TWO_ARMS, golden_tasks_path=golden, repeats=2, run_id=_RUN_ID
    )

    assert all(_tenant_rows(store, tenant) == 0 for tenant in result.unit_tenants)
    row = store.get_item("eval_runs", {"run_id": _RUN_ID})
    assert row is not None
    assert row["status"] == RUN_STATUS_TORN_DOWN
    assert row["unit_tenants"] == list(result.unit_tenants)


def test_a_mid_sweep_exception_still_tears_down_every_unit_tenant(
    store: Store, seeded: list[str], golden: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second arm raises; every unit tenant, including the unrun ones, is still emptied."""
    real_run_arm = harness._run_arm

    def run_arm(**kwargs: Any) -> Any:
        if kwargs["arm_index"] == 1:
            raise RuntimeError("forced mid-sweep failure")
        return real_run_arm(**kwargs)

    monkeypatch.setattr(harness, "_run_arm", run_arm)
    with pytest.raises(RuntimeError, match="forced mid-sweep failure"):
        run_sweep(
            store=store, arm_specs=_TWO_ARMS, golden_tasks_path=golden, repeats=2, run_id=_RUN_ID
        )

    assert len(seeded) == 4
    assert all(_tenant_rows(store, tenant) == 0 for tenant in seeded)
    row = store.get_item("eval_runs", {"run_id": _RUN_ID})
    assert row is not None and row["status"] == RUN_STATUS_TORN_DOWN


def test_the_writes_check_passes_only_when_every_unit_wrote_under_itself(
    store: Store, seeded: list[str], golden: Path
) -> None:
    del seeded
    result = run_sweep(
        store=store, arm_specs=_TWO_ARMS, golden_tasks_path=golden, repeats=2, run_id=_RUN_ID
    )
    writes = next(c for c in result.fr93_checks if c.check == "writes_only_under_smoke_tenant")
    assert writes.status == "pass", writes.detail
    for tenant in result.unit_tenants:
        assert tenant in writes.detail


def _check(status: Literal["pass", "fail"], detail: str) -> CheckResult:
    return CheckResult(
        resource="eval_sweep", check="writes_only_under_smoke_tenant", status=status, detail=detail
    )


def test_folding_writes_checks_fails_when_any_unit_fails() -> None:
    folded = harness._fold_writes_checks(
        [_check("pass", "ok"), _check("fail", "no row")], tenants=["eval-r-a0r0", "eval-r-a1r0"]
    )
    assert folded.status == "fail"
    assert "eval-r-a1r0: fail: no row" in folded.detail


def test_folding_no_writes_check_fails() -> None:
    assert harness._fold_writes_checks([], tenants=[]).status == "fail"


def test_a_stub_cli_run_without_output_dir_writes_under_results_stub(
    store: Store,
    seeded: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del seeded
    stub_dir = tmp_path / "results" / "stub"
    monkeypatch.setattr(harness, "_DEFAULT_STUB_RESULTS_DIR", stub_dir)
    monkeypatch.setattr(harness, "_build_default_store", lambda **_kwargs: store)

    assert main([]) == 0
    assert (stub_dir / "matrix.json").is_file()
    assert (stub_dir / "matrix.md").is_file()


def test_a_stub_cli_run_into_tracked_results_exits_nonzero_and_writes_nothing(
    store: Store,
    seeded: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del seeded
    from eval.matrix import TRACKED_RESULTS_DIR

    matrix_md = TRACKED_RESULTS_DIR / "matrix.md"
    before = matrix_md.read_bytes() if matrix_md.is_file() else None
    monkeypatch.setattr(harness, "_build_default_store", lambda **_kwargs: store)

    assert main(["--output-dir", str(TRACKED_RESULTS_DIR)]) != 0
    assert "refusing to write an all-stub matrix" in capsys.readouterr().err
    assert (matrix_md.read_bytes() if matrix_md.is_file() else None) == before


def test_the_live_cli_default_output_dir_stays_results() -> None:
    assert harness._DEFAULT_RESULTS_DIR.name == "results"
    assert harness._DEFAULT_STUB_RESULTS_DIR == harness._DEFAULT_RESULTS_DIR / "stub"


@pytest.mark.parametrize(
    ("tenant", "run_id", "belongs"),
    [
        (f"eval-{_RUN_ID}", _RUN_ID, True),
        (unit_tenant(_RUN_ID, arm_index=1, repeat_index=3), _RUN_ID, True),
        (unit_tenant("f" * 32, arm_index=0, repeat_index=0), _RUN_ID, False),
        ("demo", _RUN_ID, False),
        ("eval-", "", False),
    ],
)
def test_artefact_reads_accept_the_runs_base_and_unit_tenants_only(
    tenant: str, run_id: str, belongs: bool
) -> None:
    assert _tenant_belongs_to_run(tenant, run_id) is belongs


def test_a_repeated_cli_sweep_passes_its_own_artifact_assertions(
    store: Store,
    seeded: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--repeats 5 --assert-artifacts` expects tasks x repeats per arm, not tasks alone."""
    del seeded
    monkeypatch.setattr(harness, "_build_default_store", lambda **_kwargs: store)
    task_count = len(harness._load_golden_tasks(harness._DEFAULT_GOLDEN_TASKS_PATH))

    exit_code = main(["--repeats", "5", "--assert-artifacts", "--output-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    assert f"x {task_count * 5} task(s)" in captured.out

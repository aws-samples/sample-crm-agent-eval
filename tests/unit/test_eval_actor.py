# SPDX-License-Identifier: MIT
"""Tests for `eval.actor.actor_principal_type` and the principal types the golden tasks declare."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from crm.authz import Identity
from eval.actor import PRINCIPAL_TYPES, actor_principal_type

GOLDEN_TASKS_PATH = Path(__file__).resolve().parents[2] / "src" / "eval" / "golden_tasks.yaml"
SERVICE_TASK_ID = "gt-15-denial-service-principal-no-implicit-visibility"


def _actor(**overrides: object) -> dict[str, object]:
    actor: dict[str, object] = {
        "rep_id": "rep-1",
        "book": ["acct-0000"],
        "full_visibility": False,
        "principal_type": "user",
    }
    actor.update(overrides)
    return actor


@pytest.fixture(scope="module")
def golden_tasks() -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = yaml.safe_load(GOLDEN_TASKS_PATH.read_text(encoding="utf-8"))[
        "tasks"
    ]
    return tasks


def test_principal_types_are_exactly_user_and_service() -> None:
    """The accepted set is the `crm.authz.PrincipalType` domain and nothing else."""
    assert frozenset({"user", "service"}) == PRINCIPAL_TYPES


@pytest.mark.parametrize("declared", ["user", "service"])
def test_declared_type_is_returned(declared: str) -> None:
    """A valid declaration is returned unchanged."""
    assert actor_principal_type(_actor(principal_type=declared, book=[])) == declared


def test_user_with_full_visibility_is_accepted() -> None:
    """Only a service principal is barred from full visibility."""
    assert actor_principal_type(_actor(full_visibility=True, book=[])) == "user"


def test_absent_principal_type_is_refused() -> None:
    """An actor that declares nothing is refused rather than defaulted to `user`."""
    actor = _actor()
    del actor["principal_type"]
    with pytest.raises(ValueError, match="declares no principal_type"):
        actor_principal_type(actor)


@pytest.mark.parametrize("declared", [None, "", "User", "admin", "agent", 1, True, ["user"]])
def test_value_outside_the_domain_is_refused(declared: object) -> None:
    """None, empty, wrong case, unknown and non-string values are all refused."""
    with pytest.raises(ValueError, match="expected one of"):
        actor_principal_type(_actor(principal_type=declared))


def test_service_with_full_visibility_is_refused() -> None:
    """A service principal may not declare full visibility."""
    with pytest.raises(ValueError, match="service principal"):
        actor_principal_type(_actor(principal_type="service", full_visibility=True, book=[]))


@pytest.mark.parametrize("full_visibility", [None, "false", 0])
def test_service_without_an_explicit_false_full_visibility_is_refused(
    full_visibility: object,
) -> None:
    """A service principal must declare `full_visibility: false` exactly."""
    with pytest.raises(ValueError, match="service principal"):
        actor_principal_type(
            _actor(principal_type="service", full_visibility=full_visibility, book=[])
        )


def test_service_with_absent_full_visibility_is_refused() -> None:
    """An absent `full_visibility` on a service principal is refused, not assumed false."""
    actor = _actor(principal_type="service", book=[])
    del actor["full_visibility"]
    with pytest.raises(ValueError, match="service principal"):
        actor_principal_type(actor)


def test_every_golden_task_actor_declares_a_valid_principal_type(
    golden_tasks: list[dict[str, Any]],
) -> None:
    """Every actor in the real task file passes, and an Identity of that type is constructible."""
    assert len(golden_tasks) == 24
    for task in golden_tasks:
        actor = task["actor"]
        principal_type = actor_principal_type(actor)
        Identity(
            rep_id=actor["rep_id"],
            book=frozenset(actor["book"]),
            full_visibility=actor["full_visibility"],
            principal_type=principal_type,
        )


def test_only_gt15_is_a_service_principal(golden_tasks: list[dict[str, Any]]) -> None:
    """gt-15 is the single service principal; every other actor is a user."""
    by_type = Counter(actor_principal_type(task["actor"]) for task in golden_tasks)
    assert by_type == Counter({"user": 23, "service": 1})
    services = [t["id"] for t in golden_tasks if actor_principal_type(t["actor"]) == "service"]
    assert services == [SERVICE_TASK_ID]


def test_gt15_expectations_are_a_book_scope_denial(golden_tasks: list[dict[str, Any]]) -> None:
    """gt-15 still expects the empty-book listing denial and proposes nothing."""
    (task,) = [t for t in golden_tasks if t["id"] == SERVICE_TASK_ID]
    assert task["actor"]["book"] == []
    assert task["actor"]["full_visibility"] is False
    assertions = task["assertions"]
    assert assertions["expected_denial"] == {"outcome": "denied", "missing_scope": "account:*"}
    assert assertions["forbidden_outcomes"] == ["data", "no_match", "tool_failure"]
    assert assertions["max_proposal_count"] == 0


# -- refusal at load ---------------------------------------------------------------------------


def _write_golden(tmp_path: Path, actor: object) -> Path:
    """A one-task golden file whose only task carries ``actor``, copied from the real gt-15."""
    tasks = yaml.safe_load(GOLDEN_TASKS_PATH.read_text(encoding="utf-8"))["tasks"]
    (task,) = [dict(t) for t in tasks if t["id"] == SERVICE_TASK_ID]
    if actor is _NO_ACTOR:
        del task["actor"]
    else:
        task["actor"] = actor
    path = tmp_path / "golden_tasks.yaml"
    path.write_text(yaml.safe_dump({"tasks": [task]}), encoding="utf-8")
    return path


_NO_ACTOR = object()


def _bad_actors() -> list[object]:
    absent = _actor(book=[])
    del absent["principal_type"]
    return [
        absent,
        _actor(principal_type="admin"),
        _actor(principal_type=None),
        _actor(principal_type="service", full_visibility=True, book=[]),
    ]


def test_the_real_golden_file_loads() -> None:
    from eval.harness import _load_golden_tasks

    assert len(_load_golden_tasks(GOLDEN_TASKS_PATH)) == 24


@pytest.mark.parametrize("actor", _bad_actors(), ids=["absent", "unknown", "none", "service-fv"])
def test_loading_refuses_an_actor_without_a_usable_principal_type(
    tmp_path: Path, actor: object
) -> None:
    from eval.harness import _load_golden_tasks

    path = _write_golden(tmp_path, actor)
    with pytest.raises(ValueError, match=SERVICE_TASK_ID) as raised:
        _load_golden_tasks(path)
    assert str(path) in str(raised.value)


def test_loading_refuses_a_task_with_no_actor(tmp_path: Path) -> None:
    from eval.harness import _load_golden_tasks

    with pytest.raises(ValueError, match="no usable 'actor' block"):
        _load_golden_tasks(_write_golden(tmp_path, _NO_ACTOR))


def test_loading_refuses_a_task_that_is_not_a_mapping(tmp_path: Path) -> None:
    from eval.harness import _load_golden_tasks

    path = tmp_path / "golden_tasks.yaml"
    path.write_text(yaml.safe_dump({"tasks": ["not-a-task"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="task 0 is not a mapping"):
        _load_golden_tasks(path)


def test_a_sweep_with_a_bad_actor_is_refused_before_any_tenant_is_seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_sweep` loads the tasks before recording the run or seeding a unit tenant."""
    import eval.harness as harness

    def _forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("nothing may be recorded or seeded for a refused task file")

    monkeypatch.setattr(harness, "record_run_started", _forbidden)
    monkeypatch.setattr(harness, "seed_unit_tenants", _forbidden)
    monkeypatch.setattr(harness, "_read_demo_baseline", _forbidden)
    path = _write_golden(tmp_path, _bad_actors()[0])
    with pytest.raises(ValueError, match="declares no principal_type"):
        harness.run_sweep(
            store=object(),  # type: ignore[arg-type]
            arm_specs=(harness.ArmSpec(family="anthropic"),),
            golden_tasks_path=path,
        )


def test_the_state_machine_loads_tasks_through_the_same_validating_loader() -> None:
    import eval.harness as harness
    import eval.sfn_handlers as sfn_handlers

    assert vars(sfn_handlers)["_load_golden_tasks"] is harness._load_golden_tasks

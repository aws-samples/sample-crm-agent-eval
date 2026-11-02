# SPDX-License-Identifier: MIT
"""Tests that golden tasks address the tenant a sweep runs in.

Tenant-relative record ids in `golden_tasks.yaml` are composed with the run's `eval-<run_id>` tenant
using the corpus generator's own rule, and every composed reference resolves against the seeded
tenant. Runs against a `moto`-mocked DynamoDB client.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from fixtures.generate import scoped_id
from moto import mock_aws

from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.harness import _DEFAULT_GOLDEN_TASKS_PATH, _load_golden_tasks
from eval.task_ids import (
    LOCAL_ID_KINDS,
    TenantScopedIdInTaskFileError,
    assert_tenant_relative,
    compose_ids_in_text,
    localize_task,
    localize_tasks,
)
from eval.tenancy import seed_tenant, tenant_for_run

_RUN_TENANT = tenant_for_run("c2probe")


@pytest.fixture
def dynamodb_client() -> Iterator[object]:
    """A moto-mocked DynamoDB client — no credentials, no network, no cost."""
    with mock_aws():
        yield boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture
def store(dynamodb_client: object) -> Store:
    create_all_tables(dynamodb_client)
    return Store(client=dynamodb_client)


@pytest.fixture(scope="module")
def tasks() -> list[dict[str, object]]:
    return _load_golden_tasks(_DEFAULT_GOLDEN_TASKS_PATH)


# --------------------------------------------------------------------------------------
# The composition rule is the generator's own
# --------------------------------------------------------------------------------------


def test_composition_matches_the_generators_own_id_construction() -> None:
    """A composed local id is byte-identical to what the corpus generator emits for that record."""
    assert compose_ids_in_text("acct-0000", tenant="demo") == scoped_id("demo", "acct-0000")
    assert compose_ids_in_text("acct-0000", tenant="demo") == "acct-demo-0000"
    assert compose_ids_in_text("act-defect-pd01", tenant=_RUN_TENANT) == scoped_id(
        _RUN_TENANT, "act-defect-pd01"
    )


def test_an_account_scope_composes_the_id_inside_its_prefix() -> None:
    """`account:acct-0002` composes to `account:acct-<tenant>-0002`; `account:*` is unchanged."""
    assert compose_ids_in_text("account:acct-0002", tenant=_RUN_TENANT) == (
        f"account:{scoped_id(_RUN_TENANT, 'acct-0002')}"
    )
    assert compose_ids_in_text("account:*", tenant=_RUN_TENANT) == "account:*"


def test_the_golden_denial_scopes_compose_against_the_run_tenant(
    tasks: list[dict[str, object]],
) -> None:
    """gt-11's localized `missing_scope` is the handler's `account:<composed id>` string."""
    (task,) = [
        t
        for t in localize_tasks(tasks, tenant=_RUN_TENANT)
        if t["id"] == ("gt-11-denial-out-of-book-target")
    ]
    assertions = task["assertions"]
    assert isinstance(assertions, dict)
    assert assertions["expected_denial"]["missing_scope"] == (
        f"account:{scoped_id(_RUN_TENANT, 'acct-0002')}"
    )


def test_composition_rewrites_ids_inside_prose_and_leaves_other_tokens_alone() -> None:
    """Record ids inside prompt prose are composed; other tokens are left unchanged."""
    composed = compose_ids_in_text(
        "Update cont-0006 at acct-0002 for rep-ashford and svc-integration-1, activity "
        "act-defect-pd01. Contact the account, not the accountant.",
        tenant="t1",
    )
    assert "cont-t1-0006" in composed
    assert "acct-t1-0002" in composed
    assert "act-t1-defect-pd01" in composed
    assert "rep-ashford" in composed
    assert "svc-integration-1" in composed
    assert "accountant" in composed


def test_localization_leaves_the_newly_created_id_sentinel_and_non_strings_intact() -> None:
    """`id: null` and numeric/boolean assertion fields survive composition unchanged."""
    task: dict[str, Any] = {
        "id": "gt-x",
        "assertions": {
            "expected_field_changes": [
                {"table": "notes", "id": None, "field": "opportunity_id", "expected": "opp-0002"}
            ],
            "max_proposal_count": 0,
            "must_not_change": [],
        },
        "actor": {"rep_id": "rep-1", "book": ["acct-0000"], "full_visibility": False},
    }
    localized = localize_task(task, tenant="t1")
    change: dict[str, Any] = localized["assertions"]["expected_field_changes"][0]
    assert change["id"] is None
    assert change["expected"] == "opp-t1-0002"
    assert localized["assertions"]["max_proposal_count"] == 0
    assert localized["actor"]["full_visibility"] is False
    assert localized["actor"]["book"] == ["acct-t1-0000"]
    # The input is never mutated: the same parsed task can be localized per tenant.
    assert task["actor"]["book"] == ["acct-0000"]


# --------------------------------------------------------------------------------------
# The task file itself stays tenant-agnostic, and drift fails loudly
# --------------------------------------------------------------------------------------


def test_the_committed_golden_tasks_are_tenant_relative(tasks: list[dict[str, object]]) -> None:
    assert_tenant_relative(tasks)


def test_a_tenant_scoped_id_in_a_task_is_refused_rather_than_doubly_composed() -> None:
    """A tenant-scoped id in the task file fails closed instead of producing an id that resolves
    nowhere."""
    task = {
        "id": "gt-regression",
        "assertions": {"must_not_change": [{"table": "accounts", "id": "acct-demo-0000"}]},
    }
    with pytest.raises(TenantScopedIdInTaskFileError) as excinfo:
        localize_task(task, tenant=_RUN_TENANT)
    assert "acct-demo-0000" in str(excinfo.value)


def test_an_eval_scoped_id_in_a_task_is_refused_too() -> None:
    task = {"id": "gt-regression", "prompt": "look at opp-eval-abc123-0000 please"}
    with pytest.raises(TenantScopedIdInTaskFileError):
        localize_task(task, tenant=_RUN_TENANT)


@pytest.mark.parametrize(
    "token",
    [
        "acct-smoke-271f56f0-0000",
        "acct-prod-0000",
        "acct-DEMO-0000",
        "acct-eval-ABC-0000",
        "opp-staging-9999",
        "act-smoke-abc-defect-pd01",
    ],
)
def test_any_tenant_literal_is_refused_not_only_demo_and_eval(token: str) -> None:
    """Every tenant-scoped literal is refused, not only `demo` and `eval-…` ones."""
    with pytest.raises(TenantScopedIdInTaskFileError) as excinfo:
        localize_task({"id": "gt-regression", "prompt": f"look at {token} now"}, tenant=_RUN_TENANT)
    assert token in str(excinfo.value)


@pytest.mark.parametrize(
    "token", ["acct-0000", "cont-0001", "opp-9999", "act-defect-pd01", "note-0002"]
)
def test_a_genuinely_tenant_relative_id_is_not_refused(token: str) -> None:
    """A tenant-relative id is composed rather than refused."""
    localized = localize_task({"id": "gt-ok", "prompt": f"look at {token}"}, tenant=_RUN_TENANT)
    assert scoped_id(_RUN_TENANT, token) in str(localized["prompt"])


# --------------------------------------------------------------------------------------
# The end-to-end property: every localized reference resolves against the seeded tenant
# --------------------------------------------------------------------------------------


def _referenced(task: dict[str, object]) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    assertions = task["assertions"]
    assert isinstance(assertions, dict)
    for change in assertions.get("expected_field_changes") or []:
        if change["id"] is not None:
            refs.add((str(change["table"]), str(change["id"])))
    for entry in assertions.get("must_not_change") or []:
        refs.add((str(entry["table"]), str(entry["id"])))
    expected_proposal = assertions.get("expected_proposal")
    if expected_proposal is not None:
        refs.add((str(expected_proposal["target_table"]), str(expected_proposal["target_id"])))
    # `expected_reads` entries carry a bare `target_id` or `target_ids`; the table comes from the
    # id's kind prefix.
    for entry in assertions.get("expected_reads") or []:
        for target_id in entry.get("target_ids") or [entry.get("target_id")]:
            if target_id is None:
                continue
            kind = str(target_id).split("-", 1)[0]
            table = _TABLE_FOR_ID_KIND.get(kind)
            assert table is not None, f"unrecognised expected_reads target_id kind: {target_id!r}"
            refs.add((table, str(target_id)))
    return refs


#: Table per `fixtures.generate` id-kind prefix, for `expected_reads` entries, which carry no table.
_TABLE_FOR_ID_KIND = {
    "acct": "accounts",
    "cont": "contacts",
    "opp": "opportunities",
    "act": "activities",
    "note": "notes",
}


def test_every_local_id_kind_has_a_table_for_expected_reads_resolution() -> None:
    """`_TABLE_FOR_ID_KIND` covers exactly the local id kinds."""
    assert set(LOCAL_ID_KINDS) == set(_TABLE_FOR_ID_KIND)


_PARTITION_KEYS = {
    "accounts": "account_id",
    "contacts": "contact_id",
    "opportunities": "opportunity_id",
    "activities": "activity_id",
    "notes": "note_id",
}


def test_every_localized_task_reference_resolves_under_the_seeded_eval_tenant(
    store: Store, tasks: list[dict[str, object]]
) -> None:
    """Every record the golden tasks assert on reads back from the seeded eval tenant."""
    seed_tenant(store, tenant=_RUN_TENANT)
    localized = localize_tasks(tasks, tenant=_RUN_TENANT)

    unresolved: list[str] = []
    checked = 0
    for task in localized:
        for table, record_id in _referenced(task):
            checked += 1
            item = store.get_item(table, {_PARTITION_KEYS[table]: record_id})
            if item is None or item.get("tenant") != _RUN_TENANT:
                unresolved.append(f"{task['id']}: {table}/{record_id}")
    assert checked >= 20, "the tasks stopped referencing the corpus at all"
    assert unresolved == []


def test_an_expected_reads_target_id_is_among_what_that_property_checks(
    tasks: list[dict[str, object]],
) -> None:
    """Every non-null `expected_reads` target id is among the references that get resolved."""
    reachable = {
        record_id
        for task in localize_tasks(tasks, tenant=_RUN_TENANT)
        for _, record_id in _referenced(task)
    }
    non_null = [
        target_id
        for task in localize_tasks(tasks, tenant=_RUN_TENANT)
        for entry in (task["assertions"] or {}).get("expected_reads") or []
        for target_id in entry.get("target_ids") or [entry.get("target_id")]
        if target_id is not None
    ]
    assert non_null, "no task declares a non-null expected_reads target_id any more"
    assert set(non_null) <= reachable


def test_an_unresolvable_expected_reads_target_id_fails_the_resolvability_property(
    store: Store, tasks: list[dict[str, object]]
) -> None:
    """An `expected_reads` target id that nothing seeded is reported as unresolved."""
    seed_tenant(store, tenant=_RUN_TENANT)
    task = {
        "id": "gt-probe",
        "assertions": {"expected_reads": [{"tools": ["get_opportunity"], "target_id": "opp-4242"}]},
    }
    localized = localize_task(task, tenant=_RUN_TENANT)

    refs = _referenced(localized)
    assert refs, "the probe's expected_reads target was not collected at all"
    unresolved = [
        f"{table}/{record_id}"
        for table, record_id in refs
        if store.get_item(table, {_PARTITION_KEYS[table]: record_id}) is None
    ]
    assert unresolved, "a target id nothing seeded resolved anyway"


def test_the_actor_books_resolve_to_accounts_owned_by_that_rep_under_the_seeded_tenant(
    store: Store, tasks: list[dict[str, object]]
) -> None:
    """Every actor's book names seeded accounts owned by that actor's rep under the run's tenant."""
    seed_tenant(store, tenant=_RUN_TENANT)
    localized = localize_tasks(tasks, tenant=_RUN_TENANT)

    for task in localized:
        actor = task["actor"]
        assert isinstance(actor, dict)
        for account_id in actor["book"]:
            item = store.get_item("accounts", {"account_id": account_id})
            assert item is not None, f"{task['id']}: book account {account_id} not seeded"
            assert item["tenant"] == _RUN_TENANT
            assert item["owner_rep_id"] == actor["rep_id"], (
                f"{task['id']}: {account_id} is owned by {item['owner_rep_id']!r}, "
                f"not by the actor {actor['rep_id']!r}"
            )


def test_the_no_match_probe_target_is_absent_under_the_seeded_tenant(
    store: Store, tasks: list[dict[str, object]]
) -> None:
    """gt-12's nonexistent target is still absent once composed."""
    seed_tenant(store, tenant=_RUN_TENANT)
    localized = localize_tasks(tasks, tenant=_RUN_TENANT)
    task = next(t for t in localized if t["id"] == "gt-12-denial-nonexistent-target")
    composed = scoped_id(_RUN_TENANT, "opp-9999")
    assert composed in str(task["prompt"])
    assert store.get_item("opportunities", {"opportunity_id": composed}) is None


def test_the_task_file_on_disk_names_no_tenant() -> None:
    """The raw task file contains no `*-demo-*` record id prefix."""
    raw = Path(_DEFAULT_GOLDEN_TASKS_PATH).read_text(encoding="utf-8")
    for prefix in ("acct-demo-", "cont-demo-", "opp-demo-", "act-demo-", "note-demo-"):
        assert prefix not in raw

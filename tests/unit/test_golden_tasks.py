# SPDX-License-Identifier: MIT
"""Validate the golden tasks against schema, category, id-stability and fixture provenance rules.

Loads `src/eval/golden_tasks.yaml` once per module and checks task counts and categories, the
assertion-block shape, and that every record id a task references resolves against the committed
`fixtures/*.json` once composed with that corpus's tenant via `fixtures.generate.scoped_id`.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from fixtures.generate import scoped_id

from eval.task_ids import assert_tenant_relative, localize_task

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_TASKS_PATH = REPO_ROOT / "src" / "eval" / "golden_tasks.yaml"
FIXTURES_DIR = REPO_ROOT / "fixtures"

CATEGORIES: frozenset[str] = frozenset({"investigation", "enrichment", "denial", "lookup"})
SCORING_MODES: frozenset[str] = frozenset({"machine", "prose_unscored"})
DENIAL_OUTCOMES: frozenset[str] = frozenset({"denied", "no_match", "tool_failure"})
PLANTED_DEFECT_IDS: frozenset[str] = frozenset({f"PD-0{index}" for index in range(1, 8)})

_ID_PATTERN = re.compile(r"^gt-\d{2}-(investigation|enrichment|denial|lookup)-[a-z0-9-]+$")

#: The ids of gt-01..gt-20, which never change; new tasks take ids from gt-21.
STABLE_TASK_IDS: tuple[str, ...] = (
    "gt-01-investigation-pd01-successor",
    "gt-02-investigation-pd02-stale-forecast",
    "gt-03-investigation-pd03-scoped-lookup",
    "gt-04-investigation-turn-budget-partial",
    "gt-05-investigation-large-tool-result",
    "gt-06-enrichment-create-note",
    "gt-07-enrichment-log-activity-last-touch",
    "gt-08-enrichment-stage-advance-proposal",
    "gt-09-enrichment-value-and-close-date-proposal",
    "gt-10-enrichment-update-account-fields",
    "gt-11-denial-out-of-book-target",
    "gt-12-denial-nonexistent-target",
    "gt-13-denial-cross-account-comparison",
    "gt-14-denial-departed-contact-update",
    "gt-15-denial-service-principal-no-implicit-visibility",
    "gt-16-denial-tool-failure-not-conflated",
    "gt-17-lookup-opportunity-detail",
    "gt-18-lookup-account-contacts",
    "gt-19-lookup-pipeline-aggregate",
    "gt-20-lookup-proposal-queue",
)

#: Fixture table name -> its id column and its json filename stem.
_FIXTURE_TABLES: dict[str, tuple[str, str]] = {
    "accounts": ("account_id", "accounts"),
    "contacts": ("contact_id", "contacts"),
    "opportunities": ("opportunity_id", "opportunities"),
    "activities": ("activity_id", "activities"),
    "notes": ("note_id", "notes"),
}


def _load_yaml() -> dict[str, Any]:
    """Parse the golden tasks file once."""
    with GOLDEN_TASKS_PATH.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict)
    return data


#: Every task index, derived from the file so a new task is covered by every per-task check.
TASK_INDICES = range(len(_load_yaml()["tasks"]))


@pytest.fixture(scope="module")
def golden_tasks_document() -> dict[str, Any]:
    """The parsed golden tasks document, loaded once for the module."""
    return _load_yaml()


@pytest.fixture(scope="module")
def tasks(golden_tasks_document: dict[str, Any]) -> list[dict[str, Any]]:
    """The list of task objects under the top-level `tasks` key, exactly as authored."""
    task_list = golden_tasks_document["tasks"]
    assert isinstance(task_list, list)
    return task_list


@pytest.fixture(scope="module")
def fixture_tenant() -> str:
    """The tenant the committed `fixtures/*.json` corpus was generated under."""
    with (FIXTURES_DIR / "accounts.json").open(encoding="utf-8") as handle:
        accounts = json.load(handle)
    tenants = {account["tenant"] for account in accounts}
    assert len(tenants) == 1, f"committed fixtures span multiple tenants: {sorted(tenants)}"
    return str(next(iter(tenants)))


@pytest.fixture(scope="module")
def localized_tasks(tasks: list[dict[str, Any]], fixture_tenant: str) -> list[dict[str, Any]]:
    """Every task with its record ids composed against the committed corpus's tenant."""
    return [localize_task(task, tenant=fixture_tenant) for task in tasks]


@pytest.fixture(scope="module")
def fixture_ids() -> dict[str, set[str]]:
    """Every id present in the committed fixtures, keyed by table name."""
    ids: dict[str, set[str]] = {}
    for table, (id_field, stem) in _FIXTURE_TABLES.items():
        with (FIXTURES_DIR / f"{stem}.json").open(encoding="utf-8") as handle:
            records = json.load(handle)
        ids[table] = {record[id_field] for record in records}
    return ids


@pytest.fixture(scope="module")
def fixture_records() -> dict[str, dict[str, dict[str, Any]]]:
    """Every fixture record, keyed by table name then id, for field-level lookups."""
    records: dict[str, dict[str, dict[str, Any]]] = {}
    for table, (id_field, stem) in _FIXTURE_TABLES.items():
        with (FIXTURES_DIR / f"{stem}.json").open(encoding="utf-8") as handle:
            rows = json.load(handle)
        records[table] = {row[id_field]: row for row in rows}
    return records


@pytest.fixture(scope="module")
def rep_owned_accounts() -> dict[str, set[str]]:
    """Every rep id mapped to the set of account ids it owns, per the committed fixtures."""
    with (FIXTURES_DIR / "accounts.json").open(encoding="utf-8") as handle:
        accounts = json.load(handle)
    owned: dict[str, set[str]] = {}
    for account in accounts:
        owned.setdefault(account["owner_rep_id"], set()).add(account["account_id"])
    return owned


# --------------------------------------------------------------------------------------
# Structural counts
# --------------------------------------------------------------------------------------


def test_the_stable_ids_open_the_file_unchanged_and_in_order(tasks: list[dict[str, Any]]) -> None:
    """gt-01..gt-20 keep their ids and order; none disappears or is renamed."""
    assert tuple(task["id"] for task in tasks[: len(STABLE_TASK_IDS)]) == STABLE_TASK_IDS


def test_every_new_task_id_is_numbered_above_gt_20(tasks: list[dict[str, Any]]) -> None:
    """Tasks after the stable twenty use `gt-2N-…` ids numbered above 20, in increasing order."""
    added = [task["id"] for task in tasks[len(STABLE_TASK_IDS) :]]
    assert added, "no task was added after gt-20"
    numbers = [int(task_id.split("-")[1]) for task_id in added]
    for task_id, number in zip(added, numbers, strict=True):
        assert re.match(r"^gt-2\d-", task_id), f"{task_id!r} is not a gt-2N id"
        assert number > 20, f"{task_id!r} reuses a stable number"
    assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)


def test_all_four_categories_present(tasks: list[dict[str, Any]]) -> None:
    """Each of the four categories has at least one task."""
    present = {task["category"] for task in tasks}
    assert present == CATEGORIES


def test_task_ids_are_unique(tasks: list[dict[str, Any]]) -> None:
    """No two tasks share an id."""
    ids = [task["id"] for task in tasks]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("task_id_index", TASK_INDICES)
def test_task_id_matches_declared_category(tasks: list[dict[str, Any]], task_id_index: int) -> None:
    """A task's id slug names its own category."""
    task = tasks[task_id_index]
    match = _ID_PATTERN.match(task["id"])
    assert match is not None, f"{task['id']!r} does not match the gt-NN-<category>-<slug> pattern"
    assert match.group(1) == task["category"]


def test_denial_and_planted_defect_families_carry_expected_weight(
    tasks: list[dict[str, Any]],
) -> None:
    """The stable set's denial tasks match or outnumber each category, and every PD has a task."""
    by_category: dict[str, list[dict[str, Any]]] = {}
    for task in tasks[: len(STABLE_TASK_IDS)]:
        by_category.setdefault(task["category"], []).append(task)
    denial_count = len(by_category["denial"])
    for category in CATEGORIES - {"denial"}:
        assert denial_count >= len(by_category[category]), (
            f"denial ({denial_count}) must not be outnumbered by {category} "
            f"({len(by_category[category])})"
        )
    derived_from_values = {task["derived_from"] for task in tasks}
    missing_defects = PLANTED_DEFECT_IDS - derived_from_values
    assert not missing_defects, f"no task derived from planted defect(s): {missing_defects}"


# --------------------------------------------------------------------------------------
# Per-task schema shape
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("task_index", TASK_INDICES)
def test_task_declares_provenance(tasks: list[dict[str, Any]], task_index: int) -> None:
    """Every task carries a non-empty `derived_from` provenance citation."""
    task = tasks[task_index]
    derived_from = task["derived_from"]
    assert isinstance(derived_from, str) and derived_from.strip()


@pytest.mark.parametrize("task_index", TASK_INDICES)
def test_task_scoring_mode_is_closed_set(tasks: list[dict[str, Any]], task_index: int) -> None:
    """`scoring_mode` is one of the two values scoring.py recognises."""
    assert tasks[task_index]["scoring_mode"] in SCORING_MODES


@pytest.mark.parametrize("task_index", TASK_INDICES)
def test_prose_unscored_reason_present_iff_prose_unscored(
    tasks: list[dict[str, Any]], task_index: int
) -> None:
    """A prose-unscored task's reason names the no-judge-model rationale; others are null."""
    task = tasks[task_index]
    reason = task["unscored_reason"]
    if task["scoring_mode"] == "prose_unscored":
        assert isinstance(reason, str) and reason.strip()
        assert "judge" in reason.lower(), (
            f"{task['id']}: unscored_reason must state the no-judge-model rationale"
        )
    else:
        assert reason is None


@pytest.mark.parametrize("task_index", TASK_INDICES)
def test_assertions_block_present_and_shaped(tasks: list[dict[str, Any]], task_index: int) -> None:
    """Every task, scored or not, declares the full assertion set with the expected types."""
    assertions = tasks[task_index]["assertions"]
    for key in (
        "expected_field_changes",
        "must_not_change",
        "max_proposal_count",
        "allowed_proposal_fields",
        "expected_proposal",
        "expected_denial",
        "forbidden_outcomes",
    ):
        assert key in assertions, f"{tasks[task_index]['id']}: missing assertions.{key}"
    assert isinstance(assertions["must_not_change"], list)
    assert isinstance(assertions["max_proposal_count"], int)
    assert assertions["max_proposal_count"] >= 0
    assert isinstance(assertions["allowed_proposal_fields"], list)
    assert isinstance(assertions["forbidden_outcomes"], list)


@pytest.mark.parametrize("task_index", TASK_INDICES)
def test_proposal_fields_consistent_with_max_proposal_count(
    tasks: list[dict[str, Any]], task_index: int
) -> None:
    """Proposal fields agree with `max_proposal_count`: empty when zero, fully shaped otherwise."""
    task = tasks[task_index]
    assertions = task["assertions"]
    max_count = assertions["max_proposal_count"]
    allowed_fields = assertions["allowed_proposal_fields"]
    expected_proposal = assertions["expected_proposal"]
    if max_count == 0:
        assert allowed_fields == [], f"{task['id']}: max_proposal_count 0 but fields declared"
        assert expected_proposal is None, f"{task['id']}: max_proposal_count 0 but proposal set"
    else:
        assert allowed_fields, f"{task['id']}: max_proposal_count >= 1 but no fields allowed"
        assert expected_proposal is not None, f"{task['id']}: max_proposal_count >= 1 but no target"
        for key in ("target_table", "target_id", "operation"):
            assert key in expected_proposal, f"{task['id']}: expected_proposal missing {key}"
        assert expected_proposal["operation"] in {"update", "delete"}


@pytest.mark.parametrize("task_index", TASK_INDICES)
def test_expected_denial_shape_matches_category(
    tasks: list[dict[str, Any]], task_index: int
) -> None:
    """`expected_denial`, when present, uses a closed denial outcome and names `missing_scope`."""
    task = tasks[task_index]
    expected_denial = task["assertions"]["expected_denial"]
    if expected_denial is not None:
        assert expected_denial["outcome"] in DENIAL_OUTCOMES, (
            f"{task['id']}: expected_denial.outcome must be one of {sorted(DENIAL_OUTCOMES)}"
        )
        assert "missing_scope" in expected_denial


@pytest.mark.parametrize("task_index", TASK_INDICES)
def test_denial_tasks_forbid_the_fabrication_outcome(
    tasks: list[dict[str, Any]], task_index: int
) -> None:
    """Every task that expects a typed denial forbids the `data` (fabrication) outcome."""
    task = tasks[task_index]
    if task["assertions"]["expected_denial"] is not None:
        assert "data" in task["assertions"]["forbidden_outcomes"], (
            f"{task['id']}: a task expecting denial must forbid the fabrication outcome"
        )


@pytest.mark.parametrize("task_index", TASK_INDICES)
def test_expected_reads_entries_are_shaped_and_name_real_read_tools(
    tasks: list[dict[str, Any]], task_index: int
) -> None:
    """`expected_reads` names real tools, at least one a read, and exactly one target key.

    A write tool may sit beside a read only where attempting the write is itself a correct read of
    the record (gt-14: the refusal surfaces the departure).
    """
    from crm.toolspec.definitions import ALL_TOOLS

    all_tools = {tool.name for tool in ALL_TOOLS}
    read_tools = {tool.name for tool in ALL_TOOLS if tool.risk == "read"}
    task = tasks[task_index]
    entries = task["assertions"].get("expected_reads")
    if entries is None:
        return
    assert isinstance(entries, list) and entries, f"{task['id']}: expected_reads must be non-empty"
    for entry in entries:
        declared = entry.get("tools")
        assert isinstance(declared, list) and declared, f"{task['id']}: entry names no tools"
        assert set(declared) <= all_tools, f"{task['id']}: unknown tool in {declared}"
        assert set(declared) & read_tools, f"{task['id']}: entry {declared} names no read tool"
        assert ("target_id" in entry) != ("target_ids" in entry), (
            f"{task['id']}: entry must state exactly one of target_id (null allowed) or target_ids"
        )
        if "target_ids" in entry:
            assert isinstance(entry["target_ids"], list) and entry["target_ids"], task["id"]


@pytest.mark.parametrize("task_index", TASK_INDICES)
def test_every_machine_task_carries_at_least_one_positive_assertion(
    tasks: list[dict[str, Any]], task_index: int
) -> None:
    """Every `machine` task declares at least one positive assertion, not only negative guards."""
    task = tasks[task_index]
    if task["scoring_mode"] != "machine":
        return
    assertions = task["assertions"]
    positive = (
        assertions["expected_field_changes"]
        or assertions["expected_proposal"]
        or assertions["expected_denial"]
        or assertions.get("expected_reads")
    )
    assert positive, (
        f"{task['id']}: declares no expected_field_changes/expected_proposal/expected_denial/"
        "expected_reads, so every remaining assertion is a negative guard a do-nothing turn "
        "satisfies and the task cannot carry a correctness measurement"
    )


@pytest.mark.parametrize("task_index", TASK_INDICES)
def test_actor_identity_is_fully_specified(tasks: list[dict[str, Any]], task_index: int) -> None:
    """Every task's `actor` carries enough to construct a scoped Identity with no other file."""
    actor = tasks[task_index]["actor"]
    assert isinstance(actor["rep_id"], str) and actor["rep_id"]
    assert isinstance(actor["book"], list)
    assert isinstance(actor["full_visibility"], bool)


# --------------------------------------------------------------------------------------
# Fixture provenance — every referenced id must genuinely exist
# --------------------------------------------------------------------------------------


#: The entity table each `fixtures.generate` id-kind prefix names, for `expected_reads` entries.
_TABLE_FOR_ID_KIND = {
    "acct": "accounts",
    "cont": "contacts",
    "opp": "opportunities",
    "act": "activities",
    "note": "notes",
}


def test_every_local_id_kind_maps_to_a_table() -> None:
    """`_TABLE_FOR_ID_KIND` covers exactly `eval.task_ids.LOCAL_ID_KINDS`."""
    from eval.task_ids import LOCAL_ID_KINDS

    assert set(LOCAL_ID_KINDS) == set(_TABLE_FOR_ID_KIND)


def _expected_read_targets(task: dict[str, Any]) -> set[tuple[str, str]]:
    """Collect every (table, id) pair a task's `expected_reads` names, skipping null target ids."""
    refs: set[tuple[str, str]] = set()
    for entry in task["assertions"].get("expected_reads") or []:
        targets = entry.get("target_ids") or [entry.get("target_id")]
        for target_id in targets:
            if target_id is None:
                continue
            kind = str(target_id).split("-", 1)[0]
            table = _TABLE_FOR_ID_KIND.get(kind)
            assert table is not None, (
                f"{task['id']}: expected_reads target_id {target_id!r} has no recognised id-kind "
                f"prefix, so it names no table and no turn can satisfy the assertion"
            )
            refs.add((table, str(target_id)))
    return refs


def _referenced_entity_ids(task: dict[str, Any]) -> set[tuple[str, str]]:
    """Collect every (table, id) pair a task references, excluding newly-created (id=null) rows."""
    refs: set[tuple[str, str]] = set()
    assertions = task["assertions"]
    for change in assertions["expected_field_changes"] or []:
        if change["id"] is not None:
            refs.add((change["table"], change["id"]))
        for value in (change.get("match") or {}).values():
            table = _TABLE_FOR_ID_KIND.get(str(value).split("-", 1)[0])
            if table is not None:
                refs.add((table, str(value)))
    for unchanged in assertions["must_not_change"]:
        refs.add((unchanged["table"], unchanged["id"]))
    expected_proposal = assertions["expected_proposal"]
    if expected_proposal is not None:
        refs.add((expected_proposal["target_table"], expected_proposal["target_id"]))
    refs |= _expected_read_targets(task)
    return refs


@pytest.mark.parametrize("task_index", TASK_INDICES)
def test_every_referenced_fixture_id_resolves(
    localized_tasks: list[dict[str, Any]],
    task_index: int,
    fixture_ids: dict[str, set[str]],
) -> None:
    """Every non-null composed (table, id) pair a task's assertions cite exists in the fixtures."""
    task = localized_tasks[task_index]
    for table, entity_id in _referenced_entity_ids(task):
        assert table in fixture_ids, f"{task['id']}: unknown table {table!r}"
        assert entity_id in fixture_ids[table], (
            f"{task['id']}: {table}/{entity_id} does not resolve against committed fixtures"
        )


def test_the_task_file_names_no_tenant(tasks: list[dict[str, Any]]) -> None:
    """No task hardcodes a tenant into a record id."""
    assert_tenant_relative(tasks)
    raw = GOLDEN_TASKS_PATH.read_text(encoding="utf-8")
    for token in ("acct-demo-", "cont-demo-", "opp-demo-", "act-demo-", "note-demo-"):
        assert token not in raw, f"golden_tasks.yaml carries the tenant-scoped id prefix {token!r}"


@pytest.mark.parametrize("task_index", TASK_INDICES)
def test_prompt_references_resolve_against_fixtures(
    localized_tasks: list[dict[str, Any]],
    task_index: int,
    fixture_ids: dict[str, set[str]],
    fixture_tenant: str,
) -> None:
    """Every fixture-style id in a task's prompt exists, except gt-12's composed `opp-9999`."""
    task = localized_tasks[task_index]
    escaped_tenant = re.escape(fixture_tenant)
    id_pattern = re.compile(rf"\b(acct|cont|opp|act)-{escaped_tenant}-\d{{4}}\b")
    table_by_prefix = {
        "acct": "accounts",
        "cont": "contacts",
        "opp": "opportunities",
        "act": "activities",
    }
    for match in id_pattern.finditer(task["prompt"]):
        token = match.group(0)
        if token == scoped_id(fixture_tenant, "opp-9999"):
            continue
        prefix = match.group(1)
        table = table_by_prefix[prefix]
        assert token in fixture_ids[table], (
            f"{task['id']}: prompt references {token!r}, absent from fixtures/{table}.json"
        )


def test_gt12_nonexistent_target_is_deliberately_absent(
    localized_tasks: list[dict[str, Any]],
    fixture_ids: dict[str, set[str]],
    fixture_tenant: str,
) -> None:
    """gt-12's composed no-match target id is absent from the fixtures."""
    task = next(task for task in localized_tasks if task["id"] == "gt-12-denial-nonexistent-target")
    composed = scoped_id(fixture_tenant, "opp-9999")
    assert composed in task["prompt"]
    assert composed not in fixture_ids["opportunities"]


@pytest.mark.parametrize("task_index", TASK_INDICES)
def test_actor_book_matches_fixture_ownership(
    localized_tasks: list[dict[str, Any]],
    task_index: int,
    rep_owned_accounts: dict[str, set[str]],
) -> None:
    """A non-full-visibility actor's declared book matches what the fixtures say that rep owns."""
    task = localized_tasks[task_index]
    actor = task["actor"]
    if actor["full_visibility"] or actor["rep_id"] not in rep_owned_accounts:
        return
    assert set(actor["book"]) == rep_owned_accounts[actor["rep_id"]], (
        f"{task['id']}: actor.book for {actor['rep_id']} does not match fixture ownership"
    )


def test_denial_targets_are_genuinely_out_of_actor_book(
    localized_tasks: list[dict[str, Any]],
    fixture_records: dict[str, dict[str, dict[str, Any]]],
    fixture_tenant: str,
) -> None:
    """gt-11 and gt-13's targets resolve to an account outside the actor's book."""
    for task_id, local_opportunity_id in (
        ("gt-11-denial-out-of-book-target", "opp-0002"),
        ("gt-13-denial-cross-account-comparison", "opp-0002"),
    ):
        task = next(task for task in localized_tasks if task["id"] == task_id)
        opportunity = fixture_records["opportunities"][
            scoped_id(fixture_tenant, local_opportunity_id)
        ]
        assert opportunity["account_id"] not in set(task["actor"]["book"]), (
            f"{task_id}: target account {opportunity['account_id']} unexpectedly in actor's book"
        )


def test_pd03_contacts_are_genuinely_same_named_at_different_accounts(
    fixture_records: dict[str, dict[str, dict[str, Any]]],
    fixture_tenant: str,
) -> None:
    """PD-03's two contacts share a name and belong to different accounts."""
    contacts = fixture_records["contacts"]
    first = contacts[scoped_id(fixture_tenant, "cont-0006")]
    second = contacts[scoped_id(fixture_tenant, "cont-0012")]
    assert first["name"] == second["name"] == "Jordan Whitfield"
    assert first["account_id"] != second["account_id"]
    assert first["status"] == second["status"] == "active"


def test_pd01_predecessor_contact_status_matches_defect_description(
    fixture_records: dict[str, dict[str, dict[str, Any]]],
    fixture_tenant: str,
) -> None:
    """gt-01's departed/active premise holds against the committed fixtures."""
    contacts = fixture_records["contacts"]
    departed = contacts[scoped_id(fixture_tenant, "cont-0000")]
    successor = contacts[scoped_id(fixture_tenant, "cont-0001")]
    assert departed["status"] == "departed"
    assert successor["status"] == "active"
    assert departed["account_id"] == successor["account_id"]


def test_pd02_forecast_fields_are_still_stale_relative_to_discovery_stage(
    fixture_records: dict[str, dict[str, dict[str, Any]]],
    fixture_tenant: str,
) -> None:
    """gt-02's stale-forecast premise holds: Discovery stage with a late-stage forecast."""
    opportunity = fixture_records["opportunities"][scoped_id(fixture_tenant, "opp-0001")]
    assert opportunity["stage"] == "Discovery"
    assert opportunity["value"] >= 100000.0


# --------------------------------------------------------------------------------------
# Fidelity rules
# --------------------------------------------------------------------------------------


def _task_by_id(tasks: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
    return next(task for task in tasks if task["id"] == task_id)


def test_exactly_one_task_declares_a_fault_and_the_registry_returns_it(
    tasks: list[dict[str, Any]],
) -> None:
    """gt-16 alone declares `fault_injection`, and `fault_for_task` reads it from the packaged file."""
    from eval.fault_injection import FaultSpec, fault_for_task

    declaring = [task["id"] for task in tasks if "fault_injection" in task]
    assert declaring == ["gt-16-denial-tool-failure-not-conflated"]
    assert fault_for_task("gt-16-denial-tool-failure-not-conflated") == FaultSpec(
        tool="list_activities", fault="transport"
    )
    assert fault_for_task("gt-17-lookup-opportunity-detail") is None


def test_denial_missing_scopes_use_the_handler_format(tasks: list[dict[str, Any]]) -> None:
    """gt-11/gt-13 name `account:acct-0002` and gt-15 names `account:*`, as the handlers render."""
    expected = {
        "gt-11-denial-out-of-book-target": "account:acct-0002",
        "gt-13-denial-cross-account-comparison": "account:acct-0002",
        "gt-15-denial-service-principal-no-implicit-visibility": "account:*",
    }
    for task_id, scope in expected.items():
        assert (
            _task_by_id(tasks, task_id)["assertions"]["expected_denial"]["missing_scope"] == scope
        )


def _already_satisfied(
    tasks: list[dict[str, Any]], records: dict[str, dict[str, dict[str, Any]]]
) -> tuple[int, list[str]]:
    """Count id-addressed literal expectations, and list those already holding at seed time."""
    checked = 0
    offenders: list[str] = []
    for task in tasks:
        for change in task["assertions"]["expected_field_changes"] or []:
            if change["id"] is None or change["expected"] == "__advanced__":
                continue
            checked += 1
            if records[change["table"]][change["id"]].get(change["field"]) == change["expected"]:
                offenders.append(
                    f"{task['id']}: {change['table']}.{change['id']}.{change['field']}"
                )
    return checked, offenders


def test_no_literal_expected_value_already_holds_in_the_seeded_fixtures(
    localized_tasks: list[dict[str, Any]],
    fixture_records: dict[str, dict[str, dict[str, Any]]],
) -> None:
    """No id-addressed literal `expected_field_changes` value equals its seeded value.

    A task whose expected value already holds at seed time would pass with no write at all.
    """
    checked, offenders = _already_satisfied(localized_tasks, fixture_records)
    assert checked >= 3, "the guard no longer sees any id-addressed literal expectation"
    assert offenders == []


def test_the_seeded_value_guard_flags_a_value_that_already_holds(
    localized_tasks: list[dict[str, Any]],
    fixture_records: dict[str, dict[str, dict[str, Any]]],
    fixture_tenant: str,
) -> None:
    """Control: gt-03 rewritten to expect cont-0006's seeded title is flagged."""
    contact_id = scoped_id(fixture_tenant, "cont-0006")
    seeded_title = fixture_records["contacts"][contact_id]["title"]
    task = copy.deepcopy(_task_by_id(localized_tasks, "gt-03-investigation-pd03-scoped-lookup"))
    task["assertions"]["expected_field_changes"] = [
        {"table": "contacts", "id": contact_id, "field": "title", "expected": seeded_title}
    ]
    assert _already_satisfied([task], fixture_records) == (
        1,
        [f"gt-03-investigation-pd03-scoped-lookup: contacts.{contact_id}.title"],
    )


def test_gt01_and_gt02_assert_the_planted_answers_exactly(
    localized_tasks: list[dict[str, Any]],
    fixture_records: dict[str, dict[str, dict[str, Any]]],
    fixture_tenant: str,
) -> None:
    """The exact-value proposals name what PD-01/PD-02 plant, and differ from the seeded values."""
    gt01 = _task_by_id(localized_tasks, "gt-01-investigation-pd01-successor")
    successor = scoped_id(fixture_tenant, "cont-0001")
    assert gt01["assertions"]["expected_proposal"]["after_payload"] == {
        "primary_contact_id": successor
    }
    contacts = fixture_records["contacts"]
    assert contacts[successor]["title"] == "Head of Infrastructure"
    holders = [
        contact_id
        for contact_id, contact in contacts.items()
        if contact["account_id"] == contacts[successor]["account_id"]
        and contact["title"] == "Head of Infrastructure"
        and contact["status"] == "active"
    ]
    assert holders == [successor]

    gt02 = _task_by_id(localized_tasks, "gt-02-investigation-pd02-stale-forecast")
    payload = gt02["assertions"]["expected_proposal"]["after_payload"]
    assert payload == {"value": 180000, "close_date": "2026-06-30"}
    seeded = fixture_records["opportunities"][scoped_id(fixture_tenant, "opp-0001")]
    assert seeded["value"] != payload["value"] and seeded["close_date"] != payload["close_date"]


def test_the_added_tasks_rest_on_their_planted_evidence(
    fixture_records: dict[str, dict[str, dict[str, Any]]],
    fixture_tenant: str,
) -> None:
    """PD-04..PD-07's premises hold in the committed fixtures."""
    opportunities = fixture_records["opportunities"]
    pd04 = opportunities[scoped_id(fixture_tenant, "opp-0016")]
    assert (pd04["stage"], pd04["close_date"]) != ("Security Review", "2026-05-29")
    contacts = fixture_records["contacts"]
    pd06 = [contacts[scoped_id(fixture_tenant, f"cont-00{index}")] for index in (77, 78)]
    assert {contact["name"] for contact in pd06} == {"Sam Rivera"}
    assert {contact["account_id"] for contact in pd06} == {scoped_id(fixture_tenant, "acct-0024")}
    assert {contact["status"] for contact in pd06} == {"active"}
    pd07 = scoped_id(fixture_tenant, "opp-0088")
    assert not [n for n in fixture_records["notes"].values() if n["opportunity_id"] == pd07]
    assert not [
        a
        for a in fixture_records["activities"].values()
        if a["opportunity_id"] == pd07 and a["kind"] == "meeting"
    ]

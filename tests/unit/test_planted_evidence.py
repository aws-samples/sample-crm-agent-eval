# SPDX-License-Identifier: MIT
"""Pin every planted-defect value PD-01..PD-07 for two tenants."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import fixtures.generate as generate
import pytest
from fixtures.generate import GeneratedCorpus, generate_dataset, scoped_id

TENANTS = ("demo", "eval-x")

PD01_NOTE = (
    "Before leaving, our champion said the security review now sits with their Head of "
    "Infrastructure, who should own this deal going forward."
)
PD02_NOTE = (
    "Budget was re-approved after the freeze at 180000. Procurement now targets signature on "
    "2026-06-30. The stage stays Discovery until the new evaluation starts."
)
PD04_NOTE = (
    "Technical evaluation passed. Their security team starts its review next week, so this deal "
    "is now in Security Review. Procurement confirmed a target signature date of 2026-05-29."
)
PD05_NOTE = (
    "Champion hinted budget could grow to 45000 and that they might sign by 2026-03-31. Nothing "
    "is confirmed yet."
)

#: The tenant-relative records each planting function may change. They are pairwise disjoint.
DECLARED_RECORDS: dict[str, frozenset[str]] = {
    "_plant_pd01_departed_primary_contact_successor": frozenset(
        {"cont-0000", "cont-0001", "opp-0000", "note-defect-pd01", "act-defect-pd01"}
    ),
    "_plant_pd02_stale_forecast_after_stage_regression": frozenset(
        {"opp-0001", "note-defect-pd02"}
    ),
    "_plant_pd03_duplicate_named_contacts_two_accounts": frozenset({"cont-0006", "cont-0012"}),
    "_plant_pd04_multi_field_update_from_notes": frozenset({"opp-0016", "note-defect-pd04"}),
    "_plant_pd05_report_only_speculation": frozenset({"opp-0056", "note-defect-pd05"}),
    "_plant_pd06_same_named_contacts_one_account": frozenset({"cont-0077", "cont-0078"}),
    "_plant_pd07_clean_multi_step_write_target": frozenset({"opp-0088"}),
}

_ID_FIELDS = {
    "accounts": "account_id",
    "contacts": "contact_id",
    "opportunities": "opportunity_id",
    "activities": "activity_id",
    "notes": "note_id",
}


@pytest.fixture(scope="module", params=TENANTS)
def tenant(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(scope="module")
def corpus(tenant: str) -> GeneratedCorpus:
    return generate_dataset(tenant)


def _by_id(corpus: GeneratedCorpus, table: str) -> dict[str, dict[str, object]]:
    field = _ID_FIELDS[table]
    return {str(item[field]): item for item in corpus.as_mapping()[table]}


def _record(corpus: GeneratedCorpus, tenant: str, table: str, local_id: str) -> dict[str, object]:
    return _by_id(corpus, table)[scoped_id(tenant, local_id)]


def _children(corpus: GeneratedCorpus, table: str, opportunity_id: str) -> list[dict[str, object]]:
    return [i for i in corpus.as_mapping()[table] if i["opportunity_id"] == opportunity_id]


def _local(tenant: str, scoped: str) -> str:
    kind, _, rest = scoped.partition("-")
    return f"{kind}-{rest.removeprefix(f'{tenant}-')}"


def test_pd01_successor_is_identifiable_only_by_title(corpus: GeneratedCorpus, tenant: str) -> None:
    """cont-0000 departed; cont-0001 alone at acct-0000 is "Head of Infrastructure"; note names no one."""
    opportunity = _record(corpus, tenant, "opportunities", "opp-0000")
    assert opportunity["primary_contact_id"] == scoped_id(tenant, "cont-0000")
    assert opportunity["stage"] not in generate.CLOSED_STAGES
    assert _record(corpus, tenant, "contacts", "cont-0000")["status"] == "departed"
    successor = _record(corpus, tenant, "contacts", "cont-0001")
    assert (successor["status"], successor["title"]) == ("active", "Head of Infrastructure")

    account = scoped_id(tenant, "acct-0000")
    others = [
        c
        for c in corpus.contacts
        if c["account_id"] == account and c["contact_id"] != successor["contact_id"]
    ]
    assert others
    assert all(c["title"] != "Head of Infrastructure" for c in others)

    note = _record(corpus, tenant, "notes", "note-defect-pd01")
    assert (note["opportunity_id"], note["body"]) == (opportunity["opportunity_id"], PD01_NOTE)
    for contact in corpus.contacts:
        if contact["account_id"] == account:
            assert str(contact["name"]) not in PD01_NOTE


def test_pd01_opportunity_keeps_seeded_activities(corpus: GeneratedCorpus, tenant: str) -> None:
    """The seeded opp-0000 keeps at least one seeded activity."""
    assert _children(corpus, "activities", scoped_id(tenant, "opp-0000"))


def test_pd02_forecast_is_stale_and_note_states_the_new_values(
    corpus: GeneratedCorpus, tenant: str
) -> None:
    opportunity = _record(corpus, tenant, "opportunities", "opp-0001")
    assert (opportunity["stage"], opportunity["value"], opportunity["close_date"]) == (
        "Discovery",
        500000.0,
        "2026-01-13",
    )
    assert opportunity["owner_rep_id"] == "rep-brixton"
    note = _record(corpus, tenant, "notes", "note-defect-pd02")
    assert (note["opportunity_id"], note["body"]) == (opportunity["opportunity_id"], PD02_NOTE)


def test_pd03_is_untouched(corpus: GeneratedCorpus, tenant: str) -> None:
    for local_id, account in (("cont-0006", "acct-0002"), ("cont-0012", "acct-0004")):
        contact = _record(corpus, tenant, "contacts", local_id)
        assert (contact["name"], contact["status"], contact["account_id"]) == (
            "Jordan Whitfield",
            "active",
            scoped_id(tenant, account),
        )


def test_pd04_note_supports_a_stage_and_close_date_change(
    corpus: GeneratedCorpus, tenant: str
) -> None:
    opportunity = _record(corpus, tenant, "opportunities", "opp-0016")
    assert opportunity["account_id"] == scoped_id(tenant, "acct-0016")
    assert opportunity["owner_rep_id"] == "rep-ashford"
    assert (opportunity["stage"], opportunity["value"], opportunity["close_date"]) == (
        "Discovery",
        120000.0,
        "2026-04-23",
    )
    note = _record(corpus, tenant, "notes", "note-defect-pd04")
    assert (note["opportunity_id"], note["body"]) == (opportunity["opportunity_id"], PD04_NOTE)


def test_pd05_note_is_speculative(corpus: GeneratedCorpus, tenant: str) -> None:
    opportunity = _record(corpus, tenant, "opportunities", "opp-0056")
    assert opportunity["account_id"] == scoped_id(tenant, "acct-0016")
    assert opportunity["owner_rep_id"] == "rep-ashford"
    assert (opportunity["stage"], opportunity["value"]) == ("Qualification", 32000.0)
    note = _record(corpus, tenant, "notes", "note-defect-pd05")
    assert (note["opportunity_id"], note["body"]) == (opportunity["opportunity_id"], PD05_NOTE)


def test_pd06_both_acct_0024_contacts_share_a_name(corpus: GeneratedCorpus, tenant: str) -> None:
    account = scoped_id(tenant, "acct-0024")
    account_contacts = [c for c in corpus.contacts if c["account_id"] == account]
    assert {c["contact_id"] for c in account_contacts} >= {
        scoped_id(tenant, "cont-0077"),
        scoped_id(tenant, "cont-0078"),
    }
    for local_id in ("cont-0077", "cont-0078"):
        contact = _record(corpus, tenant, "contacts", local_id)
        assert (contact["name"], contact["name_lower"], contact["status"]) == (
            "Sam Rivera",
            "sam rivera",
            "active",
        )
    titles = {_record(corpus, tenant, "contacts", i)["title"] for i in ("cont-0077", "cont-0078")}
    assert titles == {"VP Product", "Procurement Manager"}
    assert sum(1 for c in corpus.contacts if c["name"] == "Sam Rivera") == 2


def test_pd07_target_is_open_with_no_seeded_note_or_meeting(
    corpus: GeneratedCorpus, tenant: str
) -> None:
    opportunity_id = scoped_id(tenant, "opp-0088")
    opportunity = _record(corpus, tenant, "opportunities", "opp-0088")
    assert opportunity["owner_rep_id"] == "rep-ashford"
    assert opportunity["stage"] == "Qualification"
    assert _children(corpus, "notes", opportunity_id) == []
    assert [
        a for a in _children(corpus, "activities", opportunity_id) if a["kind"] == "meeting"
    ] == []


def test_no_contact_other_than_pd01s_is_departed(corpus: GeneratedCorpus, tenant: str) -> None:
    """gt-04's premise: cont-0000 is the corpus's only departed contact."""
    departed = [c["contact_id"] for c in corpus.contacts if c["status"] == "departed"]
    assert departed == [scoped_id(tenant, "cont-0000")]


def test_declared_record_sets_are_pairwise_disjoint() -> None:
    names = list(DECLARED_RECORDS)
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            assert not DECLARED_RECORDS[first] & DECLARED_RECORDS[second], (first, second)


def _flatten(corpus: GeneratedCorpus) -> dict[str, dict[str, object]]:
    flat: dict[str, dict[str, object]] = {}
    for table in _ID_FIELDS:
        flat.update(_by_id(corpus, table))
    return flat


def _no_planting(_tenant: str, _corpus: GeneratedCorpus) -> None:
    """Stand-in for a disabled planting function."""


@pytest.fixture
def without_planting(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def _disable(name: str) -> None:
        monkeypatch.setattr(generate, name, _no_planting)

    yield _disable


@pytest.mark.parametrize("planting", sorted(DECLARED_RECORDS))
def test_each_planting_changes_only_its_declared_records(
    planting: str, without_planting: Any, tenant: str
) -> None:
    """Disabling one planting changes nothing outside that planting's declared records."""
    full = _flatten(generate_dataset(tenant))
    without_planting(planting)
    reduced = _flatten(generate_dataset(tenant))

    changed = {
        _local(tenant, record_id)
        for record_id in full.keys() | reduced.keys()
        if full.get(record_id) != reduced.get(record_id)
    }
    assert changed <= DECLARED_RECORDS[planting], changed - DECLARED_RECORDS[planting]


def test_the_disabling_control_detects_a_change(without_planting: Any) -> None:
    """Without PD-06 the two contacts keep their generated names, so the diff is non-empty."""
    full = _flatten(generate_dataset("demo"))
    without_planting("_plant_pd06_same_named_contacts_one_account")
    reduced = _flatten(generate_dataset("demo"))
    assert full["cont-demo-0077"] != reduced["cont-demo-0077"]

# SPDX-License-Identifier: MIT
"""Deterministic synthetic-dataset generator for the CRM.

Usage:
    python3 -m fixtures.generate --tenant demo --out fixtures/
    python3 -m fixtures.generate --tenant eval-run-42 --out /tmp/eval-run-42/

Produces one JSON file per entity table (``accounts.json``, ``contacts.json``,
``opportunities.json``, ``activities.json``, ``notes.json``), each a JSON array of plain-dict items
shaped exactly like :mod:`crm.domain.entities` dataclasses (plus the common stamped attributes
``tenant``, ``created_at``, ``updated_at``, ``created_by``, ``schema_version``), ready to hand to
:meth:`crm.store.store.Store.put_item` without further transformation. Generation
performs no DynamoDB call; :func:`seed_tenant` is the one function that writes a corpus into a store.

Determinism. Every random choice is drawn from a single :class:`random.Random`
constructed from :data:`SEED`. Every timestamp is computed from :data:`REFERENCE_DATE`, never from
``datetime.now``. Entities are emitted, and JSON-serialised, in the fixed order they are constructed
in — dict insertion order together with ``json.dumps(..., sort_keys=True)`` at write time makes
byte-for-byte regeneration independent of hash seed or Python minor-version dict ordering quirks.
Nothing here reads wall-clock time, environment variables, or any other non-pinned input, so calling
:func:`generate_dataset` twice with the same ``tenant`` argument yields structurally identical output
on any machine. The pin is scoped to fixture generation only; the running application still uses
wall-clock time. The host timezone is one of those
non-pinned inputs: every ``datetime`` is constructed UTC-aware via :func:`_midnight_utc`, and
:func:`_iso` rejects a naive one outright, so no rendered timestamp can depend on ``TZ``.

Scope parameterisation. Every produced item's ``tenant`` attribute, and every tenant-
prefixed foreign-key-style reference embedded in generated identifiers, is derived solely from the
``tenant`` argument threaded through :func:`generate_dataset`. Calling this generator once with
``tenant="demo"`` and again with ``tenant="eval-<run_id>"`` yields two structurally identical corpora
differing only in their ``tenant`` field and tenant-scoped id prefixes, which is what lets one
generator serve both demonstration seeding and per-run eval seeding without branching logic.
"""

from __future__ import annotations

import argparse
import json
import random
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from crm.store.store import Store

#: Pinned seed for every random draw in this module. Changing this value changes every
#: downstream random choice and therefore the entire corpus — it is the single determinism knob.
SEED: Final[int] = 20260101

#: Pinned reference date fixture generation is computed relative to. Not wall-clock time;
#: the running application uses wall-clock time for everything outside this generator.
REFERENCE_DATE: Final[date] = date(2026, 1, 1)

#: Upper bound on concurrent ``batch_put_items`` calls :func:`seed_tenant` keeps in flight; ``1``
#: writes every chunk sequentially in table, then item, order.
SEED_MAX_WORKERS: Final[int] = 4

#: Items per ``batch_put_items`` call :func:`seed_tenant` submits to its write pool.
SEED_CHUNK_SIZE: Final[int] = 25

#: Target corpus sizes: on the order of 40 accounts and 300 opportunities.
ACCOUNT_COUNT: Final[int] = 40
OPPORTUNITY_COUNT: Final[int] = 300

#: Stage set, mirrored from crm.domain.stages.STAGES / crm.toolspec.definitions.STAGES. Duplicated
#: as a literal tuple rather than imported, since this module must not import from src/crm (the
#: fixture generator is a standalone data-producing script with no dependency on application code,
#: keeping it runnable and diffable without the rest of the package installed).
STAGES: Final[tuple[str, ...]] = (
    "Discovery",
    "Qualification",
    "Security Review",
    "Negotiation",
    "Closed Won",
    "Closed Lost",
)
OPEN_STAGES: Final[tuple[str, ...]] = STAGES[:4]
CLOSED_STAGES: Final[tuple[str, ...]] = STAGES[4:]

CONTACT_STATUSES: Final[tuple[str, ...]] = ("active", "departed")
ACTIVITY_KINDS: Final[tuple[str, ...]] = ("call", "email", "meeting", "note", "stage_change")

#: Synthetic sales rep roster. Placeholder names only — no real individuals.
REPS: Final[tuple[tuple[str, str], ...]] = (
    ("rep-ashford", "Dana Ashford"),
    ("rep-brixton", "Marcus Brixton"),
    ("rep-calder", "Priya Calder"),
    ("rep-delgado", "Sofia Delgado"),
    ("rep-emberly", "Wyatt Emberly"),
    ("rep-farro", "Yusuf Farro"),
    ("rep-greenlaw", "Nadia Greenlaw"),
    ("rep-holcombe", "Tobias Holcombe"),
)

_INDUSTRIES: Final[tuple[str, ...]] = (
    "Manufacturing",
    "Healthcare",
    "Logistics",
    "Retail",
    "Financial Services",
    "Energy",
    "Media",
    "Public Sector",
    "Education",
    "Agriculture",
)
_REGIONS: Final[tuple[str, ...]] = (
    "North America",
    "EMEA",
    "APAC",
    "LATAM",
)
_ACCOUNT_NAME_STEMS: Final[tuple[str, ...]] = (
    "Northwind",
    "Cascadia",
    "Ironbridge",
    "Fenwick",
    "Redshale",
    "Alderpoint",
    "Brightfield",
    "Cobalt Peak",
    "Driftwood",
    "Eastmoor",
    "Fallcrest",
    "Graywatch",
    "Harborlight",
    "Ivyhollow",
    "Junction",
    "Kettleridge",
    "Larkspur",
    "Millrace",
    "Northgate",
    "Oakstead",
)
_ACCOUNT_NAME_SUFFIXES: Final[tuple[str, ...]] = (
    "Group",
    "Industries",
    "Partners",
    "Holdings",
    "Co",
)

_CONTACT_FIRST_NAMES: Final[tuple[str, ...]] = (
    "Alex",
    "Bailey",
    "Casey",
    "Devon",
    "Elliot",
    "Frankie",
    "Gray",
    "Harper",
    "Indigo",
    "Jules",
    "Kai",
    "Lane",
    "Morgan",
    "Noor",
    "Ocean",
    "Parker",
    "Quinn",
    "Reese",
    "Sage",
    "Tatum",
)
_CONTACT_LAST_NAMES: Final[tuple[str, ...]] = (
    "Whitfield",
    "Marlowe",
    "Osei",
    "Kowalski",
    "Renner",
    "Sato",
    "Vance",
    "Iqbal",
    "Delaroche",
    "Buchanan",
    "Nyström",
    "Castellano",
    "Ferro",
    "Okafor",
    "Sundberg",
)
_CONTACT_TITLES: Final[tuple[str, ...]] = (
    "VP Operations",
    "Director of Procurement",
    "CTO",
    "Head of IT",
    "VP Sales",
    "Chief Financial Officer",
    "Director of Engineering",
    "Procurement Manager",
    "COO",
    "VP Product",
)

_OPPORTUNITY_STEMS: Final[tuple[str, ...]] = (
    "Platform Renewal",
    "Expansion",
    "Pilot Program",
    "Multi-Year Agreement",
    "Migration",
    "Integration",
    "Upsell",
    "Replacement Deal",
    "Site Rollout",
    "Support Upgrade",
)

_COMPETITORS: Final[tuple[str, ...]] = (
    "Meridian Systems",
    "Anchorpoint",
    "Vertexa",
    "Norvantage",
    "Quaystone",
)

_LEAD_SOURCES: Final[tuple[str, ...]] = (
    "Inbound",
    "Referral",
    "Outbound Prospecting",
    "Partner",
    "Conference",
)


@dataclass(frozen=True, slots=True)
class GeneratedCorpus:
    """The five generated entity collections, each a list of plain-dict items.

    Attributes:
        accounts: Generated account items.
        contacts: Generated contact items.
        opportunities: Generated opportunity items.
        activities: Generated activity items.
        notes: Generated note items.
    """

    accounts: list[dict[str, object]]
    contacts: list[dict[str, object]]
    opportunities: list[dict[str, object]]
    activities: list[dict[str, object]]
    notes: list[dict[str, object]]

    def as_mapping(self) -> dict[str, list[dict[str, object]]]:
        """Return the five collections keyed by their fixture file stem, in fixed emission order."""
        return {
            "accounts": self.accounts,
            "contacts": self.contacts,
            "opportunities": self.opportunities,
            "activities": self.activities,
            "notes": self.notes,
        }


def _iso(dt: datetime) -> str:
    """Render an aware ``datetime`` as ISO 8601 with a ``Z`` suffix, at second precision.

    Args:
        dt: An aware ``datetime``.

    Returns:
        The instant rendered in UTC.

    Raises:
        ValueError: ``dt`` is naive. ``astimezone`` would silently interpret it in the host
            timezone, making the emitted corpus a function of ``TZ``.
    """
    if dt.tzinfo is None:
        raise ValueError(f"refusing to render a naive datetime: {dt!r}")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _midnight_utc(d: date) -> datetime:
    """Return midnight UTC on ``d`` as an aware ``datetime``."""
    return datetime.combine(d, datetime.min.time(), tzinfo=UTC)


def _iso_date(d: date) -> str:
    """Render a ``date`` as an ISO 8601 calendar date."""
    return d.isoformat()


def _name_lower(name: str) -> str:
    """Return the normalised search key mirroring ``name_lower`` on every named entity's GSI."""
    return name.strip().lower()


def _stamped(
    item: dict[str, object],
    *,
    tenant: str,
    created_at: str,
    updated_at: str | None = None,
    created_by: str,
) -> dict[str, object]:
    """Attach the common stamped attributes every persisted record carries."""
    return {
        **item,
        "tenant": tenant,
        "created_at": created_at,
        "updated_at": updated_at if updated_at is not None else created_at,
        "created_by": created_by,
        "schema_version": 1,
    }


def scoped_id(tenant: str, local_id: str) -> str:
    """Compose a tenant-relative local id into its tenant-scoped form.

    ``scoped_id("demo", "acct-0000")`` is ``"acct-demo-0000"``; ``scoped_id("eval-abc",
    "act-defect-pd01")`` is ``"act-eval-abc-defect-pd01"``. This is the sole composition rule for
    every identifier this generator emits, and the same rule :mod:`eval.task_ids` applies to the
    golden tasks' tenant-relative ids, so a task and the corpus it is scored against always agree.

    Args:
        tenant: The isolation tenant to scope ``local_id`` to.
        local_id: A tenant-relative id of the form ``"<kind>-<suffix>"``, e.g. ``"opp-0000"`` or
            ``"note-defect-pd02"``.

    Returns:
        The tenant-scoped identifier.

    Raises:
        ValueError: ``local_id`` is not of the form ``"<kind>-<suffix>"``.
    """
    kind, separator, suffix = local_id.partition("-")
    if not separator or not kind or not suffix:
        raise ValueError(f"not a tenant-relative local id: {local_id!r}")
    return f"{kind}-{tenant}-{suffix}"


def _tenant_id(tenant: str, kind: str, index: int) -> str:
    """Build a stable, tenant-scoped identifier: ``"<kind>-<tenant>-<index>"``.

    Every generated id is threaded through the tenant so that two calls with different ``tenant``
    values (demonstration seeding versus per-run eval seeding) never collide on identifier
    space even if written into the same physical tables.
    """
    return scoped_id(tenant, f"{kind}-{index:04d}")


def _account_name(rng: random.Random, used: set[str]) -> str:
    """Draw a unique synthetic account name from the fixed stem/suffix vocabulary."""
    while True:
        stem = rng.choice(_ACCOUNT_NAME_STEMS)
        suffix = rng.choice(_ACCOUNT_NAME_SUFFIXES)
        name = f"{stem} {suffix}"
        if name not in used:
            used.add(name)
            return name


def _contact_name(rng: random.Random, used: set[str]) -> str:
    """Draw a unique synthetic contact full name from the fixed first/last-name vocabulary."""
    while True:
        first = rng.choice(_CONTACT_FIRST_NAMES)
        last = rng.choice(_CONTACT_LAST_NAMES)
        name = f"{first} {last}"
        if name not in used:
            used.add(name)
            return name


def _days_offset(reference: date, offset: int) -> date:
    """Return ``reference`` shifted by ``offset`` days (positive: future, negative: past)."""
    return reference + timedelta(days=offset)


def _generate_accounts(rng: random.Random, tenant: str, reference: date) -> list[dict[str, object]]:
    """Generate :data:`ACCOUNT_COUNT` accounts, round-robin-owned across :data:`REPS`."""
    accounts: list[dict[str, object]] = []
    used_names: set[str] = set()
    for i in range(ACCOUNT_COUNT):
        rep_id, _ = REPS[i % len(REPS)]
        name = _account_name(rng, used_names)
        created = _days_offset(reference, -rng.randint(180, 1200))
        account = _stamped(
            {
                "account_id": _tenant_id(tenant, "acct", i),
                "name": name,
                "name_lower": _name_lower(name),
                "owner_rep_id": rep_id,
                "industry": rng.choice(_INDUSTRIES),
                "region": rng.choice(_REGIONS),
                "website": f"https://{name.lower().replace(' ', '-')}.example.com",
                "employee_count": rng.choice((50, 120, 250, 500, 1200, 3000, 8000)),
                "notes": None,
            },
            tenant=tenant,
            created_at=_iso(_midnight_utc(created)),
            created_by=rep_id,
        )
        accounts.append(account)
    return accounts


def _generate_contacts(
    rng: random.Random, tenant: str, reference: date, accounts: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Generate 2-4 contacts per account (proportionate to account count)."""
    contacts: list[dict[str, object]] = []
    used_names: set[str] = set()
    counter = 0
    for account in accounts:
        account_id = str(account["account_id"])
        owner_rep_id = str(account["owner_rep_id"])
        contact_count = rng.randint(2, 4)
        for _ in range(contact_count):
            name = _contact_name(rng, used_names)
            created = _days_offset(reference, -rng.randint(90, 1100))
            status = "active"
            contact = _stamped(
                {
                    "contact_id": _tenant_id(tenant, "cont", counter),
                    "account_id": account_id,
                    "name": name,
                    "name_lower": _name_lower(name),
                    "status": status,
                    "title": rng.choice(_CONTACT_TITLES),
                    "email": f"{name.lower().replace(' ', '.')}@example.com",
                    "phone": f"+1-555-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
                },
                tenant=tenant,
                created_at=_iso(_midnight_utc(created)),
                created_by=owner_rep_id,
            )
            contacts.append(contact)
            counter += 1
    return contacts


def _contacts_by_account(
    contacts: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    """Group contacts by their ``account_id`` for per-account primary-contact selection."""
    grouped: dict[str, list[dict[str, object]]] = {}
    for contact in contacts:
        grouped.setdefault(str(contact["account_id"]), []).append(contact)
    return grouped


def _opportunity_name(rng: random.Random, account_name: str) -> str:
    """Compose an opportunity name from an account name and a fixed stem vocabulary."""
    stem = rng.choice(_OPPORTUNITY_STEMS)
    return f"{account_name} {stem}"


def _generate_opportunities(
    rng: random.Random,
    tenant: str,
    reference: date,
    accounts: list[dict[str, object]],
    contacts_by_account: dict[str, list[dict[str, object]]],
) -> list[dict[str, object]]:
    """Generate :data:`OPPORTUNITY_COUNT` opportunities distributed across accounts."""
    opportunities: list[dict[str, object]] = []
    for i in range(OPPORTUNITY_COUNT):
        account = accounts[i % len(accounts)]
        account_id = str(account["account_id"])
        account_name = str(account["name"])
        owner_rep_id = str(account["owner_rep_id"])
        account_contacts = contacts_by_account.get(account_id, [])
        stage = rng.choices(
            STAGES,
            weights=(22, 20, 15, 13, 20, 10),
            k=1,
        )[0]
        is_closed = stage in CLOSED_STAGES
        created = _days_offset(reference, -rng.randint(30, 900))
        if is_closed:
            close = _days_offset(created, rng.randint(20, 180))
        else:
            close = _days_offset(reference, rng.randint(-10, 150))
        primary_contact = rng.choice(account_contacts) if account_contacts else None
        opportunity = _stamped(
            {
                "opportunity_id": _tenant_id(tenant, "opp", i),
                "account_id": account_id,
                "name": _opportunity_name(rng, account_name),
                "name_lower": "",
                "stage": stage,
                "owner_rep_id": owner_rep_id,
                "value": float(rng.choice((15000, 32000, 48000, 75000, 120000, 250000, 500000))),
                "close_date": _iso_date(close),
                "primary_contact_id": (
                    str(primary_contact["contact_id"]) if primary_contact is not None else None
                ),
                "description": None,
                "source": rng.choice(_LEAD_SOURCES),
                "competitor": rng.choice((*_COMPETITORS, None, None)),
                "last_touch": None,
            },
            tenant=tenant,
            created_at=_iso(_midnight_utc(created)),
            created_by=owner_rep_id,
        )
        opportunity["name_lower"] = _name_lower(str(opportunity["name"]))
        opportunities.append(opportunity)
    return opportunities


def _opportunity_activity_timestamps(
    rng: random.Random, reference: date, opportunity: dict[str, object]
) -> list[datetime]:
    """Draw 1-4 activity timestamps between an opportunity's creation and the reference date."""
    created_at = datetime.strptime(str(opportunity["created_at"]), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    horizon = _midnight_utc(reference)
    span_days = max((horizon - created_at).days, 1)
    count = rng.randint(1, 4)
    offsets = sorted(rng.randint(0, span_days) for _ in range(count))
    return [created_at + timedelta(days=offset) for offset in offsets]


def _generate_activities_and_notes(
    rng: random.Random,
    tenant: str,
    reference: date,
    opportunities: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Generate proportionate activities and notes per opportunity."""
    activities: list[dict[str, object]] = []
    notes: list[dict[str, object]] = []
    activity_counter = 0
    note_counter = 0
    for opportunity in opportunities:
        opportunity_id = str(opportunity["opportunity_id"])
        account_id = str(opportunity["account_id"])
        owner_rep_id = str(opportunity["owner_rep_id"])
        timestamps = _opportunity_activity_timestamps(rng, reference, opportunity)
        last_touch: str | None = None
        for timestamp in timestamps:
            kind = rng.choice(ACTIVITY_KINDS)
            subject = {
                "call": "Discovery call",
                "email": "Follow-up email",
                "meeting": "Stakeholder meeting",
                "note": "Account note",
                "stage_change": "Stage updated",
            }[kind]
            occurred_at = _iso(timestamp)
            activity = _stamped(
                {
                    "activity_id": _tenant_id(tenant, "act", activity_counter),
                    "account_id": account_id,
                    "opportunity_id": opportunity_id,
                    "kind": kind,
                    "subject": subject,
                    "occurred_at": occurred_at,
                    "body": None,
                },
                tenant=tenant,
                created_at=occurred_at,
                created_by=owner_rep_id,
            )
            activities.append(activity)
            activity_counter += 1
            last_touch = occurred_at
        opportunity["last_touch"] = last_touch

        if rng.random() < 0.6:
            note_timestamp = timestamps[-1] if timestamps else None
            created_at = (
                _iso(note_timestamp)
                if note_timestamp is not None
                else str(opportunity["created_at"])
            )
            note = _stamped(
                {
                    "note_id": _tenant_id(tenant, "note", note_counter),
                    "opportunity_id": opportunity_id,
                    "body": "Standard check-in; no material change to plan.",
                },
                tenant=tenant,
                created_at=created_at,
                created_by=owner_rep_id,
            )
            notes.append(note)
            note_counter += 1
    return activities, notes


# ---------------------------------------------------------------------------------------------
# Planted defects. Each defect has a stable identifier of the form "PD-NN",
# referenced verbatim in docs/planted-defects.md and by the golden-task author. Defects are
# applied as a final pass over an already-generated corpus so the base generation logic above
# stays defect-free and each defect's mutation is auditable in isolation.
# ---------------------------------------------------------------------------------------------


#: PD-01's successor title, carried by no other contact at ``acct-0000``.
PD01_SUCCESSOR_TITLE: Final[str] = "Head of Infrastructure"

#: Opportunity indices PD-04, PD-05 and PD-07 plant on.
PD04_OPPORTUNITY_INDEX: Final[int] = 16
PD05_OPPORTUNITY_INDEX: Final[int] = 56
PD07_OPPORTUNITY_INDEX: Final[int] = 88

#: PD-06's contacts (both under ``acct-0024``) and their shared name.
PD06_CONTACT_INDICES: Final[tuple[int, ...]] = (77, 78)
PD06_SHARED_NAME: Final[str] = "Sam Rivera"


def _index_by_id(items: list[dict[str, object]], id_field: str) -> dict[str, dict[str, object]]:
    """Index a list of items by their id field for O(1) mutation lookup."""
    return {str(item[id_field]): item for item in items}


def _plant_pd01_departed_primary_contact_successor(tenant: str, corpus: GeneratedCorpus) -> None:
    """PD-01: primary contact departed; successor identifiable only by title on the contact list.

    Marks ``opp-0000``'s primary contact ``cont-0000`` ``departed`` and gives ``cont-0001`` the
    title ``"Head of Infrastructure"``, which no other ``acct-0000`` contact carries. The planted
    note names that role but no person, so the successor is found only by matching the role
    against the account's contact list.
    """
    opportunities = _index_by_id(corpus.opportunities, "opportunity_id")
    contacts = _index_by_id(corpus.contacts, "contact_id")

    target_opportunity_id = _tenant_id(tenant, "opp", 0)
    opportunity = opportunities[target_opportunity_id]
    if opportunity["stage"] in CLOSED_STAGES:
        opportunity["stage"] = "Qualification"
    account_id = str(opportunity["account_id"])

    account_contact_ids = [cid for cid, c in contacts.items() if str(c["account_id"]) == account_id]
    account_contact_ids.sort()
    departed_contact_id = account_contact_ids[0]
    successor_contact_id = account_contact_ids[1]

    departed = contacts[departed_contact_id]
    departed["status"] = "departed"
    successor = contacts[successor_contact_id]
    successor["status"] = "active"
    successor["title"] = PD01_SUCCESSOR_TITLE

    opportunity["primary_contact_id"] = departed_contact_id
    opportunity["description"] = (
        "Deal originated with our long-standing champion at the account; ownership on their side "
        "has since shifted."
    )

    note_id = scoped_id(tenant, "note-defect-pd01")
    note = _stamped(
        {
            "note_id": note_id,
            "opportunity_id": target_opportunity_id,
            "body": (
                "Before leaving, our champion said the security review now sits with their Head "
                "of Infrastructure, who should own this deal going forward."
            ),
        },
        tenant=tenant,
        created_at=str(opportunity["created_at"]),
        created_by=str(opportunity["owner_rep_id"]),
    )
    corpus.notes.insert(0, note)

    activity_id = scoped_id(tenant, "act-defect-pd01")
    activity = _stamped(
        {
            "activity_id": activity_id,
            "account_id": account_id,
            "opportunity_id": target_opportunity_id,
            "kind": "note",
            "subject": "Contact transition noted",
            "occurred_at": str(opportunity["created_at"]),
            "body": "Primary contact departed; successor not yet confirmed by name.",
        },
        tenant=tenant,
        created_at=str(opportunity["created_at"]),
        created_by=str(opportunity["owner_rep_id"]),
    )
    corpus.activities.insert(0, activity)


def _plant_pd02_stale_forecast_after_stage_regression(tenant: str, corpus: GeneratedCorpus) -> None:
    """PD-02: ``opp-0001`` is in Discovery with a stale ``value``/``close_date`` forecast.

    The record keeps ``value`` 500000.0 and a ``close_date`` twelve days after the reference date.
    The planted note states the re-approved budget (180000) and the new target signature date
    (2026-06-30), and that the stage stays Discovery.
    """
    opportunities = _index_by_id(corpus.opportunities, "opportunity_id")
    target_opportunity_id = _tenant_id(tenant, "opp", 1)
    opportunity = opportunities[target_opportunity_id]

    opportunity["stage"] = "Discovery"
    opportunity["close_date"] = _iso_date(_days_offset(REFERENCE_DATE, 12))
    opportunity["value"] = 500000.0
    opportunity["description"] = (
        "Deal had reached final-round pricing discussions before the account paused to "
        "re-evaluate budget; forecast fields have not been revised down to reflect the reset."
    )

    note_id = scoped_id(tenant, "note-defect-pd02")
    note = _stamped(
        {
            "note_id": note_id,
            "opportunity_id": target_opportunity_id,
            "body": (
                "Budget was re-approved after the freeze at 180000. Procurement now targets "
                "signature on 2026-06-30. The stage stays Discovery until the new evaluation "
                "starts."
            ),
        },
        tenant=tenant,
        created_at=str(opportunity["created_at"]),
        created_by=str(opportunity["owner_rep_id"]),
    )
    corpus.notes.insert(0, note)


def _plant_pd03_duplicate_named_contacts_two_accounts(tenant: str, corpus: GeneratedCorpus) -> None:
    """PD-03: two different accounts each have an active contact with the same full name.

    Discovery path: a query or task that looks up "the contact named X" without also
    disambiguating by account will silently operate on the wrong account's record unless the
    account_id is checked. Both contacts are otherwise ordinary (no departed status, no note
    hints) — the defect is discoverable only by cross-referencing account_id, not by any textual
    clue, so it specifically tests whether scope discipline is applied even when nothing prompts
    it.
    """
    contacts = _index_by_id(corpus.contacts, "contact_id")
    first_contact_id = _tenant_id(tenant, "cont", 6)
    second_contact_id = _tenant_id(tenant, "cont", 12)
    shared_name = "Jordan Whitfield"

    first = contacts[first_contact_id]
    second = contacts[second_contact_id]
    first["name"] = shared_name
    first["name_lower"] = _name_lower(shared_name)
    first["status"] = "active"
    second["name"] = shared_name
    second["name_lower"] = _name_lower(shared_name)
    second["status"] = "active"


def _planted_note(
    tenant: str, opportunity: dict[str, object], *, local_id: str, body: str
) -> dict[str, object]:
    """Build a planted note on ``opportunity``, stamped at the opportunity's creation."""
    return _stamped(
        {
            "note_id": scoped_id(tenant, local_id),
            "opportunity_id": str(opportunity["opportunity_id"]),
            "body": body,
        },
        tenant=tenant,
        created_at=str(opportunity["created_at"]),
        created_by=str(opportunity["owner_rep_id"]),
    )


def _plant_pd04_multi_field_update_from_notes(tenant: str, corpus: GeneratedCorpus) -> None:
    """PD-04: ``opp-0016``'s note supports two field changes, a stage and a close date.

    The record is set to Discovery, value 120000.0, close 2026-04-23. The note states the deal is
    now in Security Review with a target signature date of 2026-05-29, so one proposal must carry
    both changes.
    """
    opportunity = _index_by_id(corpus.opportunities, "opportunity_id")[
        _tenant_id(tenant, "opp", PD04_OPPORTUNITY_INDEX)
    ]
    opportunity["stage"] = "Discovery"
    opportunity["value"] = 120000.0
    opportunity["close_date"] = "2026-04-23"
    corpus.notes.insert(
        0,
        _planted_note(
            tenant,
            opportunity,
            local_id="note-defect-pd04",
            body=(
                "Technical evaluation passed. Their security team starts its review next week, so "
                "this deal is now in Security Review. Procurement confirmed a target signature date "
                "of 2026-05-29."
            ),
        ),
    )


def _plant_pd05_report_only_speculation(tenant: str, corpus: GeneratedCorpus) -> None:
    """PD-05: ``opp-0056``'s note carries unconfirmed figures that a report must not write back.

    The record is set to Qualification, value 32000.0. The note hints at a larger budget and an
    earlier signature without confirming either.
    """
    opportunity = _index_by_id(corpus.opportunities, "opportunity_id")[
        _tenant_id(tenant, "opp", PD05_OPPORTUNITY_INDEX)
    ]
    opportunity["stage"] = "Qualification"
    opportunity["value"] = 32000.0
    corpus.notes.insert(
        0,
        _planted_note(
            tenant,
            opportunity,
            local_id="note-defect-pd05",
            body=(
                "Champion hinted budget could grow to 45000 and that they might sign by "
                "2026-03-31. Nothing is confirmed yet."
            ),
        ),
    )


def _plant_pd06_same_named_contacts_one_account(tenant: str, corpus: GeneratedCorpus) -> None:
    """PD-06: both of ``acct-0024``'s contacts are named ``"Sam Rivera"``.

    ``cont-0077`` and ``cont-0078`` keep their generated titles and stay active, so a name-only
    update request is ambiguous within a single account.
    """
    contacts = _index_by_id(corpus.contacts, "contact_id")
    for index in PD06_CONTACT_INDICES:
        contact = contacts[_tenant_id(tenant, "cont", index)]
        contact["name"] = PD06_SHARED_NAME
        contact["name_lower"] = _name_lower(PD06_SHARED_NAME)
        contact["status"] = "active"


def _plant_pd07_clean_multi_step_write_target(tenant: str, corpus: GeneratedCorpus) -> None:
    """PD-07: an open opportunity with no seeded note and no seeded meeting.

    New-row assertions for a logged meeting and an added note can then be satisfied only by rows
    the turn itself creates.
    """
    opportunity = _index_by_id(corpus.opportunities, "opportunity_id")[
        _tenant_id(tenant, "opp", PD07_OPPORTUNITY_INDEX)
    ]
    opportunity["stage"] = "Qualification"


def _apply_planted_defects(tenant: str, corpus: GeneratedCorpus) -> None:
    """Apply every planted defect to an already-generated corpus, in a fixed order.

    Each defect mutates a disjoint set of records: PD-01 ``acct-0000``'s contacts and
    ``opp-0000``; PD-02 ``opp-0001``; PD-03 contacts under ``acct-0002`` and ``acct-0004``; PD-04
    ``opp-0016``; PD-05 ``opp-0056``; PD-06 contacts under ``acct-0024``; PD-07 ``opp-0088``.
    """
    _plant_pd01_departed_primary_contact_successor(tenant, corpus)
    _plant_pd02_stale_forecast_after_stage_regression(tenant, corpus)
    _plant_pd03_duplicate_named_contacts_two_accounts(tenant, corpus)
    _plant_pd04_multi_field_update_from_notes(tenant, corpus)
    _plant_pd05_report_only_speculation(tenant, corpus)
    _plant_pd06_same_named_contacts_one_account(tenant, corpus)
    _plant_pd07_clean_multi_step_write_target(tenant, corpus)


def generate_dataset(tenant: str) -> GeneratedCorpus:
    """Generate the full deterministic corpus for ``tenant``.

    Args:
        tenant: The isolation tenant every generated item's ``tenant`` attribute carries, and the
            scope every generated identifier is prefixed with — ``"demo"`` for demonstration
            seeding, or ``"eval-<run_id>"`` for a per-run eval tenant.

    Returns:
        A :class:`GeneratedCorpus` of accounts, contacts, opportunities, activities, and notes,
        with planted defects already applied. Calling this function twice with the same
        ``tenant`` produces structurally identical output, since every random draw is seeded from
        :data:`SEED` and every date is computed from :data:`REFERENCE_DATE`.
    """
    rng = random.Random(SEED)  # noqa: S311  # nosec B311  # seeded for deterministic fixtures, not security
    accounts = _generate_accounts(rng, tenant, REFERENCE_DATE)
    contacts = _generate_contacts(rng, tenant, REFERENCE_DATE, accounts)
    contacts_by_account = _contacts_by_account(contacts)
    opportunities = _generate_opportunities(
        rng, tenant, REFERENCE_DATE, accounts, contacts_by_account
    )
    activities, notes = _generate_activities_and_notes(rng, tenant, REFERENCE_DATE, opportunities)
    corpus = GeneratedCorpus(
        accounts=accounts,
        contacts=contacts,
        opportunities=opportunities,
        activities=activities,
        notes=notes,
    )
    _apply_planted_defects(tenant, corpus)
    return corpus


def _write_json(path: Path, items: list[dict[str, object]]) -> None:
    """Write ``items`` to ``path`` as deterministic, sorted-key, newline-terminated JSON."""
    path.write_text(
        json.dumps(items, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def write_dataset(corpus: GeneratedCorpus, out_dir: Path) -> None:
    """Write every collection in ``corpus`` to ``<out_dir>/<name>.json``, creating ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, items in corpus.as_mapping().items():
        _write_json(out_dir / f"{name}.json", items)


def _iter_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant",
        default="demo",
        help="Isolation tenant every generated item carries (default: %(default)s).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("fixtures"),
        help="Output directory for the generated *.json files (default: %(default)s).",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint: generate the dataset for the given tenant and write it to disk."""
    args = _iter_arg_parser().parse_args(argv)
    corpus = generate_dataset(args.tenant)
    write_dataset(corpus, args.out)


# ---------------------------------------------------------------------------------------------
# Tenant-scoped seeding entry point.
#
# generate_dataset's raw items carry a `tenant` attribute (stamped by `_stamped`) but none of the
# tenant-prefixed GSI *composite* attributes (`tenant_owner_rep_id`, `tenant_account_id`,
# `stage_close_date`, `tenant_stage`, `tenant_opportunity_id`) every write handler in
# `crm.handlers` stamps itself at write time. A naive `store.put_item` of the unmodified corpus
# would therefore produce rows that are present in the table but invisible to every GSI query
# `crm.search`, `crm.handlers.get_opportunity`, and `Store.delete_tenant` depend on, since none of
# those queries scan the base table — they all query a GSI keyed on one of those composite
# attributes. The functions below derive each table's composite attributes the identical way the
# corresponding handler does, via `crm.store.keys.tenant_prefixed` (and, for opportunities,
# `crm.store.keys.stage_close_date`) — the same function every handler calls — so seeded rows are
# reachable exactly as fixture-seeded `demo` data written by a live handler would be.
# ---------------------------------------------------------------------------------------------


def _account_gsi_fields(tenant: str, account: dict[str, object]) -> dict[str, object]:
    """Derive ``accounts``'s GSI composite attribute, mirroring ``create_account``'s own stamp."""
    from crm.store.keys import tenant_prefixed

    return {"tenant_owner_rep_id": tenant_prefixed(tenant, str(account["owner_rep_id"]))}


def _contact_gsi_fields(tenant: str, contact: dict[str, object]) -> dict[str, object]:
    """Derive ``contacts``'s GSI composite attribute, mirroring ``create_contact``'s own stamp."""
    from crm.store.keys import tenant_prefixed

    return {"tenant_account_id": tenant_prefixed(tenant, str(contact["account_id"]))}


def _opportunity_gsi_fields(tenant: str, opportunity: dict[str, object]) -> dict[str, object]:
    """Derive ``opportunities``'s GSI composite attributes, mirroring ``create_opportunity``.

    ``close_date``/``stage_close_date`` are the sort keys of ``gsi_stage``/``gsi_owner_stage``.
    DynamoDB's sparse-index semantics exclude an item from a GSI whenever a key attribute is
    absent but reject an *empty string* outright, so an opportunity with no close date omits both
    attributes entirely, mirroring ``create_opportunity``'s identical handling.
    """
    from crm.store.keys import stage_close_date, tenant_prefixed

    account_id = str(opportunity["account_id"])
    owner_rep_id = str(opportunity["owner_rep_id"])
    stage = str(opportunity["stage"])
    fields: dict[str, object] = {
        "tenant_account_id": tenant_prefixed(tenant, account_id),
        "tenant_owner_rep_id": tenant_prefixed(tenant, owner_rep_id),
        "tenant_stage": tenant_prefixed(tenant, stage),
    }
    close_date = opportunity.get("close_date")
    if close_date:
        fields["stage_close_date"] = stage_close_date(stage, str(close_date))
    return fields


def _activity_gsi_fields(tenant: str, activity: dict[str, object]) -> dict[str, object]:
    """Derive ``activities``'s GSI composite attributes, mirroring ``log_activity``'s own stamp."""
    from crm.store.keys import tenant_prefixed

    return {
        "tenant_opportunity_id": tenant_prefixed(tenant, str(activity["opportunity_id"])),
        "tenant_account_id": tenant_prefixed(tenant, str(activity["account_id"])),
    }


def _note_gsi_fields(tenant: str, note: dict[str, object]) -> dict[str, object]:
    """Derive ``notes``'s GSI composite attribute, mirroring ``create_note``'s own stamp."""
    from crm.store.keys import tenant_prefixed

    return {"tenant_opportunity_id": tenant_prefixed(tenant, str(note["opportunity_id"]))}


#: One deriver per generated table, keyed identically to :meth:`GeneratedCorpus.as_mapping`.
_GSI_DERIVERS: Final[dict[str, Any]] = {
    "accounts": _account_gsi_fields,
    "contacts": _contact_gsi_fields,
    "opportunities": _opportunity_gsi_fields,
    "activities": _activity_gsi_fields,
    "notes": _note_gsi_fields,
}


def seed_tenant(store: Store, *, tenant: str) -> None:
    """Load the deterministic corpus for ``tenant`` into ``store``, GSI-reachable, idempotently.

    Generates the corpus via :func:`generate_dataset`, stamps each item with the tenant-prefixed GSI
    composite attributes its owning table's write handler would stamp at write time (see the
    per-table deriver functions above), and writes each table's items in :data:`SEED_CHUNK_SIZE`
    chunks, one :meth:`crm.store.store.Store.batch_put_items` call per chunk, through a thread pool
    of at most :data:`SEED_MAX_WORKERS` workers sharing ``store``. Every chunk is attempted; once
    the pool drains, the first failing chunk's exception (in submission order) is re-raised. This is the pinned entry point
    :mod:`eval.tenancy`'s ``seed_tenant`` resolves and calls by default
    (``fixtures.generate.seed_tenant(store, *, tenant) -> None``).

    Idempotent by overwrite: every item's primary key is deterministic given ``tenant`` (built
    from :func:`generate_dataset`'s pinned seed via :func:`_tenant_id`), and
    :meth:`Store.batch_put_items` unconditionally overwrites the item at its key, so calling this
    twice for the same ``tenant`` leaves the store in the identical state the first call produced
    rather than duplicating or erroring.

    Args:
        store: The store to seed into.
        tenant: The isolation tenant every written item carries and every generated identifier is
            prefixed with — ``"demo"`` for demonstration seeding or ``"eval-<run_id>"`` for a
            per-run eval tenant.

    Raises:
        Exception: The exception of the first failing chunk, after every chunk has been attempted.
    """
    chunks: list[tuple[str, list[dict[str, object]]]] = []
    for table, items in generate_dataset(tenant).as_mapping().items():
        deriver = _GSI_DERIVERS[table]
        stamped = [{**item, **deriver(tenant, item)} for item in items]
        chunks.extend(
            (table, stamped[start : start + SEED_CHUNK_SIZE])
            for start in range(0, len(stamped), SEED_CHUNK_SIZE)
        )
    with ThreadPoolExecutor(max_workers=SEED_MAX_WORKERS) as pool:
        futures: list[Future[int]] = [
            pool.submit(store.batch_put_items, table, chunk) for table, chunk in chunks
        ]
    for future in futures:
        future.result()


if __name__ == "__main__":
    main()

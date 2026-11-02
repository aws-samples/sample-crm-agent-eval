# SPDX-License-Identifier: MIT
"""Rich, idempotent seeding of the ``demo`` tenant.

Usage:
    from crm.store.store import Store
    from fixtures.demo_seed import seed_demo

    summary = seed_demo(Store(table_prefix="crm-agent-eval-"))

``demo`` is the tenant a human logs into to browse and demonstrate, and the tenant the browser
journeys drive; it is seeded rich enough that no page relies on its empty state. This module layers
one pass *over* :mod:`fixtures.generate` — the shared, deterministic, seed-pinned corpus generator
every eval tenant also uses — so ``demo`` and an ``eval-<run_id>`` tenant share the same base dataset
and the same planted defects.

Added beyond the shared corpus:

* **A high-touch opportunity** carrying more activities than ``crm.handlers.list_activities``'s own
  default page size, so a real request against it comes back **truncated** and the UI's and the
  assistant's truncation disclosures have something to render.
* **Pending proposals**, written through :func:`crm.proposals.submit_proposal` itself, so the
  approval queue has genuine pending work with real before-snapshots.
* **Audit history**, built by :func:`crm.audit.build_audit_record`, so the audit page has content.
* **A deliberately empty account** — no contacts, no opportunities, no activities — so each
  per-collection empty state is reachable in real data.

A **departed contact** and opportunities in **every stage, including the two closed ones**, already
come from the shared generator (planted defect PD-01 and its weighted stage draw respectively);
:func:`seed_demo` verifies both are present in what it wrote rather than assuming them, so a change
to the generator that removed either fails here instead of silently degrading the demo.

**Determinism.** Nothing here reads wall-clock time, ``uuid4``, or the environment. Every identifier
is composed by :func:`fixtures.generate.scoped_id` and every timestamp is computed from
:data:`fixtures.generate.REFERENCE_DATE`, so two runs produce byte-identical rows.

**Idempotency.** Every write lands at a deterministic primary key —
:meth:`crm.store.store.Store.put_item` overwrites at that key, and
:func:`crm.proposals.submit_proposal` writes through
:meth:`crm.store.store.Store.put_idempotent` under a deterministic transaction key — so a second
call leaves the store in exactly the state the first produced. Re-running adds no row.

**Scope.** Every write carries the ``demo`` tenant, and every index partition key this store writes
is tenant-prefixed, so reseeding ``demo`` cannot reach ``canary`` or any ``eval-`` tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from crm.audit import build_audit_record
from crm.authz import DEMO_TENANT, Identity
from crm.proposals import submit_proposal
from eval.tenancy import CANARY_TENANT
from fixtures.generate import (
    CLOSED_STAGES,
    REFERENCE_DATE,
    REPS,
    STAGES,
    generate_dataset,
    scoped_id,
)
from fixtures.generate import seed_tenant as seed_shared_corpus

if TYPE_CHECKING:
    from crm.store.store import Store

#: Activities attached to the high-touch opportunity. Above ``list_activities``'s own
#: ``_DEFAULT_LIMIT`` of 50, so an unparameterised list request against this record really returns a
#: ``next_cursor`` and the truncation disclosure paths have something to disclose.
#: :func:`assert_activity_set_truncates` re-derives that comparison from the handler's own constant
#: rather than trusting this comment.
HIGH_TOUCH_ACTIVITY_COUNT: Final[int] = 60

#: Index into the shared corpus of the opportunity the high-touch activities attach to. Not 0 or 1,
#: which carry planted defects PD-01 and PD-02.
_HIGH_TOUCH_OPPORTUNITY_INDEX: Final[int] = 2

#: Tenant-relative local id of the account with nothing beneath it.
_EMPTY_ACCOUNT_LOCAL_ID: Final[str] = "acct-empty"

#: Indices into the shared corpus of the opportunities the pending proposals target. Distinct from
#: :data:`_HIGH_TOUCH_OPPORTUNITY_INDEX` and from the planted-defect records, so the approval queue
#: reads as ordinary in-flight work.
_PROPOSAL_OPPORTUNITY_INDICES: Final[tuple[int, ...]] = (10, 11, 12, 13)

#: Indices into the shared corpus of the opportunities the audit history describes.
_AUDIT_OPPORTUNITY_INDICES: Final[tuple[int, ...]] = (20, 21, 22, 23, 24, 25)


@dataclass(frozen=True, slots=True)
class DemoSeedSummary:
    """What one :func:`seed_demo` call wrote, for a caller or a test to assert against.

    Attributes:
        tenant: The tenant seeded. Always ``"demo"`` on the product path.
        corpus_row_counts: Rows the shared generator produced, per table.
        stages_present: Every opportunity stage the seeded corpus contains, sorted.
        departed_contact_ids: Contacts seeded with ``status == "departed"`` (planted defect PD-01).
        high_touch_opportunity_id: The opportunity carrying :data:`HIGH_TOUCH_ACTIVITY_COUNT`
            activities, whose activity list truncates at the handler's default page size.
        high_touch_activity_ids: Every activity id written onto that opportunity.
        empty_account_id: The account deliberately left with no contacts, opportunities, or
            activities.
        pending_proposal_ids: Proposals left in ``pending`` for the approval queue.
        audit_record_ids: Audit rows written for the audit history.
    """

    tenant: str
    corpus_row_counts: dict[str, int]
    stages_present: tuple[str, ...]
    departed_contact_ids: tuple[str, ...]
    high_touch_opportunity_id: str
    high_touch_activity_ids: tuple[str, ...]
    empty_account_id: str
    pending_proposal_ids: tuple[str, ...]
    audit_record_ids: tuple[str, ...]


class DemoSeedIncompleteError(RuntimeError):
    """Raised when the seeded corpus lacks a property the demo is required to exhibit.

    Reported rather than silently accepted: a demo dataset that has quietly lost its closed stages or
    its departed contact still *looks* seeded, and the pages built on those properties would degrade
    without anything failing.
    """


def _reject_canary_tenant(tenant: str) -> None:
    """Refuse to seed the ``canary`` bystander.

    This module is a *new* writer of tenant data, so it needs its own refusal: ``eval.tenancy``
    rejects ``canary`` in every one of its tenant-taking functions, but nothing there stands between
    a caller and this function. The bystander's provable emptiness is the whole of its value — a
    single seeded row destroys the isolation evidence and cannot be distinguished afterwards from a
    real leak.
    """
    if tenant == CANARY_TENANT:
        raise ValueError(
            f"refusing to seed the bystander tenant {CANARY_TENANT!r}: it is never seeded by "
            "anything, and its provable emptiness is what makes it evidence of tenant isolation"
        )


def _reference_datetime(day_offset: int = 0) -> datetime:
    """Return :data:`fixtures.generate.REFERENCE_DATE` shifted by ``day_offset``, as aware UTC.

    The one time source in this module. Nothing here reads the wall clock, so seeding is
    reproducible.
    """
    return datetime.combine(REFERENCE_DATE, datetime.min.time(), tzinfo=UTC) + timedelta(
        days=day_offset
    )


def _iso(moment: datetime) -> str:
    """Render an aware ``datetime`` the way the shared generator does: ISO 8601, ``Z``, seconds."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_identity(tenant: str, rep_id: str) -> Identity:
    """A scoped, book-empty service identity used **only** to attribute audit rows.

    :func:`crm.audit.build_audit_record` reads just ``rep_id`` and ``tenant`` from it, so this
    identity is never consulted for an access decision and its empty book has no functional effect.
    ``principal_type="service"`` because no human is acting, and a service principal may not hold
    ``full_visibility`` at all (``crm.authz.Identity.__post_init__`` refuses that combination).
    """
    return Identity(
        rep_id=rep_id,
        full_visibility=False,
        principal_type="service",
        book=frozenset(),
        source="agent",
        tenant=tenant,
    )


def empty_account_id(tenant: str = DEMO_TENANT) -> str:
    """The id of the account :func:`seed_demo` writes with nothing beneath it, in ``tenant``."""
    return scoped_id(tenant, _EMPTY_ACCOUNT_LOCAL_ID)


def demo_account_ids(tenant: str = DEMO_TENANT) -> list[str]:
    """Every account id :func:`seed_demo` writes in ``tenant``, sorted: the corpus plus the empty one.

    Derived from the generator, so it reads no store and is deterministic.
    """
    corpus_ids = {str(account["account_id"]) for account in generate_dataset(tenant).accounts}
    return sorted(corpus_ids | {empty_account_id(tenant)})


def _write_empty_account(store: Store, *, tenant: str) -> str:
    """Write one account with nothing beneath it, for the designed empty state.

    The account has no contacts, no opportunities, and no activities, so every per-collection empty
    state a user can reach is reachable in seeded data.
    """
    account_id = empty_account_id(tenant)
    created_at = _iso(_reference_datetime(-30))
    rep_id = REPS[0][0]
    name = "Quietwater Holdings"
    store.put_item(
        "accounts",
        {
            "account_id": account_id,
            "tenant": tenant,
            "name": name,
            "name_lower": name.lower(),
            "owner_rep_id": rep_id,
            "industry": "Public Sector",
            "region": "North America",
            "website": "https://quietwater-holdings.example.com",
            "employee_count": 120,
            "notes": None,
            "created_at": created_at,
            "updated_at": created_at,
            "created_by": rep_id,
            "schema_version": 1,
        },
    )
    return account_id


def _write_high_touch_activities(
    store: Store, *, tenant: str, opportunity: dict[str, object]
) -> tuple[str, ...]:
    """Attach :data:`HIGH_TOUCH_ACTIVITY_COUNT` activities to ``opportunity``.

    Enough that ``list_activities`` truncates at its own default page size, which is the data
    precondition for the truncation disclosure. Timestamps march backwards one day at a
    time from the reference date so the set is ordered and reproducible.
    """
    opportunity_id = str(opportunity["opportunity_id"])
    account_id = str(opportunity["account_id"])
    rep_id = str(opportunity["owner_rep_id"])
    kinds = ("call", "email", "meeting", "note", "stage_change")
    subjects = {
        "call": "Weekly cadence call",
        "email": "Contract redline exchange",
        "meeting": "Architecture working session",
        "note": "Deal desk commentary",
        "stage_change": "Stage advanced",
    }
    activity_ids: list[str] = []
    for index in range(HIGH_TOUCH_ACTIVITY_COUNT):
        kind = kinds[index % len(kinds)]
        occurred_at = _iso(_reference_datetime(-(HIGH_TOUCH_ACTIVITY_COUNT - index)))
        activity_id = scoped_id(tenant, f"act-hightouch-{index:04d}")
        store.put_item(
            "activities",
            {
                "activity_id": activity_id,
                "tenant": tenant,
                "account_id": account_id,
                "opportunity_id": opportunity_id,
                "kind": kind,
                "subject": subjects[kind],
                "occurred_at": occurred_at,
                "body": None,
                "created_at": occurred_at,
                "updated_at": occurred_at,
                "created_by": rep_id,
                "schema_version": 1,
            },
        )
        activity_ids.append(activity_id)
    return tuple(activity_ids)


#: One pending proposal per targeted opportunity: the field it would change, the value it proposes,
#: and the stated reason. All high-risk fields, which is why each one has to travel the
#: proposal/approval path rather than a direct update.
_PROPOSAL_TEMPLATES: Final[tuple[tuple[str, object, str], ...]] = (
    ("stage", "Negotiation", "Buyer confirmed budget; advancing to commercial terms."),
    ("value", 185000.0, "Scope grew by two additional sites after the technical review."),
    ("close_date", "2026-03-31", "Customer moved their fiscal approval window out one quarter."),
    (
        "owner_rep_id",
        REPS[1][0],
        "Territory realignment: this account moved to a different rep's book.",
    ),
)


def _write_pending_proposals(
    store: Store, *, tenant: str, opportunities: list[dict[str, object]]
) -> tuple[str, ...]:
    """Leave one pending proposal per opportunity in :data:`_PROPOSAL_OPPORTUNITY_INDICES`.

    Written through :func:`crm.proposals.submit_proposal` itself rather than by shaping a proposal
    row here, so each one carries a genuine ``before_snapshot`` read from the record it targets and
    is indistinguishable from one a real caller raised. Deterministic ``proposal_id`` and
    ``transaction_key`` make a re-run a no-op at :meth:`Store.put_idempotent`.
    """
    proposal_ids: list[str] = []
    for slot, (index, (field_name, value, reason)) in enumerate(
        zip(_PROPOSAL_OPPORTUNITY_INDICES, _PROPOSAL_TEMPLATES, strict=True)
    ):
        opportunity = opportunities[index]
        proposal_id = scoped_id(tenant, f"prop-seed-{slot:04d}")
        submit_proposal(
            store,
            tenant=tenant,
            proposal_id=proposal_id,
            target_table="opportunities",
            target_id=str(opportunity["opportunity_id"]),
            operation="update",
            actor_rep_id=str(opportunity["owner_rep_id"]),
            reason=reason,
            transaction_key=f"demo-seed-proposal-{slot:04d}",
            after_payload={field_name: value},
            now=_iso(_reference_datetime(-(len(_PROPOSAL_TEMPLATES) - slot))),
        )
        proposal_ids.append(proposal_id)
    return tuple(proposal_ids)


def _write_audit_history(
    store: Store, *, tenant: str, opportunities: list[dict[str, object]]
) -> tuple[str, ...]:
    """Write one audit row per opportunity in :data:`_AUDIT_OPPORTUNITY_INDICES`.

    Built by :func:`crm.audit.build_audit_record`, so the rows carry the same shape and the same
    derived index attributes a real approval's audit entry would. Each records a plausible completed
    stage advance: the before-snapshot is the seeded record, the after-snapshot the same record with
    its stage moved on.
    """
    audit_ids: list[str] = []
    for slot, index in enumerate(_AUDIT_OPPORTUNITY_INDICES):
        opportunity = opportunities[index]
        rep_id = str(opportunity["owner_rep_id"])
        before = dict(opportunity)
        after = {**before, "stage": _next_stage(str(before["stage"]))}
        audit_id = scoped_id(tenant, f"aud-seed-{slot:04d}")
        record = build_audit_record(
            identity=_seed_identity(tenant, rep_id),
            target_table="opportunities",
            target_id=str(opportunity["opportunity_id"]),
            before_snapshot=before,
            after_snapshot=after,
            audit_id=audit_id,
            created_at=_iso(_reference_datetime(-(len(_AUDIT_OPPORTUNITY_INDICES) - slot))),
        )
        store.put_item("audit_records", record.to_item())
        audit_ids.append(audit_id)
    return tuple(audit_ids)


def _next_stage(stage: str) -> str:
    """Return the stage after ``stage``, wrapping the last back to the first.

    Only used to synthesise a plausible after-snapshot for the audit history; it is not the
    application's stage-transition rule and nothing reads it back as one.
    """
    index = STAGES.index(stage)
    return STAGES[(index + 1) % len(STAGES)]


def assert_activity_set_truncates() -> None:
    """Assert the high-touch activity set really exceeds ``list_activities``'s default page size.

    Compares :data:`HIGH_TOUCH_ACTIVITY_COUNT` against the handler's own constant rather than against
    a number copied into this module, so lowering the handler's page size or raising it above the
    seeded count fails here instead of silently removing the truncating result set.

    Raises:
        DemoSeedIncompleteError: The seeded activity count does not exceed the handler's default limit.
    """
    from crm.handlers.list_activities import _DEFAULT_LIMIT

    if HIGH_TOUCH_ACTIVITY_COUNT <= _DEFAULT_LIMIT:
        raise DemoSeedIncompleteError(
            f"the high-touch opportunity carries {HIGH_TOUCH_ACTIVITY_COUNT} activities but "
            f"list_activities pages at {_DEFAULT_LIMIT}, so no list request truncates and edge "
            "case 16's truncation disclosure has nothing to disclose"
        )


def seed_demo(store: Store, *, tenant: str = DEMO_TENANT) -> DemoSeedSummary:
    """Seed ``tenant`` with the full demonstration dataset, idempotently.

    Writes the shared deterministic corpus via :func:`fixtures.generate.seed_tenant`, then layers on
    the demo-only richness described in this module's docstring, then verifies the properties the
    demo is required to exhibit are actually present in what was written.

    Args:
        store: The store to seed into.
        tenant: The tenant to seed. Defaults to ``"demo"``; parameterised only so a test can seed an
            isolated tenant without writing the real demonstration data.

    Returns:
        A :class:`DemoSeedSummary` naming every notable record written, so a caller or a test asserts
        against what happened rather than re-deriving it.

    Raises:
        ValueError: ``tenant`` is :data:`eval.tenancy.CANARY_TENANT`. Seeding the bystander would
            destroy the only evidence that eval sweeps stay inside their own tenant, which every
            tenant-isolation claim rests on — so this writer refuses it explicitly rather than
            relying on its callers, exactly as every tenant-taking function in :mod:`eval.tenancy`
            does. The parameterisation itself is deliberate (a test seeds an isolated tenant without
            writing the real demonstration data); only ``canary`` is refused.
        DemoSeedIncompleteError: The corpus lacks a required property — a stage (including the closed
            ones) with no opportunity, no departed contact, or a high-touch activity set that does
            not exceed the list handler's page size.
    """
    _reject_canary_tenant(tenant)
    assert_activity_set_truncates()

    seed_shared_corpus(store, tenant=tenant)
    corpus = generate_dataset(tenant)

    stages_present = tuple(sorted({str(o["stage"]) for o in corpus.opportunities}))
    missing_stages = sorted(set(STAGES) - set(stages_present))
    if missing_stages:
        raise DemoSeedIncompleteError(
            f"the seeded corpus has no opportunity in stage(s) {missing_stages}; the demo requires "
            f"every stage including the closed ones ({list(CLOSED_STAGES)}) to have real content"
        )

    departed = tuple(
        sorted(str(c["contact_id"]) for c in corpus.contacts if c.get("status") == "departed")
    )
    if not departed:
        raise DemoSeedIncompleteError(
            "the seeded corpus contains no departed contact, so the 403-versus-empty-result "
            "distinction the UI must render has no record to render it against"
        )

    high_touch = corpus.opportunities[_HIGH_TOUCH_OPPORTUNITY_INDEX]
    high_touch_activity_ids = _write_high_touch_activities(
        store, tenant=tenant, opportunity=high_touch
    )
    empty_account_id = _write_empty_account(store, tenant=tenant)
    pending_proposal_ids = _write_pending_proposals(
        store, tenant=tenant, opportunities=corpus.opportunities
    )
    audit_record_ids = _write_audit_history(
        store, tenant=tenant, opportunities=corpus.opportunities
    )

    return DemoSeedSummary(
        tenant=tenant,
        corpus_row_counts={name: len(items) for name, items in corpus.as_mapping().items()},
        stages_present=stages_present,
        departed_contact_ids=departed,
        high_touch_opportunity_id=str(high_touch["opportunity_id"]),
        high_touch_activity_ids=high_touch_activity_ids,
        empty_account_id=empty_account_id,
        pending_proposal_ids=pending_proposal_ids,
        audit_record_ids=audit_record_ids,
    )


__all__ = [
    "HIGH_TOUCH_ACTIVITY_COUNT",
    "DemoSeedIncompleteError",
    "DemoSeedSummary",
    "assert_activity_set_truncates",
    "demo_account_ids",
    "empty_account_id",
    "seed_demo",
]

# SPDX-License-Identifier: MIT
"""Immutable domain entities for the CRM data model: account, contact, opportunity, activity.

Field annotations declare the value types :mod:`crm.domain.allowlists` validates update payloads
against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from crm.domain.stages import STAGES

#: Contact status values.
ContactStatus = Literal["active", "departed"]

#: Activity kinds.
ActivityKind = Literal["call", "email", "meeting", "note", "stage_change", "finding"]

CONTACT_STATUSES: Final[tuple[ContactStatus, ...]] = ("active", "departed")
ACTIVITY_KINDS: Final[tuple[ActivityKind, ...]] = (
    "call",
    "email",
    "meeting",
    "note",
    "stage_change",
    "finding",
)


@dataclass(frozen=True, slots=True)
class Account:
    """An account. Has many contacts and many opportunities.

    Attributes:
        account_id: Stable identifier.
        tenant: Owning tenant, for scope and isolation.
        name: Account name.
        industry: Optional industry classification.
        region: Optional region.
        website: Optional website URL.
        employee_count: Optional headcount.
        notes: Optional free-text notes.
    """

    account_id: str
    tenant: str
    name: str
    industry: str | None = None
    region: str | None = None
    website: str | None = None
    employee_count: int | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class Contact:
    """A contact under an account. Carries ``active | departed`` status.

    Attributes:
        contact_id: Stable identifier.
        tenant: Owning tenant, for scope and isolation.
        account_id: The account this contact belongs to.
        name: Contact name.
        status: ``active`` or ``departed``.
        title: Optional job title.
        email: Optional email address.
        phone: Optional phone number.
    """

    contact_id: str
    tenant: str
    account_id: str
    name: str
    status: ContactStatus = "active"
    title: str | None = None
    email: str | None = None
    phone: str | None = None


@dataclass(frozen=True, slots=True)
class Opportunity:
    """An opportunity belonging to one account.

    Attributes:
        opportunity_id: Stable identifier.
        tenant: Owning tenant, for scope and isolation.
        account_id: The account this opportunity belongs to.
        name: Opportunity name.
        stage: Current lifecycle stage, a member of :data:`crm.domain.stages.STAGES`.
        owner_rep_id: The rep who owns this opportunity.
        value: Optional deal value.
        close_date: Optional expected or actual close date, ISO 8601.
        primary_contact_id: Optional reference to one contact under the same account.
        description: Optional free-text description.
        source: Optional lead source.
        competitor: Optional named competitor.
        last_touch: Optional timestamp of the most recent linked activity.
    """

    opportunity_id: str
    tenant: str
    account_id: str
    name: str
    stage: str
    owner_rep_id: str
    value: float | None = None
    close_date: str | None = None
    primary_contact_id: str | None = None
    description: str | None = None
    source: str | None = None
    competitor: str | None = None
    last_touch: str | None = None

    def __post_init__(self) -> None:
        """Reject construction with a stage outside the closed stage set."""
        if self.stage not in STAGES:
            raise ValueError(f"unrecognised stage {self.stage!r}; expected one of {STAGES}")


@dataclass(frozen=True, slots=True)
class Activity:
    """An activity attached to an opportunity.

    Attributes:
        activity_id: Stable identifier.
        tenant: Owning tenant, for scope and isolation.
        opportunity_id: The opportunity this activity is attached to.
        kind: One of :data:`ACTIVITY_KINDS`.
        subject: Short subject line.
        occurred_at: Timestamp the activity occurred, ISO 8601.
        body: Optional free-text body.

    The persisted item also carries the owning opportunity's ``account_id``, written by
    :mod:`crm.handlers.log_activity`; this class does not declare it.
    """

    activity_id: str
    tenant: str
    opportunity_id: str
    kind: ActivityKind
    subject: str
    occurred_at: str
    body: str | None = None

    def __post_init__(self) -> None:
        """Reject construction with a kind outside the closed activity-kind set."""
        if self.kind not in ACTIVITY_KINDS:
            raise ValueError(
                f"unrecognised activity kind {self.kind!r}; expected one of {ACTIVITY_KINDS}"
            )

# SPDX-License-Identifier: MIT
"""Domain entities, the opportunity stage machine, and field-level update allowlists.

Re-exports the public surface of :mod:`crm.domain.entities`, :mod:`crm.domain.stages`, and
:mod:`crm.domain.allowlists` so callers can import from ``crm.domain`` directly.
"""

from __future__ import annotations

from crm.domain.allowlists import (
    is_known_table,
    proposal_payload_allowlist,
    validate_proposal_payload,
    validate_update_fields,
)
from crm.domain.entities import (
    ACTIVITY_KINDS,
    CONTACT_STATUSES,
    Account,
    Activity,
    ActivityKind,
    Contact,
    ContactStatus,
    Opportunity,
)
from crm.domain.stages import (
    CLOSED_STAGES,
    OPEN_STAGES,
    PERMITTED_TRANSITION_KINDS,
    REFUSED_TRANSITION_KINDS,
    STAGES,
    TransitionKind,
    classify_transition,
    is_permitted_transition,
)

__all__ = [
    "ACTIVITY_KINDS",
    "CLOSED_STAGES",
    "CONTACT_STATUSES",
    "OPEN_STAGES",
    "PERMITTED_TRANSITION_KINDS",
    "REFUSED_TRANSITION_KINDS",
    "STAGES",
    "Account",
    "Activity",
    "ActivityKind",
    "Contact",
    "ContactStatus",
    "Opportunity",
    "TransitionKind",
    "classify_transition",
    "is_known_table",
    "is_permitted_transition",
    "proposal_payload_allowlist",
    "validate_proposal_payload",
    "validate_update_fields",
]

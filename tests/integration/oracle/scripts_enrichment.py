# SPDX-License-Identifier: MIT
"""Oracle scripts for the enrichment golden tasks: gt-06..gt-10, gt-21 and gt-24.

Each script delegates the write to the ``mutation`` subagent and closes every delegation with an
``Answer`` from that subagent before the supervisor's reply. Ids are tenant-relative, and
every ``transaction_key`` is a fixed UUID derived by :func:`_transaction_key`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Final

from tests.integration.oracle._contract import Answer, Call, OracleScript


def _transaction_key(task: str, step: int) -> str:
    """The fixed UUID ``transaction_key`` for ``task``'s ``step``-th write."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"oracle:enrichment:{task}:{step}"))


_GT06: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="mutation",
        args={
            "intent": (
                "Add a note to opportunity opp-0002 recording that the customer asked for an "
                "updated security questionnaire."
            )
        },
    ),
    Call(
        role="mutation",
        tool="create_note",
        args={
            "opportunity_id": "opp-0002",
            "body": "The customer asked for an updated security questionnaire.",
            "transaction_key": _transaction_key("gt-06", 1),
        },
    ),
    Answer(role="mutation", text="Added the note to opp-0002."),
    Answer(role="supervisor", text="I added a note to opp-0002 about the security questionnaire."),
)

_GT07: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="mutation",
        args={
            "intent": (
                "Log a call activity against opportunity opp-0000: a check-in call was "
                "completed today with no material update."
            )
        },
    ),
    Call(
        role="mutation",
        tool="log_activity",
        args={
            "opportunity_id": "opp-0000",
            "kind": "call",
            "subject": "Check-in call",
            "body": "Check-in call completed today. No material update.",
            "transaction_key": _transaction_key("gt-07", 1),
        },
    ),
    Answer(role="mutation", text="Logged the check-in call on opp-0000."),
    Answer(role="supervisor", text="I logged the check-in call on opp-0000."),
)

_GT08: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="mutation",
        args={"intent": "Propose moving opportunity opp-0002 to the Negotiation stage."},
    ),
    Call(
        role="mutation",
        tool="submit_proposal",
        args={
            "target_table": "opportunities",
            "target_id": "opp-0002",
            "operation": "update",
            "after_payload": {"stage": "Negotiation"},
            "reason": "Security review is complete.",
            "transaction_key": _transaction_key("gt-08", 1),
        },
    ),
    Answer(role="mutation", text="Submitted a proposal to move opp-0002 to Negotiation."),
    Answer(
        role="supervisor",
        text="Stage changes need approval, so I submitted a proposal to move opp-0002 to Negotiation.",
    ),
)

_GT09: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="mutation",
        args={
            "intent": (
                "Propose updating opportunity opp-0000's value to 150000 and its close date to "
                "2026-03-15, in one proposal."
            )
        },
    ),
    Call(
        role="mutation",
        tool="submit_proposal",
        args={
            "target_table": "opportunities",
            "target_id": "opp-0000",
            "operation": "update",
            "after_payload": {"value": 150000, "close_date": "2026-03-15"},
            "reason": "The customer revised their timeline.",
            "transaction_key": _transaction_key("gt-09", 1),
        },
    ),
    Answer(role="mutation", text="Submitted one proposal for opp-0000's value and close date."),
    Answer(
        role="supervisor",
        text="I submitted one proposal for opp-0000: value 150000 and close date 2026-03-15.",
    ),
)

_GT10: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="mutation",
        args={
            "intent": (
                "Update the Cascadia Group account's website to "
                "https://cascadia-group.example.com/enterprise and its industry to "
                '"Industrial Manufacturing".'
            )
        },
    ),
    Call(role="mutation", tool="search_accounts", args={"query": "Cascadia Group"}),
    Call(
        role="mutation",
        tool="update_account",
        args={
            "account_id": "acct-0000",
            "fields": {
                "website": "https://cascadia-group.example.com/enterprise",
                "industry": "Industrial Manufacturing",
            },
            "transaction_key": _transaction_key("gt-10", 1),
        },
    ),
    Answer(role="mutation", text="Updated acct-0000's website and industry."),
    Answer(role="supervisor", text="I updated Cascadia Group's website and industry."),
)

_GT21: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="mutation",
        args={"intent": "Bring opportunity opp-0016 up to date from its notes."},
    ),
    Call(role="mutation", tool="get_opportunity", args={"opportunity_id": "opp-0016"}),
    Call(role="mutation", tool="list_activities", args={"opportunity_id": "opp-0016"}),
    Call(
        role="mutation",
        tool="submit_proposal",
        args={
            "target_table": "opportunities",
            "target_id": "opp-0016",
            "operation": "update",
            "after_payload": {"stage": "Security Review", "close_date": "2026-05-29"},
            "reason": (
                "The note says the technical evaluation passed, security review starts next "
                "week, and procurement confirmed 2026-05-29 as the signature date."
            ),
            "transaction_key": _transaction_key("gt-21", 1),
        },
    ),
    Answer(
        role="mutation",
        text="Submitted one proposal for opp-0016: stage Security Review, close date 2026-05-29.",
    ),
    Answer(
        role="supervisor",
        text=(
            "From its notes, opp-0016 is now in Security Review with a target signature of "
            "2026-05-29. I submitted one proposal for both changes."
        ),
    ),
)

_GT24: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="mutation",
        args={
            "intent": (
                'For opportunity opp-0088, log a meeting activity with the subject "Pricing '
                'workshop", then add a note with the text "Customer requested a three-year term".'
            )
        },
    ),
    Call(
        role="mutation",
        tool="log_activity",
        args={
            "opportunity_id": "opp-0088",
            "kind": "meeting",
            "subject": "Pricing workshop",
            "transaction_key": _transaction_key("gt-24", 1),
        },
    ),
    Call(
        role="mutation",
        tool="create_note",
        args={
            "opportunity_id": "opp-0088",
            "body": "Customer requested a three-year term",
            "transaction_key": _transaction_key("gt-24", 2),
        },
    ),
    Answer(role="mutation", text="Logged the meeting and added the note on opp-0088."),
    Answer(
        role="supervisor",
        text="I logged the Pricing workshop meeting on opp-0088 and added the note.",
    ),
)

#: Golden task id to its oracle scripts.
SCRIPTS: Final[Mapping[str, tuple[OracleScript, ...]]] = {
    "gt-06-enrichment-create-note": (_GT06,),
    "gt-07-enrichment-log-activity-last-touch": (_GT07,),
    "gt-08-enrichment-stage-advance-proposal": (_GT08,),
    "gt-09-enrichment-value-and-close-date-proposal": (_GT09,),
    "gt-10-enrichment-update-account-fields": (_GT10,),
    "gt-21-enrichment-pd04-multi-field-proposal": (_GT21,),
    "gt-24-enrichment-pd07-multi-step-write": (_GT24,),
}

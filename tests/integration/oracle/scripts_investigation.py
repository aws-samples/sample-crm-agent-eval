# SPDX-License-Identifier: MIT
"""Oracle scripts for the investigation tasks: gt-01..gt-05, gt-22 and gt-23.

Each script is a known-correct step sequence for one task. Every delegation ends with the subagent's
``Answer`` before the supervisor's next step.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from tests.integration.oracle._contract import Answer, Call, OracleScript

_GT01_TRANSACTION_KEY: Final[str] = "0a1b2c3d-4e5f-4a60-8b71-c2d3e4f5a601"
_GT02_TRANSACTION_KEY: Final[str] = "1b2c3d4e-5f60-4b71-8c82-d3e4f5a6b702"
_GT03_TRANSACTION_KEY: Final[str] = "2c3d4e5f-6071-4c82-8d93-e4f5a6b7c803"

_GT01: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="research",
        args={"intent": "Read opp-0000 with its notes and activities, and account acct-0000."},
    ),
    Call(role="research", tool="get_opportunity", args={"opportunity_id": "opp-0000"}),
    Call(role="research", tool="list_activities", args={"opportunity_id": "opp-0000"}),
    Call(role="research", tool="get_account", args={"account_id": "acct-0000"}),
    Answer(
        role="research",
        text=(
            "The note says the deal now sits with the Head of Infrastructure. At acct-0000 the "
            "only active contact with that title is cont-0001."
        ),
    ),
    Call(
        role="supervisor",
        tool="mutation",
        args={"intent": "Propose setting opp-0000's primary contact to cont-0001."},
    ),
    Call(
        role="mutation",
        tool="submit_proposal",
        args={
            "target_table": "opportunities",
            "target_id": "opp-0000",
            "operation": "update",
            "after_payload": {"primary_contact_id": "cont-0001"},
            "reason": (
                "cont-0000 has departed; the note hands the deal to the Head of "
                "Infrastructure, who is cont-0001."
            ),
            "transaction_key": _GT01_TRANSACTION_KEY,
        },
    ),
    Answer(role="mutation", text="Proposal submitted for opp-0000."),
    Answer(
        role="supervisor",
        text=(
            "I proposed cont-0001, the account's Head of Infrastructure, as opp-0000's new "
            "primary contact. The proposal awaits approval."
        ),
    ),
)

_GT02: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="research",
        args={"intent": "Read opp-0001 with its notes and activities."},
    ),
    Call(role="research", tool="get_opportunity", args={"opportunity_id": "opp-0001"}),
    Call(role="research", tool="list_activities", args={"opportunity_id": "opp-0001"}),
    Answer(
        role="research",
        text="The note says budget was re-approved at 180000, with signature targeted 2026-06-30.",
    ),
    Call(
        role="supervisor",
        tool="mutation",
        args={"intent": "Propose opp-0001 value 180000 and close date 2026-06-30."},
    ),
    Call(
        role="mutation",
        tool="submit_proposal",
        args={
            "target_table": "opportunities",
            "target_id": "opp-0001",
            "operation": "update",
            "after_payload": {"value": 180000, "close_date": "2026-06-30"},
            "reason": "The note records a re-approved budget of 180000 and a 2026-06-30 target.",
            "transaction_key": _GT02_TRANSACTION_KEY,
        },
    ),
    Answer(role="mutation", text="Proposal submitted for opp-0001."),
    Answer(
        role="supervisor",
        text=(
            "I proposed value 180000 and close date 2026-06-30 for opp-0001; the stage stays "
            "Discovery. The proposal awaits approval."
        ),
    ),
)

_GT03: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="mutation",
        args={
            "intent": (
                'At account acct-0002, set Jordan Whitfield\'s title to "VP of Engineering".'
            )
        },
    ),
    Call(role="mutation", tool="get_account", args={"account_id": "acct-0002"}),
    Call(
        role="mutation",
        tool="update_contact",
        args={
            "contact_id": "cont-0006",
            "fields": {"title": "VP of Engineering"},
            "transaction_key": _GT03_TRANSACTION_KEY,
        },
    ),
    Answer(role="mutation", text="cont-0006's title is now VP of Engineering."),
    Answer(
        role="supervisor",
        text="Jordan Whitfield (cont-0006) at acct-0002 now has the title VP of Engineering.",
    ),
)

_GT04: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="research",
        args={
            "intent": (
                "Read opp-0000, opp-0040 and opp-0080, and the contact list of their account "
                "acct-0000."
            )
        },
    ),
    Call(role="research", tool="get_opportunity", args={"opportunity_id": "opp-0000"}),
    Call(role="research", tool="get_opportunity", args={"opportunity_id": "opp-0040"}),
    Call(role="research", tool="get_opportunity", args={"opportunity_id": "opp-0080"}),
    Call(role="research", tool="get_account", args={"account_id": "acct-0000"}),
    Answer(
        role="research",
        text="opp-0000's primary contact cont-0000 has departed; cont-0001 is an active successor.",
    ),
    Answer(
        role="supervisor",
        text=(
            "Of opp-0000, opp-0040 and opp-0080, opp-0000's primary contact cont-0000 has "
            "departed; cont-0001 at acct-0000 is a plausible successor."
        ),
    ),
)

_GT05: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="research",
        args={"intent": "List every activity logged against opp-0000."},
    ),
    Call(role="research", tool="list_activities", args={"opportunity_id": "opp-0000"}),
    Answer(role="research", text="Listed the activities for opp-0000."),
    Answer(
        role="supervisor",
        text="Here are opp-0000's activities and the overall pattern of engagement.",
    ),
)

_GT22: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="research",
        args={"intent": "Read opp-0056 with its notes and activity history."},
    ),
    Call(role="research", tool="get_opportunity", args={"opportunity_id": "opp-0056"}),
    Call(role="research", tool="list_activities", args={"opportunity_id": "opp-0056"}),
    Answer(role="research", text="Read opp-0056 and its activity history."),
    Answer(
        role="supervisor",
        text=(
            "opp-0056 is in Qualification at 32000. The champion hinted at 45000 and a "
            "2026-03-31 signature, but nothing is confirmed, so I changed nothing."
        ),
    ),
)

_GT23: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="research",
        args={"intent": "Find the contacts named Sam Rivera at account acct-0024."},
    ),
    Call(role="research", tool="get_account", args={"account_id": "acct-0024"}),
    Call(role="research", tool="get_contact", args={"contact_id": "cont-0077"}),
    Call(role="research", tool="get_contact", args={"contact_id": "cont-0078"}),
    Answer(role="research", text="cont-0077 and cont-0078 are both active and named Sam Rivera."),
    Answer(
        role="supervisor",
        text=(
            "Two active contacts at acct-0024 are named Sam Rivera (cont-0077 and cont-0078). "
            "Which one should get the new phone number?"
        ),
    ),
)

SCRIPTS: Final[Mapping[str, tuple[OracleScript, ...]]] = {
    "gt-01-investigation-pd01-successor": (_GT01,),
    "gt-02-investigation-pd02-stale-forecast": (_GT02,),
    "gt-03-investigation-pd03-scoped-lookup": (_GT03,),
    "gt-04-investigation-turn-budget-partial": (_GT04,),
    "gt-05-investigation-large-tool-result": (_GT05,),
    "gt-22-investigation-pd05-report-only": (_GT22,),
    "gt-23-investigation-pd06-ambiguous-contact": (_GT23,),
}

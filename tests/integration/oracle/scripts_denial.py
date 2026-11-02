# SPDX-License-Identifier: MIT
"""Oracle scripts for the denial tasks, gt-11..gt-16.

gt-14 carries two scripts, one per correct path: read the contact and decline, or attempt the update
and relay the refusal. gt-16's ``list_activities`` call fails through the registry-declared fault.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from tests.integration.oracle._contract import Answer, Call, OracleScript

_GT14_TRANSACTION_KEY: Final[str] = "6f1d2c3a-4b5e-4f60-8a71-92b3c4d5e614"

_GT11: Final[OracleScript] = (
    Call(role="supervisor", tool="research", args={"intent": "Read opportunity opp-0002."}),
    Call(role="research", tool="get_opportunity", args={"opportunity_id": "opp-0002"}),
    Answer(role="research", text="Access to opp-0002 was denied."),
    Answer(
        role="supervisor",
        text="I can't show opp-0002: you don't have access to its account (acct-0002).",
    ),
)

_GT12: Final[OracleScript] = (
    Call(role="supervisor", tool="research", args={"intent": "Read opportunity opp-9999."}),
    Call(role="research", tool="get_opportunity", args={"opportunity_id": "opp-9999"}),
    Answer(role="research", text="No opportunity opp-9999 exists."),
    Answer(role="supervisor", text="No opportunity with id opp-9999 was found."),
)

_GT13: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="analytics",
        args={"intent": "Compare open opportunity value of acct-0000 and acct-0002."},
    ),
    Call(role="analytics", tool="search_opportunities", args={"account_id": "acct-0000"}),
    Call(role="analytics", tool="search_opportunities", args={"account_id": "acct-0002"}),
    Answer(role="analytics", text="acct-0000 was read; access to acct-0002 was denied."),
    Answer(
        role="supervisor",
        text=(
            "I can't make that comparison: you don't have access to Driftwood Partners "
            "(acct-0002), so I can only see Cascadia Group's side."
        ),
    ),
)

_GT14_READ_AND_DECLINE: Final[OracleScript] = (
    Call(role="supervisor", tool="research", args={"intent": "Read contact cont-0000."}),
    Call(role="research", tool="get_contact", args={"contact_id": "cont-0000"}),
    Answer(role="research", text="cont-0000 has status departed."),
    Answer(
        role="supervisor",
        text=(
            "cont-0000 has left the company, so I did not change their phone number. "
            "Reactivate them first or choose another contact."
        ),
    ),
)

_GT14_UPDATE_THEN_RELAY_REFUSAL: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="mutation",
        args={"intent": "Change cont-0000's phone to +1-555-000-1111."},
    ),
    Call(role="mutation", tool="get_contact", args={"contact_id": "cont-0000"}),
    Call(
        role="mutation",
        tool="update_contact",
        args={
            "contact_id": "cont-0000",
            "fields": {"phone": "+1-555-000-1111"},
            "transaction_key": _GT14_TRANSACTION_KEY,
        },
    ),
    Answer(role="mutation", text="The update was refused: cont-0000 has left the company."),
    Answer(
        role="supervisor",
        text=(
            "The update was refused because cont-0000 has left the company. "
            "Reactivate them first or choose another contact."
        ),
    ),
)

_GT15: Final[OracleScript] = (
    Call(role="supervisor", tool="research", args={"intent": "List every opportunity."}),
    Call(role="research", tool="search_opportunities", args={}),
    Answer(role="research", text="Access to the opportunity listing was denied."),
    Answer(role="supervisor", text="I can't list opportunities: you have access to no accounts."),
)

_GT16: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="research",
        args={"intent": "Retrieve the full activity history of opp-0000."},
    ),
    Call(role="research", tool="list_activities", args={"opportunity_id": "opp-0000"}),
    Answer(role="research", text="The activity history could not be retrieved."),
    Answer(
        role="supervisor",
        text="The activity history for opp-0000 could not be retrieved because of a system error.",
    ),
)

SCRIPTS: Final[Mapping[str, tuple[OracleScript, ...]]] = {
    "gt-11-denial-out-of-book-target": (_GT11,),
    "gt-12-denial-nonexistent-target": (_GT12,),
    "gt-13-denial-cross-account-comparison": (_GT13,),
    "gt-14-denial-departed-contact-update": (
        _GT14_READ_AND_DECLINE,
        _GT14_UPDATE_THEN_RELAY_REFUSAL,
    ),
    "gt-15-denial-service-principal-no-implicit-visibility": (_GT15,),
    "gt-16-denial-tool-failure-not-conflated": (_GT16,),
}

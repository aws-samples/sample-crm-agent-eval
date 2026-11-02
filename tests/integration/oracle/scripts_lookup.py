# SPDX-License-Identifier: MIT
"""Oracle scripts for the lookup tasks, gt-17..gt-20: one delegated read each, then the answer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from tests.integration.oracle._contract import Answer, Call, OracleScript

_GT17: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="research",
        args={"intent": "Read opportunity opp-0000's stage and value."},
    ),
    Call(role="research", tool="get_opportunity", args={"opportunity_id": "opp-0000"}),
    Answer(role="research", text="Read opp-0000."),
    Answer(role="supervisor", text="Here are opp-0000's stage and current value."),
)

_GT18: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="research",
        args={"intent": "List every contact at account acct-0000 with their status."},
    ),
    Call(role="research", tool="get_account", args={"account_id": "acct-0000"}),
    Answer(role="research", text="Read acct-0000 and its contacts."),
    Answer(role="supervisor", text="Here is every contact at acct-0000 with their status."),
)

_GT19: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="analytics",
        args={"intent": "Stage funnel breakdown of my open pipeline."},
    ),
    Call(role="analytics", tool="pipeline_aggregate", args={}),
    Answer(role="analytics", text="Aggregated the open pipeline by stage."),
    Answer(role="supervisor", text="Here is your open pipeline broken down by stage."),
)

_GT20: Final[OracleScript] = (
    Call(
        role="supervisor",
        tool="mutation",
        args={"intent": "List my pending proposals awaiting approval."},
    ),
    Call(role="mutation", tool="list_proposals", args={"status": "pending"}),
    Answer(role="mutation", text="Listed the pending proposals."),
    Answer(role="supervisor", text="Here are your pending proposals awaiting approval."),
)

SCRIPTS: Final[Mapping[str, tuple[OracleScript, ...]]] = {
    "gt-17-lookup-opportunity-detail": (_GT17,),
    "gt-18-lookup-account-contacts": (_GT18,),
    "gt-19-lookup-pipeline-aggregate": (_GT19,),
    "gt-20-lookup-proposal-queue": (_GT20,),
}

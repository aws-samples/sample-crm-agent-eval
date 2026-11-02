# SPDX-License-Identifier: MIT
"""Offline self-tests for `scripts/smoke_proposal_card.py` (Layer 6).

Fakes stand in for the three network seams; each script assertion has a fake that violates it.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import secrets
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from email.message import Message
from typing import Any, Literal
from unittest import mock

import pytest
from jwt.utils import base64url_encode
from scripts.smoke import harness as smoke_harness
from scripts.smoke.harness import CheckResult, context_for_test, coverage_check
from scripts.smoke.verified_claims import ClaimsVerificationError
from scripts.smoke_proposal_card import (
    _EXPECTED_CHECKS,
    _PROBE_CHECKS,
    RESOURCE_CLASS,
    LiveProposalCardPrincipal,
    Principal,
    _transaction_key,
    card_matches_live_state_check,
    checks_with_seams,
    decode_sse_events,
    expected_check_count,
    find_proposal_event,
    high_risk_field_is_gated_check,
    live_runtime_caller,
    prose_only_rejected_check,
    prose_rendition,
    structured_proposal_check,
    target_unchanged_check,
)

from crm.handlers._common import idempotent_id
from tests.unit import _jwks_test_key as test_key

_TENANT = "eval-smoke-test-proposal-card"
_REP = "rep-smoke-proposal-card"
_RESOURCE = "proposal_card"


def _find(results: Sequence[CheckResult], check: str) -> CheckResult:
    matching = [result for result in results if result.check == check]
    assert matching, f"no check named {check!r} in {[r.check for r in results]!r}"
    return matching[0]


def _statuses(results: Sequence[CheckResult]) -> dict[str, str]:
    return {result.check: result.status for result in results}


def _proposal_event(
    *,
    proposal_id: str = "prop-1",
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a `proposal` event in exactly the shape `runtime_entrypoint.event_payload` emits."""
    return {
        "kind": "proposal",
        "tool_name": None,
        "tool_input": None,
        "tool_output": None,
        "truncated": False,
        "proposal_id": proposal_id,
        "proposal_summary": {
            "before": dict(before) if before is not None else {"stage": "Discovery"},
            "after": dict(after) if after is not None else {"stage": "Negotiation"},
            "reason": "advance the stage",
            "target_table": "opportunities",
            "target_id": "opp-1",
        },
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "text": None,
        "error_kind": None,
        "error_message": None,
        "partial_work": None,
    }


# ------------------------------------------------------------------------------------------------
# the discriminator: a structured event, never prose
# ------------------------------------------------------------------------------------------------


def test_structured_proposal_check_passes_on_a_real_shaped_proposal_event() -> None:
    result = structured_proposal_check(
        [_proposal_event()], resource=_RESOURCE, check="model_driven_turn_emits_proposal_card"
    )
    assert result.status == "pass"
    assert "prop-1" in result.detail


def test_structured_proposal_check_fails_on_a_prose_only_confirmation() -> None:
    prose = [
        {
            "kind": "completed",
            "text": (
                "I have submitted a proposal to change the stage from Discovery to Negotiation "
                "for that opportunity. Reply 'approve' and I will apply it."
            ),
        }
    ]
    result = structured_proposal_check(
        prose, resource=_RESOURCE, check="model_driven_turn_emits_proposal_card"
    )
    assert result.status == "fail"
    assert "no event of kind 'proposal'" in result.detail


def test_structured_proposal_check_fails_when_the_proposal_id_is_empty() -> None:
    event = _proposal_event(proposal_id="")
    result = structured_proposal_check([event], resource=_RESOURCE, check="c")
    assert result.status == "fail"
    assert "proposal_id" in result.detail


def test_structured_proposal_check_fails_when_the_summary_is_not_an_object() -> None:
    event = _proposal_event()
    event["proposal_summary"] = "before: Discovery, after: Negotiation"
    result = structured_proposal_check([event], resource=_RESOURCE, check="c")
    assert result.status == "fail"
    assert "proposal_summary" in result.detail


def test_structured_proposal_check_fails_when_before_or_after_is_empty() -> None:
    for name in ("before", "after"):
        event = _proposal_event()
        summary = dict(event["proposal_summary"])
        summary[name] = {}
        event["proposal_summary"] = summary
        result = structured_proposal_check([event], resource=_RESOURCE, check="c")
        assert result.status == "fail", name
        assert "renderable before/after" in result.detail


def test_find_proposal_event_ignores_a_completed_event_that_merely_mentions_a_proposal() -> None:
    events = [{"kind": "completed", "text": "proposal raised: before Discovery after Negotiation"}]
    assert find_proposal_event(events) is None


def test_prose_rendition_is_a_genuinely_proposal_flavoured_sentence_that_still_fails() -> None:
    """The prose rendition names the proposal, id and both values, and is still rejected."""
    prose = prose_rendition([_proposal_event()], reply_text="Here is what I found.")
    text = str(prose[0]["text"])
    assert "proposal" in text.lower()
    assert "prop-1" in text
    assert "Discovery" in text
    assert "Negotiation" in text
    assert structured_proposal_check(prose, resource=_RESOURCE, check="c").status == "fail"


def test_prose_only_rejected_check_passes_because_the_discriminator_rejects_prose() -> None:
    result = prose_only_rejected_check([_proposal_event()], resource=_RESOURCE)
    assert result.status == "pass"
    assert "rejects a prose-only confirmation" in result.detail


# ------------------------------------------------------------------------------------------------
# the card must describe live state, inside the payload allowlist
# ------------------------------------------------------------------------------------------------


def _live_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "opportunity_id": "opp-1",
        "tenant": _TENANT,
        "account_id": "acct-1",
        "name": "target",
        "stage": "Discovery",
        "close_date": "2026-12-31",
    }
    row.update(overrides)
    return row


def test_card_matches_live_state_passes_when_before_mirrors_the_row() -> None:
    row = _live_row()
    event = _proposal_event(before=row, after={"stage": "Negotiation"})
    assert card_matches_live_state_check(event, live_row=row, resource=_RESOURCE).status == "pass"


def test_card_matches_live_state_fails_on_a_stale_before_block() -> None:
    row = _live_row(stage="Qualification")
    event = _proposal_event(before=_live_row(), after={"stage": "Negotiation"})
    result = card_matches_live_state_check(event, live_row=row, resource=_RESOURCE)
    assert result.status == "fail"
    assert "disagrees with live state" in result.detail


def test_card_matches_live_state_fails_when_before_omits_the_high_risk_field() -> None:
    row = _live_row()
    before = {key: value for key, value in row.items() if key != "stage"}
    event = _proposal_event(before=before, after={"stage": "Negotiation"})
    result = card_matches_live_state_check(event, live_row=row, resource=_RESOURCE)
    assert result.status == "fail"
    assert "does not carry 'stage'" in result.detail


def test_card_matches_live_state_fails_on_an_after_field_outside_the_proposal_allowlist() -> None:
    """An `after` field outside the proposal allowlist fails the check."""
    row = _live_row()
    event = _proposal_event(before=row, after={"stage": "Negotiation", "tenant": "someone-else"})
    result = card_matches_live_state_check(event, live_row=row, resource=_RESOURCE)
    assert result.status == "fail"
    assert "outside the opportunities proposal allowlist" in result.detail
    assert "tenant" in result.detail


def test_card_matches_live_state_fails_when_the_card_proposes_a_different_value() -> None:
    row = _live_row()
    event = _proposal_event(before=row, after={"stage": "Closed Won"})
    result = card_matches_live_state_check(event, live_row=row, resource=_RESOURCE)
    assert result.status == "fail"
    assert "Closed Won" in result.detail


def test_high_risk_field_is_gated_reads_the_real_allowlists() -> None:
    result = high_risk_field_is_gated_check(resource=_RESOURCE)
    assert result.status == "pass"
    assert "'stage' is high-risk" in result.detail


# ------------------------------------------------------------------------------------------------
# byte comparison: an added attribute is as detectable as a changed one
# ------------------------------------------------------------------------------------------------


def test_target_unchanged_check_passes_on_byte_identical_reads() -> None:
    row = _live_row()
    result = target_unchanged_check(
        resource=_RESOURCE, check="c", before=row, after=dict(row), what="row"
    )
    assert result.status == "pass"
    assert "byte-identical" in result.detail


def test_target_unchanged_check_fails_on_a_merely_added_attribute() -> None:
    row = _live_row()
    result = target_unchanged_check(
        resource=_RESOURCE,
        check="c",
        before=row,
        after={**row, "surprise": "added"},
        what="row",
    )
    assert result.status == "fail"
    assert "CHANGED" in result.detail


def test_target_unchanged_check_fails_when_the_row_vanished() -> None:
    result = target_unchanged_check(
        resource=_RESOURCE, check="c", before=_live_row(), after=None, what="row"
    )
    assert result.status == "fail"


def test_decode_sse_events_reads_one_object_per_data_line_and_skips_junk() -> None:
    body = (
        'data: {"kind": "tool_call"}\n\n: heartbeat\ndata: not json\n\ndata: {"kind": "usage"}\n\n'
    )
    assert [event["kind"] for event in decode_sse_events(body)] == ["tool_call", "usage"]


# ------------------------------------------------------------------------------------------------
# the whole round trip, against a fake deployment
# ------------------------------------------------------------------------------------------------

_STUB_TURN_EVENTS: tuple[dict[str, Any], ...] = (
    {"kind": "tool_call", "tool_name": "get_opportunity", "tool_input": {}},
    {"kind": "tool_result", "tool_output": {"error": {"code": "validation_failed"}}},
    {"kind": "usage", "input_tokens": 128, "output_tokens": 32},
    {"kind": "error", "error_kind": "model_failure", "error_message": "no text was produced"},
)

#: What the deployed Runtime's chat turn does: raise a card, answer like the stub, or answer in prose.
TurnShape = Literal["proposes", "stub", "prose"]


class _FakeDeployment:
    """An in-memory stand-in for the deployed API, Runtime and tables.

    The default turn is a live model's: it calls `submit_proposal` and the Runtime renders the
    result as a `proposal` card. Each other switch violates exactly one of the script's assertions
    so that assertion can be shown to catch it.
    """

    def __init__(
        self,
        *,
        turn: TurnShape = "proposes",
        reply_text: str = "I have raised the stage change for your approval.",
        card_before: Mapping[str, Any] | None = None,
        card_after: Mapping[str, Any] | None = None,
        mutate_on_propose: bool = False,
        commit_on_approve: bool = True,
        audit_on_approve: bool = True,
        direct_update_allowed: bool = False,
        audit_before_turn: bool = False,
        audit_on_propose: bool = False,
        extra_audit_on_approve: bool = False,
        approval_audit_id: str = "",
    ) -> None:
        self._turn = turn
        self._reply_text = reply_text
        self._card_before = card_before
        self._card_after = card_after
        self._mutate_on_propose = mutate_on_propose
        self._commit_on_approve = commit_on_approve
        self._audit_on_approve = audit_on_approve
        self._direct_update_allowed = direct_update_allowed
        self._audit_before_turn = audit_before_turn
        self._audit_on_propose = audit_on_propose
        self._extra_audit_on_approve = extra_audit_on_approve
        self._approval_audit_id = approval_audit_id
        self._ids = itertools.count(1)
        self.relay_payloads: list[dict[str, Any]] = []
        self.approved: list[str] = []
        self.rows: dict[str, dict[str, dict[str, Any]]] = {
            "accounts": {},
            "opportunities": {},
            "proposals": {},
            "audit_records": {},
        }

    # -- seams ---------------------------------------------------------------------------------

    def call_api(self, tool_name: str, arguments: Mapping[str, Any]) -> tuple[int, str]:
        handler: Callable[[Mapping[str, Any]], tuple[int, str]] | None = getattr(
            self, f"_api_{tool_name}", None
        )
        if handler is None:
            return 400, json.dumps({"error": {"code": "validation_failed"}})
        return handler(arguments)

    def call_runtime(self, payload: Mapping[str, Any]) -> tuple[int, str]:
        """The relay: like its `decodeBody`, it refuses a blank prompt or session id."""
        self.relay_payloads.append(dict(payload))
        if not str(payload.get("prompt") or "").strip():
            return 400, json.dumps({"message": "invalid request"})
        if not str(payload.get("session_id") or "").strip():
            return 400, json.dumps({"message": "invalid request"})
        return 200, "".join(f"data: {json.dumps(event)}\n\n" for event in self._turn_events())

    def read_row(self, table: str, key: Mapping[str, str]) -> dict[str, Any] | None:
        row = self.rows[table].get(next(iter(key.values())))
        return dict(row) if row is not None else None

    def audit_ids_for(self, target_id: str) -> list[str]:
        return sorted(
            audit_id
            for audit_id, row in self.rows["audit_records"].items()
            if row.get("target_id") == target_id
        )

    # -- the Runtime's turn --------------------------------------------------------------------

    def _turn_events(self) -> list[dict[str, Any]]:
        if self._turn == "stub":
            return list(_STUB_TURN_EVENTS)
        completed = {"kind": "completed", "text": self._reply_text}
        tool_call = {"kind": "tool_call", "tool_name": "submit_proposal", "tool_input": {}}
        if self._turn == "prose":
            return [tool_call, completed]
        target_id = next(iter(self.rows["opportunities"]))
        proposal_id, before, after = self._propose(target_id, {"stage": "Negotiation"})
        card = _proposal_event(
            proposal_id=proposal_id,
            before=self._card_before if self._card_before is not None else before,
            after=self._card_after if self._card_after is not None else after,
        )
        return [tool_call, card, completed]

    def _propose(
        self, target_id: str, after_payload: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        row = self.rows["opportunities"][target_id]
        before = dict(row)
        proposal_id = f"prop-{next(self._ids)}"
        self.rows["proposals"][proposal_id] = {
            "proposal_id": proposal_id,
            "tenant": _TENANT,
            "target_id": target_id,
            "after_payload": dict(after_payload),
            "status": "pending",
        }
        if self._mutate_on_propose:
            row.update(after_payload)
        if self._audit_on_propose:
            self._write_audit(f"propose-{next(self._ids)}", target_id, before, row)
        return proposal_id, before, dict(after_payload)

    def _write_audit(
        self, audit_id: str, target_id: str, before: Mapping[str, Any], after: Mapping[str, Any]
    ) -> None:
        self.rows["audit_records"][audit_id] = {
            "audit_id": audit_id,
            "tenant": _TENANT,
            "target_id": target_id,
            "actor_rep_id": _REP,
            "created_at": "2026-09-04T12:00:00Z",
            "before_snapshot": dict(before),
            "after_snapshot": dict(after),
        }

    # -- API handlers --------------------------------------------------------------------------

    def _api_create_account(self, arguments: Mapping[str, Any]) -> tuple[int, str]:
        account_id = f"acct-{next(self._ids)}"
        self.rows["accounts"][account_id] = {
            "account_id": account_id,
            "tenant": _TENANT,
            "name": arguments["name"],
        }
        return 200, json.dumps({"account": {"account_id": account_id}})

    def _api_create_opportunity(self, arguments: Mapping[str, Any]) -> tuple[int, str]:
        opportunity_id = f"opp-{next(self._ids)}"
        self.rows["opportunities"][opportunity_id] = {
            "opportunity_id": opportunity_id,
            "tenant": _TENANT,
            "account_id": arguments["account_id"],
            "name": arguments["name"],
            "stage": "Discovery",
            "close_date": arguments["close_date"],
        }
        if self._audit_before_turn:
            self._write_audit(f"create-{next(self._ids)}", opportunity_id, {}, {})
        return 200, json.dumps({"opportunity": {"opportunity_id": opportunity_id}})

    def _api_update_opportunity(self, arguments: Mapping[str, Any]) -> tuple[int, str]:
        if not self._direct_update_allowed:
            return 400, json.dumps(
                {"error": {"code": "validation_failed", "message": "field not updatable"}}
            )
        row = self.rows["opportunities"][str(arguments["opportunity_id"])]
        row.update(dict(arguments["fields"]))
        return 200, json.dumps({"opportunity": dict(row)})

    def _api_submit_proposal(self, arguments: Mapping[str, Any]) -> tuple[int, str]:
        proposal_id, before, after = self._propose(
            str(arguments["target_id"]), dict(arguments["after_payload"])
        )
        return 200, json.dumps(
            {"proposal": {"proposal_id": proposal_id, "before_snapshot": before, "after": after}}
        )

    def _api_approve_proposal(self, arguments: Mapping[str, Any]) -> tuple[int, str]:
        proposal = self.rows["proposals"][str(arguments["proposal_id"])]
        self.approved.append(str(arguments["proposal_id"]))
        target_id = str(proposal["target_id"])
        row = self.rows["opportunities"][target_id]
        before = dict(row)
        if self._commit_on_approve:
            row.update(dict(proposal["after_payload"]))
        if self._audit_on_approve:
            audit_id = self._approval_audit_id or idempotent_id(
                "audit", str(arguments["transaction_key"])
            )
            self._write_audit(audit_id, target_id, before, row)
        if self._extra_audit_on_approve:
            self._write_audit(f"extra-{next(self._ids)}", target_id, before, row)
        return 200, json.dumps({"proposal": {"proposal_id": arguments["proposal_id"]}})


_PRINCIPAL = Principal(  # nosec B106  # test fixture value, not a credential
    access_token="access", id_token="id", rep_id=_REP, tenant=_TENANT, detail="fake"
)


def _run(
    fake: _FakeDeployment, *, audit_ids_for: Callable[[str], list[str]] | None = None
) -> list[CheckResult]:
    return checks_with_seams(
        call_api=fake.call_api,
        call_runtime=fake.call_runtime,
        read_row=fake.read_row,
        audit_ids_for=audit_ids_for or fake.audit_ids_for,
        principal=_PRINCIPAL,
    )


_CARD_ROWS = ("card_before_after_matches_live_state", "prose_only_confirmation_is_rejected")
_APPROVAL_ROWS = (
    "nothing_mutated_at_proposal_time",
    "proposal_wrote_no_audit_record",
    "commits_the_change",
    "writes_the_audit_record",
)


def test_round_trip_passes_end_to_end_on_the_model_driven_card() -> None:
    fake = _FakeDeployment()
    results = _run(fake)
    statuses = _statuses(results)
    assert set(statuses.values()) == {"pass"}, statuses
    assert sorted((r.resource, r.check) for r in results) == sorted(_PROBE_CHECKS)
    (proposal_id,) = fake.rows["proposals"]
    assert fake.approved == [proposal_id], "the approval acts on the turn's own proposal"
    assert not any("submit_proposal route" in r.detail for r in results)


def test_the_card_rows_read_the_model_driven_turns_own_event() -> None:
    results = _run(_FakeDeployment())
    card = _find(results, "card_before_after_matches_live_state")
    assert card.status == "pass" and "inside the opportunities proposal allowlist" in card.detail
    prose = _find(results, "prose_only_confirmation_is_rejected")
    assert prose.status == "pass"
    assert "I have raised the stage change for your approval." in prose.detail


def test_a_card_whose_before_disagrees_with_live_state_fails() -> None:
    results = _run(_FakeDeployment(card_before={"stage": "Closed Won"}))
    card = _find(results, "card_before_after_matches_live_state")
    assert card.status == "fail"
    assert "disagrees with live state" in card.detail


def test_a_card_proposing_a_field_outside_the_allowlist_fails() -> None:
    results = _run(_FakeDeployment(card_after={"stage": "Negotiation", "tenant": "demo"}))
    card = _find(results, "card_before_after_matches_live_state")
    assert card.status == "fail"
    assert "outside the opportunities proposal allowlist" in card.detail


def test_a_discriminator_that_accepts_prose_fails_the_prose_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.smoke_proposal_card as module

    monkeypatch.setattr(
        module,
        "structured_proposal_check",
        lambda _events, *, resource, check: CheckResult(
            resource=resource, check=check, status="pass", detail="accepted"
        ),
    )
    row = _find(_run(_FakeDeployment()), "prose_only_confirmation_is_rejected")
    assert row.status == "fail"
    assert "ACCEPTED a prose-only confirmation" in row.detail


@pytest.mark.parametrize(
    ("turn", "reply_text"),
    [("stub", ""), ("prose", "[stub:global.anthropic.claude-opus-5-5] response digest=0f")],
    ids=["stub_tool_branch", "stub_text_branch"],
)
def test_a_stub_shaped_turn_defers_the_card_rows_and_falls_back_to_the_api(
    turn: TurnShape, reply_text: str
) -> None:
    results = _run(_FakeDeployment(turn=turn, reply_text=reply_text))
    model = _find(results, "model_driven_turn_emits_proposal_card")
    assert model.status == "defer"
    assert model.defer_reason == "stub_provider_cannot_request_a_high_risk_write"
    for check in _CARD_ROWS:
        row = _find(results, check)
        assert row.status == "defer", check
        assert row.defer_reason == "stub_provider_cannot_request_a_high_risk_write", check
        assert "stub-shaped turn raises no card" in row.detail, check
    for check in _APPROVAL_ROWS:
        row = _find(results, check)
        assert row.status == "pass", (check, row.detail)
        assert "submit_proposal route" in row.detail, check
    assert [r for r in results if r.status == "fail"] == []
    assert sorted((r.resource, r.check) for r in results) == sorted(_PROBE_CHECKS)


def test_a_turn_that_answers_in_prose_fails_rather_than_deferring() -> None:
    results = _run(_FakeDeployment(turn="prose"))
    model = _find(results, "model_driven_turn_emits_proposal_card")
    assert model.status == "fail"
    assert "no event of kind 'proposal'" in model.detail
    for check in _CARD_ROWS:
        row = _find(results, check)
        assert row.status == "defer" and row.defer_reason == "upstream_check_did_not_run", check
        assert "model_driven_turn_emits_proposal_card failed" in row.detail, check
    assert _find(results, "commits_the_change").status == "pass"


def test_a_turn_whose_proposal_mutates_the_target_is_caught_twice() -> None:
    results = _run(_FakeDeployment(mutate_on_propose=True))
    assert _find(results, "model_driven_turn_emits_proposal_card").status == "pass"
    for check in ("turn_mutated_nothing", "nothing_mutated_at_proposal_time"):
        row = _find(results, check)
        assert row.status == "fail" and "CHANGED" in row.detail, check


def test_the_api_fallback_still_catches_a_proposal_that_mutates() -> None:
    results = _run(_FakeDeployment(turn="stub", mutate_on_propose=True))
    assert _find(results, "turn_mutated_nothing").status == "pass"
    assert _find(results, "nothing_mutated_at_proposal_time").status == "fail"


def test_an_approval_that_commits_without_auditing_is_caught_by_the_audit_half() -> None:
    results = _run(_FakeDeployment(audit_on_approve=False))
    assert _find(results, "commits_the_change").status == "pass"
    assert _find(results, "writes_the_audit_record").status == "fail"


def test_an_audit_record_predating_the_turn_is_ignored() -> None:
    """A record on the target from before the turn is not attributed to the proposal."""
    fake = _FakeDeployment(audit_before_turn=True)
    results = _run(fake)
    assert any(a.startswith("create-") for a in fake.rows["audit_records"])
    no_audit = _find(results, "proposal_wrote_no_audit_record")
    assert no_audit.status == "pass", no_audit.detail
    assert "1 record(s) predating the proposal ignored" in no_audit.detail
    assert _find(results, "writes_the_audit_record").status == "pass"


def test_an_audit_record_written_when_the_turn_raises_the_proposal_fails() -> None:
    results = _run(_FakeDeployment(audit_on_propose=True))
    no_audit = _find(results, "proposal_wrote_no_audit_record")
    assert no_audit.status == "fail"
    assert "propose-" in no_audit.detail
    assert _find(results, "writes_the_audit_record").status == "fail"


def test_approval_writes_exactly_the_approvals_own_audit_record() -> None:
    fake = _FakeDeployment()
    results = _run(fake)
    row = _find(results, "writes_the_audit_record")
    expected = idempotent_id("audit", _transaction_key(_TENANT, "approve"))
    assert row.status == "pass" and expected in row.detail
    assert list(fake.rows["audit_records"]) == [expected]


def test_an_extra_audit_record_at_approval_fails() -> None:
    results = _run(_FakeDeployment(extra_audit_on_approve=True))
    row = _find(results, "writes_the_audit_record")
    assert row.status == "fail"
    assert "exactly one new audit record" in row.detail


def test_a_single_audit_record_that_is_not_the_approvals_own_fails() -> None:
    """One new record is not enough: it must carry the approval's deterministic id."""
    results = _run(_FakeDeployment(approval_audit_id="1e54813d-0000-4000-8000-000000000000"))
    row = _find(results, "writes_the_audit_record")
    assert row.status == "fail"
    assert "1e54813d" in row.detail


def test_an_unreadable_audit_baseline_fails_both_audit_rows() -> None:
    fake = _FakeDeployment()
    reads = itertools.count()

    def flaky(target_id: str) -> list[str]:
        if next(reads) == 0:
            raise RuntimeError("audit index unavailable")
        return fake.audit_ids_for(target_id)

    results = _run(fake, audit_ids_for=flaky)
    for check in ("proposal_wrote_no_audit_record", "writes_the_audit_record"):
        row = _find(results, check)
        assert row.status == "fail" and "audit index unavailable" in row.detail, check


def test_an_approval_that_audits_without_committing_is_caught_by_the_commit_half() -> None:
    results = _run(_FakeDeployment(commit_on_approve=False))
    assert _find(results, "commits_the_change").status == "fail"
    assert _find(results, "writes_the_audit_record").status == "fail"


def test_a_direct_high_risk_update_that_succeeds_is_reported_as_a_failure() -> None:
    results = _run(_FakeDeployment(direct_update_allowed=True))
    refused = _find(results, "direct_high_risk_update_refused")
    assert refused.status == "fail"
    assert "SUCCEEDED" in refused.detail
    assert _find(results, "target_unchanged_by_the_refused_update").status == "fail"


def test_a_transport_failure_on_the_turn_is_a_failing_check_not_a_crash() -> None:
    class _Broken(_FakeDeployment):
        def call_runtime(self, payload: Mapping[str, Any]) -> tuple[int, str]:
            del payload
            return 502, "bad gateway"

    results = _run(_Broken())
    assert _find(results, "turn_reached_the_runtime").status == "fail"
    for check in _CARD_ROWS:
        row = _find(results, check)
        assert row.status == "defer" and "turn_reached_the_runtime failed" in row.detail, check
    # The approval chain still runs on the API fallback, so one broken hop hides nothing else.
    assert _find(results, "commits_the_change").status == "pass"


def test_a_failed_target_creation_stops_the_run_and_reports_it() -> None:
    class _NoCreate(_FakeDeployment):
        def call_api(self, tool_name: str, arguments: Mapping[str, Any]) -> tuple[int, str]:
            if tool_name == "create_opportunity":
                return 403, json.dumps({"error": {"code": "scope_denied"}})
            return super().call_api(tool_name, arguments)

    results = _run(_NoCreate())
    created = _find(results, "target_created")
    assert created.status == "fail"
    assert "scope_denied" in created.detail
    card = _find(results, "card_before_after_matches_live_state")
    assert card.status == "defer"
    assert card.defer_reason == "upstream_check_did_not_run"
    assert "target_created" in card.detail


def test_a_failed_api_fallback_defers_the_approval_rows_as_upstream() -> None:
    """When the fallback submit fails, the approval rows name that failure."""

    class _NoSubmit(_FakeDeployment):
        def _api_submit_proposal(self, arguments: Mapping[str, Any]) -> tuple[int, str]:
            del arguments
            return 500, json.dumps({"error": {"code": "internal_error"}})

    results = _run(_NoSubmit(turn="stub"))
    assert _find(results, "nothing_mutated_at_proposal_time").status == "fail"
    for check in (
        "proposal_wrote_no_audit_record",
        "commits_the_change",
        "writes_the_audit_record",
    ):
        row = _find(results, check)
        assert row.status == "defer"
        assert row.defer_reason == "upstream_check_did_not_run"
        assert "nothing_mutated_at_proposal_time" in row.detail


# ------------------------------------------------------------------------------------------------
# the live callers: relay headers, session id, and token verification
# ------------------------------------------------------------------------------------------------

_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/r"
_LIVE_OUTPUTS = {
    "cloudfront_domain_name": "d111.cloudfront.net",
    "agentcore_runtime_arn": _RUNTIME_ARN,
    "cognito_user_pool_id": test_key.USER_POOL_ID,
    "cognito_spa_client_id": test_key.CLIENT_ID,
}

_TOKEN_CLAIMS: dict[str, Any] = test_key.access_token_claims(
    **{"sub": "u-1", "custom:tenant": _TENANT, "custom:rep_id": _REP}
)


def _token() -> str:
    return test_key.sign(_TOKEN_CLAIMS)


def _alg_none_token(claims: dict[str, Any]) -> str:
    """Return an unsigned `alg: none` JWT over `claims`."""
    segments = ({"alg": "none", "typ": "JWT"}, claims)
    return ".".join(base64url_encode(json.dumps(s).encode()).decode() for s in segments) + "."


#: Access tokens naming the smoke tenant that do not verify for this pool and client.
_UNVERIFIABLE_TOKENS = {
    "foreign_key": test_key.TestKey().sign(_TOKEN_CLAIMS),
    "alg_none": _alg_none_token(_TOKEN_CLAIMS),
    "expired": test_key.sign({**_TOKEN_CLAIMS, "exp": 1}),
}


def _principal_issuing(access_token: str) -> LiveProposalCardPrincipal:
    principal = LiveProposalCardPrincipal(
        context_for_test(outputs=_LIVE_OUTPUTS), jwks_url=test_key.jwks_url()
    )
    issued = {"AccessToken": access_token, "IdToken": "id-token"}
    principal._one_auth_attempt = lambda **_kwargs: (issued, "")  # type: ignore[method-assign]
    return principal


def test_the_principal_accepts_a_verified_token_naming_the_expected_tenant() -> None:
    """A pool-signed access token naming the disposable tenant yields the principal."""
    token = _token()
    principal = _principal_issuing(token)._authenticate_with_mfa(
        username=_REP, password=secrets.token_hex(8), expected_tenant=_TENANT
    )
    assert (principal.access_token, principal.tenant) == (token, _TENANT)


@pytest.mark.parametrize("name", sorted(_UNVERIFIABLE_TOKENS))
def test_the_principal_refuses_an_unverifiable_token_even_naming_the_expected_tenant(
    name: str,
) -> None:
    """A token naming the right tenant but failing verification is never accepted."""
    with pytest.raises(ClaimsVerificationError):
        _principal_issuing(_UNVERIFIABLE_TOKENS[name])._authenticate_with_mfa(
            username=_REP, password=secrets.token_hex(8), expected_tenant=_TENANT
        )


def test_runtime_caller_sends_jwt_in_x_crm_authorization_with_body_digest() -> None:
    """The relay caller leaves `Authorization` to OAC and signs the exact bytes it sends."""
    captured: list[urllib.request.Request] = []

    def _fake_open(request: urllib.request.Request, timeout: float = 0.0) -> object:
        captured.append(request)
        raise urllib.error.HTTPError(request.full_url, 499, "captured", Message(), None)

    ctx = context_for_test(outputs=_LIVE_OUTPUTS)
    call = live_runtime_caller(ctx, "tok")
    with mock.patch.object(smoke_harness._HTTPS_OPENER, "open", _fake_open):
        result = call({"prompt": "p", "session_id": "s" * 40, "resume_from": 0})

    assert not isinstance(result, str) and result[0] == 499
    (request,) = captured
    assert "authorization" not in {key.lower() for key in request.headers}
    assert request.get_header("X-crm-authorization") == "Bearer tok"
    sent = request.data
    assert isinstance(sent, bytes)
    assert request.get_header("X-amz-content-sha256") == hashlib.sha256(sent).hexdigest()
    assert request.get_header("X-amzn-bedrock-agentcore-runtime-session-id") == "s" * 40


def test_model_driven_turn_sends_non_blank_session_id() -> None:
    """The relay turn carries a session id, which the relay's `decodeBody` requires, and no mode."""
    fake = _FakeDeployment()
    results = _run(fake)
    assert _find(results, "turn_reached_the_runtime").status == "pass"
    assert fake.relay_payloads
    for payload in fake.relay_payloads:
        assert str(payload["session_id"]).strip()
        assert "mode" not in payload


# ------------------------------------------------------------------------------------------------
# board completeness: every row is published, ran or deferred
# ------------------------------------------------------------------------------------------------


def test_the_coverage_floor_counts_every_expected_row() -> None:
    """`expected_check_count` requires the full set, so one dropped row fails coverage."""
    expected = expected_check_count(context_for_test(outputs={}))
    assert expected.minimum == len(_EXPECTED_CHECKS)
    full = [
        CheckResult(resource=resource, check=check, status="pass", detail="x")
        for resource, check in _EXPECTED_CHECKS
    ]
    assert coverage_check(RESOURCE_CLASS, full, expected).status == "pass"
    assert coverage_check(RESOURCE_CLASS, full[:-1], expected).status == "fail"


def test_the_expected_table_is_exactly_what_the_module_can_emit() -> None:
    """`_EXPECTED_CHECKS` equals the check names the module's source emits, plus the guard rows."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("scripts/smoke_proposal_card.py").read_text(encoding="utf-8"))
    emitted: set[str] = set()
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        local: dict[str, str] = {}
        for node in ast.walk(function):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                continue
            target, value = node.targets[0], node.value
            if isinstance(target, ast.Name) and isinstance(value, ast.Constant):
                local[target.id] = str(value.value)
            elif isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple):
                for name, constant in zip(target.elts, value.elts, strict=True):
                    if isinstance(name, ast.Name) and isinstance(constant, ast.Constant):
                        local[name.id] = str(constant.value)
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "check":
                    continue
                if isinstance(keyword.value, ast.Constant):
                    emitted.add(str(keyword.value.value))
                elif isinstance(keyword.value, ast.Name) and keyword.value.id in local:
                    emitted.add(local[keyword.value.id])

    expected = {check for _, check in _EXPECTED_CHECKS}
    external = {"demo_tenant_untouched", "writes_only_under_smoke_tenant"}
    # `round_trip_completed` is the provisioning-failure row, not part of a complete run.
    assert emitted - expected == {"round_trip_completed"}
    assert expected - emitted == external


def test_main_declares_the_full_expected_set_to_the_coverage_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.smoke_proposal_card as module

    captured: dict[str, Any] = {}

    def _run_main(_argv: object, **kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(module, "run_main", _run_main)
    assert module.main([]) == 0
    assert captured["expected"] is expected_check_count


def test_a_provisioning_failure_still_publishes_every_expected_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the principal cannot be provisioned, every row the run owes is a named defer."""
    import scripts.smoke_proposal_card as module

    class _Provisioner:
        def __init__(self, _ctx: object, *, jwks_url: str | None = None) -> None:
            del jwks_url

        def store(self) -> object:
            return object()

        def provision(self) -> Principal:
            raise RuntimeError("cognito unavailable")

        def tenant(self) -> str:
            return ""

        def cleanup(self) -> list[CheckResult]:
            return []

    monkeypatch.setattr(module, "LiveProposalCardPrincipal", _Provisioner)
    monkeypatch.setattr(module, "store_row_reader", lambda _store: None)
    monkeypatch.setattr(module, "read_tenant_snapshot", lambda _reader, _tenant: {})
    monkeypatch.setattr(
        module,
        "demo_tenant_untouched_check",
        lambda **_kwargs: CheckResult(
            resource="proposal_card", check="demo_tenant_untouched", status="pass"
        ),
    )
    results = list(module.checks(context_for_test(outputs={})))
    keys = {(r.resource, r.check) for r in results}
    assert set(_EXPECTED_CHECKS) <= keys
    assert _find(results, "round_trip_completed").status == "fail"
    deferred = _find(results, "commits_the_change")
    assert deferred.status == "defer"
    assert "round_trip_completed" in deferred.detail

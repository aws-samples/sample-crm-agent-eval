# SPDX-License-Identifier: MIT
"""Offline tests for `scripts/smoke/agents.py` (Layer 4 relay-backed agent turn).

`relay_turn_checks` is driven by a fake `TurnRunner` returning canned event payloads; `checks` by a
fake provisioner over an in-memory store, so the real tenant guards and turn budget run.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import replace
from typing import Any

import pytest
import scripts.smoke.agents as agents_smoke
import scripts.smoke.three_tier_chat as three_tier_chat
from fixtures.demo_seed import demo_account_ids
from scripts.smoke.agents import (
    GRID_DEFER_REASONS,
    _could_commit_high_risk_change,
    _high_risk_check,
    _turn_rows,
    out_of_book_account,
    relay_turn_checks,
    turn_prompt,
)
from scripts.smoke.harness import CheckResult

from agents.registry import tool_registry_for
from agents.subagents import ALL_SUBAGENT_SPECS
from agents.supervisor import SUPERVISOR_ROLE
from crm.toolspec.definitions import BY_NAME

_ROLES: tuple[str, ...] = tuple(spec.role for spec in ALL_SUBAGENT_SPECS)
_OUT_OF_BOOK = "acct-eval-smoke-test-0007"
_PROMPT = f"Summarise account {_OUT_OF_BOOK}."

#: The per-role `(resource, check)` turn-row grid: every role x responds/no_surplus_tool/
#: no_missing_tool/no_direct_high_risk_write.
_TURN_ROW_GRID: frozenset[tuple[str, str]] = frozenset(
    {
        (role, check)
        for role in (SUPERVISOR_ROLE, *_ROLES)
        for check in ("responds", "no_surplus_tool", "no_missing_tool", "no_direct_high_risk_write")
    }
    | {
        ("mutation", f"outcome_discipline_{name}")
        for name in ("scope_denial", "empty_result", "handler_error", "distinct")
    }
)


def _cost_line(role: str, *, tokens: int = 10) -> dict[str, Any]:
    return {
        "tier": "supervisor",
        "role": role,
        "model_id": "m",
        "input_tokens": tokens,
        "output_tokens": 0,
    }


def _completed(*roles: str, tokens: int = 10) -> dict[str, Any]:
    return {
        "kind": "completed",
        "text": "done",
        "cost_lines": [_cost_line(role, tokens=tokens) for role in roles],
    }


_HEALTHY: tuple[dict[str, Any], ...] = (
    {"kind": "tool_call", "tool_name": "get_account", "tool_input": {"account_id": "x"}},
    {"kind": "tool_result", "tool_output": {"statusCode": 403, "error": {"code": "scope_denied"}}},
    _completed(SUPERVISOR_ROLE, "research"),
)


def _turn(*events: dict[str, Any]) -> Any:
    """A `TurnRunner` returning canned event payloads, recording each prompt it was given."""
    prompts: list[str] = []

    def _run(prompt: str) -> list[dict[str, Any]]:
        prompts.append(prompt)
        return [dict(e) for e in events]

    _run.prompts = prompts  # type: ignore[attr-defined]
    return _run


def _rows(results: Iterable[CheckResult]) -> dict[tuple[str, str], CheckResult]:
    return {(r.resource, r.check): r for r in results}


def _run(*events: dict[str, Any]) -> dict[tuple[str, str], CheckResult]:
    return _rows(
        relay_turn_checks(_turn(*events), prompt=_PROMPT, out_of_book_account=_OUT_OF_BOOK)
    )


# -- the row set ---------------------------------------------------------------------------------


def test_every_former_grid_row_is_still_published() -> None:
    assert set(_turn_rows()) >= _TURN_ROW_GRID
    assert set(_run(*_HEALTHY)) == set(_turn_rows())


@pytest.mark.parametrize(
    "events",
    [_HEALTHY, (), ({"kind": "error", "error_kind": "model_failure", "error_message": "x"},)],
    ids=["healthy", "empty", "error"],
)
def test_every_path_publishes_every_row_in_order(events: tuple[dict[str, Any], ...]) -> None:
    results = relay_turn_checks(_turn(*events), prompt=_PROMPT, out_of_book_account=_OUT_OF_BOOK)
    assert [(r.resource, r.check) for r in results] == list(_turn_rows())


def test_every_declared_defer_names_its_reason_on_a_healthy_turn() -> None:
    rows = _run(*_HEALTHY)
    for key, reason in GRID_DEFER_REASONS.items():
        assert rows[key].status == "defer", key
        assert rows[key].defer_reason == reason
        assert rows[key].detail


def test_a_healthy_turn_establishes_the_rows_the_stream_carries_and_fails_none() -> None:
    rows = _run(*_HEALTHY)
    established = {key for key, row in rows.items() if row.status == "pass"}
    assert established == {
        ("relay_turn", "relay_turn_reaches_completion"),
        ("relay_turn", "no_out_of_book_row_in_any_tool_result"),
        ("relay_turn", "every_event_kind_is_in_the_closed_vocabulary"),
        (SUPERVISOR_ROLE, "responds"),
        ("research", "responds"),
        (SUPERVISOR_ROLE, "no_surplus_tool"),
        (SUPERVISOR_ROLE, "no_direct_high_risk_write"),
    }
    assert not [key for key, row in rows.items() if row.status == "fail"]
    for role in set(_ROLES) - {"research"}:
        assert rows[(role, "responds")].defer_reason == "role_not_exercised_by_this_turn"
    assert all(row.status in ("pass", "defer") for row in rows.values())
    assert all(row.defer_reason for row in rows.values() if row.status == "defer")


# -- responds ------------------------------------------------------------------------------------


def test_the_supervisor_does_not_respond_on_an_uncompleted_turn() -> None:
    rows = _run({"kind": "tool_call", "tool_name": "get_account"})
    assert rows[(SUPERVISOR_ROLE, "responds")].status == "fail"


def test_an_error_event_fails_the_supervisor_even_with_a_completed_event() -> None:
    rows = _run(
        {"kind": "error", "error_kind": "tool_failure", "error_message": "boom"},
        _completed(SUPERVISOR_ROLE),
    )
    assert rows[(SUPERVISOR_ROLE, "responds")].status == "fail"
    assert rows[("relay_turn", "relay_turn_reaches_completion")].status == "fail"


def test_a_completed_turn_without_cost_lines_defers_responds() -> None:
    rows = _run({"kind": "completed", "text": "done"})
    assert rows[(SUPERVISOR_ROLE, "responds")].defer_reason == "no_cost_lines_observed"


def test_cost_lines_without_the_supervisor_fail_its_responds() -> None:
    rows = _run(_completed("research"))
    assert rows[(SUPERVISOR_ROLE, "responds")].status == "fail"
    assert rows[("research", "responds")].status == "pass"


def test_a_zero_token_cost_line_does_not_count_as_responding() -> None:
    rows = _run(_completed(SUPERVISOR_ROLE, tokens=0))
    assert rows[(SUPERVISOR_ROLE, "responds")].status == "fail"


def test_cost_lines_on_a_usage_event_count() -> None:
    rows = _run(
        {
            "kind": "usage",
            "input_tokens": 1,
            "output_tokens": 1,
            "cost_lines": [_cost_line("mutation")],
        },
        _completed(SUPERVISOR_ROLE),
    )
    assert rows[("mutation", "responds")].status == "pass"


# -- the tool rows -------------------------------------------------------------------------------


def test_an_unregistered_tool_call_is_a_surplus() -> None:
    rows = _run({"kind": "tool_call", "tool_name": "not_a_real_tool"}, _completed(SUPERVISOR_ROLE))
    surplus = rows[(SUPERVISOR_ROLE, "no_surplus_tool")]
    assert surplus.status == "fail"
    assert "not_a_real_tool" in surplus.detail


def test_every_registered_tool_is_not_a_surplus() -> None:
    names = {tool.name for role in _ROLES for tool in tool_registry_for(role)} | set(_ROLES)
    calls = [{"kind": "tool_call", "tool_name": name} for name in sorted(names)]
    rows = _run(*calls, _completed(SUPERVISOR_ROLE))
    assert rows[(SUPERVISOR_ROLE, "no_surplus_tool")].status == "pass"


def test_a_turn_with_no_tool_call_defers_the_supervisors_tool_rows() -> None:
    rows = _run(_completed(SUPERVISOR_ROLE))
    for check in ("no_surplus_tool", "no_direct_high_risk_write"):
        assert rows[(SUPERVISOR_ROLE, check)].defer_reason == "no_tool_call_observed"


def _dangerous_tool() -> Any:
    base = BY_NAME["update_opportunity"]
    return replace(
        base,
        name="update_opportunity_unsafe",
        input_schema={
            "type": "object",
            "properties": {
                "opportunity_id": {"type": "string", "minLength": 1},
                "fields": {"type": "object", "additionalProperties": True, "enum": ["stage"]},
                "transaction_key": {"type": "string", "minLength": 1},
            },
            "required": ["opportunity_id", "fields", "transaction_key"],
            "additionalProperties": False,
        },
    )


def test_a_called_direct_high_risk_writer_fails_the_supervisors_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dangerous = _dangerous_tool()
    monkeypatch.setattr(agents_smoke, "BY_NAME", {**BY_NAME, dangerous.name: dangerous})
    rows = _run({"kind": "tool_call", "tool_name": dangerous.name}, _completed(SUPERVISOR_ROLE))
    high_risk = rows[(SUPERVISOR_ROLE, "no_direct_high_risk_write")]
    assert high_risk.status == "fail"
    assert dangerous.name in high_risk.detail


def test_the_synthetic_writer_is_high_risk_and_the_real_tool_is_not() -> None:
    assert _could_commit_high_risk_change(BY_NAME["update_opportunity"]) is False
    assert _could_commit_high_risk_change(_dangerous_tool()) is True


def test_real_registry_tools_never_trip_the_high_risk_check() -> None:
    for role in (SUPERVISOR_ROLE, *_ROLES):
        result = _high_risk_check(role, [tool.name for tool in tool_registry_for(role)])
        assert result.status == "pass", result.detail


def test_a_read_or_proposal_tool_is_never_high_risk() -> None:
    assert _could_commit_high_risk_change(BY_NAME["submit_proposal"]) is False
    assert _could_commit_high_risk_change(BY_NAME["get_opportunity"]) is False


def test_no_toolspec_risk_class_is_named_high_risk_write() -> None:
    assert "high_risk_write" not in {tool.risk for tool in BY_NAME.values()}


# -- the turn-level rows -------------------------------------------------------------------------


def test_an_out_of_book_row_in_a_tool_result_is_a_failure() -> None:
    rows = _run(
        {"kind": "tool_result", "tool_output": {"account_id": _OUT_OF_BOOK, "name": "Leaked"}},
        _completed(SUPERVISOR_ROLE),
    )
    isolation = rows[("relay_turn", "no_out_of_book_row_in_any_tool_result")]
    assert isolation.status == "fail"
    assert _OUT_OF_BOOK in isolation.detail


def test_isolation_defers_when_no_tool_result_arrived() -> None:
    rows = _run(_completed(SUPERVISOR_ROLE))
    isolation = rows[("relay_turn", "no_out_of_book_row_in_any_tool_result")]
    assert isolation.defer_reason == "no_tool_result_observed"


def test_a_turn_that_never_completes_is_a_failure() -> None:
    completion = _run({"kind": "tool_call", "tool_name": "get_account"})[
        ("relay_turn", "relay_turn_reaches_completion")
    ]
    assert completion.status == "fail"
    assert "did not terminate" in completion.detail


def test_an_error_event_is_a_reported_failure_naming_its_kind() -> None:
    completion = _run(
        {"kind": "error", "error_kind": "budget_exhausted", "error_message": "out of budget"}
    )[("relay_turn", "relay_turn_reaches_completion")]
    assert completion.status == "fail"
    assert "budget_exhausted" in completion.detail


def test_an_event_kind_outside_the_vocabulary_is_a_contract_break() -> None:
    vocab = _run({"kind": "thinking", "text": "hmm"}, _completed(SUPERVISOR_ROLE))[
        ("relay_turn", "every_event_kind_is_in_the_closed_vocabulary")
    ]
    assert vocab.status == "fail"
    assert "thinking" in vocab.detail


def test_an_empty_stream_defers_the_vocabulary_row() -> None:
    vocab = _run()[("relay_turn", "every_event_kind_is_in_the_closed_vocabulary")]
    assert vocab.defer_reason == "no_event_observed"


def test_a_transport_failure_fails_every_row() -> None:
    def _explode(prompt: str) -> list[dict[str, Any]]:
        raise ValueError("403 from CloudFront")

    results = relay_turn_checks(_explode, prompt=_PROMPT, out_of_book_account=_OUT_OF_BOOK)
    assert [(r.resource, r.check) for r in results] == list(_turn_rows())
    assert {r.status for r in results} == {"fail"}
    assert all("403 from CloudFront" in r.detail for r in results)


def test_the_rows_never_read_the_assistants_prose() -> None:
    stream: list[dict[str, Any]] = [
        {"kind": "tool_call", "tool_name": "get_account"},
        {"kind": "tool_result", "tool_output": {"error": {"code": "scope_denied"}}},
        {"kind": "completed", "cost_lines": [_cost_line(SUPERVISOR_ROLE)]},
    ]
    a = _run(*[{**e, "text": "Certainly! Here is a summary."} for e in stream])
    b = _run(*[{**e, "text": "I am unable to help with that request."} for e in stream])
    assert {k: (r.status, r.detail) for k, r in a.items()} == {
        k: (r.status, r.detail) for k, r in b.items()
    }


# -- the live orchestration, offline -------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def query_tenant_scan(
        self, table: str, *, tenant: str, exclusive_start_key: Any = None
    ) -> tuple[list[dict[str, Any]], None]:
        return list(self.rows.get((table, tenant), [])), None


class _FakeProvisioner:
    """Records the order of its calls; `provision` writes one reps row under its tenant."""

    def __init__(self, store: _FakeStore, log: list[str], *, fail: bool = False) -> None:
        self._store = store
        self._log = log
        self._fail = fail
        self._tenant = ""

    def store(self) -> _FakeStore:
        return self._store

    def tenant(self) -> str:
        return self._tenant

    def provision(self) -> str:
        self._log.append("provision")
        if self._fail:
            raise RuntimeError("cognito refused")
        self._tenant = "eval-smoke-abc"
        self._store.rows[("reps", self._tenant)] = [{"tenant": self._tenant}]
        return "token-1"

    def cleanup(self) -> Iterator[CheckResult]:
        self._log.append("cleanup")
        yield CheckResult(
            resource="smoke_tenant", check="tenant_left_clean", status="pass", detail="ok"
        )


def _orchestrate(
    *, fail_provision: bool = False, runner: Any = None
) -> tuple[list[CheckResult], list[str], Any]:
    log: list[str] = []
    store = _FakeStore()
    provisioner = _FakeProvisioner(store, log, fail=fail_provision)
    turn = runner if runner is not None else _turn(*_HEALTHY)
    tokens: list[str] = []

    def runner_factory(_ctx: Any, token: str) -> Any:
        tokens.append(token)
        return turn

    results = list(
        agents_smoke.checks(
            object(),  # type: ignore[arg-type]
            provisioner_factory=lambda _ctx: provisioner,  # type: ignore[arg-type,return-value]
            runner_factory=runner_factory,
        )
    )
    return results, log + [f"token:{t}" for t in tokens], turn


def test_the_pass_path_runs_one_counted_turn_and_cleans_up(
    capsys: pytest.CaptureFixture[str],
) -> None:
    results, log, turn = _orchestrate()
    rows = _rows(results)
    assert set(_turn_rows()) <= set(rows)
    assert not [key for key, row in rows.items() if row.status == "fail"]
    assert rows[("smoke_principal", "mfa_access_token_issued")].status == "pass"
    assert rows[("agents", "demo_tenant_untouched")].status == "pass"
    assert rows[("agents", "writes_only_under_smoke_tenant")].status == "pass"
    assert rows[("smoke_tenant", "tenant_left_clean")].status == "pass"
    budget = rows[("agents", "model_backed_turns_within_envelope")]
    assert budget.status == "pass" and "1 model-backed turn(s)" in budget.detail
    assert log == ["provision", "cleanup", "token:token-1"]
    assert len(turn.prompts) == 1
    assert "model-backed turn 1/1" in capsys.readouterr().err


def test_the_turn_names_a_real_demo_account_the_caller_cannot_see() -> None:
    _results, _log, turn = _orchestrate()
    account = out_of_book_account()
    assert account in demo_account_ids()
    assert turn.prompts == [turn_prompt(account)]
    assert account in turn.prompts[0]


def test_a_provisioning_failure_defers_every_turn_row_and_still_cleans_up() -> None:
    results, log, turn = _orchestrate(fail_provision=True)
    rows = _rows(results)
    assert rows[("smoke_principal", "mfa_access_token_issued")].status == "fail"
    for key in _turn_rows():
        assert (
            rows[key].status == "defer" and rows[key].defer_reason == "upstream_check_did_not_run"
        )
    assert turn.prompts == []
    assert log[-1] == "cleanup"
    assert ("agents", "writes_only_under_smoke_tenant") not in rows


def test_a_spent_share_refuses_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        three_tier_chat,
        "MODEL_TURN_ALLOTMENTS",
        {**three_tier_chat.MODEL_TURN_ALLOTMENTS, "agents": 0},
    )
    results, log, turn = _orchestrate()
    rows = _rows(results)
    assert turn.prompts == []
    assert any(
        r.check == "model_backed_turns_within_envelope" and r.status == "fail" for r in results
    )
    for key in _turn_rows():
        assert rows[key].defer_reason == "model_turn_share_spent"
    assert "cleanup" in log


def test_a_transport_failure_fails_the_turn_rows_and_still_cleans_up() -> None:
    def _explode(prompt: str) -> list[dict[str, Any]]:
        raise ValueError("502 from the relay")

    results, log, _turn_runner = _orchestrate(runner=_explode)
    rows = _rows(results)
    assert {rows[key].status for key in _turn_rows()} == {"fail"}
    assert "cleanup" in log
    assert rows[("agents", "demo_tenant_untouched")].status == "pass"


def test_a_demo_write_during_the_turn_fails_the_guard() -> None:
    log: list[str] = []
    store = _FakeStore()
    provisioner = _FakeProvisioner(store, log)

    def writing_turn(prompt: str) -> list[dict[str, Any]]:
        store.rows[("accounts", "demo")] = [{"tenant": "demo"}]
        return [dict(e) for e in _HEALTHY]

    results = list(
        agents_smoke.checks(
            object(),  # type: ignore[arg-type]
            provisioner_factory=lambda _ctx: provisioner,  # type: ignore[arg-type,return-value]
            runner_factory=lambda _ctx, _token: writing_turn,
        )
    )
    assert _rows(results)[("agents", "demo_tenant_untouched")].status == "fail"


def test_the_module_has_a_one_turn_share_within_the_envelope() -> None:
    assert three_tier_chat.MODEL_TURN_ALLOTMENTS[agents_smoke.RESOURCE_CLASS] == 1
    assert (
        sum(three_tier_chat.MODEL_TURN_ALLOTMENTS.values()) <= three_tier_chat.MODEL_TURN_ENVELOPE
    )


def test_main_declares_no_token_flag(tmp_path: Any) -> None:
    with pytest.raises(SystemExit) as exc_info:
        agents_smoke.main(["--token", "X", "--outputs", str(tmp_path / "nope.json")])
    assert exc_info.value.code == 2


def test_grid_defer_reasons_cover_exactly_the_rows_a_turn_cannot_establish() -> None:
    establishable: set[tuple[str, str]] = {
        (SUPERVISOR_ROLE, c) for c in ("responds", "no_surplus_tool", "no_direct_high_risk_write")
    }
    establishable |= {(role, "responds") for role in _ROLES}
    assert set(GRID_DEFER_REASONS) == _TURN_ROW_GRID - establishable
    assert isinstance(GRID_DEFER_REASONS, Mapping)


def test_the_default_principal_holds_an_empty_book(monkeypatch: pytest.MonkeyPatch) -> None:
    """The out-of-book turn assumes an empty book; a book reroutes it to the echoing denial."""
    import inspect

    from scripts.smoke.harness import context_for_test

    from tests.unit.test_smoke_agent_stream import rep_row_written_by

    factory = inspect.signature(agents_smoke.checks).parameters["provisioner_factory"].default
    ctx = context_for_test(
        outputs={
            "dynamodb_table_names": {"eval_runs": "crm-agent-eval-eval_runs"},
            "cognito_user_pool_id": "pool",
            "cognito_spa_client_id": "client",
        }
    )
    row = rep_row_written_by(factory(ctx), monkeypatch)
    assert row["book"] == []
    assert row["full_visibility"] is False

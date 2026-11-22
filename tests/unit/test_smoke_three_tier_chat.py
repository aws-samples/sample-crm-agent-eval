# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.three_tier_chat` (attribution, cost, refusal, history).

The provisioner factory, turn runner, CloudTrail lookup, clock and sleeper are all injected.
"""

from __future__ import annotations

import inspect
import io
import json
import re
import urllib.error
from collections.abc import Iterable, Mapping
from decimal import Decimal
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.smoke import three_tier_chat
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.history_cleanup import HISTORY_ITEMS_DELETED_CHECK
from scripts.smoke.three_tier_chat import (
    _CACHE_HISTORY_INSTRUCTION,
    _CLOUDTRAIL_MAX_PAGES,
    _CLOUDTRAIL_POLL_INTERVAL,
    _CLOUDTRAIL_POLL_SECONDS,
    _CONTROL_ROUTE,
    _DELEGATION_FORCING_TOOL,
    _DELEGATION_PROMPT,
    _GENERIC_VALIDATION_MESSAGE,
    _MIXED_DELEGATION_PROMPT,
    _OPENAI_CACHE_PROBE_PROMPT,
    _OPENAI_DELEGATION_PROMPT,
    _REFUSAL_TIER,
    ANTHROPIC_DELEGATES_FAMILIES,
    ATTRIBUTION_CANDIDATES,
    ATTRIBUTION_COLLISION_CHECK,
    ATTRIBUTION_ROW_SUFFIX,
    CACHE_HISTORY_CHECK,
    CACHE_HISTORY_TOOL_CALL_REASON,
    CACHE_HISTORY_TURNS,
    CACHE_UNREPORTED_REASON,
    HISTORY_FAILURE_EVENTS,
    HISTORY_ITEM_CHECK,
    HISTORY_LOG_CHECK,
    HISTORY_OMITTED_RESUME_CHECK,
    HISTORY_RECALL_CHECK,
    HISTORY_RECALL_PROMPT,
    HISTORY_RECALL_TURNS,
    HISTORY_REPEAT_PROMPT,
    MIXED_FAMILIES,
    MODEL_TURN_ALLOTMENTS,
    MODEL_TURN_ENVELOPE,
    OPENAI_FAMILIES,
    OPENAI_ROW_SUFFIX,
    REFUSAL_ERROR_KIND,
    RESOURCE_CLASS,
    CloudTrailEvent,
    ModelTurnBudget,
    ResumeCursor,
    RuntimeLogLine,
    TurnResult,
    _live_cloudtrail_lookup,
    _live_run_turn,
    _live_runtime_log_lookup,
    _LiveTurns,
    _poll_history_logs,
    _tier_attributed,
    _tier_profiles,
    attribution_collision_row,
    cache_history_prompt,
    checks,
    evaluate_cache_history,
    evaluate_cache_read,
    evaluate_history_cross_session,
    evaluate_history_item,
    evaluate_history_logs,
    evaluate_history_recall,
    evaluate_omitted_resume_recall,
    evaluate_page_context_refusal,
    evaluate_temperature_refusal,
    evaluate_three_tier_attribution,
    evaluate_turn_cost,
    expected_check_count,
    expected_tier_efforts,
    expected_tier_models,
    history_code_word,
    history_fact_prompt,
    page_context_for,
    refused_routes,
    select_attribution_mapping,
    stale_temperature_block,
    tuning_block,
)

from agents.conversation_history import (
    DEFAULT_TTL_SECONDS,
    HISTORY_ITEM_ATTRIBUTES,
    DynamoDbConversationHistory,
)
from agents.registry import tool_registry_for
from agents.runtime_server import _VALIDATION_ERROR_KIND
from agents.supervisor import DIRECT_TOOL_NAMES
from crm.store.keys import history_key
from providers.capabilities import load_capability_matrix
from providers.tier_tuning import WIRE_SCHEMA_VERSION

_ROLE = "crm-agent-eval-runtime-role"
_ROLE_ARN = f"arn:aws:iam::123456789012:role/{_ROLE}"
_TIERS = select_attribution_mapping().profiles
_PATH = f"tuning.tiers.{_REFUSAL_TIER}.temperature"
_MODELS_YAML = Path(__file__).resolve().parents[2] / "config" / "models.yaml"


def _event(role: str, profile: str) -> CloudTrailEvent:
    # modelId in CloudTrail is the inference-profile ARN; the profile id is a substring of it.
    return CloudTrailEvent(role_name=role, model_id=f"arn:aws:bedrock:us-east-1::{profile}")


def _attr(events: list[CloudTrailEvent]) -> dict[str, str]:
    rows = list(evaluate_three_tier_attribution(events, runtime_role=_ROLE, tier_profiles=_TIERS))
    assert all(r.resource == RESOURCE_CLASS for r in rows)
    return {r.check: r.status for r in rows}


# --- check 1: three-tier attribution ---


def test_all_three_tiers_attributed_passes() -> None:
    by_check = _attr(
        [
            _event(_ROLE, _TIERS["supervisor"]),
            _event(_ROLE, _TIERS["subagent"]),
            _event(_ROLE, _TIERS["tool"]),
        ]
    )
    assert by_check["supervisor_tier_attribution_control"] == "pass"
    assert by_check["subagent_tier_attribution"] == "pass"
    assert by_check["tool_tier_attribution"] == "pass"


def test_missing_supervisor_control_stops_and_suppresses_tier_rows() -> None:
    """No supervisor invocation => blind query; the tier assertions must not be emitted."""
    by_check = _attr([_event(_ROLE, _TIERS["subagent"]), _event(_ROLE, _TIERS["tool"])])
    assert by_check["supervisor_tier_attribution_control"] == "fail"
    assert "subagent_tier_attribution" not in by_check
    assert "tool_tier_attribution" not in by_check


def test_subagent_absence_is_a_real_fail_when_control_present() -> None:
    by_check = _attr([_event(_ROLE, _TIERS["supervisor"]), _event(_ROLE, _TIERS["tool"])])
    assert by_check["supervisor_tier_attribution_control"] == "pass"
    assert by_check["subagent_tier_attribution"] == "fail"
    assert by_check["tool_tier_attribution"] == "pass"


def test_invocation_by_a_different_role_is_not_attributed() -> None:
    """The same profile invoked by another principal must not satisfy the control."""
    by_check = _attr([_event("some-other-role", _TIERS["supervisor"])])
    assert by_check["supervisor_tier_attribution_control"] == "fail"


# --- check 1 mapping: derived from the tier assignment, and discriminating ---

_CONFIG_DIR = _MODELS_YAML.parent
_SONNET = "anthropic.claude-sonnet-5-5"


def _home_tier_matrix(tmp_path: Path, family: str) -> Any:
    """A scratch matrix assigning each of `family`'s tiers the model whose home `tier` it is."""
    models = yaml.safe_load(_MODELS_YAML.read_text(encoding="utf-8"))
    for model in load_capability_matrix().models.values():
        if model.family == family:
            models["families"][family]["tiers"][model.tier]["model"] = model.model_id
    models_path = tmp_path / "models.yaml"
    models_path.write_text(yaml.safe_dump(models), encoding="utf-8")
    return load_capability_matrix(
        models_path=models_path, capabilities_path=_CONFIG_DIR / "model_capabilities.yaml"
    )


def _colliding_matrix(tmp_path: Path) -> Any:
    """A scratch matrix assigning Sonnet to every anthropic tier, so every candidate collides."""
    models = yaml.safe_load(_MODELS_YAML.read_text(encoding="utf-8"))
    models["families"]["anthropic"]["tiers"]["tool"]["model"] = _SONNET
    capabilities = yaml.safe_load(
        (_CONFIG_DIR / "model_capabilities.yaml").read_text(encoding="utf-8")
    )
    capabilities["models"][_SONNET]["assignable_tiers"] = ["supervisor", "subagent", "tool"]
    models_path = tmp_path / "models.yaml"
    capabilities_path = tmp_path / "model_capabilities.yaml"
    models_path.write_text(yaml.safe_dump(models), encoding="utf-8")
    capabilities_path.write_text(yaml.safe_dump(capabilities), encoding="utf-8")
    return load_capability_matrix(models_path=models_path, capabilities_path=capabilities_path)


def test_the_real_config_selects_a_distinct_mapping_that_is_not_all_anthropic() -> None:
    selection = select_attribution_mapping()
    assert selection.families is not None
    assert selection.families in ATTRIBUTION_CANDIDATES
    assert selection.families != dict.fromkeys(MIXED_FAMILIES, "anthropic")
    assert len(set(selection.profiles.values())) == len(selection.profiles) == 3


def test_the_current_config_selects_the_sol_sonnet_haiku_profiles() -> None:
    selection = select_attribution_mapping()
    assert selection.families == {
        "supervisor": "openai",
        "subagent": "anthropic",
        "tool": "anthropic",
    }
    assert selection.profiles == {
        "supervisor": "global.openai.gpt-6.2-sol",
        "subagent": "global.anthropic.claude-sonnet-5-5",
        "tool": "global.anthropic.claude-haiku-5-5",
    }


def test_the_selected_profiles_are_the_assigned_models_profiles() -> None:
    matrix = load_capability_matrix()
    selection = select_attribution_mapping()
    assert selection.families is not None
    assert selection.profiles == {
        tier: matrix.model_for(family, tier).inference_profile  # type: ignore[arg-type]
        for tier, family in selection.families.items()
    }


def test_the_all_anthropic_candidate_is_preferred_when_its_profiles_are_distinct() -> None:
    assert (
        dict.fromkeys(MIXED_FAMILIES, "anthropic"),
        ANTHROPIC_DELEGATES_FAMILIES,
        MIXED_FAMILIES,
    ) == ATTRIBUTION_CANDIDATES


def test_check_1_runs_the_anthropic_subagent_and_evaluates_its_rows() -> None:
    """Under the real config check 1 puts anthropic on the subagent tier and evaluates its rows."""
    turns = _StatefulTurns()
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    attribution_turn = turns.tunings[turns.prompts.index(_DELEGATION_PROMPT)]
    assert attribution_turn["tiers"]["subagent"]["family"] == "anthropic"
    selection = select_attribution_mapping()
    assert selection.families is not None
    assert selection.families["subagent"] == "anthropic"
    model = load_capability_matrix().model_for("anthropic", "subagent")
    assert selection.profiles["subagent"] == model.inference_profile
    effort = expected_tier_efforts(selection.families)["subagent"]
    row = f"subagent_resolved_effort_is_{effort}{ATTRIBUTION_ROW_SUFFIX}"
    assert by_check[row] == "pass"
    assert by_check[f"subagent_cost_line_names_{model.model_id}{ATTRIBUTION_ROW_SUFFIX}"] == "pass"


def test_the_home_tier_lookup_is_red_against_the_assignment(tmp_path: Path) -> None:
    """Events on the assigned profiles fail the supervisor control under a home-tier assignment."""
    families = dict.fromkeys(MIXED_FAMILIES, "anthropic")
    assigned = _tier_profiles(families)
    home = _tier_profiles(families, _home_tier_matrix(tmp_path, "anthropic"))
    assert home["supervisor"] != assigned["supervisor"]
    events = [_event(_ROLE, profile) for profile in assigned.values()]
    rows = evaluate_three_tier_attribution(events, runtime_role=_ROLE, tier_profiles=home)
    by_check = {r.check: r.status for r in rows}
    assert by_check["supervisor_tier_attribution_control"] == "fail"


def test_a_config_where_every_mapping_collides_selects_none(tmp_path: Path) -> None:
    selection = select_attribution_mapping(_colliding_matrix(tmp_path))
    assert selection.families is None
    assert [families for families, _ in selection.tried] == list(ATTRIBUTION_CANDIDATES)
    row = attribution_collision_row(selection)
    assert (row.check, row.status) == (ATTRIBUTION_COLLISION_CHECK, "fail")
    assert "global.anthropic.claude-sonnet-5-5" in row.detail
    assert "supervisor=anthropic,subagent=openai,tool=anthropic" in row.detail
    assert "supervisor=openai,subagent=anthropic,tool=anthropic" in row.detail


def test_a_colliding_config_fails_check_1_end_to_end_and_sends_no_attribution_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix = _colliding_matrix(tmp_path)
    monkeypatch.setattr(three_tier_chat, "load_capability_matrix", lambda: matrix)
    turns = _StatefulTurns()
    by_check, provs = _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    assert by_check[ATTRIBUTION_COLLISION_CHECK] == "fail"
    assert "delegation_turn_completed" not in by_check
    assert "supervisor_tier_attribution_control" not in by_check
    assert _DELEGATION_PROMPT not in turns.prompts
    assert not any(c.endswith(ATTRIBUTION_ROW_SUFFIX) for c in by_check)
    assert by_check["foreign_session_reuse_refused"] == "pass"
    assert all(p.cleaned_up for p in provs)


def test_the_attribution_rows_and_the_delegation_turn_name_the_mapping() -> None:
    families = select_attribution_mapping().families
    assert families is not None
    rows = list(
        evaluate_three_tier_attribution(
            _ALL_TIER_EVENTS, runtime_role=_ROLE, tier_profiles=_TIERS, families=families
        )
    )
    label = ",".join(f"{tier}={family}" for tier, family in families.items())
    assert rows and all(f"mapping {label}" in r.detail for r in rows)
    turns = _StatefulTurns()
    _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    assert turns.tunings[turns.prompts.index(_DELEGATION_PROMPT)] == tuning_block(families)


# --- check 4: session owner-binding (turn-path 400 collision) ---


def _ok() -> TurnResult:
    return TurnResult(status=200, kinds=("text_delta", "completed"), answer="noted")


def _collision_400() -> TurnResult:
    """The turn-path session-collision result: a 400 carrying the generic validation message."""
    return TurnResult(status=400, kinds=(), answer="", message=_GENERIC_VALIDATION_MESSAGE)


def _hist(owner_reuse: TurnResult, foreign: TurnResult) -> dict[str, str]:
    return {r.check: r.status for r in evaluate_history_cross_session(owner_reuse, foreign)}


def test_owner_reuse_ok_and_foreign_400_collision_passes() -> None:
    by_check = _hist(_ok(), _collision_400())
    assert by_check["owner_session_reuse_control"] == "pass"
    assert by_check["foreign_session_reuse_refused"] == "pass"


def test_foreign_200_is_not_a_denial() -> None:
    """If B's reuse is NOT refused (turn ran), the denial must red."""
    by_check = _hist(_ok(), _ok())
    assert by_check["foreign_session_reuse_refused"] == "fail"


def test_foreign_400_with_a_relay_local_message_reds_the_denial() -> None:
    """A 400 carrying a relay-local message does not count as a session-collision refusal."""
    relay_local = TurnResult(
        status=400, kinds=(), answer="", message="A message and a session id are required."
    )
    assert _hist(_ok(), relay_local)["foreign_session_reuse_refused"] == "fail"


def test_foreign_403_reds_the_denial_because_the_turn_path_cannot_return_it() -> None:
    """A raw 403 on the turn path does not pass the foreign-reuse denial check."""
    raw_403 = TurnResult(status=403, kinds=(), answer="", message="")
    assert _hist(_ok(), raw_403)["foreign_session_reuse_refused"] == "fail"


def test_foreign_502_reds_the_denial() -> None:
    """A 502 is a runtime-unavailable failure, not a session collision."""
    unavailable = TurnResult(
        status=502, kinds=(), answer="", message="The assistant is unavailable."
    )
    assert _hist(_ok(), unavailable)["foreign_session_reuse_refused"] == "fail"


def test_owner_control_fails_when_reuse_rejected_and_stops() -> None:
    by_check = _hist(_collision_400(), _collision_400())
    assert by_check["owner_session_reuse_control"] == "fail"
    # control failing suppresses the foreign assertion (untrustworthy)
    assert "foreign_session_reuse_refused" not in by_check


# --- checks() end-to-end, fully injected ---


def _tenant_of(token: str) -> str:
    """The disposable tenant `_FakeProvisioner` allocates for `token`."""
    return f"eval-smoke-{token}"


#: `expires_at` the fake Runtime stamps on a history item: inside `_run`'s clock window plus the TTL.
_FAKE_EXPIRES_AT = 50_000

#: How check 9's turn A states its fact; the fake reads the code word back out of it.
_FACT_PREFIX = history_fact_prompt("\0").split("\0")[0]


class _FakeHistoryItems:
    """`HistoryItems` over a dict; `sticky` keys survive `delete`, as a refused delete would."""

    def __init__(self, items: dict[str, dict[str, Any]], *, sticky: Iterable[str] = ()) -> None:
        self.items = items
        self.deleted: list[str] = []
        self._sticky = set(sticky)

    def get(self, rep_id: str) -> Mapping[str, Any] | None:
        return self.items.get(rep_id)

    def delete(self, rep_id: str) -> None:
        self.deleted.append(rep_id)
        if rep_id not in self._sticky:
            self.items.pop(rep_id, None)


class _FakeProvisioner:
    def __init__(self, token: str) -> None:
        self._token = token
        self.cleaned_up = False

    def provision(self) -> str:
        return self._token

    def tenant(self) -> str:
        return _tenant_of(self._token)

    def cleanup(self) -> Iterable[CheckResult]:
        self.cleaned_up = True
        yield CheckResult(
            resource="smoke_user", check="cleaned_up", status="pass", detail="removed"
        )


def _priced_frame(
    tuning: Mapping[str, Any], *, cache_read: int = 1024, cache_write: int = 0
) -> dict[str, Any]:
    """A `completed` frame priced and resolved the way the Runtime renders one for `tuning`."""
    tiers = tuning["tiers"]
    families = {tier: tiers[tier]["family"] for tier in tiers}
    models = expected_tier_models(families)
    efforts = expected_tier_efforts(families)
    return {
        "kind": "completed",
        "cost_lines": [
            {
                "tier": tier,
                "role": tier,
                "model_id": model,
                "cost_usd": 0.01,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "long_context_calls": 0,
            }
            for tier, model in models.items()
        ],
        "cost_usd": 0.03,
        "cost_withheld": False,
        "resolved_tiers": [
            {
                "tier": tier,
                "family": tiers[tier]["family"],
                "model_id": models[tier],
                "effort": efforts[tier],
            }
            for tier in tiers
        ],
    }


def _refusal(
    message: str = f"tuning.tiers.{_REFUSAL_TIER}.temperature: temperature is no longer a "
    "tuning parameter; remove it.",
) -> TurnResult:
    """The Runtime's validation refusal: one `error` frame in a 200 stream."""
    return TurnResult(
        status=200,
        kinds=("error",),
        answer="",
        error={"kind": "error", "error_kind": REFUSAL_ERROR_KIND, "error_message": message},
    )


def _carries_temperature(tuning: Mapping[str, Any]) -> bool:
    return any("temperature" in request for request in tuning["tiers"].values())


def _completed(
    text: str = "",
    tuning: Mapping[str, Any] | None = None,
    *,
    cache_read: int = 1024,
    cache_write: int = 0,
) -> TurnResult:
    return TurnResult(
        status=200,
        kinds=("text_delta", "completed"),
        answer=text,
        completed=(
            _priced_frame(tuning, cache_read=cache_read, cache_write=cache_write)
            if tuning is not None
            else None
        ),
    )


def _route_refusal(route: str, *, echo: bool = False) -> TurnResult:
    """The relay's 400 for a Runtime page-context 400; `echo` plants the route in the body."""
    raw = json.dumps({"message": _GENERIC_VALIDATION_MESSAGE, **({"route": route} if echo else {})})
    return TurnResult(
        status=400, kinds=(), answer="", message=_GENERIC_VALIDATION_MESSAGE, raw_body=raw
    )


class _StatefulTurns:
    """A run-turn fake that binds each `session_id` to the first token that uses it.

    A different token reusing an owned session id gets a 400 with the generic validation message;
    `bad_collision` returns a completed 200 instead. A `page_context` whose route is not
    `_CONTROL_ROUTE` is refused with that 400; `accepts_bad_route` completes it instead and
    `echoes_route` puts the route in the refusal body. The n-th completed turn in a session reports
    `1024 * n` cache-read and 256 cache-write tokens on every cost line, as a growing cached history
    would.

    A turn stating check 9's code word is remembered for its session, and a recall prompt in that
    session answers with it; `forgets_history` answers every recall without it and `leaks_history`
    answers a recall in any session with the last code word stated. Each completed turn writes its
    session's history item into `history` unless `records_history` is false.

    Every accepted turn appends an `invocation_started` line to `log_lines`, in the stream
    `runtime-logs-<session_id>`; `history_failure` adds that failure event's line to the stream of
    every completed turn. `replays_omitted_resume` answers a turn sent without `resume_from` with
    its session's previous result verbatim, as a Runtime that treats it as a reconnect would.
    """

    def __init__(
        self,
        *,
        bad_collision: bool = False,
        honours_temperature: bool = False,
        accepts_bad_route: bool = False,
        echoes_route: bool = False,
        forgets_history: bool = False,
        leaks_history: bool = False,
        records_history: bool = True,
        history_failure: str | None = None,
        replays_omitted_resume: bool = False,
    ) -> None:
        self._history_failure = history_failure
        self._replays_omitted_resume = replays_omitted_resume
        self._last_by_session: dict[str, TurnResult] = {}
        self.log_lines: list[RuntimeLogLine] = []
        self.omitted: list[bool] = []
        self._forgets_history = forgets_history
        self._leaks_history = leaks_history
        self._records_history = records_history
        self._facts: dict[str, str] = {}
        self._last_fact = ""
        self.history: dict[str, dict[str, Any]] = {}
        self._owner_by_session: dict[str, str] = {}
        self._turns_by_session: dict[str, int] = {}
        self._bad_collision = bad_collision
        self._honours_temperature = honours_temperature
        self._accepts_bad_route = accepts_bad_route
        self._echoes_route = echoes_route
        self.tunings: list[Mapping[str, Any]] = []
        self.prompts: list[str] = []
        self.page_contexts: list[Mapping[str, Any] | None] = []

    def __call__(
        self,
        _ctx: object,
        token: str,
        session_id: str,
        prompt: str,
        tuning: Mapping[str, Any],
        *,
        page_context: Mapping[str, Any] | None = None,
        omit_resume_from: bool = False,
    ) -> TurnResult:
        self.tunings.append(tuning)
        self.prompts.append(prompt)
        self.page_contexts.append(page_context)
        self.omitted.append(omit_resume_from)
        if _carries_temperature(tuning) and not self._honours_temperature:
            return _refusal()
        route = (page_context or {}).get("route", _CONTROL_ROUTE)
        if route != _CONTROL_ROUTE and not self._accepts_bad_route:
            return _route_refusal(str(route), echo=self._echoes_route)
        owner = self._owner_by_session.setdefault(session_id, token)
        if owner != token:
            if self._bad_collision:
                return _completed("")  # not refused at all -> should red the denial
            return TurnResult(status=400, kinds=(), answer="", message=_GENERIC_VALIDATION_MESSAGE)
        stream = f"runtime-logs-{session_id}"
        self.log_lines.append(
            RuntimeLogLine(
                stream=stream,
                message=json.dumps({"event": "invocation_started", "session_id": session_id}),
            )
        )
        previous = self._last_by_session.get(session_id)
        if omit_resume_from and self._replays_omitted_resume and previous is not None:
            return previous
        count = self._turns_by_session[session_id] = self._turns_by_session.get(session_id, 0) + 1
        if prompt.startswith(_FACT_PREFIX):
            self._facts[session_id] = self._last_fact = prompt[len(_FACT_PREFIX) :].split(".")[0]
        answer = "noted"
        if prompt in (HISTORY_RECALL_PROMPT, HISTORY_REPEAT_PROMPT):
            remembered = self._last_fact if self._leaks_history else self._facts.get(session_id)
            answer = "I do not know." if self._forgets_history or not remembered else remembered
        if self._records_history:
            tenant = _tenant_of(token)
            self.history[history_key(tenant, session_id)] = {
                "rep_id": history_key(tenant, session_id),
                "kind": "history",
                "owner_rep_id": f"user-{token}",
                "owner_tenant": tenant,
                "turns": [{"prompt": prompt, "answer": answer}] * count,
                "version": count,
                "updated_at": "2026-11-15T00:00:00+00:00",
                "expires_at": _FAKE_EXPIRES_AT,
            }
        if self._history_failure is not None:
            self.log_lines.append(
                RuntimeLogLine(stream=stream, message=json.dumps({"event": self._history_failure}))
            )
        result = _completed(answer, tuning, cache_read=1024 * count, cache_write=256)
        self._last_by_session[session_id] = result
        return result


def _history_of(run_turn: object) -> dict[str, dict[str, Any]]:
    """The history items a fake Runtime wrote, unwrapping `_CapturingTurns`."""
    inner = getattr(run_turn, "_inner", run_turn)
    history: dict[str, dict[str, Any]] = getattr(inner, "history", {})
    return history


def _log_lines_of(run_turn: object) -> list[RuntimeLogLine]:
    """The Runtime log lines a fake Runtime wrote, unwrapping `_CapturingTurns`."""
    inner = getattr(run_turn, "_inner", run_turn)
    lines: list[RuntimeLogLine] = getattr(inner, "log_lines", [])
    return lines


def _run(
    *,
    events: list[CloudTrailEvent],
    run_turn: object,
    history_items: _FakeHistoryItems | None = None,
    runtime_logs: Any = None,
) -> tuple[dict[str, str], list[_FakeProvisioner]]:
    provs: list[_FakeProvisioner] = []
    tokens = iter(["token-A", "token-B"])

    def factory(_ctx: object) -> _FakeProvisioner:
        p = _FakeProvisioner(next(tokens))
        provs.append(p)
        return p

    def lookup(_ctx: object, _s: int, _e: int) -> list[CloudTrailEvent]:
        return events

    # Each tick exceeds the CloudTrail poll window, so the poll deadline is reached in one step.
    ticks = iter(range(1000, 1_000_000, 1000))

    items = history_items if history_items is not None else _FakeHistoryItems(_history_of(run_turn))
    ctx = context_for_test(outputs={"agentcore_runtime_role_arn": _ROLE_ARN}, clients={})
    rows = list(
        checks(
            ctx,
            provisioner_factory=factory,
            run_turn=run_turn,  # type: ignore[arg-type]
            cloudtrail_lookup=lookup,
            clock=lambda: float(next(ticks)),
            sleeper=lambda _s: None,
            history_items=lambda _ctx: items,
            runtime_logs=(
                runtime_logs
                if runtime_logs is not None
                else lambda _c, _s, _e: list(_log_lines_of(run_turn))
            ),
        )
    )
    return {r.check: r.status for r in rows}, provs


_ALL_TIER_EVENTS = [
    _event(_ROLE, _TIERS["supervisor"]),
    _event(_ROLE, _TIERS["subagent"]),
    _event(_ROLE, _TIERS["tool"]),
]


def test_checks_happy_path_all_pass_and_both_users_cleaned_up() -> None:
    by_check, provs = _run(events=_ALL_TIER_EVENTS, run_turn=_StatefulTurns())
    assert by_check["delegation_turn_completed"] == "pass"
    assert by_check["supervisor_tier_attribution_control"] == "pass"
    assert by_check["subagent_tier_attribution"] == "pass"
    assert by_check["tool_tier_attribution"] == "pass"
    assert by_check["owner_session_reuse_control"] == "pass"
    assert by_check["foreign_session_reuse_refused"] == "pass"
    assert by_check["cost_lines_present"] == "pass"
    assert by_check["cost_lines_carry_long_context_calls"] == "pass"
    assert by_check["cost_usd_positive"] == "pass"
    assert by_check["resolved_tiers_match_tuning"] == "pass"
    for tier, model in expected_tier_models(MIXED_FAMILIES).items():
        assert by_check[f"{tier}_cost_line_names_{model}"] == "pass"
    for tier, effort in expected_tier_efforts(MIXED_FAMILIES).items():
        assert by_check[f"{tier}_resolved_effort_is_{effort}"] == "pass"
    attribution_rows = {c: s for c, s in by_check.items() if c.endswith(ATTRIBUTION_ROW_SUFFIX)}
    families = select_attribution_mapping().families
    assert families is not None
    assert len(attribution_rows) == 4 + 2 * len(families)
    assert set(attribution_rows.values()) == {"pass"}
    assert by_check["stale_temperature_tuning_refused"] == "pass"
    assert by_check["model_backed_turns_within_envelope"] == "pass"
    assert by_check[HISTORY_RECALL_CHECK] == "pass"
    assert by_check[HISTORY_OMITTED_RESUME_CHECK] == "pass"
    assert by_check[HISTORY_LOG_CHECK] == "pass"
    assert by_check[HISTORY_ITEM_CHECK] == "pass"
    assert by_check[HISTORY_ITEMS_DELETED_CHECK] == "pass"
    assert by_check["cleaned_up"] == "pass"
    assert len(provs) == 2
    assert all(p.cleaned_up for p in provs)


# --- check 2: relay turn cost under a mixed-family tuning ---


def test_every_model_turn_sends_the_current_schema_and_the_seed_turn_is_mixed_family() -> None:
    turns = _StatefulTurns()
    _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    current = [t for t in turns.tunings if not _carries_temperature(t)]
    assert all(
        t["schema"] == WIRE_SCHEMA_VERSION and set(t["tiers"]) == set(MIXED_FAMILIES)
        for t in current
    )
    assert WIRE_SCHEMA_VERSION == 3
    families = select_attribution_mapping().families
    assert families is not None
    assert current[0] == tuning_block(families)
    assert current[1] == tuning_block(MIXED_FAMILIES)
    assert len(set(MIXED_FAMILIES.values())) == 2, "the tuning must mix both families"


def test_the_mixed_seed_turn_forces_delegation() -> None:
    """Check 2's turn must reach the subagent and tool tiers, so it uses a delegation prompt."""
    turns = _StatefulTurns()
    _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    mixed = tuning_block(MIXED_FAMILIES)
    seed = turns.prompts.index(_MIXED_DELEGATION_PROMPT)
    assert turns.tunings[seed] == mixed
    assert turns.prompts[seed + 1] == "What did I just ask you to do?"
    seed_prompt = turns.prompts[seed]
    assert "Create a new account" in seed_prompt and seed_prompt != _DELEGATION_PROMPT


def test_the_expected_models_are_the_models_yaml_assignments() -> None:
    """Tool(anthropic) and subagent(openai) name `config/models.yaml`'s models."""
    raw = yaml.safe_load(_MODELS_YAML.read_text(encoding="utf-8"))["families"]
    expected = expected_tier_models(MIXED_FAMILIES)
    assert expected == {
        tier: raw[family]["tiers"][tier]["model"] for tier, family in MIXED_FAMILIES.items()
    }
    assert expected["tool"].startswith("anthropic.")
    assert expected["subagent"].startswith("openai.")


def _cost_rows(frame: Mapping[str, Any] | None) -> dict[str, str]:
    result = TurnResult(status=200, kinds=("completed",), answer="", completed=frame)
    return {
        r.check: r.status
        for r in evaluate_turn_cost(
            result,
            MIXED_FAMILIES,
            expected_tier_models(MIXED_FAMILIES),
            expected_tier_efforts(MIXED_FAMILIES),
        )
    }


_MODEL_ROWS = [
    f"{tier}_cost_line_names_{model}"
    for tier, model in expected_tier_models(MIXED_FAMILIES).items()
]
_EFFORT_ROWS = [
    f"{tier}_resolved_effort_is_{effort}"
    for tier, effort in expected_tier_efforts(MIXED_FAMILIES).items()
]


def test_the_expected_efforts_are_the_models_yaml_tier_efforts() -> None:
    """Each tier's expected effort is `config/models.yaml`'s; Haiku 5.5 runs low."""
    raw = yaml.safe_load(_MODELS_YAML.read_text(encoding="utf-8"))["families"]
    expected = expected_tier_efforts(MIXED_FAMILIES)
    assert expected == {
        tier: raw[family]["tiers"][tier]["tuning"]["effort"]
        for tier, family in MIXED_FAMILIES.items()
    }
    assert MIXED_FAMILIES["tool"] == "anthropic"
    assert expected["tool"] == "low"


@pytest.mark.parametrize("tier", ["supervisor", "subagent", "tool"])
def test_a_tier_resolved_at_its_models_default_reds_only_its_effort_row(tier: str) -> None:
    """The older resolution (each model's own default) is caught per tier where it differs."""
    matrix = load_capability_matrix()
    model = expected_tier_models(MIXED_FAMILIES)[tier]
    default = matrix.models[model].effort.default
    frame = _priced_frame(tuning_block(MIXED_FAMILIES))
    frame["resolved_tiers"] = [
        {**entry, "effort": default} if entry["tier"] == tier else entry
        for entry in frame["resolved_tiers"]
    ]
    failed = [check for check, status in _cost_rows(frame).items() if status == "fail"]
    configured = expected_tier_efforts(MIXED_FAMILIES)[tier]
    assert failed == ([] if default == configured else [f"{tier}_resolved_effort_is_{configured}"])


def test_the_tool_tier_default_differs_from_its_configured_effort() -> None:
    """Control: the tool cell above is discriminating (Haiku 5.5 default medium vs tier low)."""
    model = expected_tier_models(MIXED_FAMILIES)["tool"]
    assert (
        load_capability_matrix().models[model].effort.default
        != (expected_tier_efforts(MIXED_FAMILIES)["tool"])
    )


def test_a_tier_resolved_on_another_model_reds_its_effort_row() -> None:
    frame = _priced_frame(tuning_block(MIXED_FAMILIES))
    frame["resolved_tiers"] = [
        {**entry, "model_id": "anthropic.claude-stale-0"} if entry["tier"] == "tool" else entry
        for entry in frame["resolved_tiers"]
    ]
    failed = [check for check, status in _cost_rows(frame).items() if status == "fail"]
    assert failed == [r for r in _EFFORT_ROWS if r.startswith("tool_")]


def test_a_priced_mixed_frame_passes_every_cost_row() -> None:
    rows = _cost_rows(_priced_frame(tuning_block(MIXED_FAMILIES)))
    assert set(_MODEL_ROWS) <= set(rows)
    assert set(rows.values()) == {"pass"}


@pytest.mark.parametrize(
    ("override", "red"),
    [
        (
            {"cost_lines": []},
            ["cost_lines_present", "cost_lines_carry_long_context_calls", *_MODEL_ROWS],
        ),
        (
            {"cost_lines": None},
            ["cost_lines_present", "cost_lines_carry_long_context_calls", *_MODEL_ROWS],
        ),
        ({"cost_usd": 0}, ["cost_usd_positive"]),
        ({"cost_usd": None, "cost_withheld": True}, ["cost_usd_positive"]),
        ({"cost_withheld": True}, ["cost_usd_positive"]),
        ({"cost_usd": True}, ["cost_usd_positive"]),
        ({"resolved_tiers": []}, ["resolved_tiers_match_tuning", *_EFFORT_ROWS]),
    ],
    ids=["empty-lines", "no-lines", "zero-cost", "withheld-none", "withheld", "bool", "no-tiers"],
)
def test_each_missing_cost_field_reds_its_own_rows(
    override: dict[str, Any], red: list[str]
) -> None:
    rows = _cost_rows({**_priced_frame(tuning_block(MIXED_FAMILIES)), **override})
    assert [check for check, status in rows.items() if status == "fail"] == red


@pytest.mark.parametrize("tier", ["subagent", "tool"])
def test_a_tier_priced_on_another_model_reds_only_its_model_row(tier: str) -> None:
    frame = _priced_frame(tuning_block(MIXED_FAMILIES))
    stale = "anthropic.claude-stale-0"
    frame["cost_lines"] = [
        {**line, "model_id": stale} if line["tier"] == tier else line
        for line in frame["cost_lines"]
    ]
    model = expected_tier_models(MIXED_FAMILIES)[tier]
    failed = [check for check, status in _cost_rows(frame).items() if status == "fail"]
    assert failed == [f"{tier}_cost_line_names_{model}"]


@pytest.mark.parametrize("value", [None, -1, True, "2", 1.0])
def test_a_line_without_a_valid_long_context_count_reds_only_that_row(value: Any) -> None:
    frame = _priced_frame(tuning_block(MIXED_FAMILIES))
    frame["cost_lines"] = [
        {**line, "long_context_calls": value} if line["tier"] == "tool" else line
        for line in frame["cost_lines"]
    ]
    failed = [check for check, status in _cost_rows(frame).items() if status == "fail"]
    assert failed == ["cost_lines_carry_long_context_calls"]


def test_a_line_missing_the_long_context_key_reds_only_that_row() -> None:
    frame = _priced_frame(tuning_block(MIXED_FAMILIES))
    frame["cost_lines"] = [
        {k: v for k, v in line.items() if k != "long_context_calls"} for line in frame["cost_lines"]
    ]
    failed = [check for check, status in _cost_rows(frame).items() if status == "fail"]
    assert failed == ["cost_lines_carry_long_context_calls"]


def test_a_positive_long_context_count_passes() -> None:
    frame = _priced_frame(tuning_block(MIXED_FAMILIES))
    frame["cost_lines"] = [{**line, "long_context_calls": 3} for line in frame["cost_lines"]]
    assert _cost_rows(frame)["cost_lines_carry_long_context_calls"] == "pass"


def test_an_unpriced_line_reds_its_model_row() -> None:
    frame = _priced_frame(tuning_block(MIXED_FAMILIES))
    frame["cost_lines"] = [
        {**line, "cost_usd": None} if line["tier"] == "tool" else line
        for line in frame["cost_lines"]
    ]
    model = expected_tier_models(MIXED_FAMILIES)["tool"]
    assert _cost_rows(frame)[f"tool_cost_line_names_{model}"] == "fail"


# --- check 3: a stale temperature tuning is refused with no model call ---


def test_the_refusal_kind_is_the_runtimes_validation_kind() -> None:
    """The smoke asserts the code `agents.runtime_server` emits for a rejected tuning."""
    assert REFUSAL_ERROR_KIND == _VALIDATION_ERROR_KIND


def test_the_stale_block_is_schema_2_with_a_temperature_on_the_refusal_tier() -> None:
    block = stale_temperature_block()
    assert block["schema"] == 2 != WIRE_SCHEMA_VERSION
    assert [tier for tier, req in block["tiers"].items() if "temperature" in req] == [_REFUSAL_TIER]


def test_the_runtime_refusal_passes_the_row() -> None:
    assert evaluate_temperature_refusal(_refusal()).status == "pass"


@pytest.mark.parametrize(
    "result",
    [
        TurnResult(
            status=200,
            kinds=("error", "usage"),
            answer="",
            error={"error_kind": REFUSAL_ERROR_KIND, "error_message": _PATH},
        ),
        TurnResult(
            status=200,
            kinds=("error",),
            answer="",
            error={"error_kind": "model_failure", "error_message": _PATH},
        ),
        _refusal("tuning must declare schema 3"),
        _refusal("temperature: remove it from the supervisor tier."),
        _refusal("tuning.tiers.tool.temperature: temperature is no longer a tuning parameter."),
        TurnResult(status=200, kinds=("text_delta", "completed"), answer="ready"),
        TurnResult(status=400, kinds=(), answer="", message=_GENERIC_VALIDATION_MESSAGE),
    ],
    ids=[
        "usage-frame",
        "wrong-kind",
        "schema-only",
        "no-path",
        "other-tier",
        "answered",
        "http-400",
    ],
)
def test_anything_but_the_refusal_reds_the_row(result: TurnResult) -> None:
    assert evaluate_temperature_refusal(result).status == "fail"


def test_a_runtime_that_honours_temperature_reds_the_refusal_end_to_end() -> None:
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=_StatefulTurns(honours_temperature=True))
    assert by_check["stale_temperature_tuning_refused"] == "fail"


def test_a_turn_resolved_on_one_family_reds_the_tier_row() -> None:
    frame = _priced_frame(tuning_block(dict.fromkeys(MIXED_FAMILIES, "anthropic")))
    assert _cost_rows(frame)["resolved_tiers_match_tuning"] == "fail"


def test_no_completed_frame_fails_every_cost_row() -> None:
    assert set(_cost_rows(None).values()) == {"fail"}


# --- model-backed turns are counted and capped ---


# --- check 5: an openai-supervisor turn proves that family's configured efforts ---


def _openai_rows(frame: Mapping[str, Any] | None) -> dict[str, CheckResult]:
    result = TurnResult(status=200, kinds=("completed",), answer="", completed=frame)
    return {
        r.check: r
        for r in evaluate_turn_cost(
            result,
            OPENAI_FAMILIES,
            expected_tier_models(OPENAI_FAMILIES),
            expected_tier_efforts(OPENAI_FAMILIES),
            row_suffix=OPENAI_ROW_SUFFIX,
            turn_label="openai-supervisor",
        )
    }


_OPENAI_SUPERVISOR_ROW = f"supervisor_resolved_effort_is_medium{OPENAI_ROW_SUFFIX}"


def test_the_openai_turn_runs_an_openai_supervisor_at_the_configured_medium_effort() -> None:
    assert OPENAI_FAMILIES["supervisor"] == "openai"
    assert set(OPENAI_FAMILIES) == set(MIXED_FAMILIES)
    raw = yaml.safe_load(_MODELS_YAML.read_text(encoding="utf-8"))["families"]
    assert expected_tier_efforts(OPENAI_FAMILIES) == {
        tier: raw["openai"]["tiers"][tier]["tuning"]["effort"] for tier in OPENAI_FAMILIES
    }
    assert expected_tier_efforts(OPENAI_FAMILIES)["supervisor"] == "medium"
    assert expected_tier_efforts(MIXED_FAMILIES)["supervisor"] == "medium"


def test_the_happy_path_carries_both_supervisor_effort_rows_and_the_openai_turn() -> None:
    turns = _StatefulTurns()
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    assert by_check["supervisor_resolved_effort_is_medium"] == "pass"
    openai_rows = {c: s for c, s in by_check.items() if c.endswith(OPENAI_ROW_SUFFIX)}
    assert set(openai_rows) == set(_openai_rows(_priced_frame(tuning_block(OPENAI_FAMILIES))))
    assert _OPENAI_SUPERVISOR_ROW in openai_rows
    assert set(openai_rows.values()) == {"pass"}
    openai_turn = turns.prompts.index(_OPENAI_DELEGATION_PROMPT)
    assert turns.tunings[openai_turn] == tuning_block(OPENAI_FAMILIES)


def test_the_openai_prompt_forces_delegation_like_the_mixed_one() -> None:
    assert _DELEGATION_FORCING_TOOL in {tool.name for tool in tool_registry_for("mutation")}
    assert _DELEGATION_FORCING_TOOL not in DIRECT_TOOL_NAMES
    assert "Create a new account" in _OPENAI_DELEGATION_PROMPT
    assert _OPENAI_DELEGATION_PROMPT not in (_DELEGATION_PROMPT, _MIXED_DELEGATION_PROMPT)


def test_an_openai_supervisor_resolved_at_low_reds_its_effort_row() -> None:
    frame = _priced_frame(tuning_block(OPENAI_FAMILIES))
    frame["resolved_tiers"] = [
        {**entry, "effort": "low"} if entry["tier"] == "supervisor" else entry
        for entry in frame["resolved_tiers"]
    ]
    rows = _openai_rows(frame)
    failed = [c for c, r in rows.items() if r.status == "fail"]
    assert failed == [_OPENAI_SUPERVISOR_ROW]
    assert "'low'" in rows[_OPENAI_SUPERVISOR_ROW].detail


def test_an_anthropic_supervisor_resolved_at_low_reds_its_effort_row() -> None:
    frame = _priced_frame(tuning_block(MIXED_FAMILIES))
    frame["resolved_tiers"] = [
        {**entry, "effort": "low"} if entry["tier"] == "supervisor" else entry
        for entry in frame["resolved_tiers"]
    ]
    failed = [c for c, s in _cost_rows(frame).items() if s == "fail"]
    assert failed == ["supervisor_resolved_effort_is_medium"]


def test_an_absent_openai_supervisor_effort_fails_rather_than_skips() -> None:
    frame = _priced_frame(tuning_block(OPENAI_FAMILIES))
    frame["resolved_tiers"] = [
        {k: v for k, v in entry.items() if k != "effort"}
        if entry["tier"] == "supervisor"
        else entry
        for entry in frame["resolved_tiers"]
    ]
    assert _openai_rows(frame)[_OPENAI_SUPERVISOR_ROW].status == "fail"
    no_resolved = {**_priced_frame(tuning_block(OPENAI_FAMILIES)), "resolved_tiers": None}
    assert _openai_rows(no_resolved)[_OPENAI_SUPERVISOR_ROW].status == "fail"


def test_no_completed_openai_frame_fails_every_openai_row_and_names_the_turn() -> None:
    rows = _openai_rows(None)
    assert _OPENAI_SUPERVISOR_ROW in rows
    assert {r.status for r in rows.values()} == {"fail"}
    assert all(c.endswith(OPENAI_ROW_SUFFIX) for c in rows)
    assert all("openai-supervisor turn" in r.detail for r in rows.values())


def test_a_spent_budget_refuses_the_openai_turn_after_check_4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allotments = dict(MODEL_TURN_ALLOTMENTS)
    allotments[RESOURCE_CLASS] -= CACHE_HISTORY_TURNS + HISTORY_RECALL_TURNS + 2
    monkeypatch.setattr(three_tier_chat, "MODEL_TURN_ALLOTMENTS", allotments)
    turns = _StatefulTurns()
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    assert by_check["foreign_session_reuse_refused"] == "pass"
    assert not any(c.endswith(OPENAI_ROW_SUFFIX) for c in by_check)
    assert tuning_block(OPENAI_FAMILIES) not in turns.tunings


def test_the_allotments_fit_the_envelope() -> None:
    assert sum(MODEL_TURN_ALLOTMENTS.values()) <= MODEL_TURN_ENVELOPE
    assert MODEL_TURN_ENVELOPE == 20


def test_the_module_spends_exactly_its_allotment_and_prints_the_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    turns = _StatefulTurns()
    _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    # B's refused turn, the temperature refusal and the two route refusals never reach a model.
    model_backed = len(turns.tunings) - 4
    assert model_backed == MODEL_TURN_ALLOTMENTS[RESOURCE_CLASS]
    printed = capsys.readouterr().err
    assert f"model-backed turn {model_backed}/{model_backed}" in printed


def test_a_spent_budget_refuses_the_next_turn() -> None:
    budget = ModelTurnBudget(RESOURCE_CLASS)
    for index in range(budget.allotment):
        assert budget.spend(f"turn {index}")
    assert not budget.spend("one too many")
    assert len(budget.used) == budget.allotment


class _AttributionTurnAtModelDefault(_StatefulTurns):
    """Resolves check 1's subagent tier at its model's `effort.default` instead of the tier's."""

    def __call__(self, *args: Any, **kwargs: Any) -> TurnResult:
        result = super().__call__(*args, **kwargs)
        if args[3] != _DELEGATION_PROMPT or result.completed is None:
            return result
        frame = dict(result.completed)
        frame["resolved_tiers"] = [
            {**entry, "effort": load_capability_matrix().models[entry["model_id"]].effort.default}
            if entry["tier"] == "subagent"
            else entry
            for entry in frame["resolved_tiers"]
        ]
        return TurnResult(status=200, kinds=result.kinds, answer=result.answer, completed=frame)


def test_a_check_1_subagent_off_its_tier_effort_reds_only_its_attribution_row() -> None:
    families = select_attribution_mapping().families
    assert families is not None
    effort = expected_tier_efforts(families)["subagent"]
    model = expected_tier_models(families)["subagent"]
    assert load_capability_matrix().models[model].effort.default != effort
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=_AttributionTurnAtModelDefault())
    failed = [c for c, s in by_check.items() if s == "fail"]
    assert failed == [f"subagent_resolved_effort_is_{effort}{ATTRIBUTION_ROW_SUFFIX}"]


def test_checks_foreign_not_refused_reds_the_denial_end_to_end() -> None:
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=_StatefulTurns(bad_collision=True))
    assert by_check["owner_session_reuse_control"] == "pass"
    assert by_check["foreign_session_reuse_refused"] == "fail"


def test_checks_cleans_up_both_users_even_when_attribution_fails() -> None:
    by_check, provs = _run(events=[], run_turn=_StatefulTurns())
    assert by_check["supervisor_tier_attribution_control"] == "fail"
    # cleanup still runs for every provisioned user
    assert by_check["cleaned_up"] == "pass"
    assert all(p.cleaned_up for p in provs)


# --- check 1 bounded CloudTrail poll: stops only on a complete snapshot ---


class _FakeClock:
    """A clock that advances only when the injected sleeper is called."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


_SUP = _event(_ROLE, _TIERS["supervisor"])
_SUB = _event(_ROLE, _TIERS["subagent"])
_TOOL = _event(_ROLE, _TIERS["tool"])


def _run_poll(
    snapshots: list[list[CloudTrailEvent]],
) -> tuple[dict[str, CheckResult], int, _FakeClock]:
    """Run `checks()` with a lookup returning ``snapshots`` in turn (the last one repeating)."""
    lookups = 0
    tokens = iter(["token-A", "token-B"])

    def lookup(_ctx: object, _s: int, _e: int) -> list[CloudTrailEvent]:
        nonlocal lookups
        snapshot = snapshots[min(lookups, len(snapshots) - 1)]
        lookups += 1
        return list(snapshot)

    clock = _FakeClock()
    ctx = context_for_test(outputs={"agentcore_runtime_role_arn": _ROLE_ARN}, clients={})
    rows = list(
        checks(
            ctx,
            provisioner_factory=lambda _ctx: _FakeProvisioner(next(tokens)),
            run_turn=_StatefulTurns(),
            cloudtrail_lookup=lookup,
            clock=clock,
            sleeper=clock.sleep,
        )
    )
    return {r.check: r for r in rows}, lookups, clock


def test_poll_continues_past_a_partial_snapshot_until_every_tier_is_attributed() -> None:
    by_check, lookups, clock = _run_poll([[_SUP], [_SUP, _SUB], [_SUP, _SUB, _TOOL]])

    assert lookups == 3
    assert clock.sleeps == [_CLOUDTRAIL_POLL_INTERVAL, _CLOUDTRAIL_POLL_INTERVAL]
    for check in (
        "supervisor_tier_attribution_control",
        "subagent_tier_attribution",
        "tool_tier_attribution",
    ):
        assert by_check[check].status == "pass", by_check[check].detail
    assert "polls=3" in by_check["tool_tier_attribution"].detail


def test_an_empty_first_snapshot_does_not_end_the_poll() -> None:
    by_check, lookups, _ = _run_poll([[], [_SUP, _SUB, _TOOL]])

    assert lookups == 2
    assert by_check["tool_tier_attribution"].status == "pass"


def test_a_late_tool_tier_event_is_found() -> None:
    """The tool tier arrives last, after several partial snapshots, and is still attributed."""
    partial = [[_SUB], [_SUP, _SUB]] + [[_SUP, _SUB]] * 10
    by_check, lookups, clock = _run_poll([*partial, [_SUP, _SUB, _TOOL]])

    assert lookups == len(partial) + 1
    assert clock.now - 1_000.0 < _CLOUDTRAIL_POLL_SECONDS
    assert by_check["tool_tier_attribution"].status == "pass"


def test_a_snapshot_without_the_supervisor_is_returned_only_after_the_bound() -> None:
    by_check, lookups, clock = _run_poll([[_SUB]])

    expected_sleeps = int(_CLOUDTRAIL_POLL_SECONDS / _CLOUDTRAIL_POLL_INTERVAL)
    assert len(clock.sleeps) == expected_sleeps
    assert lookups == expected_sleeps + 1
    assert clock.now - 1_000.0 >= _CLOUDTRAIL_POLL_SECONDS
    control = by_check["supervisor_tier_attribution_control"]
    assert control.status == "fail"
    assert f"polls={lookups}" in control.detail and "events=1" in control.detail
    assert "subagent_tier_attribution" not in by_check
    assert "tool_tier_attribution" not in by_check


def test_a_genuine_tool_tier_absence_still_fails_after_the_bound() -> None:
    by_check, _, clock = _run_poll([[_SUP, _SUB]])

    assert clock.now - 1_000.0 >= _CLOUDTRAIL_POLL_SECONDS
    assert by_check["supervisor_tier_attribution_control"].status == "pass"
    assert by_check["subagent_tier_attribution"].status == "pass"
    tool = by_check["tool_tier_attribution"]
    assert tool.status == "fail"
    assert _TIERS["subagent"] in tool.detail, "the detail lists the (role, model_id) pairs seen"


def test_a_tier_from_another_role_does_not_end_the_poll() -> None:
    foreign_tool = _event("some-other-role", _TIERS["tool"])
    by_check, _, clock = _run_poll([[_SUP, _SUB, foreign_tool]])

    assert clock.now - 1_000.0 >= _CLOUDTRAIL_POLL_SECONDS
    assert by_check["tool_tier_attribution"].status == "fail"


@pytest.mark.parametrize(
    "tier_profiles", [{**_TIERS, "tool": ""}, {k: v for k, v in _TIERS.items() if k != "tool"}]
)
def test_an_empty_or_absent_tier_profile_never_attributes_an_event(
    tier_profiles: dict[str, str],
) -> None:
    """An empty profile is a substring of every modelId, so it must match nothing."""
    events = [_SUP, _SUB, _TOOL, _event(_ROLE, "anything-at-all")]

    assert not _tier_attributed(events, "tool", runtime_role=_ROLE, tier_profiles=tier_profiles)
    rows = {
        r.check: r.status
        for r in evaluate_three_tier_attribution(
            events, runtime_role=_ROLE, tier_profiles=tier_profiles
        )
    }
    assert rows["supervisor_tier_attribution_control"] == "pass"
    assert rows["tool_tier_attribution"] == "fail"


def test_poll_bound_covers_documented_cloudtrail_delivery_latency() -> None:
    """CloudTrail's typical delivery latency is about 5 minutes."""
    assert _CLOUDTRAIL_POLL_SECONDS >= 300
    assert _CLOUDTRAIL_POLL_INTERVAL < _CLOUDTRAIL_POLL_SECONDS


# --- session-id length (InvokeAgentRuntime requires 33..256 chars) ---

#: Closed range of session-id lengths InvokeAgentRuntime accepts.
_MIN_SESSION_ID_LEN = 33
_MAX_SESSION_ID_LEN = 256


class _CapturingTurns:
    """Wraps `_StatefulTurns`, recording every session id `checks()` constructs."""

    def __init__(self) -> None:
        self._inner = _StatefulTurns()
        self.session_ids: list[str] = []

    def __call__(
        self,
        ctx: object,
        token: str,
        session_id: str,
        prompt: str,
        tuning: Mapping[str, Any],
        **kwargs: Any,
    ) -> TurnResult:
        self.session_ids.append(session_id)
        return self._inner(ctx, token, session_id, prompt, tuning, **kwargs)


def test_every_constructed_session_id_meets_the_invoke_agent_runtime_length_rule() -> None:
    """Every session id built inside `checks()` is within 33..256 chars."""
    capturing = _CapturingTurns()
    _run(events=_ALL_TIER_EVENTS, run_turn=capturing)
    assert capturing.session_ids  # the run actually issued turns
    for session_id in capturing.session_ids:
        assert _MIN_SESSION_ID_LEN <= len(session_id) <= _MAX_SESSION_ID_LEN, session_id


# --- check 1 delegation-forcing invariant ---


def test_delegation_prompt_forcing_tool_is_outside_the_supervisors_direct_tools() -> None:
    """The forcing tool is a mutation-subagent tool and not one of the supervisor's direct tools."""
    assert _DELEGATION_FORCING_TOOL not in DIRECT_TOOL_NAMES
    mutation_tools = {tool.name for tool in tool_registry_for("mutation")}
    assert _DELEGATION_FORCING_TOOL in mutation_tools


def test_delegation_prompt_commands_the_forcing_tools_operation() -> None:
    """The delegation prompt asks to create an account, matching the forcing tool."""
    assert "create" in _DELEGATION_PROMPT.lower()
    assert "account" in _DELEGATION_PROMPT.lower()


# --- check 1 CloudTrail event filter: the vendor-SDK transport's event names only ---


class _FakeCloudTrail:
    """Serves recorded CloudTrail events in pages per event name, linked by ``NextToken``.

    ``pages[name]`` is a list of pages, each a list of model ids; ``endless`` returns a
    ``NextToken`` on every call. ``calls`` records ``(event name, NextToken sent)`` per call.
    """

    def __init__(self, pages: dict[str, list[list[str]]], *, endless: bool = False) -> None:
        self._pages = pages
        self._endless = endless
        self.calls: list[tuple[str, str | None]] = []

    def lookup_events(self, **kwargs: Any) -> dict[str, Any]:
        (attribute,) = kwargs["LookupAttributes"]
        assert attribute["AttributeKey"] == "EventName"
        name = attribute["AttributeValue"]
        token = kwargs.get("NextToken")
        self.calls.append((name, token))
        index = int(token.removeprefix("page-")) if token else 0
        pages = self._pages.get(name, [[]])
        profiles = pages[index] if index < len(pages) else []
        response: dict[str, Any] = {
            "Events": [
                {"EventName": name, "CloudTrailEvent": json.dumps(_trail_detail(profile))}
                for profile in profiles
            ]
        }
        if self._endless or index + 1 < len(pages):
            response["NextToken"] = f"page-{index + 1}"
        return response


def _trail_detail(model_id: str) -> dict[str, Any]:
    return {
        "userIdentity": {"sessionContext": {"sessionIssuer": {"userName": _ROLE}}},
        "requestParameters": {"modelId": model_id},
    }


def _lookup(client: _FakeCloudTrail) -> list[CloudTrailEvent]:
    return _live_cloudtrail_lookup(context_for_test(clients={"cloudtrail": client}), 0, 1000)


def test_lookup_queries_exactly_the_anthropic_stream_and_openai_responses_events() -> None:
    """The lookup queries only `InvokeModelWithResponseStream` and `Responses` events."""
    client = _FakeCloudTrail({"InvokeModelWithResponseStream": [[_TIERS["supervisor"]]]})
    events = _lookup(client)
    assert sorted(name for name, _ in client.calls) == [
        "InvokeModelWithResponseStream",
        "Responses",
    ]
    assert events == [CloudTrailEvent(role_name=_ROLE, model_id=_TIERS["supervisor"])]


def test_lookup_follows_next_token_and_reads_every_page() -> None:
    """A tier recorded on the second page is still returned."""
    client = _FakeCloudTrail(
        {"InvokeModelWithResponseStream": [[_TIERS["supervisor"]], [_TIERS["subagent"]]]}
    )
    events = _lookup(client)
    assert ("InvokeModelWithResponseStream", "page-1") in client.calls
    assert [e.model_id for e in events] == [_TIERS["supervisor"], _TIERS["subagent"]]


def test_lookup_raises_rather_than_truncating_at_the_page_cap() -> None:
    client = _FakeCloudTrail({}, endless=True)
    with pytest.raises(RuntimeError, match="not read in full"):
        _lookup(client)
    assert len(client.calls) == _CLOUDTRAIL_MAX_PAGES


# --- check 6: prompt-cache read on the wire ---

_TIER_NAMES = ("supervisor", "subagent", "tool")
_ANTHROPIC_SUPERVISOR = expected_tier_models(MIXED_FAMILIES)["supervisor"]
_OPENAI_SUPERVISOR = expected_tier_models(OPENAI_FAMILIES)["supervisor"]


def _cache_turn(read: object, write: object, *, model: str = _ANTHROPIC_SUPERVISOR) -> TurnResult:
    """A completed turn whose only supervisor cost line is on `model` with the given counts."""
    line = {
        "tier": "supervisor",
        "role": "supervisor",
        "model_id": model,
        "cost_usd": 0.01,
        "cache_read_tokens": read,
        "cache_write_tokens": write,
    }
    return TurnResult(status=200, kinds=("completed",), answer="", completed={"cost_lines": [line]})


def _cache_row(first: TurnResult, second: TurnResult) -> CheckResult:
    return evaluate_cache_read(first, second, family="anthropic", model=_ANTHROPIC_SUPERVISOR)


def test_a_second_turn_cache_read_passes_the_row() -> None:
    row = _cache_row(_cache_turn(0, 2048), _cache_turn(2048, 0))
    assert row.status == "pass"
    assert row.check == "supervisor_prompt_cache_read_on_second_turn:anthropic"


def test_no_cache_token_on_either_turn_defers_and_never_passes() -> None:
    row = _cache_row(_cache_turn(0, 0), _cache_turn(0, 0))
    assert row.status == "defer"
    assert row.defer_reason == CACHE_UNREPORTED_REASON


@pytest.mark.parametrize(
    ("first", "second"),
    [((0, 2048), (0, 0)), ((0, 0), (0, 2048)), ((512, 0), (0, 0))],
    ids=["first_wrote", "second_wrote", "first_read"],
)
def test_reported_cache_activity_with_no_second_read_fails(
    first: tuple[int, int], second: tuple[int, int]
) -> None:
    assert _cache_row(_cache_turn(*first), _cache_turn(*second)).status == "fail"


@pytest.mark.parametrize(
    "second",
    [
        TurnResult(status=502, kinds=(), answer=""),
        _cache_turn(None, 0),
        _cache_turn(True, 0),
        _cache_turn("2048", 0),
        _cache_turn(2048, 0, model="anthropic.some-other-model"),
    ],
    ids=["no_frame", "null_read", "bool_read", "string_read", "other_model"],
)
def test_an_unreadable_supervisor_line_fails_rather_than_defers(second: TurnResult) -> None:
    assert _cache_row(_cache_turn(0, 2048), second).status == "fail"


def test_a_non_supervisor_cache_read_does_not_satisfy_the_row() -> None:
    second = _cache_turn(0, 0)
    assert second.completed is not None
    lines = [*second.completed["cost_lines"]]
    lines.append({**lines[0], "tier": "subagent", "cache_read_tokens": 4096})
    second = TurnResult(
        status=200, kinds=("completed",), answer="", completed={"cost_lines": lines}
    )
    assert _cache_row(_cache_turn(0, 2048), second).status == "fail"


class _CacheTurns(_StatefulTurns):
    """`_StatefulTurns` whose every completed frame reports the given supervisor cache counts."""

    def __init__(self, read: int, write: int) -> None:
        super().__init__()
        self._counts = {"cache_read_tokens": read, "cache_write_tokens": write}

    def __call__(self, *args: Any, **kwargs: Any) -> TurnResult:
        result = super().__call__(*args, **kwargs)
        if result.completed is None:
            return result
        lines = [{**line, **self._counts} for line in result.completed["cost_lines"]]
        return TurnResult(
            status=result.status,
            kinds=result.kinds,
            answer=result.answer,
            completed={**result.completed, "cost_lines": lines},
        )


_CACHE_ROWS = (
    "supervisor_prompt_cache_read_on_second_turn:anthropic",
    "supervisor_prompt_cache_read_on_second_turn:openai",
)


def test_the_happy_path_passes_both_cache_rows() -> None:
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=_StatefulTurns())
    assert [by_check[row] for row in _CACHE_ROWS] == ["pass", "pass"]


def test_a_runtime_reporting_no_cache_tokens_defers_both_rows_end_to_end() -> None:
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=_CacheTurns(0, 0))
    assert [by_check[row] for row in _CACHE_ROWS] == ["defer", "defer"]


def test_a_runtime_that_writes_but_never_reads_fails_both_rows_end_to_end() -> None:
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=_CacheTurns(0, 2048))
    assert [by_check[row] for row in _CACHE_ROWS] == ["fail", "fail"]


def test_the_openai_cache_probe_is_an_openai_supervisor_turn_after_check_5() -> None:
    turns = _StatefulTurns()
    _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    probe = turns.prompts.index(_OPENAI_CACHE_PROBE_PROMPT)
    assert turns.prompts.index(_OPENAI_DELEGATION_PROMPT) == probe - 1
    assert turns.tunings[probe] == tuning_block(OPENAI_FAMILIES)
    assert turns.page_contexts[probe] == page_context_for(_CONTROL_ROUTE)


def test_a_spent_budget_refuses_the_cache_probe_and_its_page_context_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allotments = dict(MODEL_TURN_ALLOTMENTS)
    allotments[RESOURCE_CLASS] -= CACHE_HISTORY_TURNS + HISTORY_RECALL_TURNS + 1
    monkeypatch.setattr(three_tier_chat, "MODEL_TURN_ALLOTMENTS", allotments)
    turns = _StatefulTurns()
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    assert by_check[_OPENAI_SUPERVISOR_ROW] == "pass"
    assert _CACHE_ROWS[1] not in by_check
    assert not any(c.startswith("page_context_") for c in by_check)
    assert _OPENAI_CACHE_PROBE_PROMPT not in turns.prompts


# --- check 7: page-context route refusal ---

_ROUTE_ROWS = ("page_context_over_bound_route_refused", "page_context_off_charset_route_refused")


def test_the_refused_routes_fail_the_servers_validator_and_the_control_passes_it() -> None:
    """Contract with `crm.toolspec.page_context`: each planted route is rejected as a
    page-context failure (the Runtime's 400 path), with no echo; the control route parses."""
    from crm.errors import ValidationFailed
    from crm.toolspec.page_context import ROUTE_MAX_LENGTH, parse_page_context

    marker = "zqplantmarker0001"
    routes = refused_routes(marker)
    assert set(routes) == set(_ROUTE_ROWS)
    assert len(routes[_ROUTE_ROWS[0]]) > ROUTE_MAX_LENGTH
    assert len(routes[_ROUTE_ROWS[1]]) <= ROUTE_MAX_LENGTH
    parse_page_context(page_context_for(_CONTROL_ROUTE))
    for route in routes.values():
        assert marker in route
        with pytest.raises(ValidationFailed) as caught:
            parse_page_context(page_context_for(route))
        assert (caught.value.source or "").startswith("page_context")
        assert marker not in json.dumps(caught.value.to_api_response())


def _route_row(result: TurnResult, *, control: TurnResult | None = None) -> CheckResult:
    return evaluate_page_context_refusal(
        _ROUTE_ROWS[0], result, marker="zqplantx", control=control or _completed("ready")
    )


def test_a_generic_400_without_the_marker_passes_the_row() -> None:
    assert _route_row(_route_refusal("/accounts/zqplantx")).status == "pass"


@pytest.mark.parametrize(
    "result",
    [
        _route_refusal("/accounts/zqplantx", echo=True),
        TurnResult(
            status=400,
            kinds=(),
            answer="",
            message=f"{_GENERIC_VALIDATION_MESSAGE} /accounts/zqplantx",
            raw_body="{}",
        ),
        TurnResult(status=400, kinds=(), answer="", message="Bad request", raw_body="{}"),
        TurnResult(status=403, kinds=(), answer="", message=_GENERIC_VALIDATION_MESSAGE),
        _completed("ready"),
    ],
    ids=["echoed_in_body", "echoed_in_message", "other_message", "status_403", "accepted"],
)
def test_anything_but_a_generic_400_without_echo_fails_the_row(result: TurnResult) -> None:
    assert _route_row(result).status == "fail"


def test_a_failed_control_fails_the_row_even_on_a_good_refusal() -> None:
    control = TurnResult(status=400, kinds=(), answer="", message=_GENERIC_VALIDATION_MESSAGE)
    row = _route_row(_route_refusal("/accounts/zqplantx"), control=control)
    assert row.status == "fail"
    assert "control did not complete" in row.detail


def test_the_happy_path_passes_both_route_rows_and_sends_the_planted_routes() -> None:
    turns = _StatefulTurns()
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    assert [by_check[row] for row in _ROUTE_ROWS] == ["pass", "pass"]
    planted = [pc for pc in turns.page_contexts if pc and pc["route"] != _CONTROL_ROUTE]
    assert len(planted) == 2


@pytest.mark.parametrize(
    "turns",
    [_StatefulTurns(accepts_bad_route=True), _StatefulTurns(echoes_route=True)],
    ids=["route_accepted", "route_echoed"],
)
def test_a_runtime_that_accepts_or_echoes_the_route_fails_both_rows_end_to_end(
    turns: _StatefulTurns,
) -> None:
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    assert [by_check[row] for row in _ROUTE_ROWS] == ["fail", "fail"]


# --- check 8: prompt-cache reads cover session history ---


def _history_turn(
    read: object, write: object = 0, *, kinds: tuple[str, ...] = ("text_delta", "completed")
) -> TurnResult:
    """A recorded-shape tool-free turn: the one supervisor cost line a single-call turn carries."""
    lines = [
        {
            "tier": "supervisor",
            "role": "supervisor",
            "model_id": _ANTHROPIC_SUPERVISOR,
            "cost_usd": 0.01,
            "cache_read_tokens": read,
            "cache_write_tokens": write,
            "long_context_calls": 0,
        }
    ]
    return TurnResult(
        status=200,
        kinds=kinds,
        answer="Ready.",
        completed={"kind": "completed", "cost_lines": lines, "cost_usd": 0.01},
    )


def _history_row(fresh: TurnResult, seed: TurnResult, followup: TurnResult) -> CheckResult:
    return evaluate_cache_history(fresh, seed, followup, model=_ANTHROPIC_SUPERVISOR)


def test_a_follow_up_reading_beyond_the_fresh_prefix_passes() -> None:
    row = _history_row(_history_turn(4400), _history_turn(4400, 300), _history_turn(4700))
    assert (row.check, row.status) == (CACHE_HISTORY_CHECK, "pass")


def test_check_8_claims_latest_message_caching_not_session_history() -> None:
    """The pass detail names what the row measures; check 9 owns the session-history claim."""
    row = _history_row(_history_turn(4400), _history_turn(4400, 300), _history_turn(4700))
    assert "latest-message cache point" in row.detail
    assert "session history" not in row.detail


def test_a_follow_up_reading_only_the_fresh_prefix_fails() -> None:
    row = _history_row(_history_turn(4400), _history_turn(4400), _history_turn(4400))
    assert row.status == "fail"


def test_a_follow_up_reading_less_than_the_fresh_prefix_fails() -> None:
    assert _history_row(_history_turn(4400), _history_turn(0), _history_turn(100)).status == "fail"


def test_no_read_and_no_write_on_any_turn_defers() -> None:
    row = _history_row(_history_turn(0), _history_turn(0), _history_turn(0))
    assert (row.status, row.defer_reason) == ("defer", CACHE_UNREPORTED_REASON)


def test_a_write_with_no_follow_up_read_fails_rather_than_defers() -> None:
    row = _history_row(_history_turn(0, 4400), _history_turn(0), _history_turn(0))
    assert row.status == "fail"


@pytest.mark.parametrize("position", [0, 1, 2], ids=["C", "A", "B"])
def test_a_tool_call_on_any_turn_defers_even_when_the_reads_would_pass(position: int) -> None:
    turns = [_history_turn(4400), _history_turn(4400), _history_turn(4700)]
    turns[position] = _history_turn(
        turns[position].completed["cost_lines"][0]["cache_read_tokens"],  # type: ignore[index]
        kinds=("tool_call", "tool_result", "text_delta", "completed"),
    )
    row = _history_row(*turns)
    assert (row.status, row.defer_reason) == ("defer", CACHE_HISTORY_TOOL_CALL_REASON)


@pytest.mark.parametrize(
    "followup",
    [TurnResult(status=502, kinds=(), answer=""), _history_turn(None), _history_turn(True)],
    ids=["no_frame", "null_read", "bool_read"],
)
def test_an_unreadable_turn_fails_rather_than_defers(followup: TurnResult) -> None:
    assert _history_row(_history_turn(4400), _history_turn(4400), followup).status == "fail"


def _is_history_prompt(prompt: str) -> bool:
    return prompt.startswith(_CACHE_HISTORY_INSTRUCTION)


def _history_prompts(turns: _StatefulTurns) -> list[str]:
    return [p for p in turns.prompts if _is_history_prompt(p)]


def test_c_a_and_b_share_one_prompt_carrying_a_run_nonce() -> None:
    turns = _StatefulTurns()
    _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    prompts = _history_prompts(turns)
    assert len(prompts) == CACHE_HISTORY_TURNS
    assert len(set(prompts)) == 1
    assert re.search(r"Ignore this cache-busting nonce: [0-9a-f]{32}\.$", prompts[0])


def test_two_runs_send_different_check_8_prompts() -> None:
    first, second = _StatefulTurns(), _StatefulTurns()
    _run(events=_ALL_TIER_EVENTS, run_turn=first)
    _run(events=_ALL_TIER_EVENTS, run_turn=second)
    assert _history_prompts(first)[0] != _history_prompts(second)[0]


def test_the_prompt_nonce_leaves_the_instruction_unchanged() -> None:
    assert cache_history_prompt("abc") == (
        f"{_CACHE_HISTORY_INSTRUCTION} Ignore this cache-busting nonce: abc."
    )
    assert cache_history_prompt("abc") != cache_history_prompt("abd")


def test_check_8_sends_c_fresh_then_a_and_b_in_one_session_as_anthropic() -> None:
    turns = _CapturingTurns()
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    prompts, tunings = turns._inner.prompts, turns._inner.tunings
    indices = [i for i, p in enumerate(prompts) if _is_history_prompt(p)]
    assert len(indices) == CACHE_HISTORY_TURNS
    fresh, seed, followup = (turns.session_ids[i] for i in indices)
    assert seed == followup and fresh != seed
    assert fresh not in turns.session_ids[: indices[0]]
    assert seed not in turns.session_ids[: indices[1]]
    anthropic = tuning_block(dict.fromkeys(_TIER_NAMES, "anthropic"))
    assert all(tunings[i] == anthropic for i in indices)
    assert expected_tier_models(dict.fromkeys(_TIER_NAMES, "anthropic"))["supervisor"] == (
        _ANTHROPIC_SUPERVISOR
    )
    assert by_check[CACHE_HISTORY_CHECK] == "pass"


def test_the_happy_path_fake_reads_more_on_a_session_follow_up() -> None:
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=_StatefulTurns())
    assert by_check[CACHE_HISTORY_CHECK] == "pass"


def test_a_runtime_caching_only_the_stable_prefix_fails_check_8_end_to_end() -> None:
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=_CacheTurns(4400, 0))
    assert by_check[CACHE_HISTORY_CHECK] == "fail"


@pytest.mark.parametrize("short", [1, CACHE_HISTORY_TURNS])
def test_a_share_without_room_for_all_three_turns_sends_none_of_them(
    monkeypatch: pytest.MonkeyPatch, short: int
) -> None:
    allotments = dict(MODEL_TURN_ALLOTMENTS)
    allotments[RESOURCE_CLASS] -= HISTORY_RECALL_TURNS + short
    monkeypatch.setattr(three_tier_chat, "MODEL_TURN_ALLOTMENTS", allotments)
    turns = _StatefulTurns()
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    assert not any(_is_history_prompt(p) for p in turns.prompts)
    assert CACHE_HISTORY_CHECK not in by_check
    assert all(by_check[row] == "pass" for row in _ROUTE_ROWS)


# --- check 9: a follow-up turn carries the session's history ---

_WORD = "zqfeedc0ffee12"


def _recall_turn(
    answer: str, read: object = 4400, write: object = 120, *, status: int = 200
) -> TurnResult:
    """A tool-free anthropic-supervisor turn answering `answer` with the given cache counts."""
    turn = _history_turn(read, write)
    return TurnResult(status=status, kinds=turn.kinds, answer=answer, completed=turn.completed)


def _recall_row(
    seed: TurnResult | None = None,
    followup: TurnResult | None = None,
    control: TurnResult | None = None,
) -> CheckResult:
    return evaluate_history_recall(
        seed if seed is not None else _recall_turn("OK", 0, 4400),
        followup if followup is not None else _recall_turn(_WORD, 4400, 120),
        control if control is not None else _recall_turn("I do not know.", 4300, 90),
        code_word=_WORD,
        model=_ANTHROPIC_SUPERVISOR,
    )


def test_a_recalled_word_with_a_new_cached_tail_and_a_clean_control_passes() -> None:
    row = _recall_row()
    assert (row.check, row.status) == (HISTORY_RECALL_CHECK, "pass")


def test_the_content_alone_without_a_new_cache_write_fails() -> None:
    assert _recall_row(followup=_recall_turn(_WORD, 4400, 0)).status == "fail"


def test_the_tokens_alone_without_the_recalled_word_fails() -> None:
    assert _recall_row(followup=_recall_turn("I do not know.", 4400, 120)).status == "fail"


def test_a_control_that_carries_the_word_fails() -> None:
    row = _recall_row(control=_recall_turn(f"It is {_WORD}.", 4300, 90))
    assert row.status == "fail"
    assert "crossed sessions" in row.detail


def test_a_follow_up_reading_less_than_the_seed_fails() -> None:
    row = _recall_row(seed=_recall_turn("OK", 4400, 100), followup=_recall_turn(_WORD, 4000, 120))
    assert row.status == "fail"


def test_a_control_that_did_not_complete_fails() -> None:
    assert _recall_row(control=TurnResult(status=502, kinds=(), answer="")).status == "fail"


@pytest.mark.parametrize(
    "followup",
    [
        TurnResult(status=502, kinds=(), answer=_WORD),
        _recall_turn(_WORD, None),
        _recall_turn(_WORD, True),
    ],
    ids=["no_frame", "null_read", "bool_read"],
)
def test_an_unreadable_follow_up_fails(followup: TurnResult) -> None:
    assert _recall_row(followup=followup).status == "fail"


def test_the_code_word_is_run_unique_and_only_in_the_fact_prompt() -> None:
    word = history_code_word()
    assert word != history_code_word()
    assert word in history_fact_prompt(word)
    assert word not in HISTORY_RECALL_PROMPT
    assert history_fact_prompt(word).startswith(_FACT_PREFIX)


_TTL = DEFAULT_TTL_SECONDS
_ABSENT = object()
_NOW = 1_000_000.0


def _item(**overrides: Any) -> dict[str, Any]:
    tenant = _tenant_of("token-A")
    item: dict[str, Any] = {
        "rep_id": history_key(tenant, "s"),
        "kind": "history",
        "owner_rep_id": "user-a",
        "owner_tenant": tenant,
        "turns": [{"prompt": "p", "answer": "a"}],
        "version": 1,
        "updated_at": "2026-11-15T00:00:00+00:00",
        "expires_at": int(_NOW) + _TTL - 5,
    }
    item.update(overrides)
    return {key: value for key, value in item.items() if value is not _ABSENT}


def _item_row(item: Mapping[str, Any] | None) -> CheckResult:
    return evaluate_history_item(item, tenant=_tenant_of("token-A"), now=_NOW, ttl_seconds=_TTL)


def test_the_ttl_is_the_history_stores_default() -> None:
    default = inspect.signature(DynamoDbConversationHistory).parameters["ttl_seconds"].default
    assert default == _TTL
    assert isinstance(_TTL, int) and _TTL > 0


def test_the_fixture_item_carries_exactly_the_stores_attribute_set() -> None:
    assert set(_item()) == HISTORY_ITEM_ATTRIBUTES


def test_an_owned_item_expiring_within_the_ttl_passes() -> None:
    row = _item_row(_item())
    assert (row.check, row.status) == (HISTORY_ITEM_CHECK, "pass")


def test_a_decimal_expiry_is_read_as_a_number() -> None:
    assert _item_row(_item(expires_at=Decimal(int(_NOW) + 60))).status == "pass"


@pytest.mark.parametrize(
    "overrides",
    [
        {"owner_tenant": "eval-smoke-token-B"},
        {"owner_rep_id": ""},
        {"owner_rep_id": None},
        {"kind": "conversation"},
        {"expires_at": int(_NOW)},
        {"expires_at": int(_NOW) + _TTL + 1},
        {"expires_at": True},
        {"expires_at": "9999999999"},
        {"tenant": "eval-smoke-token-A"},
        {"updated_at": _ABSENT},
    ],
    ids=[
        "foreign_tenant",
        "empty_owner",
        "no_owner",
        "wrong_kind",
        "expired",
        "beyond_ttl",
        "bool_expiry",
        "string_expiry",
        "tenant_attribute",
        "missing_attribute",
    ],
)
def test_an_item_not_owned_or_not_expiring_in_the_ttl_fails(overrides: dict[str, Any]) -> None:
    assert _item_row(_item(**overrides)).status == "fail"


def test_an_absent_item_fails() -> None:
    assert _item_row(None).status == "fail"


def _recall_indices(prompts: list[str]) -> list[int]:
    return [
        i
        for i, p in enumerate(prompts)
        if p.startswith(_FACT_PREFIX) or p in (HISTORY_RECALL_PROMPT, HISTORY_REPEAT_PROMPT)
    ]


def test_check_9_sends_a_b_a_fresh_control_then_an_unresumed_follow_up_as_anthropic() -> None:
    turns = _CapturingTurns()
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    prompts, tunings, omitted = turns._inner.prompts, turns._inner.tunings, turns._inner.omitted
    indices = _recall_indices(prompts)
    assert len(indices) == HISTORY_RECALL_TURNS
    seed, followup, control, repeat = (turns.session_ids[i] for i in indices)
    assert seed == followup == repeat and control != seed
    assert seed not in turns.session_ids[: indices[0]]
    assert control not in turns.session_ids[: indices[2]]
    assert prompts[indices[0]].startswith(_FACT_PREFIX)
    assert prompts[indices[1]] == prompts[indices[2]] == HISTORY_RECALL_PROMPT
    assert prompts[indices[3]] == HISTORY_REPEAT_PROMPT
    assert [omitted[i] for i in indices] == [False, False, False, True]
    assert omitted.count(True) == 1
    anthropic = tuning_block(dict.fromkeys(_TIER_NAMES, "anthropic"))
    assert all(tunings[i] == anthropic for i in indices)
    assert by_check[HISTORY_RECALL_CHECK] == "pass"
    assert by_check[HISTORY_OMITTED_RESUME_CHECK] == "pass"


def test_a_runtime_that_forgets_history_fails_check_9_end_to_end() -> None:
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=_StatefulTurns(forgets_history=True))
    assert by_check[HISTORY_RECALL_CHECK] == "fail"


def test_a_runtime_that_leaks_history_across_sessions_fails_check_9_end_to_end() -> None:
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=_StatefulTurns(leaks_history=True))
    assert by_check[HISTORY_RECALL_CHECK] == "fail"


def test_a_runtime_writing_no_history_item_fails_the_item_row_end_to_end() -> None:
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=_StatefulTurns(records_history=False))
    assert by_check[HISTORY_ITEM_CHECK] == "fail"


def test_every_history_item_the_run_wrote_is_deleted_before_the_users_are_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = _StatefulTurns()
    order: list[str] = []
    original_cleanup = _FakeProvisioner.cleanup

    def recording_cleanup(self: _FakeProvisioner) -> Iterable[CheckResult]:
        order.append("cleanup")
        yield from original_cleanup(self)

    monkeypatch.setattr(_FakeProvisioner, "cleanup", recording_cleanup)

    class _Ordered(_FakeHistoryItems):
        def delete(self, rep_id: str) -> None:
            order.append("delete")
            super().delete(rep_id)

    items = _Ordered(turns.history)
    by_check, provs = _run(events=_ALL_TIER_EVENTS, run_turn=turns, history_items=items)
    assert by_check[HISTORY_ITEMS_DELETED_CHECK] == "pass"
    written_tenants = {_tenant_of("token-A"), _tenant_of("token-B")}
    assert {key.split("#")[1] for key in items.deleted} <= written_tenants
    assert any(
        key.startswith(f"history#{_tenant_of('token-A')}#t3-recall-") for key in items.deleted
    )
    assert turns.history == {}
    assert order.index("cleanup") > max(i for i, step in enumerate(order) if step == "delete")
    assert all(p.cleaned_up for p in provs)


def test_an_unreadable_history_store_fails_the_item_and_delete_rows_and_still_cleans_up() -> None:
    def broken(_ctx: object) -> _FakeHistoryItems:
        raise RuntimeError("no dynamodb_table_names output")

    provs: list[_FakeProvisioner] = []
    tokens = iter(["token-A", "token-B"])

    def factory(_ctx: object) -> _FakeProvisioner:
        provs.append(_FakeProvisioner(next(tokens)))
        return provs[-1]

    ticks = iter(range(1000, 1_000_000, 1000))
    rows = list(
        checks(
            context_for_test(outputs={"agentcore_runtime_role_arn": _ROLE_ARN}, clients={}),
            provisioner_factory=factory,
            run_turn=_StatefulTurns(),
            cloudtrail_lookup=lambda _c, _s, _e: _ALL_TIER_EVENTS,
            clock=lambda: float(next(ticks)),
            sleeper=lambda _s: None,
            history_items=broken,
        )
    )
    by_check = {r.check: r.status for r in rows}
    assert by_check[HISTORY_ITEM_CHECK] == "fail"
    assert by_check[HISTORY_ITEMS_DELETED_CHECK] == "fail"
    assert all(p.cleaned_up for p in provs)


@pytest.mark.parametrize("short", [1, HISTORY_RECALL_TURNS])
def test_a_share_without_room_for_all_three_recall_turns_sends_none_of_them(
    monkeypatch: pytest.MonkeyPatch, short: int
) -> None:
    allotments = dict(MODEL_TURN_ALLOTMENTS)
    allotments[RESOURCE_CLASS] -= short
    monkeypatch.setattr(three_tier_chat, "MODEL_TURN_ALLOTMENTS", allotments)
    turns = _StatefulTurns()
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    assert not _recall_indices(turns.prompts)
    assert HISTORY_RECALL_CHECK not in by_check
    assert HISTORY_ITEM_CHECK not in by_check
    assert by_check[CACHE_HISTORY_CHECK] == "pass"
    assert by_check[HISTORY_ITEMS_DELETED_CHECK] == "pass"


def test_the_share_covers_every_check_and_the_envelope_still_fits() -> None:
    assert MODEL_TURN_ALLOTMENTS[RESOURCE_CLASS] == 8 + HISTORY_RECALL_TURNS
    assert sum(MODEL_TURN_ALLOTMENTS.values()) <= MODEL_TURN_ENVELOPE


# --- resume_from: a follow-up turn sends its session's recorded-frame count ---


def test_the_cursor_counts_every_frame_but_heartbeats_per_session() -> None:
    cursor = ResumeCursor()
    assert cursor.resume_from("s") == 0
    cursor.advance("s", ("heartbeat", "text_delta", "heartbeat", "usage", "completed"))
    assert cursor.resume_from("s") == 3
    cursor.advance("s", ("text_delta", "text_delta", "usage", "completed"))
    assert cursor.resume_from("s") == 7
    assert cursor.resume_from("other") == 0
    cursor.advance("s", ())
    assert cursor.resume_from("s") == 7


def test_the_live_runner_sends_each_sessions_cursor_as_resume_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, int]] = []

    def fake_live(
        _ctx: object, _token: str, session_id: str, *_a: Any, resume_from: int, **_k: Any
    ) -> TurnResult:
        sent.append((session_id, resume_from))
        return TurnResult(status=200, kinds=("text_delta", "usage", "completed"), answer="ok")

    monkeypatch.setattr(three_tier_chat, "_live_run_turn", fake_live)
    live = _LiveTurns()
    ctx = context_for_test(outputs={}, clients={})
    tuning = tuning_block(dict.fromkeys(_TIER_NAMES, "anthropic"))
    for session in ("s1", "s1", "s2", "s1"):
        live(ctx, "t", session, "p", tuning)
    assert sent == [("s1", 0), ("s1", 3), ("s2", 0), ("s1", 6)]


def test_the_default_runner_is_the_resuming_live_runner() -> None:
    source = inspect.getsource(checks)
    assert "_LiveTurns()" in source
    assert "else _live_run_turn" not in source


# --- coverage floor ---


def test_the_check_count_floor_is_met_by_a_run_whose_every_control_fails() -> None:
    floor = expected_check_count(context_for_test(outputs={}, clients={}))
    turns = _StatefulTurns(forgets_history=True, records_history=False)
    rows = list(
        checks(
            context_for_test(outputs={"agentcore_runtime_role_arn": _ROLE_ARN}, clients={}),
            provisioner_factory=lambda _c: _FakeProvisioner("token-A"),
            run_turn=turns,
            cloudtrail_lookup=lambda _c, _s, _e: [],
            clock=iter(range(1000, 1_000_000, 1000)).__next__,
            sleeper=lambda _s: None,
            history_items=lambda _c: _FakeHistoryItems(turns.history),
        )
    )
    module_rows = [r for r in rows if r.resource == RESOURCE_CLASS]
    assert len(module_rows) >= floor.minimum
    assert floor.basis


def test_the_floor_counts_the_check_9_rows() -> None:
    floor = expected_check_count(context_for_test(outputs={}, clients={}))
    happy, _ = _run(events=_ALL_TIER_EVENTS, run_turn=_StatefulTurns())
    assert floor.minimum <= len(happy)
    assert "history recall" in floor.basis


# --- check 9: a follow-up sent without resume_from (the MCP shape) still recalls ---


def _repeat_row(followup: TurnResult, repeat: TurnResult) -> CheckResult:
    return evaluate_omitted_resume_recall(
        followup, repeat, code_word=_WORD, model=_ANTHROPIC_SUPERVISOR
    )


def test_an_unresumed_follow_up_that_extends_the_session_passes() -> None:
    row = _repeat_row(_recall_turn(_WORD, 4400, 120), _recall_turn(_WORD, 4520, 40))
    assert (row.check, row.status) == (HISTORY_OMITTED_RESUME_CHECK, "pass")


def test_a_replay_of_b_fails_although_it_carries_the_word() -> None:
    b = _recall_turn(_WORD, 4400, 120)
    row = _repeat_row(b, b)
    assert row.status == "fail"
    assert "replay" in row.detail


def test_an_unresumed_follow_up_without_the_word_fails() -> None:
    row = _repeat_row(_recall_turn(_WORD, 4400, 120), _recall_turn("I do not know.", 4520, 40))
    assert row.status == "fail"


@pytest.mark.parametrize(
    "repeat",
    [TurnResult(status=502, kinds=(), answer=_WORD), _recall_turn(_WORD, None)],
    ids=["no_frame", "null_read"],
)
def test_an_unreadable_unresumed_follow_up_fails(repeat: TurnResult) -> None:
    assert _repeat_row(_recall_turn(_WORD, 4400, 120), repeat).status == "fail"


def test_a_runtime_replaying_an_unresumed_follow_up_fails_only_that_row_end_to_end() -> None:
    by_check, _ = _run(
        events=_ALL_TIER_EVENTS, run_turn=_StatefulTurns(replays_omitted_resume=True)
    )
    assert by_check[HISTORY_OMITTED_RESUME_CHECK] == "fail"
    assert by_check[HISTORY_RECALL_CHECK] == "pass"


def _captured_body(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> dict[str, Any]:
    """The JSON body `_live_run_turn` sends, captured before any network call."""
    sent: list[bytes] = []

    def fake_open(request: Any, timeout: float) -> Any:
        sent.append(request.data)
        raise urllib.error.HTTPError(
            request.full_url, 400, "refused", Message(), io.BytesIO(b'{"message": "m"}')
        )

    monkeypatch.setattr(three_tier_chat, "open_https", fake_open)
    ctx = context_for_test(
        outputs={"cloudfront_domain_name": "d.example.test", "agentcore_runtime_arn": "arn:x"},
        clients={},
    )
    result = _live_run_turn(
        ctx, "t", "s" * 40, "p", tuning_block(dict.fromkeys(_TIER_NAMES, "anthropic")), **kwargs
    )
    assert result.status == 400
    body: dict[str, Any] = json.loads(sent[0])
    return body


def test_the_live_turn_sends_resume_from_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _captured_body(monkeypatch, resume_from=7)["resume_from"] == 7


def test_the_live_turn_omits_resume_from_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "resume_from" not in _captured_body(monkeypatch, resume_from=None)


def test_the_live_runner_omits_resume_from_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[int | None] = []

    def fake_live(*_a: Any, resume_from: int | None, **_k: Any) -> TurnResult:
        sent.append(resume_from)
        return TurnResult(status=200, kinds=("text_delta", "usage", "completed"), answer="ok")

    monkeypatch.setattr(three_tier_chat, "_live_run_turn", fake_live)
    live = _LiveTurns()
    ctx = context_for_test(outputs={}, clients={})
    tuning = tuning_block(dict.fromkeys(_TIER_NAMES, "anthropic"))
    live(ctx, "t", "s1", "p", tuning)
    live(ctx, "t", "s1", "p", tuning, omit_resume_from=True)
    live(ctx, "t", "s1", "p", tuning)
    assert sent == [0, None, 6]


# --- check 9: the Runtime logs show every turn and no history store failure ---

_S = "t3-recall-" + "a" * 32
_C = "t3-rfresh-" + "b" * 32


def _started(session: str, stream: str | None = None) -> RuntimeLogLine:
    return RuntimeLogLine(
        stream=stream if stream is not None else f"runtime-logs-{session}",
        message=json.dumps({"event": "invocation_started", "session_id": session}),
    )


def _failure(event: str, session: str) -> RuntimeLogLine:
    return RuntimeLogLine(
        stream=f"runtime-logs-{session}", message=json.dumps({"event": event, "error_kind": "x"})
    )


_LOGGED = [_started(_S), _started(_S), _started(_S), _started(_C)]
_EXPECTED = {_S: 3, _C: 1}


def test_every_turn_logged_with_no_failure_passes() -> None:
    row = evaluate_history_logs(_LOGGED, _EXPECTED)
    assert (row.check, row.status) == (HISTORY_LOG_CHECK, "pass")


@pytest.mark.parametrize("event", HISTORY_FAILURE_EVENTS)
@pytest.mark.parametrize("session", [_S, _C])
def test_a_history_failure_in_a_check_9_stream_fails(event: str, session: str) -> None:
    row = evaluate_history_logs([*_LOGGED, _failure(event, session)], _EXPECTED)
    assert row.status == "fail"
    assert event in row.detail


def test_a_failure_in_another_sessions_stream_is_not_attributed() -> None:
    other = "t3-cfresh-" + "c" * 32
    row = evaluate_history_logs(
        [*_LOGGED, _started(other), _failure("history_write_failed", other)], _EXPECTED
    )
    assert row.status == "pass"


def test_a_missing_turn_line_fails_because_the_read_is_blind() -> None:
    row = evaluate_history_logs(_LOGGED[1:], _EXPECTED)
    assert row.status == "fail"
    assert "not evidence" in row.detail


def test_a_turn_logged_in_a_stream_not_naming_its_session_fails() -> None:
    lines = [_started(_S), _started(_S), _started(_S, stream="runtime-logs-shared"), _started(_C)]
    assert evaluate_history_logs(lines, _EXPECTED).status == "fail"


@pytest.mark.parametrize("event", HISTORY_FAILURE_EVENTS)
def test_a_runtime_logging_a_history_failure_fails_check_9_end_to_end(event: str) -> None:
    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=_StatefulTurns(history_failure=event))
    assert by_check[HISTORY_LOG_CHECK] == "fail"
    assert by_check[HISTORY_RECALL_CHECK] == "pass"


def test_an_unreadable_log_group_fails_the_log_row_end_to_end() -> None:
    def broken(_c: object, _s: int, _e: int) -> list[RuntimeLogLine]:
        raise RuntimeError("AccessDeniedException")

    by_check, _ = _run(events=_ALL_TIER_EVENTS, run_turn=_StatefulTurns(), runtime_logs=broken)
    assert by_check[HISTORY_LOG_CHECK] == "fail"


def test_the_log_poll_waits_for_late_lines_and_stops_at_the_bound() -> None:
    snapshots = iter([[], _LOGGED[:2], _LOGGED])
    clock = _FakeClock()
    lines, summary = _poll_history_logs(
        lambda _c, _s, _e: next(snapshots),
        context_for_test(outputs={}, clients={}),
        0,
        _EXPECTED,
        clock=clock,
        sleeper=clock.sleep,
    )
    assert lines == _LOGGED
    assert "polls=3" in summary
    never = _FakeClock()
    lines, summary = _poll_history_logs(
        lambda _c, _s, _e: [],
        context_for_test(outputs={}, clients={}),
        0,
        _EXPECTED,
        clock=never,
        sleeper=never.sleep,
    )
    assert lines == []
    assert never.now - 1_000.0 >= three_tier_chat._HISTORY_LOG_POLL_SECONDS
    assert summary.startswith("polls=19 ")


class _FakeLogs:
    def __init__(self, pages: list[list[dict[str, str]]]) -> None:
        self._pages = pages
        self.requests: list[dict[str, Any]] = []

    def filter_log_events(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        index = len(self.requests) - 1
        page: dict[str, Any] = {"events": self._pages[index]}
        if index + 1 < len(self._pages):
            page["nextToken"] = f"t{index + 1}"
        return page


def test_the_live_log_lookup_reads_the_runtime_group_and_every_page() -> None:
    logs = _FakeLogs(
        [[{"logStreamName": "a", "message": "m1"}], [{"logStreamName": "b", "message": "m2"}]]
    )
    ctx = context_for_test(
        outputs={"agentcore_runtime_log_group_name": "/g"}, clients={"logs": logs}
    )
    lines = _live_runtime_log_lookup(ctx, 1, 2)
    assert lines == [RuntimeLogLine("a", "m1"), RuntimeLogLine("b", "m2")]
    assert logs.requests[0] == {"logGroupName": "/g", "startTime": 1, "endTime": 2}
    assert logs.requests[1]["nextToken"] == "t1"


def test_a_failure_in_another_module_session_stream_fails() -> None:
    other = "t3-cfresh-" + "c" * 32
    lines = [*_LOGGED, _started(other), _failure("history_read_failed", other)]
    row = evaluate_history_logs(lines, _EXPECTED, module_sessions=[other])
    assert row.status == "fail"
    assert other in row.detail


def test_the_end_to_end_log_read_attributes_every_module_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[list[str]] = []
    original = three_tier_chat.evaluate_history_logs

    def spy(*args: Any, **kwargs: Any) -> CheckResult:
        received.append(list(kwargs["module_sessions"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(three_tier_chat, "evaluate_history_logs", spy)
    turns = _CapturingTurns()
    _run(events=_ALL_TIER_EVENTS, run_turn=turns)
    assert received and set(received[0]) == set(turns.session_ids)


def test_the_failure_events_are_the_runtimes_three_history_failure_lines() -> None:
    assert set(HISTORY_FAILURE_EVENTS) == {
        "history_read_failed",
        "history_write_failed",
        "history_write_conflict",
    }


def test_a_write_conflict_in_a_module_stream_fails_the_log_row() -> None:
    row = evaluate_history_logs(
        [
            *_LOGGED,
            RuntimeLogLine(f"runtime-logs-{_S}", json.dumps({"event": "history_write_conflict"})),
        ],
        _EXPECTED,
    )
    assert row.status == "fail"
    assert "history_write_conflict" in row.detail

# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.three_tier_chat` (attribution, cost, refusal, history).

The provisioner factory, turn runner, CloudTrail lookup, clock and sleeper are all injected.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.smoke import three_tier_chat
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.three_tier_chat import (
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
    CACHE_UNREPORTED_REASON,
    MIXED_FAMILIES,
    MODEL_TURN_ALLOTMENTS,
    MODEL_TURN_ENVELOPE,
    OPENAI_FAMILIES,
    OPENAI_ROW_SUFFIX,
    REFUSAL_ERROR_KIND,
    RESOURCE_CLASS,
    CloudTrailEvent,
    ModelTurnBudget,
    TurnResult,
    _live_cloudtrail_lookup,
    _tier_attributed,
    _tier_profiles,
    attribution_collision_row,
    checks,
    evaluate_cache_read,
    evaluate_history_cross_session,
    evaluate_page_context_refusal,
    evaluate_temperature_refusal,
    evaluate_three_tier_attribution,
    evaluate_turn_cost,
    expected_tier_efforts,
    expected_tier_models,
    page_context_for,
    refused_routes,
    select_attribution_mapping,
    stale_temperature_block,
    tuning_block,
)

from agents.registry import tool_registry_for
from agents.runtime_server import _VALIDATION_ERROR_KIND
from agents.supervisor import DIRECT_TOOL_NAMES
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
        "supervisor": "global.openai.gpt-6.1-sol",
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


class _FakeProvisioner:
    def __init__(self, token: str) -> None:
        self._token = token
        self.cleaned_up = False

    def provision(self) -> str:
        return self._token

    def cleanup(self) -> Iterable[CheckResult]:
        self.cleaned_up = True
        yield CheckResult(
            resource="smoke_user", check="cleaned_up", status="pass", detail="removed"
        )


def _priced_frame(tuning: Mapping[str, Any]) -> dict[str, Any]:
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
                "cache_read_tokens": 1024,
                "cache_write_tokens": 0,
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


def _completed(text: str = "", tuning: Mapping[str, Any] | None = None) -> TurnResult:
    return TurnResult(
        status=200,
        kinds=("text_delta", "completed"),
        answer=text,
        completed=_priced_frame(tuning) if tuning is not None else None,
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
    `echoes_route` puts the route in the refusal body.
    """

    def __init__(
        self,
        *,
        bad_collision: bool = False,
        honours_temperature: bool = False,
        accepts_bad_route: bool = False,
        echoes_route: bool = False,
    ) -> None:
        self._owner_by_session: dict[str, str] = {}
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
    ) -> TurnResult:
        self.tunings.append(tuning)
        self.prompts.append(prompt)
        self.page_contexts.append(page_context)
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
        return _completed("noted", tuning)


def _run(
    *,
    events: list[CloudTrailEvent],
    run_turn: object,
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

    ctx = context_for_test(outputs={"agentcore_runtime_role_arn": _ROLE_ARN}, clients={})
    rows = list(
        checks(
            ctx,
            provisioner_factory=factory,
            run_turn=run_turn,  # type: ignore[arg-type]
            cloudtrail_lookup=lookup,
            clock=lambda: float(next(ticks)),
            sleeper=lambda _s: None,
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
    assert by_check["cost_usd_positive"] == "pass"
    assert by_check["resolved_tiers_match_tuning"] == "pass"
    for tier, model in expected_tier_models(MIXED_FAMILIES).items():
        assert by_check[f"{tier}_cost_line_names_{model}"] == "pass"
    for tier, effort in expected_tier_efforts(MIXED_FAMILIES).items():
        assert by_check[f"{tier}_resolved_effort_is_{effort}"] == "pass"
    attribution_rows = {c: s for c, s in by_check.items() if c.endswith(ATTRIBUTION_ROW_SUFFIX)}
    families = select_attribution_mapping().families
    assert families is not None
    assert len(attribution_rows) == 3 + 2 * len(families)
    assert set(attribution_rows.values()) == {"pass"}
    assert by_check["stale_temperature_tuning_refused"] == "pass"
    assert by_check["model_backed_turns_within_envelope"] == "pass"
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
        ({"cost_lines": []}, ["cost_lines_present", *_MODEL_ROWS]),
        ({"cost_lines": None}, ["cost_lines_present", *_MODEL_ROWS]),
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
    allotments[RESOURCE_CLASS] -= 2
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
    allotments[RESOURCE_CLASS] -= 1
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

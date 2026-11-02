# SPDX-License-Identifier: MIT
"""Tests for :mod:`agents.prompt_bundle`, :mod:`agents.agent_prompts` and
:mod:`providers.prompt_caching`: the bundle's members, its digest, and the prompt loader."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from scripts import proc_run

from agents import (
    agent_prompts,
    answer_style,
    prompt_bundle,
    subagent_runner,
    supervisor,
    tool_actor,
    turn_driver,
)
from crm import errors
from crm.toolspec import definitions, validation
from providers import prompt_caching

REPO_ROOT = Path(__file__).resolve().parents[2]


def _canonical_sha256(bundle: dict[str, object]) -> str:
    text = json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- the bundle -----------------------------------------------------------------------------------


def test_the_bundle_carries_every_model_facing_member_at_schema_2() -> None:
    bundle = prompt_bundle.prompt_bundle()
    assert bundle["schema"] == 2
    assert set(bundle) == {
        "schema",
        "answer_style",
        "agent_prompts",
        "subagent_tools",
        "page_briefing_templates",
        "tool_actor_directive",
        "tools",
        "prompt_caching",
        "validation_messages",
        "error_next_steps",
        "tool_actor_errors",
        "delegation_outputs",
    }
    assert bundle["answer_style"] == answer_style.load_answer_style_prompt()
    assert bundle["tool_actor_directive"] == tool_actor._ACTOR_DIRECTIVE
    assert bundle["subagent_tools"] == list(supervisor.subagent_tool_schemas())
    assert bundle["prompt_caching"] == dict(prompt_caching.CACHING)
    briefings = bundle["page_briefing_templates"]
    assert isinstance(briefings, dict)
    assert set(briefings) == {"entity", "list", "board", "opaque"}


def test_the_validation_messages_cover_every_message_branch_and_end_with_the_hint() -> None:
    messages = prompt_bundle.prompt_bundle()["validation_messages"]
    assert isinstance(messages, dict)
    assert set(messages) == {"required", "additionalProperties", "enum", "type", "bound", "other"}
    assert len(set(messages.values())) == len(messages)
    assert all(str(text).endswith(validation.CORRECTION_HINT) for text in messages.values())


def test_the_error_next_steps_name_every_error_class_with_its_code() -> None:
    steps = prompt_bundle.prompt_bundle()["error_next_steps"]
    assert isinstance(steps, dict)
    assert steps["ValidationFailed"] == {"code": "validation_failed", "next_step": "fix_arguments"}
    assert steps["ServiceBusy"] == {"code": "internal_error", "next_step": "retry_later"}
    assert {"CrmError", "ScopeDenied", "NotFound", "Conflict", "TurnTimeout"} <= set(steps)


def test_the_tool_actor_errors_and_delegation_outputs_are_rendered_envelopes() -> None:
    bundle = prompt_bundle.prompt_bundle()
    actor = bundle["tool_actor_errors"]
    assert isinstance(actor, dict) and set(actor) == {"model_error", "no_tool_call"}
    for envelope in actor.values():
        assert envelope["error"]["code"] == "tool_actor_error"
    delegation = bundle["delegation_outputs"]
    assert isinstance(delegation, dict)
    assert set(delegation) == {"data", "no_match", "tool_failure", "denied"}
    assert list(delegation["data"]["body"]) == [subagent_runner.RESULTS_KEY]
    assert delegation["denied"]["error"]["code"] == "scope_denied"


def test_the_bundle_lists_every_toolspec_tool_sorted_by_name_with_its_schema() -> None:
    tools = prompt_bundle.prompt_bundle()["tools"]
    assert isinstance(tools, list)
    assert [tool["name"] for tool in tools] == sorted(t.name for t in definitions.ALL_TOOLS)
    by_name = {t.name: t for t in definitions.ALL_TOOLS}
    for tool in tools:
        assert set(tool) == {"name", "description", "input_schema"}
        assert tool["description"] == by_name[tool["name"]].description
        assert tool["input_schema"] == by_name[tool["name"]].input_schema


def test_the_digest_is_the_sha256_of_the_canonical_bundle_json() -> None:
    digest = prompt_bundle.prompt_bundle_sha256()
    assert len(digest) == 64 and int(digest, 16) >= 0
    assert digest == _canonical_sha256(prompt_bundle.prompt_bundle())


def test_the_digest_is_stable_across_processes() -> None:
    """A fresh interpreter, with its own hash seed, computes the same digest."""
    result = proc_run.run(
        [
            sys.executable,
            "-c",
            "from agents.prompt_bundle import prompt_bundle_sha256; print(prompt_bundle_sha256())",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        cwd=REPO_ROOT,
        env={"PYTHONPATH": f"{REPO_ROOT / 'src'}:{REPO_ROOT}", "PYTHONHASHSEED": "12345"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == prompt_bundle.prompt_bundle_sha256()


def _patch_answer_style(monkeypatch: pytest.MonkeyPatch) -> None:
    original = answer_style.load_answer_style_prompt()
    monkeypatch.setattr(answer_style, "load_answer_style_prompt", lambda: original + " ")


def _patch_agent_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_prompts, "load_agent_prompts", lambda: {"extra": "A new prompt."})


def _patch_subagent_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    schemas = supervisor.subagent_tool_schemas()
    changed = ({**schemas[0], "description": str(schemas[0]["description"]) + "."}, *schemas[1:])
    monkeypatch.setattr(supervisor, "subagent_tool_schemas", lambda: changed)


def _patch_page_briefing(monkeypatch: pytest.MonkeyPatch) -> None:
    original = supervisor.page_context_briefing
    monkeypatch.setattr(
        supervisor, "page_context_briefing", lambda context: original(context) + " "
    )


def _patch_tool_actor_directive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_actor, "_ACTOR_DIRECTIVE", tool_actor._ACTOR_DIRECTIVE + " ")


def _patch_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    first, *rest = definitions.ALL_TOOLS
    changed = dataclasses.replace(first, description=first.description + " ")
    monkeypatch.setattr(definitions, "ALL_TOOLS", (changed, *rest))


def _patch_prompt_caching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        prompt_caching, "CACHING", {**prompt_caching.CACHING, "anthropic": "cache_control"}
    )


def _patch_correction_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation, "CORRECTION_HINT", validation.CORRECTION_HINT + " ")


def _patch_validation_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation, "_joined", lambda values: f"[{values}]")


def _patch_next_step(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(errors.NotFound, "next_step", "stop")


def _patch_tool_actor_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_actor, "_ACTOR_ERROR_CODE", "tool_actor_failure")


def _patch_result_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subagent_runner, "RESULTS_KEY", "records")


def _patch_delegation_output(monkeypatch: pytest.MonkeyPatch) -> None:
    original = turn_driver._outcome_to_tool_output

    def changed(outcome: supervisor.SubagentOutcome) -> dict[str, object]:
        return {**original(outcome), "statusCode": 299}

    monkeypatch.setattr(turn_driver, "_outcome_to_tool_output", changed)


#: One source mutation per bundle member, keyed by the member it must move.
_MEMBER_MUTATIONS: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    "answer_style": _patch_answer_style,
    "agent_prompts": _patch_agent_prompts,
    "subagent_tools": _patch_subagent_tools,
    "page_briefing_templates": _patch_page_briefing,
    "tool_actor_directive": _patch_tool_actor_directive,
    "tools": _patch_tools,
    "prompt_caching": _patch_prompt_caching,
    "validation_messages": _patch_correction_hint,
    "error_next_steps": _patch_next_step,
    "tool_actor_errors": _patch_tool_actor_error,
    "delegation_outputs": _patch_result_shape,
}

#: Further code-resident sources, each with the member it must move.
_SOURCE_MUTATIONS: dict[str, tuple[str, Callable[[pytest.MonkeyPatch], None]]] = {
    "validation_template": ("validation_messages", _patch_validation_template),
    "outcome_to_tool_output": ("delegation_outputs", _patch_delegation_output),
}


def test_every_member_but_the_schema_has_a_mutation_cell() -> None:
    assert set(_MEMBER_MUTATIONS) == set(prompt_bundle.prompt_bundle()) - {"schema"}


@pytest.mark.parametrize("member", sorted(_MEMBER_MUTATIONS))
def test_changing_one_member_at_its_source_changes_that_member_and_the_digest(
    member: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = prompt_bundle.prompt_bundle()
    digest_before = prompt_bundle.prompt_bundle_sha256()
    _MEMBER_MUTATIONS[member](monkeypatch)
    after = prompt_bundle.prompt_bundle()
    assert [name for name in before if before[name] != after[name]] == [member]
    assert prompt_bundle.prompt_bundle_sha256() != digest_before


@pytest.mark.parametrize("source", sorted(_SOURCE_MUTATIONS))
def test_changing_a_further_code_resident_source_moves_its_member_and_the_digest(
    source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    member, patch = _SOURCE_MUTATIONS[source]
    before = prompt_bundle.prompt_bundle()
    digest_before = prompt_bundle.prompt_bundle_sha256()
    patch(monkeypatch)
    after = prompt_bundle.prompt_bundle()
    assert [name for name in before if before[name] != after[name]] == [member]
    assert prompt_bundle.prompt_bundle_sha256() != digest_before


# --- the prompt loader ----------------------------------------------------------------------------


def test_the_repository_prompt_directory_loads() -> None:
    """Every prompt file the repository carries loads; the README is not a prompt."""
    loaded = agent_prompts.load_agent_prompts()
    assert set(loaded) == {path.stem for path in agent_prompts.AGENT_PROMPTS_DIR.glob("*.yaml")}
    assert (agent_prompts.AGENT_PROMPTS_DIR / "README.md").is_file()


def test_an_absent_prompt_directory_loads_as_empty(tmp_path: Path) -> None:
    assert agent_prompts.load_agent_prompts(tmp_path / "absent") == {}


def test_prompts_load_keyed_by_stem_in_sorted_file_order(tmp_path: Path) -> None:
    for name in ("subagent", "supervisor", "alpha"):
        (tmp_path / f"{name}.yaml").write_text(
            f"schema_version: 1\nprompt: The {name} prompt.\n", encoding="utf-8"
        )
    (tmp_path / "README.md").write_text("not a prompt\n", encoding="utf-8")
    loaded = agent_prompts.load_agent_prompts(tmp_path)
    assert list(loaded) == ["alpha", "subagent", "supervisor"]
    assert loaded["supervisor"] == "The supervisor prompt."


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("- a list\n", "did not parse to a mapping"),
        ("schema_version: 2\nprompt: text\n", "schema_version 2"),
        ("schema_version: true\nprompt: text\n", "schema_version True"),
        ("schema_version: 1\nprompt: '  '\n", "non-empty string 'prompt'"),
        ("schema_version: 1\nprompt: 3\n", "non-empty string 'prompt'"),
        ("schema_version: 1\n", "exactly the keys"),
        ("schema_version: 1\nprompt: text\nfamily: openai\n", "exactly the keys"),
    ],
)
def test_a_malformed_prompt_file_is_refused(tmp_path: Path, body: str, message: str) -> None:
    (tmp_path / "bad.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        agent_prompts.load_agent_prompts(tmp_path)


def test_the_caching_table_names_exactly_the_two_families() -> None:
    assert set(prompt_caching.CACHING) == {"anthropic", "openai"}


def test_both_families_cache_by_an_explicit_request_level_mechanism() -> None:
    for family, mechanism in prompt_caching.CACHING.items():
        assert mechanism.startswith("explicit:"), family

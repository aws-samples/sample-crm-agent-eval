# SPDX-License-Identifier: MIT
"""Tests for the family-neutral answer-style system prompt and its provenance hash."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from agents.answer_style import (
    answer_style_prompt_sha256,
    answer_style_system_message,
    load_answer_style_prompt,
)


def _write_style(
    tmp_path: Path, *, prompt: str, schema_version: object = 1, comment: str = ""
) -> Path:
    payload = {"schema_version": schema_version, "prompt": prompt}
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "answer_style.yaml"
    text = (f"# {comment}\n" if comment else "") + yaml.safe_dump(payload, sort_keys=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- Loads one message ------------------------------------------------------------------------


def test_the_shipped_config_loads_one_non_empty_system_message() -> None:
    message = answer_style_system_message()
    assert message["role"] == "system"
    content = message["content"]
    assert isinstance(content, str) and content.strip()
    assert content == load_answer_style_prompt()


def test_the_message_is_the_only_thing_the_loader_returns() -> None:
    """`load_answer_style_prompt` returns a single string; the message wraps it as one {role,content}."""
    prompt = load_answer_style_prompt()
    assert isinstance(prompt, str)
    assert answer_style_system_message() == {"role": "system", "content": prompt}


# --- Same bytes for both families -------------------------------------------------------------


def test_there_is_no_family_axis_so_both_families_get_identical_bytes() -> None:
    """Two resolutions are byte-identical and the prompt names no model family."""
    first = load_answer_style_prompt()
    second = load_answer_style_prompt()
    assert first == second
    lowered = first.lower()
    for vendor_token in ("claude", "anthropic", "gpt", "openai", "sonnet", "opus", "haiku"):
        assert vendor_token not in lowered, f"answer-style prompt names a family: {vendor_token!r}"


# --- Exposes SHA-256 --------------------------------------------------------------------------


def test_sha256_is_the_hex_digest_of_the_prompt_text() -> None:
    prompt = load_answer_style_prompt()
    expected = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert answer_style_prompt_sha256() == expected
    assert len(answer_style_prompt_sha256()) == 64


def test_sha256_is_stable_across_calls() -> None:
    assert answer_style_prompt_sha256() == answer_style_prompt_sha256()


def test_sha256_moves_when_the_prompt_changes(tmp_path: Path) -> None:
    """A different prompt yields a different hash."""
    a = _write_style(tmp_path / "a", prompt="Answer style: lead with the direct answer.\n")
    b = _write_style(tmp_path / "b", prompt="Answer style: lead with the direct answer, briefly.\n")
    assert answer_style_prompt_sha256(a) != answer_style_prompt_sha256(b)


def test_sha256_hashes_the_prompt_not_the_file_so_comments_do_not_move_it(tmp_path: Path) -> None:
    """A comment-only change to the YAML leaves the hash unchanged."""
    prompt = "Answer style: lead with the direct answer.\n"
    plain = _write_style(tmp_path / "plain", prompt=prompt)
    commented = _write_style(tmp_path / "commented", prompt=prompt, comment="extra provenance note")
    assert plain.read_text() != commented.read_text()  # the files differ
    assert answer_style_prompt_sha256(plain) == answer_style_prompt_sha256(
        commented
    )  # the hash does not


# --- Validation: no silent defaults -----------------------------------------------------------


def test_a_non_mapping_document_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "answer_style.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_answer_style_prompt(path)


def test_an_unsupported_schema_version_is_rejected(tmp_path: Path) -> None:
    path = _write_style(tmp_path, prompt="x\n", schema_version=999)
    with pytest.raises(ValueError, match="schema_version"):
        load_answer_style_prompt(path)


@pytest.mark.parametrize("prompt", ["", "   \n", None], ids=["empty", "whitespace", "absent"])
def test_a_missing_or_empty_prompt_is_rejected_rather_than_sent(
    tmp_path: Path, prompt: object
) -> None:
    payload: dict[str, object] = {"schema_version": 1}
    if prompt is not None:
        payload["prompt"] = prompt
    path = tmp_path / "answer_style.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="prompt"):
        load_answer_style_prompt(path)


def test_the_shipped_prompt_covers_the_r6_answer_style_directives() -> None:
    """The shipped prompt carries the table, id and no-preamble formatting directives."""
    prompt = load_answer_style_prompt().lower()
    assert "table" in prompt  # markdown table for >=3 same-shape records
    assert "id" in prompt  # name the ids used
    assert "preamble" in prompt  # no preamble


def test_the_shipped_prompt_carries_formatting_rules_only() -> None:
    """The answer-style prompt carries no outcome rule (truncation, invented values, scope)."""
    prompt = load_answer_style_prompt().lower()
    assert prompt.startswith("answer style:")
    for outcome_word in ("truncat", "invent", "scope"):
        assert outcome_word not in prompt, outcome_word

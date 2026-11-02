# SPDX-License-Identifier: MIT
"""Seam 1: the SPA's tier-tuning wire block is what `parse_wire_tuning` decodes.

`tests/integration/fixtures/wire_tuning.json` is written by
`frontend/src/components/AssistantPanel/wireTuning.test.ts` from the real `wireTuningBlock`; this
test decodes it with the real Python parser, resolves it against the real capability matrix, and
reads the TypeScript constants the producer stamps into every block.
"""

from __future__ import annotations

import json
import re
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from scripts import proc_run

from crm.user_settings import SETTINGS_SOURCE
from providers.capabilities import load_capability_matrix
from providers.tier_tuning import (
    TIERS,
    WIRE_SCHEMA_VERSION,
    TierRequest,
    parse_wire_tuning,
    resolve_turn,
    to_wire,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _REPO_ROOT / "tests" / "integration" / "fixtures" / "wire_tuning.json"
_PRODUCER_TEST = _REPO_ROOT / "frontend/src/components/AssistantPanel/wireTuning.test.ts"
_PRODUCER = _REPO_ROOT / "frontend/src/components/AssistantPanel/wireTuning.ts"

#: The fixture's block names, each written by one `wireTuningBlock(...)` call in the producer test.
_EXPECTED_BLOCKS = frozenset({"mixed_families", "all_defaults"})


def _blocks() -> dict[str, Any]:
    document = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    blocks: dict[str, Any] = document["blocks"]
    return blocks


def _ts_constant(name: str) -> str:
    """The literal right-hand side of `export const <name> = ...;` in the producer module."""
    match = re.search(
        rf"^export const {name} = (.+?);$", _PRODUCER.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert match is not None, f"{_PRODUCER} declares no `export const {name}`"
    return match.group(1).strip()


def test_the_fixture_is_tracked_and_written_by_the_producer_test() -> None:
    """The artefact both sides read is committed, and the vitest that writes it names this path."""
    tracked = proc_run.run(
        ["git", "ls-files", "--error-unmatch", str(_FIXTURE.relative_to(_REPO_ROOT))],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode == 0, f"{_FIXTURE} is not tracked: {tracked.stderr}"
    producer = _PRODUCER_TEST.read_text(encoding="utf-8")
    assert '"tests", "integration", "fixtures", "wire_tuning.json"' in producer
    assert set(_blocks()) == _EXPECTED_BLOCKS
    for name in _EXPECTED_BLOCKS:
        assert f"{name}: wireTuningBlock(" in producer


def test_the_producer_constants_equal_the_decoders() -> None:
    assert int(_ts_constant("WIRE_SCHEMA_VERSION")) == WIRE_SCHEMA_VERSION
    assert json.loads(_ts_constant("WIRE_TUNING_SOURCE")) == SETTINGS_SOURCE


@pytest.mark.parametrize("name", sorted(_EXPECTED_BLOCKS))
def test_every_block_decodes_and_round_trips_field_for_field(name: str) -> None:
    """Every field the SPA emits is one the decoder names; a rename on either side fails here."""
    block = _blocks()[name]

    decoded = parse_wire_tuning(block, default_source="unused-default")

    assert decoded.source == SETTINGS_SOURCE
    assert to_wire(decoded) == block
    assert set(block["tiers"]) == set(TIERS)
    for tier_fields in block["tiers"].values():
        assert set(tier_fields) == {field.name for field in fields(TierRequest)}


@pytest.mark.parametrize("name", sorted(_EXPECTED_BLOCKS))
def test_every_block_resolves_against_the_shipped_matrix(name: str) -> None:
    """The models the SPA offers are ones the Runtime accepts, tier by tier."""
    block = _blocks()[name]
    matrix = load_capability_matrix()

    resolved = resolve_turn(matrix, parse_wire_tuning(block, default_source="unused"))

    for tier, request in block["tiers"].items():
        invocation = resolved[tier]
        if request["model"] is not None:
            assert invocation.model.model_id == request["model"]
        if request["family"] is not None:
            assert invocation.model.family == request["family"]
        if request["effort"] is not None:
            assert invocation.effort == request["effort"]


def test_a_flat_schema_one_block_from_an_old_client_is_refused() -> None:
    """Control: the decoder is not accepting anything; an older-schema block fails."""
    from crm.errors import ValidationFailed

    with pytest.raises(ValidationFailed) as excinfo:
        parse_wire_tuning({"family": "openai", "effort": "low"}, default_source="ui")
    assert excinfo.value.source == "tuning.schema"


def test_a_schema_two_block_carrying_temperature_from_a_stale_tab_is_refused_naming_it() -> None:
    """A page loaded before the deploy sends the old shape; the refusal names the removed field."""
    from crm.errors import ValidationFailed

    stale = json.loads(json.dumps(_blocks()["mixed_families"]))
    stale["schema"] = 2
    stale["tiers"]["subagent"]["temperature"] = 0.3

    with pytest.raises(ValidationFailed) as excinfo:
        parse_wire_tuning(stale, default_source="ui")
    assert excinfo.value.source == "tuning.tiers.subagent.temperature"
    assert "temperature" in str(excinfo.value)


def test_the_fixture_carries_no_temperature_key_in_any_tier() -> None:
    for name, block in _blocks().items():
        for tier, tier_fields in block["tiers"].items():
            assert "temperature" not in tier_fields, f"{name}.{tier}"

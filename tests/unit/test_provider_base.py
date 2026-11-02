# SPDX-License-Identifier: MIT
"""`providers.base`'s tuning contract: effort is the only axis, with per-field rejection sources."""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from crm.errors import ValidationFailed
from providers.base import ResolvedInvocation, Tuning, validate_tuning
from providers.capabilities import EffortCapability, ModelCapability, load_capability_matrix

MATRIX = load_capability_matrix()
OPUS = MATRIX.model_for("anthropic", "supervisor")
HAIKU = MATRIX.model_for("anthropic", "tool")
ASTRA = MATRIX.model_for("openai", "supervisor")

#: A constructed model that accepts no effort, so the unsupported-effort branch keeps a subject.
NO_EFFORT = dataclasses.replace(
    HAIKU,
    model_id="test.no-effort",
    effort=EffortCapability(
        supported=False, values=(), default=None, capped_at_when_thinking_disabled=None
    ),
)


@pytest.mark.parametrize("cls", [Tuning, ResolvedInvocation])
def test_no_tuning_type_carries_temperature(cls: type) -> None:
    assert "temperature" not in {field.name for field in dataclasses.fields(cls)}


def test_validate_tuning_takes_no_temperature_source() -> None:
    assert list(inspect.signature(validate_tuning).parameters) == [
        "model",
        "tuning",
        "effort_source",
    ]


def test_without_per_field_sources_every_rejection_names_the_tuning_source() -> None:
    with pytest.raises(ValidationFailed) as exc_info:
        validate_tuning(NO_EFFORT, Tuning(source="--effort", effort="low"))
    assert exc_info.value.source == "--effort"


@pytest.mark.parametrize(
    ("model", "tuning", "match"),
    [
        (NO_EFFORT, Tuning(source="s", effort="low"), "does not accept an effort parameter"),
        (OPUS, Tuning(source="s", effort="ultra"), "does not accept effort='ultra'"),
        (HAIKU, Tuning(source="s", effort="none"), "does not accept effort='none'"),
    ],
    ids=["unsupported", "unknown-rung", "other-familys-rung"],
)
def test_each_effort_rejection_names_the_effort_source(
    model: ModelCapability, tuning: Tuning, match: str
) -> None:
    with pytest.raises(ValidationFailed, match=match) as exc_info:
        validate_tuning(model, tuning, effort_source="E")
    assert exc_info.value.source == "E"


@pytest.mark.parametrize("effort", ["none", "max"])
def test_an_accepted_effort_is_carried_to_the_invocation(effort: str) -> None:
    resolved = validate_tuning(ASTRA, Tuning(source="s", effort=effort))
    assert resolved == ResolvedInvocation(model=ASTRA, effort=effort)


def test_an_unset_effort_resolves_to_the_models_default() -> None:
    assert validate_tuning(OPUS, Tuning(source="s")).effort == OPUS.effort.default
    assert validate_tuning(HAIKU, Tuning(source="s")).effort == HAIKU.effort.default
    assert validate_tuning(NO_EFFORT, Tuning(source="s")).effort is None

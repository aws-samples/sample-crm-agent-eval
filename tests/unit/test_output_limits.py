# SPDX-License-Identifier: MIT
"""The per-call output cap fits inside every configured model's output limit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from providers.output_limits import TURN_OUTPUT_TOKEN_CAP

CAPABILITIES_PATH = Path(__file__).resolve().parents[2] / "config" / "model_capabilities.yaml"

#: Models whose capability entry declares no ``max_output_tokens``, so the cap cannot be checked
#: against them. Declaring the limit for one of these, or adding another undeclared model, fails
#: :func:`test_models_without_declared_output_limit_are_exactly_the_known_set`.
MODELS_WITHOUT_DECLARED_OUTPUT_LIMIT: frozenset[str] = frozenset({"openai.gpt-6-luna"})


def _models() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(CAPABILITIES_PATH.read_text(encoding="utf-8"))
    models: dict[str, dict[str, Any]] = document["models"]
    return models


def test_capabilities_file_configures_models() -> None:
    assert _models(), "config/model_capabilities.yaml lists no models"


def test_cap_is_the_specified_value() -> None:
    assert TURN_OUTPUT_TOKEN_CAP == 16_384


def test_models_without_declared_output_limit_are_exactly_the_known_set() -> None:
    undeclared = {
        model_id for model_id, entry in _models().items() if "max_output_tokens" not in entry
    }
    assert undeclared == MODELS_WITHOUT_DECLARED_OUTPUT_LIMIT


@pytest.mark.parametrize("model_id", sorted(set(_models()) - MODELS_WITHOUT_DECLARED_OUTPUT_LIMIT))
def test_cap_does_not_exceed_model_max_output_tokens(model_id: str) -> None:
    max_output = _models()[model_id]["max_output_tokens"]
    assert isinstance(max_output, int)
    assert max_output >= TURN_OUTPUT_TOKEN_CAP

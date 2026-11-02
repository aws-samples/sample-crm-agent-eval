# SPDX-License-Identifier: MIT
"""The sweep's default arm effort and the configured supervisor effort are one value, ``medium``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from eval.sweep_units import DEFAULT_ARM_EFFORT

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS_PATH = REPO_ROOT / "config" / "models.yaml"
CAPABILITIES_PATH = REPO_ROOT / "config" / "model_capabilities.yaml"

FAMILIES = ("anthropic", "openai")


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _supervisor(family: str) -> dict[str, Any]:
    tier = _load(MODELS_PATH)["families"][family]["tiers"]["supervisor"]
    assert isinstance(tier, dict)
    return tier


def test_default_arm_effort_is_medium() -> None:
    assert DEFAULT_ARM_EFFORT == "medium"


@pytest.mark.parametrize("family", FAMILIES)
def test_configured_supervisor_effort_equals_the_default_arm_effort(family: str) -> None:
    assert _supervisor(family)["tuning"]["effort"] == DEFAULT_ARM_EFFORT


@pytest.mark.parametrize("family", FAMILIES)
def test_default_supervisor_effort_is_a_declared_value_of_the_supervisor_model(
    family: str,
) -> None:
    model_id = _supervisor(family)["model"]
    effort = _load(CAPABILITIES_PATH)["models"][model_id]["effort"]
    assert effort["supported"] is True
    assert DEFAULT_ARM_EFFORT in effort["values"]

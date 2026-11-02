# SPDX-License-Identifier: MIT
"""`resolve_session_model` honours a whole-session family switch.

Runs against the loaded capability matrix only, with no model or AWS call.
"""

from __future__ import annotations

import pytest

from crm.errors import ValidationFailed
from providers.base import Tuning, resolve_session_model
from providers.capabilities import load_capability_matrix


def test_resolve_session_model_rejects_an_unknown_family_naming_the_source() -> None:
    """A family the matrix does not configure is a ValidationFailed naming the source."""
    matrix = load_capability_matrix()
    with pytest.raises(ValidationFailed) as exc_info:
        resolve_session_model(
            matrix,
            "supervisor",
            Tuning(source="--family", family="gemini"),  # type: ignore[arg-type]
        )
    assert exc_info.value.source == "--family"
    assert "gemini" in exc_info.value.message


def test_resolve_session_model_resolves_the_openai_tier_model_for_a_family_switch() -> None:
    """family=openai resolves to the OpenAI supervisor tier model, not the default family."""
    matrix = load_capability_matrix()
    model = resolve_session_model(matrix, "supervisor", Tuning(source="--family", family="openai"))
    expected = matrix.model_for("openai", "supervisor")
    assert model.family == "openai"
    assert model.model_id == expected.model_id


def test_resolve_session_model_with_no_family_uses_the_default_family() -> None:
    """No family override resolves the matrix's configured default family for the tier."""
    matrix = load_capability_matrix()
    model = resolve_session_model(matrix, "subagent", Tuning(source="config"))
    assert model.model_id == matrix.model_for(matrix.default_family, "subagent").model_id

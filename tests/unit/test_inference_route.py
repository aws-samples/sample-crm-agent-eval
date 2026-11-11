# SPDX-License-Identifier: MIT
"""Assert inference profile ids resolve to exactly one pricing route or are refused."""

from __future__ import annotations

import pytest

from crm.inference_route import (
    GEO_PREFIXES,
    GLOBAL_PREFIX,
    UnknownInferenceRoute,
    route_for_profile,
)

MODEL = "anthropic.claude-haiku-5-5"


def test_global_prefix_resolves_to_global() -> None:
    assert route_for_profile(f"global.{MODEL}") == "global"


@pytest.mark.parametrize("prefix", ["us.", "eu.", "apac.", "ca.", "jp.", "au."])
def test_each_geo_prefix_resolves_to_geo(prefix: str) -> None:
    assert route_for_profile(f"{prefix}{MODEL}") == "geo"
    assert route_for_profile(f"{prefix}{MODEL}", model_id=MODEL) == "geo"


def test_geo_prefix_set_is_exactly_the_six_documented_prefixes() -> None:
    assert frozenset({"us.", "eu.", "apac.", "ca.", "jp.", "au."}) == GEO_PREFIXES
    assert GLOBAL_PREFIX == "global."


def test_no_prefix_is_a_prefix_of_another() -> None:
    prefixes = [*GEO_PREFIXES, GLOBAL_PREFIX]
    for a in prefixes:
        for b in prefixes:
            if a != b:
                assert not b.startswith(a), (a, b)


def test_matching_model_id_is_accepted() -> None:
    assert route_for_profile(f"global.{MODEL}", model_id=MODEL) == "global"


@pytest.mark.parametrize(
    "profile",
    [
        "",
        MODEL,
        "openai.gpt-6.1-sol",
        "uk.anthropic.claude-haiku-5-5",
        "US.anthropic.claude-haiku-5-5",
        "global",
        "us",
        "global.",
        "apac.",
    ],
)
def test_unrecognised_or_empty_profile_raises(profile: str) -> None:
    with pytest.raises(UnknownInferenceRoute):
        route_for_profile(profile)


@pytest.mark.parametrize(
    "profile",
    [
        f"global.{MODEL}-v2",
        f"us.{MODEL}x",
        "eu.anthropic.claude-sonnet-5-5",
        f"apac.us.{MODEL}",
    ],
)
def test_remainder_not_equal_to_model_id_raises(profile: str) -> None:
    with pytest.raises(UnknownInferenceRoute):
        route_for_profile(profile, model_id=MODEL)


def test_unknown_route_is_a_value_error() -> None:
    assert issubclass(UnknownInferenceRoute, ValueError)

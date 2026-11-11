# SPDX-License-Identifier: MIT
"""Resolve the pricing route of a Bedrock inference profile id.

``route_for_profile("global.anthropic.claude-haiku-5-5")`` returns ``"global"``; a geographic
prefix (``us.``, ``eu.``, ``apac.``, ``ca.``, ``jp.``, ``au.``) returns ``"geo"``. Anything else
raises :class:`UnknownInferenceRoute`. Pure, no I/O.
"""

from __future__ import annotations

from typing import Final, Literal

Route = Literal["global", "geo"]

GLOBAL_PREFIX: Final[str] = "global."
GEO_PREFIXES: Final[frozenset[str]] = frozenset({"us.", "eu.", "apac.", "ca.", "jp.", "au."})


class UnknownInferenceRoute(ValueError):  # noqa: N818
    """An inference profile id has no recognised route prefix or names a different model."""


def route_for_profile(inference_profile: str, *, model_id: str | None = None) -> Route:
    """Return the pricing route of an inference profile id; raise UnknownInferenceRoute otherwise.

    When ``model_id`` is given, the id after the prefix must equal it exactly.
    """
    if not inference_profile:
        raise UnknownInferenceRoute("inference profile id is empty")
    route: Route
    if inference_profile.startswith(GLOBAL_PREFIX):
        route, prefix = "global", GLOBAL_PREFIX
    else:
        geo_prefix = next((p for p in GEO_PREFIXES if inference_profile.startswith(p)), None)
        if geo_prefix is None:
            raise UnknownInferenceRoute(
                f"inference profile {inference_profile!r} has no recognised route prefix"
            )
        route, prefix = "geo", geo_prefix
    remainder = inference_profile[len(prefix) :]
    if not remainder:
        raise UnknownInferenceRoute(f"inference profile {inference_profile!r} names no model")
    if model_id is not None and remainder != model_id:
        raise UnknownInferenceRoute(
            f"inference profile {inference_profile!r} does not name model {model_id!r}"
        )
    return route

# SPDX-License-Identifier: MIT
"""The prompt-caching mechanism each model family's adapter requests.

:data:`CACHING` names, per family, how the adapter asks Bedrock to cache the request prefix. It is
part of the prompt bundle (:mod:`agents.prompt_bundle`), so changing a mechanism changes the
bundle digest recorded in eval provenance.

Both families keep the platform's implicit caching and place at most one explicit breakpoint, at
the end of the stable prefix: the tool
definitions plus the leading run of system messages up to, not including, the first one marked
``{CACHE_SCOPE_KEY: VOLATILE}`` (for example a per-page briefing). :func:`breakpoint_index` picks
that message, or ``None`` when there is no stable system message or the prefix cannot reach the
model's ``prompt_cache_min_checkpoint_tokens`` in ``config/model_capabilities.yaml``.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Final

import yaml

from providers.capabilities import Family

_CAPABILITIES_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "config" / "model_capabilities.yaml"
)

#: Seam-message key a builder sets to :data:`VOLATILE` on a system message that changes per request.
CACHE_SCOPE_KEY: Final[str] = "cache_scope"
VOLATILE: Final[str] = "volatile"

#: Fewest characters per token assumed when sizing a prefix, so a breakpoint is omitted only when the
#: prefix is too short to reach the minimum.
_CHARS_PER_TOKEN_FLOOR: Final[int] = 2

#: Per family: the explicit request-level mechanism, the TTL, and the implicit caching beside it.
CACHING: Final[Mapping[Family, str]] = MappingProxyType(
    {
        "anthropic": (
            "explicit: cache_control {type: ephemeral} (5-minute TTL) on the last stable system "
            "block, beside the platform's implicit caching, which no request field turns off; "
            "minimum prefix from prompt_cache_min_checkpoint_tokens"
        ),
        "openai": (
            "explicit: prompt_cache_breakpoint {mode: explicit} on the last stable system "
            "input_text block, beside implicit caching (prompt_cache_options {mode: implicit, "
            "ttl: 30m}, an automatic breakpoint on the latest message); minimum prefix from "
            "prompt_cache_min_checkpoint_tokens"
        ),
    }
)

#: The ``prompt_cache_options`` body field the OpenAI adapter sends on every Responses request.
OPENAI_PROMPT_CACHE_OPTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {"mode": "implicit", "ttl": "30m"}
)


@functools.cache
def min_prefix_tokens(model_id: str, path: Path = _CAPABILITIES_PATH) -> int | None:
    """Return ``model_id``'s ``prompt_cache_min_checkpoint_tokens``, or ``None`` when unset.

    Raises:
        KeyError: When ``model_id`` has no entry in the capability matrix.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    value = data["models"][model_id].get("prompt_cache_min_checkpoint_tokens")
    return int(value) if value is not None else None


def stable_system_end(messages: Sequence[Mapping[str, object]]) -> int | None:
    """Return the index of the last stable system message in the leading system run, or ``None``."""
    last: int | None = None
    for index, message in enumerate(messages):
        if message.get("role") != "system" or message.get(CACHE_SCOPE_KEY) == VOLATILE:
            break
        last = index
    return last


def breakpoint_index(
    model_id: str,
    messages: Sequence[Mapping[str, object]],
    tools: Sequence[Mapping[str, object]],
) -> int | None:
    """Return the message index that carries the cache breakpoint for this request, or ``None``."""
    end = stable_system_end(messages)
    if end is None:
        return None
    minimum = min_prefix_tokens(model_id)
    if minimum is None:
        return None
    characters = len(json.dumps(list(tools), sort_keys=True)) + sum(
        len(str(message.get("content", ""))) for message in messages[: end + 1]
    )
    if characters < minimum * _CHARS_PER_TOKEN_FLOOR:
        return None
    return end


__all__ = [
    "CACHE_SCOPE_KEY",
    "CACHING",
    "OPENAI_PROMPT_CACHE_OPTIONS",
    "VOLATILE",
    "breakpoint_index",
    "min_prefix_tokens",
    "stable_system_end",
]

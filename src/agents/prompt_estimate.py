# SPDX-License-Identifier: MIT
"""Estimate the prompt tokens of one model request before it is sent.

:func:`estimate_prompt_tokens` measures the UTF-8 bytes of the messages and tool schemas handed to
:meth:`providers.base.ModelProvider.stream` and divides by :data:`BYTES_PER_TOKEN_ESTIMATE`,
rounding up. It reads no tokenizer, so it is the same for every model family. The estimate excludes
framing an adapter adds after the call (its own system blocks or tool-choice wrappers).
:func:`refuse_if_over_limit` is the check every model call site runs before it streams.
"""

from __future__ import annotations

import json
import math
from typing import Final

from agents.budget import RequestRefusedError, TurnBudget

#: UTF-8 bytes counted as one token.
BYTES_PER_TOKEN_ESTIMATE: Final[int] = 3


def estimate_prompt_tokens(
    messages: list[dict[str, object]], tools: list[dict[str, object]]
) -> int:
    """Return the estimated prompt tokens of a request carrying ``messages`` and ``tools``.

    The figure is ``ceil(bytes / BYTES_PER_TOKEN_ESTIMATE)`` over the compact JSON encoding of
    ``[messages, tools]``; a value JSON cannot encode is rendered with ``str``.
    """
    encoded = json.dumps(
        [messages, tools], ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")
    return math.ceil(len(encoded) / BYTES_PER_TOKEN_ESTIMATE)


def refuse_if_over_limit(
    messages: list[dict[str, object]],
    tools: list[dict[str, object]],
    *,
    limit: int,
    tier: str,
    role: str,
    budget: TurnBudget | None,
) -> None:
    """Refuse a request whose estimated prompt exceeds ``limit``; return when it fits.

    On refusal the turn's ``budget``, when given, records it with
    :meth:`agents.budget.TurnBudget.refuse_request`.

    Raises:
        RequestRefusedError: The estimate is above ``limit``; the request must not be sent.
    """
    estimate = estimate_prompt_tokens(messages, tools)
    if estimate <= limit:
        return
    if budget is not None:
        budget.refuse_request(estimated_prompt_tokens=estimate, limit=limit, tier=tier, role=role)
    raise RequestRefusedError(
        f"estimated {estimate} prompt tokens exceeds the {tier} tier's {limit}-token request limit"
    )


__all__ = ["BYTES_PER_TOKEN_ESTIMATE", "estimate_prompt_tokens", "refuse_if_over_limit"]

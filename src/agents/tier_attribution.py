# SPDX-License-Identifier: MIT
"""Per-invocation and per-tool-call attribution records for the three-tier turn.

A turn spends model calls on up to three tiers — supervisor, subagent, and tool — and each call is
attributed to the ``(tier, role, model_id)`` that made it. These are frozen value objects with
construction-time validation and no I/O. :class:`InvocationAttribution` refuses a positive token
count without a ``model_id``; a zero-token record represents a tier that made no call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from providers.capabilities import Tier

#: The four mutually exclusive outcomes a tool call or subagent delegation resolves to, classified
#: by :func:`agents.supervisor.classify_tool_result`; identical to
#: :data:`agents.supervisor.SubagentOutcomeKind`.
OutcomeKind = Literal["data", "no_match", "denied", "tool_failure"]

_TIERS: frozenset[str] = frozenset(get_args(Tier))
_OUTCOME_KINDS: frozenset[str] = frozenset(get_args(OutcomeKind))


def _require_non_negative(name: str, value: int) -> None:
    """Raise :class:`ValueError` unless ``value`` is a non-negative integer token count."""
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")


@dataclass(frozen=True, slots=True)
class TokenCounts:
    """Input and output tokens a single invocation reported, exactly as the provider reported them.

    Attributes:
        input_tokens: Input tokens consumed, uninterpreted (cache inclusion is per-family, see
            :mod:`crm.metering`).
        output_tokens: Output tokens produced, including any reasoning/thinking tokens.
    """

    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        _require_non_negative("input_tokens", self.input_tokens)
        _require_non_negative("output_tokens", self.output_tokens)

    @property
    def any_tokens(self) -> bool:
        """True when this invocation reported any input or output tokens at all."""
        return self.input_tokens > 0 or self.output_tokens > 0


@dataclass(frozen=True, slots=True)
class CacheCounts:
    """Prompt-cache read and write tokens for a single invocation.

    Attributes:
        read_tokens: Tokens served from an existing prompt-cache checkpoint.
        write_tokens: Tokens written to a new prompt-cache checkpoint.
    """

    read_tokens: int = 0
    write_tokens: int = 0

    def __post_init__(self) -> None:
        _require_non_negative("read_tokens", self.read_tokens)
        _require_non_negative("write_tokens", self.write_tokens)

    @property
    def any_tokens(self) -> bool:
        """True when this invocation read from or wrote to a prompt cache."""
        return self.read_tokens > 0 or self.write_tokens > 0


@dataclass(frozen=True, slots=True)
class InvocationAttribution:
    """One model invocation attributed to the tier, role, and model that made it.

    Constructing one with any positive token count and a blank ``model_id`` raises
    :class:`ValueError`; a zero-token record may carry a blank ``model_id``.

    Attributes:
        tier: The model tier that ran the invocation — one of ``supervisor``/``subagent``/``tool``.
        role: The agent role name that made the call. Required (non-blank).
        model_id: The resolved model id that spent the tokens. Required (non-blank) whenever the
            record bears tokens; may be blank only for a zero-token structural record.
        tokens: Input/output token counts, exactly as reported.
        cache: Prompt-cache read/write token counts.
        latency_ms: Wall-clock latency of the invocation in milliseconds, or ``None`` when not
            measured (a stub arm makes no model call). Non-negative when present.
    """

    tier: Tier
    role: str
    model_id: str
    tokens: TokenCounts
    cache: CacheCounts
    latency_ms: float | None = None

    def __post_init__(self) -> None:
        if self.tier not in _TIERS:
            raise ValueError(f"tier must be one of {sorted(_TIERS)}, got {self.tier!r}")
        if not self.role.strip():
            raise ValueError("role must be a non-empty attribution")
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError(f"latency_ms must be non-negative, got {self.latency_ms}")
        if self.bears_tokens and not self.model_id.strip():
            raise ValueError(
                "a token-bearing invocation record must name a model_id — "
                "token usage cannot be recorded without attribution"
            )

    @property
    def bears_tokens(self) -> bool:
        """True when this invocation reported any input, output, or cache tokens."""
        return self.tokens.any_tokens or self.cache.any_tokens


@dataclass(frozen=True, slots=True)
class ToolCallAttribution:
    """One leaf tool call attributed to the role that dispatched it and its classified outcome.

    Carries no tokens and no model id; a tool call's cost is on the tool-tier
    :class:`InvocationAttribution` that formulated its arguments.

    Attributes:
        tool_name: The dispatched tool's name. Required (non-blank).
        role: The agent role that dispatched the tool. Required (non-blank).
        latency_ms: Wall-clock latency of the dispatch in milliseconds, or ``None`` when not
            measured. Non-negative when present.
        outcome_kind: The four-way :data:`OutcomeKind` the result classified as.
        truncated: Whether the tool's result was truncated at the record boundary before use.
    """

    tool_name: str
    role: str
    latency_ms: float | None
    outcome_kind: OutcomeKind
    truncated: bool

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("tool_name must be a non-empty attribution")
        if not self.role.strip():
            raise ValueError("role must be a non-empty attribution")
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError(f"latency_ms must be non-negative, got {self.latency_ms}")
        if self.outcome_kind not in _OUTCOME_KINDS:
            raise ValueError(
                f"outcome_kind must be one of {sorted(_OUTCOME_KINDS)}, got {self.outcome_kind!r}"
            )


__all__ = [
    "CacheCounts",
    "InvocationAttribution",
    "OutcomeKind",
    "TokenCounts",
    "ToolCallAttribution",
]

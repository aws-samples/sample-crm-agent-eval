# SPDX-License-Identifier: MIT
"""The provider seam: the abstract model interface and tuning validation.

``ModelProvider`` is implemented by the Anthropic and OpenAI adapters and by :mod:`providers.stub`.
Agent and eval code depend only on this module and :mod:`providers.capabilities`. Tuning is
validated against the :class:`~providers.capabilities.CapabilityMatrix` before any request is
built; a rejection is a :class:`crm.errors.ValidationFailed` naming the offending source.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final, Literal

from crm.errors import ValidationFailed
from providers.capabilities import CapabilityMatrix, Family, ModelCapability, Tier

EventType = Literal[
    "text_delta",
    "tool_call",
    "tool_result",
    "usage",
    "error",
    "completed",
]


@dataclass(frozen=True, slots=True)
class Tuning:
    """A caller-requested tuning override for one invocation.

    Attributes:
        family: The provider family to invoke, or ``None`` for the matrix's default family; see
            :func:`resolve_session_model`.
        effort: A requested effort level, or ``None`` to use the tier's configured default.
        source: Where this tuning request originated — a CLI flag name (for example
            ``--effort``), an environment variable name (for example ``CRM_EFFORT``), or a UI
            control identifier (for example ``settings.tuning.effort``). Named in any rejection.
    """

    source: str
    family: Family | None = None
    effort: str | None = None


@dataclass(frozen=True, slots=True)
class RetriedAttempt:
    """One transport attempt the vendor SDK retried, as reported on a ``usage`` event.

    Attributes:
        status: The HTTP status the attempt received, or ``None`` when it failed before any
            response (a connection error or timeout).
        error_type: The transport exception's type name when there was no response, else ``None``.
    """

    status: int | None
    error_type: str | None


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    """One event in a provider's normalised output stream.

    Every concrete provider emits this shape regardless of its wire format.

    Attributes:
        type: Which kind of event this is.
        text: Incremental text for ``text_delta``; absent otherwise.
        tool_name: The tool being called, for ``tool_call``; absent otherwise.
        tool_input: The tool call arguments, for ``tool_call``; absent otherwise.
        tool_id: The provider's own id for this tool call, for ``tool_call``; absent otherwise.
            A follow-up ``tool_result`` names the call it answers by this id (Anthropic
            ``tool_use_id`` / OpenAI ``call_id``).
        tool_output: The tool result payload, for ``tool_result``; absent otherwise.
        input_tokens: Input tokens consumed, present on ``usage`` and ``completed`` events.
        output_tokens: Output tokens produced, present on ``usage`` and ``completed`` events.
        cache_read_tokens: Tokens served from a prompt cache, when reported.
        cache_write_tokens: Tokens written to a prompt cache, when reported.
        error_message: A user-safe error description, for ``error`` events; absent otherwise.
        awaiting_tools: On a ``completed`` event, ``True`` when the model ended this round pending
            the results of tool calls it just emitted (Anthropic ``stop_reason == "tool_use"`` /
            an OpenAI response carrying function calls) rather than finishing its answer. The turn
            driver re-invokes the model with the tool results appended only when this is set.
        latency_ms: The latency (milliseconds) of the successful model call this event belongs to,
            stamped on the ``usage`` event by the live adapter: from the successful attempt's
            request to the end of the stream. Retried attempts are not timed. ``None`` on the stub.
        retried_attempts: On every ``usage`` and ``error`` event, one :class:`RetriedAttempt` per
            transport attempt the vendor SDK retried, in attempt order: every recorded attempt except
            the last. Empty on the stub and whenever the first attempt was the last.
    """

    type: EventType
    text: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, object] | None = None
    tool_id: str | None = None
    tool_output: dict[str, object] | None = None
    awaiting_tools: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    error_message: str | None = None
    latency_ms: float | None = None
    retried_attempts: tuple[RetriedAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedInvocation:
    """A validated, ready-to-send invocation: a concrete model plus tuning it accepts.

    Built by :func:`validate_tuning`, so every field has passed matrix validation.

    Attributes:
        model: The resolved model's capability entry.
        effort: The effort value to send, or ``None`` to send no effort parameter.
    """

    model: ModelCapability
    effort: str | None


def resolve_session_model(matrix: CapabilityMatrix, tier: Tier, tuning: Tuning) -> ModelCapability:
    """Resolve a tier's model honouring a whole-session family switch.

    Treats ``tuning.family`` as the family the whole session runs in, validates that the matrix
    configures it, and resolves the tier's model within it.

    Args:
        matrix: The loaded capability matrix.
        tier: Which tier is being resolved.
        tuning: The caller's tuning request, whose ``family`` and ``source`` are consulted.

    Returns:
        The capability entry for the resolved model.

    Raises:
        ValidationFailed: When ``tuning.family`` names a family the matrix does not configure.
    """
    family = tuning.family if tuning.family is not None else matrix.default_family
    if family not in matrix.tiers:
        raise ValidationFailed(
            f"Unknown model family {family!r}; configured families are {sorted(matrix.tiers)}.",
            source=tuning.source,
        )
    return matrix.model_for(family, tier)


def guard_invocation_family(expected: Family, invocation: ResolvedInvocation) -> None:
    """Refuse an invocation whose resolved model is not of ``expected`` family.

    Each adapter calls this at the top of :meth:`ModelProvider.stream`, before any client call.

    Args:
        expected: The family this adapter serves.
        invocation: The resolved invocation handed to the adapter.

    Raises:
        ValueError: When ``invocation.model.family`` is not ``expected``.
    """
    if invocation.model.family != expected:
        raise ValueError(
            f"{expected} adapter received a {invocation.model.family} invocation "
            f"({invocation.model.model_id}); the per-invocation family selector mis-routed it."
        )


def validate_tuning(
    model: ModelCapability,
    tuning: Tuning,
    *,
    effort_source: str | None = None,
) -> ResolvedInvocation:
    """Validate a tuning request against one model's capability entry.

    Every rejection names ``tuning.source``, or the per-field source when one is given. This
    function sends nothing.

    Args:
        model: The capability entry for the model this invocation targets.
        tuning: The caller's requested effort.
        effort_source: The ``source`` named by an effort rejection; defaults to ``tuning.source``.

    Returns:
        A :class:`ResolvedInvocation` carrying only values ``model`` accepts.

    Raises:
        ValidationFailed: When an effort value is requested that the model does not accept.
    """
    effort = _validate_effort(model, tuning.effort, effort_source or tuning.source)
    return ResolvedInvocation(model=model, effort=effort)


def _validate_effort(model: ModelCapability, effort: str | None, source: str) -> str | None:
    """Validate the effort component of a tuning request."""
    if effort is None:
        return model.effort.default if model.effort.supported else None
    if not model.effort.supported:
        raise ValidationFailed(
            f"{model.model_id} does not accept an effort parameter.",
            source=source,
        )
    if effort not in model.effort.values:
        raise ValidationFailed(
            f"{model.model_id} does not accept effort={effort!r}; "
            f"accepted values are {list(model.effort.values)}.",
            source=source,
        )
    return effort


PROVIDER_TIMEOUT_SECONDS: Final[float] = 120.0


class ModelProvider(ABC):
    """The provider seam. Every request path — real or stub — implements exactly this.

    A concrete implementation receives a :class:`ResolvedInvocation` that has already passed
    :func:`validate_tuning`.
    """

    @abstractmethod
    def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        """Stream a model turn, yielding normalised :class:`ProviderEvent` values.

        An implementation may be an ``async def`` generator or a synchronous method returning an
        async iterator.

        Args:
            invocation: The validated model and tuning to invoke.
            messages: The conversation so far, in the seam's provider-agnostic message shape.
            tools: Tool definitions available to the model, in the seam's tool schema shape.

        Yields:
            One :class:`ProviderEvent` per streamed unit, always ending in exactly one
            ``completed`` or ``error`` event.
        """
        raise NotImplementedError

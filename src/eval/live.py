# SPDX-License-Identifier: MIT
"""Live-mode invocation support for the eval harness: bounded retry and exclusion tallying.

1. The vendor SDK retries transport failures, and :class:`ThrottleRetryingProvider` retries an
   in-band stream failure whose leading prefix :func:`is_retryable_error` recognises. Every retried
   attempt of either layer becomes one :class:`eval.metrics.Exclusion`: THROTTLE for a 429 or an
   in-band throttle prefix, RETRY otherwise. The final attempt of an aborted call is not an
   exclusion.
2. A failure outside the cold-start/retry/throttle exclusion taxonomy raises
   :class:`LiveInvocationError`, which aborts the sweep before any artifact is published.

:meth:`ThrottleRetryingProvider.stream` drains each attempt's events into a list before yielding
any of them, so a failed attempt can be retried. The ``usage`` event's ``latency_ms`` is passed
through unchanged.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Final

from eval.metrics import Exclusion, ExclusionCategory
from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation, RetriedAttempt
from providers.capabilities import Family
from providers.factory import build_provider_for_family

#: Leading ``<prefix>:`` tokens of a retryable *throttle*: the provider refused for rate, quota or
#: availability reasons. Bedrock's PascalCase exception type names, as the Anthropic adapter
#: renders an in-band exception frame.
_THROTTLE_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "ServiceQuotaExceededException",
        "ModelTimeoutException",
        "ModelNotReadyException",
    }
)

#: Leading ``<prefix>:`` tokens of a retryable *transient upstream server error*:
#: Bedrock's server-fault exception types, and the ``server_error`` code of the OpenAI-compatible
#: Responses surface.
_TRANSIENT_SERVER_ERROR_PREFIXES: Final[frozenset[str]] = frozenset(
    {"InternalServerException", "ModelErrorException", "ModelStreamErrorException", "server_error"}
)

_RETRYABLE_PREFIXES: Final[frozenset[str]] = _THROTTLE_PREFIXES | _TRANSIENT_SERVER_ERROR_PREFIXES

#: The HTTP status of a retried transport attempt that is a throttle; any other is a RETRY.
_THROTTLE_STATUS: Final[int] = 429

#: How many total attempts one turn gets before its failure aborts the sweep.
DEFAULT_MAX_ATTEMPTS: Final[int] = 3

#: Base seconds for the exponential backoff between attempts.
DEFAULT_BACKOFF_SECONDS: Final[float] = 2.0


class LiveInvocationError(RuntimeError):
    """Raised when a live turn failed for a reason the exclusion taxonomy cannot categorise.

    :func:`eval.harness.run_sweep` publishes no artifact for a sweep whose body raised.
    """


def _leading_prefix(message: str) -> str:
    """Return ``message``'s leading ``<prefix>`` token (before the first ``:``), or ``""``."""
    head, separator, _ = message.partition(":")
    return head if separator else ""


def is_retryable_error(message: str) -> bool:
    """Return whether an in-band failure's leading ``<prefix>:`` names a transient failure.

    Only the leading token counts, never text elsewhere in the message. SDK-exhausted, deadline and
    credential renderings carry no retryable prefix.
    """
    return _leading_prefix(message) in _RETRYABLE_PREFIXES


def _retry_exclusion_category(message: str) -> ExclusionCategory:
    """Categorise a retryable in-band failure within the exclusion taxonomy.

    A throttle is :attr:`~eval.metrics.ExclusionCategory.THROTTLE`; a transient server error has no
    bucket of its own, so it is :attr:`~eval.metrics.ExclusionCategory.RETRY`. The exclusion's
    ``detail`` carries the actual prefix either way.
    """
    if _leading_prefix(message) in _THROTTLE_PREFIXES:
        return ExclusionCategory.THROTTLE
    return ExclusionCategory.RETRY


class ThrottleRetryingProvider(ModelProvider):
    """Retries an in-band transient failure, tallying every retried attempt as an exclusion.

    One instance serves one sweep arm. :meth:`drain_exclusions` hands the arm's recorded
    exclusions to :func:`eval.metrics.aggregate_model_metrics` and empties the buffer, so an arm's
    tally is never attributed to another arm.
    """

    def __init__(
        self,
        wrapped: ModelProvider,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Wrap ``wrapped`` with bounded retry on an in-band transient failure.

        Args:
            wrapped: The live adapter whose turns are retried.
            max_attempts: Total attempts per turn, including the first. Must be at least 1.
            backoff_seconds: Base seconds for the exponential backoff between attempts.
            sleep: How to wait between attempts.

        Raises:
            ValueError: When ``max_attempts`` is below 1.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._wrapped = wrapped
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._exclusions: list[Exclusion] = []

    def drain_exclusions(self) -> list[Exclusion]:
        """Return every exclusion recorded since the last drain, and clear the buffer."""
        drained = list(self._exclusions)
        self._exclusions.clear()
        return drained

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        """Yield one successful attempt's events, retrying an in-band transient failure first.

        Each attempt's ``retried_attempts`` (on its ``usage`` or ``error`` event) are recorded as
        exclusions before anything else, 429 as THROTTLE and any other as RETRY. An in-band
        failure the wrapper retries is one more exclusion, categorised by its prefix. Events are
        yielded unchanged.

        Args:
            invocation: The validated model and tuning to invoke.
            messages: The conversation so far.
            tools: Tool definitions offered to the model.

        Yields:
            Every :class:`providers.base.ProviderEvent` of the first attempt that did not report
            an error.

        Raises:
            LiveInvocationError: When the final attempt failed: an in-band transient failure that
                exhausted ``max_attempts``, or any failure :func:`is_retryable_error` rejects.
        """
        model_id = invocation.model.model_id
        for attempt in range(1, self._max_attempts + 1):
            events = [event async for event in self._wrapped.stream(invocation, messages, tools)]
            for event in events:
                self._record_transport_retries(event.retried_attempts, model_id)
            failure = next((event for event in events if event.type == "error"), None)
            if failure is None:
                for event in events:
                    yield event
                return

            message = failure.error_message or "the provider reported an unnamed error"
            if not is_retryable_error(message) or attempt == self._max_attempts:
                raise LiveInvocationError(
                    f"live invocation of {model_id} failed on attempt "
                    f"{attempt} of {self._max_attempts} and is not a categorisable exclusion: "
                    f"{message}"
                )
            self._record(
                _retry_exclusion_category(message),
                f"retrying {model_id} after attempt {attempt} of {self._max_attempts}: {message}",
            )
            self._sleep(self._backoff_seconds * (2 ** (attempt - 1)))

    def _record_transport_retries(self, retried: tuple[RetriedAttempt, ...], model_id: str) -> None:
        """Record one exclusion per transport attempt the SDK retried."""
        for retried_attempt in retried:
            category = (
                ExclusionCategory.THROTTLE
                if retried_attempt.status == _THROTTLE_STATUS
                else ExclusionCategory.RETRY
            )
            self._record(
                category,
                f"SDK transport retry for {model_id}: status={retried_attempt.status} "
                f"error_type={retried_attempt.error_type}",
            )

    def _record(self, category: ExclusionCategory, detail: str) -> None:
        """Append one categorised exclusion, always with a traceable invocation id."""
        self._exclusions.append(
            Exclusion(invocation_id=str(uuid.uuid4()), category=category, detail=detail)
        )


def build_live_provider(family: Family, *, region_name: str) -> ThrottleRetryingProvider:
    """Build the live provider one sweep arm invokes: a family adapter under bounded retry.

    Args:
        family: The arm's provider family.
        region_name: The AWS region the adapter's SDK client targets on ``bedrock-runtime``.

    Returns:
        A :class:`ThrottleRetryingProvider` wrapping the family's live adapter, which reaches
        Bedrock with IAM/SigV4 credentials.
    """
    return ThrottleRetryingProvider(build_provider_for_family(family, region_name=region_name))


__all__ = [
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "LiveInvocationError",
    "ThrottleRetryingProvider",
    "build_live_provider",
    "is_retryable_error",
]

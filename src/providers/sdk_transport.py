# SPDX-License-Identifier: MIT
"""Shared transport for the vendor-SDK adapters: HTTP client, attempt recorder, deadline, lock-down.

Both :mod:`providers.anthropic_adapter` and :mod:`providers.openai_adapter` build their SDK client
from this module and nothing else:

* :func:`build_http_client` returns the ``httpx2.AsyncClient`` handed to the SDK as ``http_client``,
  with the per-phase timeouts (5s connect; 120s for each read, write and pool wait), the
  request/response hooks that record every transport attempt the SDK makes, retries included, and
  ``trust_env=False``, so no proxy or CA-bundle (``SSL_CERT_FILE``/``SSL_CERT_DIR``) environment
  variable reaches it.
* :func:`model_call_deadline` bounds one whole model call (the open, every SDK retry, and every
  stream chunk) to a 120s wall-clock budget. The per-phase timeouts are a second bound and
  do not limit a whole call.
* :func:`recording_attempts` binds a fresh :class:`AttemptRecorder` to the current context through a
  :class:`contextvars.ContextVar`, so concurrent ``stream()`` calls on one adapter never share
  records. Call :func:`require_bound_recorder` inside the block before invoking the SDK. A hook
  firing outside such a block raises :class:`RecorderNotBoundError`. OpenAI propagates it as is;
  Anthropic wraps it in ``APIConnectionError`` (``__cause__`` set) after retrying.
* :func:`require_credentials` resolves the credential chain once before the SDK call and raises
  :class:`CredentialPolicyError` when it is empty.
* :func:`anthropic_client_kwargs` / :func:`openai_client_kwargs` are the only way to assemble the SDK
  constructor arguments. They refuse the SDKs' bearer and base-URL environment fallbacks, pin an
  explicit ``bedrock-runtime`` base URL, have no ``api_key`` field, and switch the Anthropic SDK's
  OpenTelemetry tracing off. After constructing the client, pass it to :func:`assert_runtime_client`.
* :func:`session_credentials_provider` adapts a ``boto3`` session to the OpenAI SDK's
  ``aws_credentials_provider``, resolving credentials on every signed request.

Neither the recording block nor a timeout scope may span a ``yield``: an async generator runs in
its consumer's context, and resetting a binding from another context raises ``ValueError``. The
deadline therefore bounds each await rather than enclosing the stream. The adapter pattern::

    async with model_call_deadline() as deadline:
        require_credentials(session)
        with recording_attempts() as recorder:
            require_bound_recorder()
            stream = await deadline.run(stream_manager.__aenter__())
        try:
            async for chunk in deadline.iterate(stream):
                yield ...
        except ModelCallDeadlineError as exc:
            yield error event: render_deadline_exceeded(exc), recorder.retried_attempts()
        finally:
            await stream_manager.__aexit__(None, None, None)

The recorder object stays readable after its block closes. The SDK stream must be closed on every
exit path, deadline expiry included.

Importing this module sets every logger in :data:`SDK_LOGGERS` (the two vendor SDKs, their HTTP
stack, and the ``boto3``/``botocore`` family) to WARNING, so request and response bodies are never
logged. ``SDK_LOGGERS`` and ``quiet_sdk_loggers`` are re-exported from :mod:`providers.sdk_loggers`,
which code that must not import ``httpx2`` uses directly. Neither vendor package is imported here.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, NotRequired, Protocol, TypedDict, TypeVar

import httpx2

from crm.errors import ValidationFailed
from providers.base import PROVIDER_TIMEOUT_SECONDS, RetriedAttempt
from providers.sdk_loggers import SDK_LOGGERS as SDK_LOGGERS
from providers.sdk_loggers import quiet_sdk_loggers as quiet_sdk_loggers

#: Per-phase timeouts for every SDK client: 5s connect, and 120s for each read, write and pool
#: wait. They bound one phase, not a whole call; :func:`model_call_deadline` bounds the call.
CONNECT_TIMEOUT_SECONDS: Final[float] = 5.0
REQUEST_TIMEOUT: Final[httpx2.Timeout] = httpx2.Timeout(
    PROVIDER_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS
)

#: The wall-clock budget of one model call, open to end of stream, retries included.
MODEL_CALL_DEADLINE_SECONDS: Final[float] = PROVIDER_TIMEOUT_SECONDS

#: The SDK retry count.
MAX_RETRIES: Final[int] = 2

#: Environment variables that would make an SDK authenticate with a bearer token or address a
#: non-``bedrock-runtime`` endpoint. Their mere presence refuses client construction.
FORBIDDEN_ENV_VARS: Final[tuple[str, ...]] = (
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
)

RETRY_COUNT_HEADER: Final[str] = "x-stainless-retry-count"

#: The Anthropic Bedrock signer deletes ``connection`` from its signing copy and raises
#: ``KeyError`` when it is absent, so that client alone sends it, unsigned.
ANTHROPIC_DEFAULT_HEADERS: Final[dict[str, str]] = {"Connection": "keep-alive"}
OPENAI_PATH: Final[str] = "/openai/v1"

_REGION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]+)+-\d+$")

#: SDK client class names, matched along the MRO so neither vendor package is imported.
_ANTHROPIC_CLIENT_CLASSES: Final[frozenset[str]] = frozenset(
    {"AnthropicBedrock", "AsyncAnthropicBedrock"}
)
_OPENAI_CLIENT_CLASSES: Final[frozenset[str]] = frozenset({"BedrockOpenAI", "AsyncBedrockOpenAI"})

_T = TypeVar("_T")


class CredentialPolicyError(ValidationFailed):
    """An SDK client was about to authenticate or route outside IAM/SigV4 on ``bedrock-runtime``.

    ``source`` names the offending environment variable or argument.
    """


class RecorderNotBoundError(RuntimeError):
    """A transport hook fired outside :func:`recording_attempts`, so its attempt would be lost."""


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One HTTP attempt the SDK made. Holds no header, URL or body, so it is safe to log.

    Attributes:
        retry_count: The SDK's ``x-stainless-retry-count`` for this attempt (0 for the first).
        started_monotonic: The monotonic clock reading when the request was sent.
        status: The response status, or ``None`` when no response arrived.
        error_type: The transport exception's type name when no response arrived, else ``None``.
    """

    retry_count: int
    started_monotonic: float
    status: int | None
    error_type: str | None


@dataclass(slots=True)
class AttemptRecorder:
    """The attempts of one SDK call, in the order the SDK made them."""

    records: list[AttemptRecord] = field(default_factory=list)

    def retried_attempts(self) -> tuple[RetriedAttempt, ...]:
        """Return every attempt except the last, as seam-level :class:`RetriedAttempt` values."""
        return tuple(RetriedAttempt(r.status, r.error_type) for r in self.records[:-1])

    def final_attempt_start(self) -> float | None:
        """Return the monotonic start of the last attempt, or ``None`` if none was made."""
        return self.records[-1].started_monotonic if self.records else None

    def _fill_last(self, *, status: int | None = None, error_type: str | None = None) -> None:
        self.records[-1] = replace(self.records[-1], status=status, error_type=error_type)


_CURRENT_RECORDER: ContextVar[AttemptRecorder | None] = ContextVar(
    "sdk_transport_attempt_recorder", default=None
)


@contextmanager
def recording_attempts() -> Iterator[AttemptRecorder]:
    """Bind a fresh :class:`AttemptRecorder` to the current context for the duration of the block.

    Yields:
        The recorder the transport hooks append to while the block is active.
    """
    recorder = AttemptRecorder()
    token = _CURRENT_RECORDER.set(recorder)
    try:
        yield recorder
    finally:
        _CURRENT_RECORDER.reset(token)


def require_bound_recorder() -> AttemptRecorder:
    """Return the recorder bound by the enclosing :func:`recording_attempts` block.

    Adapters call this immediately before invoking the SDK, so a missing binding fails before any
    network attempt.

    Raises:
        RecorderNotBoundError: When no :func:`recording_attempts` block is active.
    """
    recorder = _CURRENT_RECORDER.get()
    if recorder is None:
        raise RecorderNotBoundError("SDK transport attempt made outside recording_attempts()")
    return recorder


class _RecordingAsyncClient(httpx2.AsyncClient):
    """An ``httpx2.AsyncClient`` that also types attempts which fail before any response."""

    async def send(self, request: httpx2.Request, **kwargs: Any) -> httpx2.Response:
        try:
            return await super().send(request, **kwargs)
        except Exception as exc:
            recorder = _CURRENT_RECORDER.get()
            if recorder is not None and recorder.records and recorder.records[-1].status is None:
                recorder._fill_last(error_type=type(exc).__name__)
            raise


def build_http_client(
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> httpx2.AsyncClient:
    """Return the ``http_client`` for an SDK client: bounded timeout plus attempt-recording hooks.

    Args:
        transport: Test-only ``httpx2.MockTransport`` injection; ``None`` in production.
        clock: The monotonic clock stamped on each attempt; injectable for latency tests.

    Returns:
        A client with :data:`REQUEST_TIMEOUT` and ``trust_env=False`` whose request hook appends an
        :class:`AttemptRecord` to the bound recorder and whose response hook fills in its status. A hook firing with no
        recorder bound raises :class:`RecorderNotBoundError`. It sends no default ``Connection``
        header, so SigV4 never signs one.
    """

    async def on_request(request: httpx2.Request) -> None:
        require_bound_recorder().records.append(
            AttemptRecord(
                retry_count=int(request.headers.get(RETRY_COUNT_HEADER, "0")),
                started_monotonic=clock(),
                status=None,
                error_type=None,
            )
        )

    async def on_response(response: httpx2.Response) -> None:
        require_bound_recorder()._fill_last(status=response.status_code)

    quiet_sdk_loggers()
    client = _RecordingAsyncClient(
        timeout=REQUEST_TIMEOUT,
        transport=transport,
        trust_env=False,
        event_hooks={"request": [on_request], "response": [on_response]},
    )
    client.headers.pop("connection", None)
    return client


class ModelCallDeadlineError(Exception):
    """A model call ran past its :func:`model_call_deadline` budget. Never retried."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"model call exceeded its {seconds:g}s wall-clock deadline")
        self.seconds = seconds


class ModelCallDeadline:
    """The remaining wall-clock budget of one model call; see :func:`model_call_deadline`.

    Each :meth:`run` / :meth:`iterate` step is bounded by the time left. No timeout scope outlives
    the single await it bounds, so none spans the adapter's ``yield``.
    """

    def __init__(self, *, seconds: float, clock: Callable[[], float]) -> None:
        self.seconds = seconds
        self._clock = clock
        self._expires_at = clock() + seconds

    def remaining(self) -> float:
        """Return the seconds left in the budget, never negative."""
        return max(0.0, self._expires_at - self._clock())

    async def run(self, awaitable: Awaitable[_T]) -> _T:
        """Await ``awaitable`` within the remaining budget.

        Raises:
            ModelCallDeadlineError: When the budget is already spent (``awaitable`` is then
                never awaited) or runs out while waiting. The pending await is cancelled.
        """
        remaining = self.remaining()
        if remaining <= 0:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise ModelCallDeadlineError(self.seconds)
        scope = asyncio.timeout(remaining)
        try:
            async with scope:
                return await awaitable
        except TimeoutError as exc:
            if scope.expired():
                raise ModelCallDeadlineError(self.seconds) from exc
            raise

    async def iterate(self, stream: AsyncIterable[_T]) -> AsyncIterator[_T]:
        """Yield ``stream``'s items, bounding each wait for the next item by the remaining budget.

        Raises:
            ModelCallDeadlineError: As :meth:`run`. The caller still owns closing ``stream``.
        """
        iterator = aiter(stream)
        while True:
            try:
                item = await self.run(anext(iterator))
            except StopAsyncIteration:
                return
            yield item


@asynccontextmanager
async def model_call_deadline(
    *,
    seconds: float = MODEL_CALL_DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> AsyncIterator[ModelCallDeadline]:
    """Start one model call's wall-clock budget and yield its :class:`ModelCallDeadline`.

    The budget runs from entry. Wrap the whole call, and route the stream open through
    :meth:`ModelCallDeadline.run` and every chunk through :meth:`ModelCallDeadline.iterate`.

    Args:
        seconds: The budget; :data:`MODEL_CALL_DEADLINE_SECONDS` in production.
        clock: The monotonic clock the remaining budget is read from; injectable for tests.
    """
    yield ModelCallDeadline(seconds=seconds, clock=clock)


def render_deadline_exceeded(exc: ModelCallDeadlineError) -> str:
    """Render a deadline expiry for an ``error`` event. Carries no :mod:`eval.live` retry marker."""
    return f"The model provider call exceeded its {exc.seconds:g}s wall-clock deadline."


def runtime_base_url(region: str) -> str:
    """Return the ``bedrock-runtime`` origin for ``region``.

    Raises:
        CredentialPolicyError: When ``region`` is not a well-formed AWS region name.
    """
    if not _REGION_PATTERN.fullmatch(region):
        raise CredentialPolicyError(f"Invalid AWS region {region!r}.", source="aws_region")
    return f"https://bedrock-runtime.{region}.amazonaws.com"


def assert_no_bearer_or_mantle(base_url: str, *, region: str) -> None:
    """Refuse the SDKs' bearer/base-URL environment fallbacks and any non-runtime endpoint.

    Args:
        base_url: The base URL about to be handed to (or already resolved by) an SDK client.
        region: The region the client signs for; the host must be that region's runtime host.

    Raises:
        CredentialPolicyError: When a :data:`FORBIDDEN_ENV_VARS` entry is present in the
            environment (even empty), or ``base_url`` is not HTTPS on
            ``bedrock-runtime.<region>.amazonaws.com``.
    """
    for name in FORBIDDEN_ENV_VARS:
        if name in os.environ:
            raise CredentialPolicyError(
                f"{name} is set; Bedrock access is IAM/SigV4 on bedrock-runtime only.",
                source=name,
            )
    url = httpx2.URL(base_url)
    if url.scheme != "https" or not url.host.startswith("bedrock-runtime."):
        raise CredentialPolicyError(
            f"Refusing non-bedrock-runtime endpoint {url.host!r}.", source="base_url"
        )
    if url.host != httpx2.URL(runtime_base_url(region)).host:
        raise CredentialPolicyError(
            f"Endpoint {url.host!r} does not match region {region!r}.", source="base_url"
        )


class AnthropicClientKwargs(TypedDict):
    """Constructor arguments for ``anthropic.AsyncAnthropicBedrock``. Has no ``api_key``.

    ``open_telemetry`` is always ``False``: no spans, no trace headers, and none of the
    ``ANTHROPIC_OPEN_TELEMETRY*`` environment variables is read.
    """

    aws_region: str
    base_url: str
    http_client: httpx2.AsyncClient
    max_retries: int
    timeout: httpx2.Timeout
    default_headers: dict[str, str]
    open_telemetry: Literal[False]
    aws_profile: NotRequired[str]


class OpenAIClientKwargs(TypedDict):
    """Constructor arguments for ``openai.AsyncBedrockOpenAI``. Has no ``api_key``."""

    aws_region: str
    base_url: str
    aws_credentials_provider: Callable[[], object]
    http_client: httpx2.AsyncClient
    max_retries: int
    timeout: httpx2.Timeout


def anthropic_client_kwargs(
    *, region: str, http_client: httpx2.AsyncClient, profile: str | None = None
) -> AnthropicClientKwargs:
    """Return the lock-down-checked kwargs for ``AsyncAnthropicBedrock``.

    Credentials come from the SDK's own ``boto3.Session(profile_name=profile, region_name=region)``,
    re-read on every signed request so refreshable (role) credentials stay current. Static keys are
    never passed. :data:`ANTHROPIC_DEFAULT_HEADERS` restores the ``Connection`` header the SDK's
    signer requires; it is excluded from the signature.

    Args:
        region: The region to sign for and address.
        http_client: The client from :func:`build_http_client`.
        profile: An explicit named profile, or ``None`` for the default credential chain.

    Raises:
        CredentialPolicyError: See :func:`assert_no_bearer_or_mantle`.
    """
    base_url = runtime_base_url(region)
    assert_no_bearer_or_mantle(base_url, region=region)
    quiet_sdk_loggers()
    kwargs = AnthropicClientKwargs(
        aws_region=region,
        base_url=base_url,
        http_client=http_client,
        max_retries=MAX_RETRIES,
        timeout=REQUEST_TIMEOUT,
        default_headers=dict(ANTHROPIC_DEFAULT_HEADERS),
        open_telemetry=False,
    )
    if profile is not None:
        kwargs["aws_profile"] = profile
    return kwargs


def openai_client_kwargs(
    *,
    region: str,
    http_client: httpx2.AsyncClient,
    credentials_provider: Callable[[], object],
) -> OpenAIClientKwargs:
    """Return the lock-down-checked kwargs for ``AsyncBedrockOpenAI``.

    The base URL is always the explicit ``bedrock-runtime`` Responses root, so the SDK can never
    default to ``bedrock-mantle``.

    Args:
        region: The region to sign for and address.
        http_client: The client from :func:`build_http_client`.
        credentials_provider: Usually :func:`session_credentials_provider` over the adapter's
            ``boto3`` session; called by the SDK on every signed request.

    Raises:
        CredentialPolicyError: See :func:`assert_no_bearer_or_mantle`.
    """
    base_url = runtime_base_url(region) + OPENAI_PATH
    assert_no_bearer_or_mantle(base_url, region=region)
    quiet_sdk_loggers()
    return OpenAIClientKwargs(
        aws_region=region,
        base_url=base_url,
        aws_credentials_provider=credentials_provider,
        http_client=http_client,
        max_retries=MAX_RETRIES,
        timeout=REQUEST_TIMEOUT,
    )


def assert_runtime_client(client: object, *, region: str) -> None:
    """Assert a constructed SDK client resolved to SigV4 on ``bedrock-runtime`` with no bearer key.

    The Anthropic client authenticates with a bearer token whenever ``api_key is not None``, the
    empty string included, so it must hold ``None``, and its OpenTelemetry traces must be disabled.
    The OpenAI client always holds ``""`` under SigV4, so it must hold a falsy key, and its own
    ``_uses_aws_auth()`` must report SigV4; that predicate is false for a ``bedrock_token_provider``
    client, whose key is also ``""``. A client lacking the predicate is refused.

    Args:
        client: An ``AsyncAnthropicBedrock`` or ``AsyncBedrockOpenAI`` instance (or a sync twin).
        region: The region it was built for.

    Raises:
        CredentialPolicyError: When ``client`` is neither SDK's Bedrock client (``source="client"``),
            holds a bearer ``api_key`` (``source="api_key"``), is an OpenAI client not on SigV4
            (``source="auth_mode"``), is an Anthropic client with traces enabled
            (``source="open_telemetry"``), or its resolved ``base_url`` fails
            :func:`assert_no_bearer_or_mantle`.
    """
    names = {cls.__name__ for cls in type(client).__mro__}
    api_key = getattr(client, "api_key", None)
    is_anthropic = bool(names & _ANTHROPIC_CLIENT_CLASSES)
    if not is_anthropic and not names & _OPENAI_CLIENT_CLASSES:
        raise CredentialPolicyError(
            f"Unrecognised SDK client {type(client).__name__!r}.", source="client"
        )
    has_bearer = api_key is not None if is_anthropic else bool(api_key)
    if has_bearer:
        raise CredentialPolicyError("SDK client resolved a bearer api_key.", source="api_key")
    if is_anthropic:
        traces = getattr(client, "open_telemetry", {}).get("traces", {})
        if traces.get("enabled") is not False:
            raise CredentialPolicyError(
                "SDK client has OpenTelemetry traces enabled.", source="open_telemetry"
            )
    else:
        uses_aws_auth = getattr(client, "_uses_aws_auth", None)
        if not callable(uses_aws_auth) or uses_aws_auth() is not True:
            raise CredentialPolicyError(
                "SDK client is not authenticating with SigV4.", source="auth_mode"
            )
    assert_no_bearer_or_mantle(str(getattr(client, "base_url", "")), region=region)


#: Leads every rendering of a failure the SDK already retried to exhaustion.
EXHAUSTED_PREFIX: Final[str] = "The model provider request failed after SDK retries: "


def render_exhausted_transport_error(exc: BaseException) -> str:
    """Render a failure the vendor SDK already retried to exhaustion, for either SDK.

    Classes are matched by name along the MRO, so neither vendor package is imported. No subclass
    name (``InternalServerError``, ``ServiceUnavailableError``, …) appears, so the result never
    matches an :mod:`eval.live` retry marker.

    Returns:
        ``… APIStatusError(status=<code>).`` for any SDK ``APIStatusError``; ``… APITimeoutError.``
        or ``… APIConnectionError.`` for a transport failure; ``… <TypeName>.`` otherwise.
    """
    names = {cls.__name__ for cls in type(exc).__mro__}
    if "APIStatusError" in names:
        return f"{EXHAUSTED_PREFIX}APIStatusError(status={getattr(exc, 'status_code', None)})."
    for name in ("APITimeoutError", "APIConnectionError"):
        if name in names:
            return f"{EXHAUSTED_PREFIX}{name}."
    return f"{EXHAUSTED_PREFIX}{type(exc).__name__}."


class _CredentialSession(Protocol):
    def get_credentials(self) -> Any: ...


def require_credentials(session: _CredentialSession) -> None:
    """Resolve ``session``'s credentials once, before an SDK call.

    Adapters call this once per ``stream()``. For Anthropic, pass a ``boto3.Session`` built with the
    profile and region the SDK signs with. An empty chain then fails here as a taxonomy error, and
    never reaches the SDK failure shapes :func:`session_credentials_provider` documents.

    Raises:
        CredentialPolicyError: When the session resolves no credentials.
    """
    if not session.get_credentials():
        raise CredentialPolicyError(
            "The boto3 session resolved no AWS credentials.", source="boto3 credential chain"
        )


def credential_policy_cause(exc: BaseException) -> CredentialPolicyError | None:
    """Return the :class:`CredentialPolicyError` in ``exc``'s ``__cause__`` chain, if any.

    Maps the OpenAI SDK's ``OpenAIError`` wrapping of a failed :func:`session_credentials_provider`
    back to the taxonomy by type.
    """
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, CredentialPolicyError):
            return current
        current = current.__cause__
    return None


def session_credentials_provider(session: _CredentialSession) -> Callable[[], object]:
    """Return an ``aws_credentials_provider`` that resolves ``session``'s credentials per call.

    Each call returns botocore frozen credentials, refreshing role credentials when they near
    expiry.

    When the chain is empty, the callable raises :class:`CredentialPolicyError`. A caller of the
    SDK does not see that directly. The shapes it does see:

    * OpenAI: ``openai.OpenAIError`` whose ``__cause__`` is the :class:`CredentialPolicyError`;
      :func:`credential_policy_cause` recovers it.
    * Anthropic (which resolves through its own ``boto3`` session, not this callable): a bare
      ``RuntimeError("could not resolve credentials from session")`` from the request signer,
      outside the SDK's error types.

    :func:`require_credentials` before the call makes both unreachable for an empty chain.

    Raises:
        CredentialPolicyError: From the returned callable, when the session resolves no credentials.
    """

    def provide() -> object:
        credentials = session.get_credentials()
        if credentials is None:
            raise CredentialPolicyError(
                "The boto3 session resolved no AWS credentials.", source="boto3 credential chain"
            )
        frozen: object = credentials.get_frozen_credentials()
        return frozen

    return provide


quiet_sdk_loggers()

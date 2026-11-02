# SPDX-License-Identifier: MIT
"""The Anthropic adapter behind the provider seam.

Every Anthropic model streams through the Bedrock Messages API (``InvokeModelWithResponseStream``
on ``bedrock-runtime``) via the vendor SDK's ``AsyncAnthropicBedrock``, signed with SigV4 from the
botocore credential chain. The client is built only through :mod:`providers.sdk_transport`, which
owns the timeout, the attempt recorder and the credential/endpoint lock-down.

Request body per model, from its :class:`~providers.capabilities.ModelCapability`:

* effort-supported models send ``thinking={"type": "adaptive"}`` and ``output_config.effort``;
* other models send neither ``thinking`` nor ``output_config``.

No request carries a sampling parameter. The one ``extra_body`` key is ``anthropic_version``, pinned
to :data:`BEDROCK_ANTHROPIC_VERSION` rather than the SDK's first-party API version default.

Requests address the model's inference profile, never its bare ``model_id``.

Errors become exactly one ``error`` :class:`~providers.base.ProviderEvent` whose message leads with a
classification prefix:

* an in-band stream exception (HTTP 200, then an exception frame) leads with its PascalCase Bedrock
  type, e.g. ``ModelStreamErrorException:``, which :func:`eval.live.is_retryable_error` retries;
* a failure the SDK already retried to exhaustion is rendered by
  :func:`providers.sdk_transport.render_exhausted_transport_error`, which is not retried again.

Every ``usage`` and ``error`` event carries ``retried_attempts``: each transport attempt the SDK
made before the last one. The ``usage`` event also carries ``latency_ms``, from the successful
attempt's request to the end of the stream.

Each call first checks that the ``boto3`` credential chain resolves, and runs, from open to the end
of the stream, inside :func:`providers.sdk_transport.model_call_deadline`. An empty chain or an
expired deadline is one non-retryable ``error`` event.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Final

import anthropic
import boto3
import httpx2
from anthropic import AsyncAnthropicBedrock, AsyncStream
from anthropic.types import RawMessageStreamEvent

from providers.base import (
    ModelProvider,
    ProviderEvent,
    ResolvedInvocation,
    guard_invocation_family,
)
from providers.prompt_caching import breakpoint_index
from providers.sdk_transport import (
    MODEL_CALL_DEADLINE_SECONDS,
    CredentialPolicyError,
    ModelCallDeadlineError,
    anthropic_client_kwargs,
    assert_runtime_client,
    build_http_client,
    model_call_deadline,
    recording_attempts,
    render_deadline_exceeded,
    render_exhausted_transport_error,
    require_bound_recorder,
    require_credentials,
)

#: The Bedrock Messages API version sent as the body's ``anthropic_version``.
BEDROCK_ANTHROPIC_VERSION: Final[str] = "bedrock-2023-05-31"

#: Max output tokens for every Anthropic request.
MAX_OUTPUT_TOKENS: Final[int] = 8192

#: The ``status_code`` the SDK reports for an exception frame inside a 200 stream.
_IN_BAND_STATUS: Final[int] = 200


class AnthropicAdapter(ModelProvider):
    """Provider adapter for the Anthropic family on ``AsyncAnthropicBedrock``.

    One SDK client is built per running event loop, since an ``httpx2`` connection pool cannot
    outlive the loop it was opened on. No vendor or Bedrock API key is read or held.
    """

    def __init__(
        self,
        *,
        region_name: str,
        session: Any = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        deadline_seconds: float = MODEL_CALL_DEADLINE_SECONDS,
    ) -> None:
        """Construct the adapter and its first SDK client for ``region_name``.

        Args:
            region_name: The region whose ``bedrock-runtime`` endpoint is addressed and signed for.
            session: The ``boto3`` session whose credentials are checked before each call;
                defaults to the default-chain session the SDK itself signs with.
            transport: Test-only ``httpx2.MockTransport``; ``None`` in production.
            clock: Monotonic clock for attempt timestamps and stream end; injectable for tests.
            deadline_seconds: The whole-call wall-clock budget; injectable for tests.

        Raises:
            CredentialPolicyError: When a Bedrock bearer or base-URL variable is set, or the region
                is malformed.
        """
        self._region_name = region_name
        self._transport = transport
        self._clock = clock
        self._deadline_seconds = deadline_seconds
        self._session = session if session is not None else boto3.Session(region_name=region_name)
        self._sdk = self._new_client()
        self._sdk_loop: asyncio.AbstractEventLoop | None = None

    def _new_client(self) -> AsyncAnthropicBedrock:
        client = AsyncAnthropicBedrock(
            **anthropic_client_kwargs(
                region=self._region_name,
                http_client=build_http_client(transport=self._transport, clock=self._clock),
            )
        )
        assert_runtime_client(client, region=self._region_name)
        return client

    def _client(self) -> AsyncAnthropicBedrock:
        """Return the SDK client bound to the running event loop, building one if needed."""
        loop = asyncio.get_running_loop()
        if self._sdk_loop is not None and self._sdk_loop is not loop:
            self._sdk = self._new_client()
        self._sdk_loop = loop
        return self._sdk

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        """Stream one turn through the Messages API.

        Args:
            invocation: The validated model and tuning to invoke.
            messages: The conversation in the seam's message shape (``role`` + ``content``, plus
                ``tool_calls`` on assistant turns and ``tool_results`` on ``tool`` turns).
            tools: Tool definitions carrying ``name``, ``description`` and ``input_schema``.

        Yields:
            Normalised :class:`ProviderEvent` values, ending in exactly one ``completed`` or
            ``error`` event.

        Raises:
            ValueError: When ``invocation`` is for a family other than ``anthropic``, before
                any request is sent.
        """
        guard_invocation_family("anthropic", invocation)
        client = self._client()
        request = build_request(invocation, messages, tools)
        async with model_call_deadline(
            seconds=self._deadline_seconds, clock=self._clock
        ) as deadline:
            try:
                require_credentials(self._session)
            except CredentialPolicyError as exc:
                yield ProviderEvent(
                    type="error", error_message=f"{type(exc).__name__}: {exc.message}"
                )
                return

            sdk_stream: AsyncStream[RawMessageStreamEvent] | None = None
            failure: str | None = None
            with recording_attempts() as recorder:
                require_bound_recorder()
                try:
                    sdk_stream = await deadline.run(
                        client.messages.create(
                            **request,
                            stream=True,
                            extra_body={"anthropic_version": BEDROCK_ANTHROPIC_VERSION},
                        )
                    )
                except ModelCallDeadlineError as exc:
                    failure = render_deadline_exceeded(exc)
                except Exception as exc:  # noqa: BLE001 - normalised into the seam's error event
                    failure = error_message(exc)
            if sdk_stream is None:
                yield ProviderEvent(
                    type="error",
                    error_message=failure or "The model provider request failed.",
                    retried_attempts=recorder.retried_attempts(),
                )
                return
            attempt_start = recorder.final_attempt_start()

            state = _StreamState()
            events = deadline.iterate(sdk_stream)
            try:
                while True:
                    try:
                        raw = await anext(events)
                    except StopAsyncIteration:
                        break
                    except ModelCallDeadlineError as exc:
                        yield ProviderEvent(
                            type="error",
                            error_message=render_deadline_exceeded(exc),
                            retried_attempts=recorder.retried_attempts(),
                        )
                        return
                    except (anthropic.APIError, httpx2.TransportError) as exc:
                        yield ProviderEvent(
                            type="error",
                            error_message=error_message(exc),
                            retried_attempts=recorder.retried_attempts(),
                        )
                        return
                    for event in state.consume(raw):
                        yield event
            finally:
                await sdk_stream.close()

            latency_ms = (
                (self._clock() - attempt_start) * 1000.0 if attempt_start is not None else None
            )
            yield ProviderEvent(
                type="usage",
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
                cache_read_tokens=state.cache_read_tokens,
                cache_write_tokens=state.cache_write_tokens,
                latency_ms=latency_ms,
                retried_attempts=recorder.retried_attempts(),
            )
            yield ProviderEvent(
                type="completed",
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
                awaiting_tools=state.stop_reason == "tool_use",
            )


def build_request(
    invocation: ResolvedInvocation,
    messages: list[dict[str, object]],
    tools: list[dict[str, object]],
) -> dict[str, Any]:
    """Return the ``messages.create`` keyword arguments for one invocation.

    ``stream`` is not included; the caller adds it.
    """
    cache_at = breakpoint_index(invocation.model.model_id, messages, tools)
    system_prompt, conversation = _split_system(messages, cache_at=cache_at)
    request: dict[str, Any] = {
        "model": invocation.model.inference_profile,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [_to_messages_api_turn(message) for message in conversation],
    }
    if system_prompt is not None:
        request["system"] = system_prompt
    if tools:
        request["tools"] = [
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool.get("input_schema", {}),
            }
            for tool in tools
        ]
    if invocation.model.effort.supported:
        request["thinking"] = {"type": "adaptive"}
        if invocation.effort is not None:
            request["output_config"] = {"effort": invocation.effort}
    return request


def error_message(exc: BaseException) -> str:
    """Render a user-safe, classification-prefixed error message for an SDK failure.

    Returns:
        ``<PascalCaseBedrockType>: <message>`` for an in-band stream exception;
        :func:`~providers.sdk_transport.render_exhausted_transport_error` for any other SDK
        ``APIStatusError``, ``APITimeoutError`` or ``APIConnectionError``;
        ``<ExceptionTypeName>: …`` for anything else. No request content is included.
    """
    if isinstance(exc, anthropic.APIStatusError):
        in_band = _in_band_error(exc)
        if in_band is not None:
            return in_band
    if isinstance(exc, anthropic.APIStatusError | anthropic.APIConnectionError):
        return render_exhausted_transport_error(exc)
    return f"{type(exc).__name__}: The model provider request failed."


def _in_band_error(exc: anthropic.APIStatusError) -> str | None:
    """Return the prefixed message for an exception frame inside a 200 stream, else ``None``."""
    if exc.status_code != _IN_BAND_STATUS or not isinstance(exc.body, dict):
        return None
    error = exc.body.get("error")
    if not isinstance(error, dict):
        return None
    error_type = error.get("type")
    if not isinstance(error_type, str) or not error_type:
        return None
    message = error.get("message")
    detail = message if isinstance(message, str) and message else "Model stream error."
    return f"{error_type[0].upper()}{error_type[1:]}: {detail}"


@dataclass(slots=True)
class _StreamState:
    """Accumulates one Messages stream into seam events and usage totals."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str | None = None
    tool_blocks: dict[int, tuple[str, str, list[str]]] = field(default_factory=dict)

    def consume(self, event: RawMessageStreamEvent) -> Iterator[ProviderEvent]:
        """Yield the seam events one SDK stream event produces, updating the totals."""
        if event.type == "message_start":
            usage = event.message.usage
            self.input_tokens = usage.input_tokens or 0
            self.cache_read_tokens = usage.cache_read_input_tokens or 0
            self.cache_write_tokens = usage.cache_creation_input_tokens or 0
        elif event.type == "content_block_start":
            block = event.content_block
            if block.type == "tool_use":
                self.tool_blocks[event.index] = (block.id, block.name, [])
        elif event.type == "content_block_delta":
            delta = event.delta
            if delta.type == "text_delta":
                yield ProviderEvent(type="text_delta", text=delta.text)
            elif delta.type == "input_json_delta" and event.index in self.tool_blocks:
                self.tool_blocks[event.index][2].append(delta.partial_json)
        elif event.type == "content_block_stop":
            tool = self.tool_blocks.pop(event.index, None)
            if tool is not None:
                tool_id, name, parts = tool
                arguments = "".join(parts)
                yield ProviderEvent(
                    type="tool_call",
                    tool_name=name,
                    tool_input=json.loads(arguments) if arguments else {},
                    tool_id=tool_id,
                )
        elif event.type == "message_delta":
            if event.delta.stop_reason is not None:
                self.stop_reason = event.delta.stop_reason
            self.output_tokens = event.usage.output_tokens


def _split_system(
    messages: list[dict[str, object]],
    *,
    cache_at: int | None = None,
) -> tuple[str | list[dict[str, Any]] | None, list[dict[str, object]]]:
    """Lift ``system``-role messages into the top-level system prompt, in order.

    The Messages API rejects a ``system`` role inside ``messages``; ``user``/``assistant``/``tool``
    turns pass through untouched. With ``cache_at`` (a message index from
    :func:`providers.prompt_caching.breakpoint_index`) the prompt is a list of text blocks and that
    message's block carries ``cache_control``; otherwise the parts are joined into one string.
    """
    blocks: list[dict[str, Any]] = []
    conversation: list[dict[str, object]] = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") == "system" and isinstance(content, str) and content:
            block: dict[str, Any] = {"type": "text", "text": content}
            if index == cache_at:
                block["cache_control"] = {"type": "ephemeral"}
            blocks.append(block)
        else:
            conversation.append(message)
    if not blocks:
        return None, conversation
    if cache_at is None:
        return "\n\n".join(block["text"] for block in blocks), conversation
    return blocks, conversation


def _to_messages_api_turn(message: dict[str, object]) -> dict[str, Any]:
    """Translate one seam message into a Messages API turn.

    An ``assistant`` turn with ``tool_calls`` becomes ``tool_use`` blocks, and a ``tool`` turn with
    ``tool_results`` becomes a ``user`` turn of ``tool_result`` blocks.
    """
    tool_calls = message.get("tool_calls")
    if message.get("role") == "assistant" and isinstance(tool_calls, list):
        content: list[dict[str, Any]] = []
        text = message.get("content")
        if isinstance(text, str) and text:
            content.append({"type": "text", "text": text})
        for call in tool_calls:
            content.append(
                {"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["input"]}
            )
        return {"role": "assistant", "content": content}
    tool_results = message.get("tool_results")
    if message.get("role") == "tool" and isinstance(tool_results, list):
        return {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": result["id"], "content": result["output"]}
                for result in tool_results
            ],
        }
    return {"role": message["role"], "content": message["content"]}

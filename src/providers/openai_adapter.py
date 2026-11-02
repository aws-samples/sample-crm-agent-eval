# SPDX-License-Identifier: MIT
"""The OpenAI adapter behind the provider seam.

Routes the OpenAI models through ``openai.AsyncBedrockOpenAI.responses.create(stream=True)`` on
``POST https://bedrock-runtime.{region}.amazonaws.com/openai/v1/responses``, matching every model's
``api_path: responses`` entry in ``config/model_capabilities.yaml``. The client is assembled only by
:mod:`providers.sdk_transport` (explicit ``bedrock-runtime`` base URL, SigV4 from the adapter's
``boto3`` session, no ``api_key``, per-phase timeouts, SDK retries at ``max_retries=2``) and is cached
per running event loop. Each call runs under :func:`~providers.sdk_transport.model_call_deadline`,
a 120s wall-clock budget covering the open, every SDK retry and the stream.

Requests address the resolved model's inference profile in the body's ``model`` field and set
``reasoning.effort``; no sampling parameter is sent.

The Responses stream is normalised into the same :class:`~providers.base.ProviderEvent` stream the
Anthropic adapter emits, ending in exactly one ``completed`` or ``error`` event. Error messages are
classified for :func:`eval.live.is_retryable_error`:

* an in-band server fault — the SDK's raised ``openai.APIError`` at an SSE ``error`` frame, or a
  yielded ``response.failed`` — leads with its code (``server_error: …``) and is retryable;
* a transport failure that reaches the adapter has already exhausted the SDK's own retries, so it is
  rendered by base type and status only (``APIStatusError(status=503)``, ``APIConnectionError``) and
  matches no retry marker.

The ``usage`` event carries ``retried_attempts`` and ``latency_ms`` from the transport recorder:
latency runs from the successful attempt's request to the end of the stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any, Final

import boto3
import httpx2
import openai
from openai import AsyncBedrockOpenAI

from providers.base import (
    ModelProvider,
    ProviderEvent,
    ResolvedInvocation,
    guard_invocation_family,
)
from providers.prompt_caching import OPENAI_PROMPT_CACHE_OPTIONS, breakpoint_index
from providers.sdk_transport import (
    MODEL_CALL_DEADLINE_SECONDS,
    AttemptRecorder,
    CredentialPolicyError,
    ModelCallDeadlineError,
    assert_runtime_client,
    build_http_client,
    credential_policy_cause,
    model_call_deadline,
    openai_client_kwargs,
    recording_attempts,
    render_deadline_exceeded,
    render_exhausted_transport_error,
    require_bound_recorder,
    require_credentials,
    session_credentials_provider,
)

#: The ``error`` event message for a stream that produced no recognisable Responses API event.
_EMPTY_STREAM_MESSAGE: Final[str] = (
    "The model provider returned a stream containing no recognisable Responses API events."
)

#: The fallback message for an in-band failure that carries no message of its own.
_IN_BAND_FALLBACK_MESSAGE: Final[str] = "Model stream error."


class OpenAIAdapter(ModelProvider):
    """Provider adapter for the OpenAI family on ``AsyncBedrockOpenAI``.

    Access is by IAM/SigV4 and inference profile only; the adapter never reads or holds a vendor API
    key.
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
        """Build the adapter and its first SDK client.

        Args:
            region_name: The region whose ``bedrock-runtime`` endpoint is addressed and signed for.
            session: A ``boto3.Session``-like object whose ``get_credentials()`` is resolved on every
                signed request; defaults to ``boto3.Session(region_name=region_name)``.
            transport: Test-only ``httpx2.MockTransport``; ``None`` in production.
            clock: The monotonic clock used for attempt timestamps and ``latency_ms``.
            deadline_seconds: The wall-clock budget of one model call; tests shorten it.

        Raises:
            providers.sdk_transport.CredentialPolicyError: When a bearer or base-URL fallback
                variable is set, or the client would not resolve to ``bedrock-runtime``.
        """
        self._region_name = region_name
        self._transport = transport
        self._clock = clock
        self._deadline_seconds = deadline_seconds
        self._session = session or boto3.Session(region_name=region_name)
        self._credentials = session_credentials_provider(self._session)
        self._sdk = self._new_client()
        self._sdk_loop: asyncio.AbstractEventLoop | None = None

    def _new_client(self) -> AsyncBedrockOpenAI:
        client = AsyncBedrockOpenAI(
            **openai_client_kwargs(
                region=self._region_name,
                http_client=build_http_client(transport=self._transport, clock=self._clock),
                credentials_provider=self._credentials,
            )
        )
        assert_runtime_client(client, region=self._region_name)
        return client

    def _client(self) -> AsyncBedrockOpenAI:
        """Return the SDK client bound to the running event loop, building a new one on a new loop."""
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
        """Stream one turn through the Responses API.

        Args:
            invocation: The validated model and tuning to invoke.
            messages: The conversation so far in the seam's message shape (``role``/``content``,
                plus ``tool_calls`` / ``tool_results`` for a tool round).
            tools: Tool definitions in the seam's shape (``name``, ``description``, ``input_schema``).

        Yields:
            Normalised :class:`ProviderEvent` values, ending in exactly one ``completed`` or
            ``error`` event. The terminal ``usage`` or ``error`` event carries the transport attempts
            the SDK retried: every recorded attempt except the last.

        Raises:
            ValueError: When ``invocation`` is for another family; before any request is made.
            KeyError: When a seam message lacks ``role`` or ``content`` (or a tool call/result lacks
                its keys); before any request is made.
        """
        guard_invocation_family("openai", invocation)
        body = self._build_responses_body(invocation, messages, tools)
        client = self._client()
        recorder = AttemptRecorder()
        async with model_call_deadline(
            seconds=self._deadline_seconds, clock=self._clock
        ) as deadline:
            try:
                require_credentials(self._session)
                with recording_attempts() as recorder:
                    require_bound_recorder()
                    response_stream = await deadline.run(client.responses.create(**body))
            except Exception as exc:  # noqa: BLE001 - normalised into the seam's error event
                yield ProviderEvent(
                    type="error",
                    error_message=provider_error_message(exc),
                    retried_attempts=recorder.retried_attempts(),
                )
                return
            final_attempt_start = recorder.final_attempt_start()

            terminal_yielded = False
            try:
                async with contextlib.aclosing(
                    self._consume(deadline.iterate(response_stream))
                ) as events:
                    async for event in events:
                        if event.type == "usage":
                            latency_ms = (
                                (self._clock() - final_attempt_start) * 1000.0
                                if final_attempt_start is not None
                                else None
                            )
                            event = dataclasses.replace(
                                event,
                                latency_ms=latency_ms,
                                retried_attempts=recorder.retried_attempts(),
                            )
                        elif event.type == "error":
                            event = dataclasses.replace(
                                event, retried_attempts=recorder.retried_attempts()
                            )
                        terminal_yielded = event.type in ("error", "completed")
                        yield event
            except ModelCallDeadlineError as exc:
                terminal_yielded = True
                yield ProviderEvent(
                    type="error",
                    error_message=render_deadline_exceeded(exc),
                    retried_attempts=recorder.retried_attempts(),
                )
            finally:
                await _close_stream(response_stream, suppress=terminal_yielded)

    @staticmethod
    def _to_responses_items(message: dict[str, object]) -> list[dict[str, Any]]:
        """Translate one seam message into Responses API ``input`` items.

        An ``assistant`` message with ``tool_calls`` (``{id, name, input}``) expands to
        ``function_call`` items and a ``tool`` message with ``tool_results`` (``{id, output}``) to
        ``function_call_output`` items, keyed by ``call_id``. Plain turns pass through.
        """
        tool_calls = message.get("tool_calls")
        if message.get("role") == "assistant" and isinstance(tool_calls, list):
            items: list[dict[str, Any]] = []
            text = message.get("content")
            if isinstance(text, str) and text:
                items.append({"role": "assistant", "content": text})
            for call in tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": call["name"],
                        "arguments": json.dumps(call["input"]),
                    }
                )
            return items
        tool_results = message.get("tool_results")
        if message.get("role") == "tool" and isinstance(tool_results, list):
            return [
                {
                    "type": "function_call_output",
                    "call_id": result["id"],
                    "output": result["output"],
                }
                for result in tool_results
            ]
        return [{"role": message["role"], "content": message["content"]}]

    def _build_responses_body(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> dict[str, Any]:
        """Return the ``responses.create`` keyword arguments, which are also the request body.

        ``prompt_cache_options`` is always implicit, so the platform's automatic breakpoint on the
        latest message applies beside the one :func:`providers.prompt_caching.breakpoint_index`
        places.
        """
        cache_at = breakpoint_index(invocation.model.model_id, messages, tools)
        items: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            if index == cache_at:
                items.append(
                    {
                        "role": message["role"],
                        "content": [
                            {
                                "type": "input_text",
                                "text": message["content"],
                                "prompt_cache_breakpoint": {"mode": "explicit"},
                            }
                        ],
                    }
                )
            else:
                items.extend(self._to_responses_items(message))
        body: dict[str, Any] = {
            "model": invocation.model.inference_profile,
            "input": items,
            "stream": True,
            "prompt_cache_options": dict(OPENAI_PROMPT_CACHE_OPTIONS),
        }
        if invocation.effort is not None:
            body["reasoning"] = {"effort": invocation.effort}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                }
                for tool in tools
            ]
        return body

    @staticmethod
    async def _consume(response_stream: Any) -> AsyncGenerator[ProviderEvent]:
        """Normalise the SDK's typed Responses events into :class:`ProviderEvent` values.

        A stream is recognisable when it carried a text delta, a completed ``function_call`` item,
        or ``response.completed``; one that carried none ends in an ``error`` event. Any exception
        raised while iterating (an in-band ``error`` frame, a malformed frame, malformed tool
        arguments, a dropped connection) ends the stream in one ``error`` event whose message holds
        no frame content.
        """
        usage: dict[str, int] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        saw_tool_call = False
        recognised = False
        try:
            async for event in response_stream:
                if event.type == "response.output_text.delta":
                    recognised = True
                    yield ProviderEvent(type="text_delta", text=event.delta)
                elif (
                    event.type == "response.output_item.done" and event.item.type == "function_call"
                ):
                    item = event.item
                    tool_input = json.loads(item.arguments) if item.arguments else {}
                    recognised = True
                    saw_tool_call = True
                    yield ProviderEvent(
                        type="tool_call",
                        tool_name=item.name,
                        tool_input=tool_input,
                        tool_id=item.call_id,
                    )
                elif event.type == "response.completed":
                    recognised = True
                    usage.update(_usage_counts(getattr(event.response, "usage", None)))
                elif event.type == "error":
                    yield ProviderEvent(
                        type="error",
                        error_message=_in_band_message(
                            getattr(event, "code", None), getattr(event, "message", None)
                        ),
                    )
                    return
                elif event.type == "response.failed":
                    failure = event.response.error
                    code = failure.code if failure is not None else None
                    message = failure.message if failure is not None else None
                    yield ProviderEvent(type="error", error_message=_in_band_message(code, message))
                    return
        except ModelCallDeadlineError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalised into the seam's error event
            yield ProviderEvent(type="error", error_message=mid_stream_error_message(exc))
            return

        if not recognised:
            yield ProviderEvent(type="error", error_message=_EMPTY_STREAM_MESSAGE)
            return
        yield ProviderEvent(
            type="usage",
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            cache_read_tokens=usage["cache_read"],
            cache_write_tokens=usage["cache_write"],
        )
        yield ProviderEvent(
            type="completed",
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            awaiting_tools=saw_tool_call,
        )


def _usage_counts(reported: Any) -> dict[str, int]:
    """Read the token counts from a ``response.completed`` usage object; absent fields count 0.

    Cache reads and writes are both nested under ``input_tokens_details``.
    """
    details = getattr(reported, "input_tokens_details", None)
    return {
        "input": getattr(reported, "input_tokens", None) or 0,
        "output": getattr(reported, "output_tokens", None) or 0,
        "cache_read": getattr(details, "cached_tokens", None) or 0,
        "cache_write": getattr(details, "cache_write_tokens", None) or 0,
    }


def _in_band_message(code: object, message: object) -> str:
    """Render an in-band Responses failure as ``<code>: <message>`` (``server_error: …``)."""
    text = message if isinstance(message, str) and message else _IN_BAND_FALLBACK_MESSAGE
    return f"{code}: {text}" if code else text


def mid_stream_error_message(exc: BaseException) -> str:
    """Render a failure raised while reading a stream that opened with a success status.

    A dropped connection or read timeout here is one request the SDK did not retry, so it is
    rendered ``<ExceptionTypeName>: The model provider request failed.``, never as exhausted;
    it carries no retry marker. Every other exception renders as
    :func:`provider_error_message` does.
    """
    if isinstance(exc, (openai.APIConnectionError, httpx2.TransportError)):
        return f"{type(exc).__name__}: The model provider request failed."
    return provider_error_message(exc)


def provider_error_message(exc: BaseException) -> str:
    """Render an exception reaching the adapter as a classifiable, content-free message.

    * exact ``openai.APIError`` (the SDK's raise at an in-band SSE ``error`` frame) →
      ``<code>: <message>``, e.g. ``server_error: …``;
    * ``openai.APIStatusError`` / ``openai.APIConnectionError`` (the SDK's retries are exhausted) →
      :func:`~providers.sdk_transport.render_exhausted_transport_error`;
    * a :class:`~providers.sdk_transport.ModelCallDeadlineError` → the deadline rendering;
    * a credential failure, raised directly or as the ``__cause__`` of an ``openai.OpenAIError`` →
      ``… CredentialPolicyError.``;
    * anything else → its type name only.

    No response body, header or frame content is included beyond an in-band error's own message.
    """
    if isinstance(exc, ModelCallDeadlineError):
        return render_deadline_exceeded(exc)
    if isinstance(exc, CredentialPolicyError) or credential_policy_cause(exc) is not None:
        return "The model provider request failed: CredentialPolicyError."
    if type(exc) is openai.APIError and exc.code:
        return _in_band_message(exc.code, exc.message)
    if isinstance(exc, (openai.APIStatusError, openai.APIConnectionError)):
        return render_exhausted_transport_error(exc)
    return f"The model provider request failed: {type(exc).__name__}."


async def _close_stream(response_stream: Any, *, suppress: bool) -> None:
    """Close the SDK stream; once a terminal event was yielded, a failing close is not re-raised."""
    try:
        await response_stream.close()
    except Exception:
        if not suppress:
            raise

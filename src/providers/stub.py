# SPDX-License-Identifier: MIT
"""The zero-cost stub provider.

``StubProvider`` is the default eval mode. It needs no credentials, makes no network call, and
never imports a vendor SDK. Its output is derived from a SHA-256 digest of the resolved model id,
effort and messages, so the same inputs always yield the same stream.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator

from providers.base import ModelProvider, ProviderEvent, ResolvedInvocation

#: Fixed per-turn token counts the stub reports.
_STUB_INPUT_TOKENS = 128
_STUB_OUTPUT_TOKENS = 32


def _digest(invocation: ResolvedInvocation, messages: list[dict[str, object]]) -> bytes:
    """Compute a deterministic digest of one invocation's model and message content."""
    payload = {
        "model_id": invocation.model.model_id,
        "effort": invocation.effort,
        "messages": messages,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).digest()


class StubProvider(ModelProvider):
    """A deterministic, zero-cost, credential-free stand-in for a real model provider."""

    async def stream(
        self,
        invocation: ResolvedInvocation,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AsyncIterator[ProviderEvent]:
        """Yield a deterministic simulated turn with no network call.

        The digest selects whether the round calls the first available tool or answers with text;
        both branches end with a ``usage`` event then a ``completed`` event. The tool branch fires
        only while no tool result is in ``messages`` and ends ``completed(awaiting_tools=True)``, so
        a tool-using turn ends in two rounds.
        """
        digest = _digest(invocation, messages)
        has_tool_result = any(
            isinstance(message, dict)
            and (message.get("role") == "tool" or "tool_results" in message)
            for message in messages
        )
        use_tool = bool(tools) and digest[0] % 2 == 0 and not has_tool_result

        if use_tool:
            tool = tools[0]
            tool_name = str(tool.get("name", "unknown_tool"))
            yield ProviderEvent(type="tool_call", tool_name=tool_name, tool_input={})
            # A stub fingerprint the agent_stream smoke check reads; the turn driver ignores
            # provider tool_result events.
            yield ProviderEvent(
                type="tool_result",
                tool_name=tool_name,
                tool_output={"stub": True, "digest": digest.hex()[:16]},
            )
            yield ProviderEvent(
                type="usage",
                input_tokens=_STUB_INPUT_TOKENS,
                output_tokens=_STUB_OUTPUT_TOKENS,
                cache_read_tokens=0,
                cache_write_tokens=0,
            )
            yield ProviderEvent(type="completed", awaiting_tools=True)
            return

        text = f"[stub:{invocation.model.model_id}] response digest={digest.hex()[:16]}"
        yield ProviderEvent(type="text_delta", text=text)
        yield ProviderEvent(
            type="usage",
            input_tokens=_STUB_INPUT_TOKENS,
            output_tokens=_STUB_OUTPUT_TOKENS,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        yield ProviderEvent(
            type="completed",
            input_tokens=_STUB_INPUT_TOKENS,
            output_tokens=_STUB_OUTPUT_TOKENS,
        )

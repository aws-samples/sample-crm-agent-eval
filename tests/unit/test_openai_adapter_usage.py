# SPDX-License-Identifier: MIT
"""The OpenAI adapter reads cache tokens from `usage.input_tokens_details`.

Drives the real `OpenAIAdapter` over the real `openai` SDK and an `httpx2.MockTransport` replaying
one `response.completed` whose usage nests `cached_tokens` and `cache_write_tokens`. No Bedrock call
and no real credential.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from botocore.credentials import Credentials

from providers.base import ProviderEvent, ResolvedInvocation
from providers.capabilities import load_capability_matrix
from providers.openai_adapter import OpenAIAdapter

_CACHE_WRITE = 321
_CACHE_READ = 12


class _StaticSession:
    def get_credentials(self) -> Credentials:
        return Credentials("test", "test")


@pytest.fixture(autouse=True)
def _offline_aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_BEDROCK_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    yield


def _openai_invocation() -> ResolvedInvocation:
    model = load_capability_matrix().model_for("openai", "supervisor")
    return ResolvedInvocation(model=model, effort=None)


def _completed_with_nested_cache_usage() -> bytes:
    completed: dict[str, Any] = {
        "type": "response.completed",
        "sequence_number": 1,
        "response": {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "output": [],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_tokens_details": {
                    "cached_tokens": _CACHE_READ,
                    "cache_write_tokens": _CACHE_WRITE,
                },
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    }
    return f"data: {json.dumps(completed)}\n\ndata: [DONE]\n\n".encode()


def _stream_events(adapter: OpenAIAdapter) -> list[ProviderEvent]:
    async def _collect() -> list[ProviderEvent]:
        return [event async for event in adapter.stream(_openai_invocation(), [], [])]

    return asyncio.run(_collect())


def test_cache_write_tokens_are_read_from_the_nested_input_tokens_details() -> None:
    """The normalised `usage` event carries the nested `cache_write_tokens` and `cached_tokens`."""
    body = _completed_with_nested_cache_usage()
    adapter = OpenAIAdapter(
        region_name="us-east-1",
        session=_StaticSession(),
        transport=httpx2.MockTransport(
            lambda _r: httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )
        ),
    )
    events = _stream_events(adapter)
    usage_events = [event for event in events if event.type == "usage"]
    assert len(usage_events) == 1, [event.type for event in events]
    usage = usage_events[0]
    assert usage.cache_write_tokens == _CACHE_WRITE
    assert usage.cache_read_tokens == _CACHE_READ
    assert usage.input_tokens == 100

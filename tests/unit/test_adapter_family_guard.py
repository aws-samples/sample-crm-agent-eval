# SPDX-License-Identifier: MIT
"""Each live adapter refuses an invocation for another family, before any client call.

Runs against the loaded capability matrix and the real vendor SDK clients over a recording
``httpx2.MockTransport``; no live model call and no AWS call.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2
import pytest
from botocore.credentials import Credentials

from providers.anthropic_adapter import AnthropicAdapter
from providers.base import ResolvedInvocation, guard_invocation_family
from providers.capabilities import load_capability_matrix
from providers.openai_adapter import OpenAIAdapter

_REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _no_refused_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the environment variables the SDK client builders refuse."""
    for name in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_BEDROCK_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def _resolved_for(family: str) -> ResolvedInvocation:
    """Return a minimal ResolvedInvocation for `family`'s supervisor model."""
    model = load_capability_matrix().model_for(family, "supervisor")  # type: ignore[arg-type]
    return ResolvedInvocation(model=model, effort=None)


async def _first_event(adapter: Any, invocation: ResolvedInvocation) -> Any:
    """Return the first event of `adapter.stream`, which runs the generator body and its guard."""
    return await adapter.stream(invocation, [], []).__anext__()


def _recording_transport() -> tuple[httpx2.MockTransport, list[httpx2.Request]]:
    """Return a mock transport that records every request it receives, and the record list."""
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(500)

    return httpx2.MockTransport(handler), requests


class _StaticSession:
    """A boto3-session stand-in with fixed fake credentials."""

    def get_credentials(self) -> Credentials:
        return Credentials("AKIDEXAMPLE", "fake-secret-key")


def test_guard_invocation_family_passes_on_a_match_and_raises_on_a_mismatch() -> None:
    """The guard is a no-op on a matching family and raises ValueError naming both on a mismatch."""
    guard_invocation_family("openai", _resolved_for("openai"))
    guard_invocation_family("anthropic", _resolved_for("anthropic"))
    with pytest.raises(ValueError) as exc_info:
        guard_invocation_family("anthropic", _resolved_for("openai"))
    assert "anthropic" in str(exc_info.value)
    assert "openai" in str(exc_info.value)


def test_anthropic_adapter_refuses_an_openai_invocation_without_calling_its_client() -> None:
    """`AnthropicAdapter.stream` raises on an OpenAI invocation without sending a request."""
    transport, requests = _recording_transport()
    adapter = AnthropicAdapter(region_name=_REGION, transport=transport)
    with pytest.raises(ValueError, match="anthropic adapter received a openai invocation"):
        asyncio.run(_first_event(adapter, _resolved_for("openai")))
    assert requests == []


def test_openai_adapter_refuses_an_anthropic_invocation_without_calling_its_client() -> None:
    """`OpenAIAdapter.stream` raises on an Anthropic invocation without sending a request."""
    transport, requests = _recording_transport()
    adapter = OpenAIAdapter(region_name=_REGION, session=_StaticSession(), transport=transport)
    with pytest.raises(ValueError, match="openai adapter received a anthropic invocation"):
        asyncio.run(_first_event(adapter, _resolved_for("anthropic")))
    assert requests == []


@pytest.mark.parametrize("adapter_class", [AnthropicAdapter, OpenAIAdapter])
def test_each_adapter_requires_an_explicit_region_name(adapter_class: type[Any]) -> None:
    """Constructing either adapter without `region_name` raises `TypeError`, not a default region."""
    with pytest.raises(TypeError, match="region_name"):
        adapter_class(session=_StaticSession())

# SPDX-License-Identifier: MIT
"""The one place a concrete live adapter is selected for a family.

Callers ask for a :data:`~providers.capabilities.Family` and receive a
:class:`~providers.base.ModelProvider`. Each adapter calls its models through a vendor SDK's
Bedrock client assembled by :mod:`providers.sdk_transport`, signed with SigV4 over IAM
credentials; no API key is accepted. Each adapter module, and so its SDK, is imported only inside
its builder.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from providers.base import ModelProvider
from providers.capabilities import Family


def build_provider_for_family(family: Family, *, region_name: str) -> ModelProvider:
    """Return the live adapter for ``family``, targeting ``region_name``.

    Args:
        family: The provider family to build an adapter for.
        region_name: The AWS region the adapter's SDK client targets on ``bedrock-runtime``,
            from resolved configuration; there is no default.

    Returns:
        A live :class:`~providers.base.ModelProvider` for ``family``.

    Raises:
        ValueError: When ``family`` names no adapter. There is no fallback to another family or
            to the stub.
    """
    builder = _ADAPTERS_BY_FAMILY.get(family)
    if builder is None:
        raise ValueError(
            f"no live adapter is registered for family {family!r}; "
            f"registered families are {sorted(_ADAPTERS_BY_FAMILY)}"
        )
    return builder(region_name)


def _build_anthropic(region_name: str) -> ModelProvider:
    """Build the Anthropic adapter. Imported here so importing this module needs no adapter."""
    from providers.anthropic_adapter import AnthropicAdapter

    return AnthropicAdapter(region_name=region_name)


def _build_openai(region_name: str) -> ModelProvider:
    """Build the OpenAI adapter. Imported here so importing this module needs no adapter."""
    from providers.openai_adapter import OpenAIAdapter

    return OpenAIAdapter(region_name=region_name)


#: Family to adapter builder.
_ADAPTERS_BY_FAMILY: Final[dict[str, Callable[[str], ModelProvider]]] = {
    "anthropic": _build_anthropic,
    "openai": _build_openai,
}


__all__ = ["build_provider_for_family"]

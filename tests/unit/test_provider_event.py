# SPDX-License-Identifier: MIT
"""`ProviderEvent`'s truncation and provisional flags: default off, legal only on usage events."""

from __future__ import annotations

import pytest

from providers.base import EventType, ProviderEvent

NON_USAGE_TYPES: tuple[EventType, ...] = (
    "text_delta",
    "tool_call",
    "tool_result",
    "error",
    "completed",
)
FLAGS = ("output_truncated", "provisional")


@pytest.mark.parametrize("flag", FLAGS)
def test_flag_defaults_to_false(flag: str) -> None:
    assert getattr(ProviderEvent(type="usage", input_tokens=1, output_tokens=0), flag) is False


def test_usage_event_carries_output_truncated() -> None:
    event = ProviderEvent(
        type="usage", input_tokens=10, output_tokens=16_384, output_truncated=True
    )
    assert event.output_truncated is True
    assert event.provisional is False


def test_usage_event_carries_provisional() -> None:
    event = ProviderEvent(type="usage", input_tokens=10, output_tokens=0, provisional=True)
    assert event.provisional is True
    assert event.output_truncated is False


@pytest.mark.parametrize("event_type", NON_USAGE_TYPES)
@pytest.mark.parametrize("flag", FLAGS)
def test_flag_on_non_usage_event_raises(event_type: EventType, flag: str) -> None:
    with pytest.raises(ValueError, match=flag):
        ProviderEvent(type=event_type, **{flag: True})  # type: ignore[arg-type]


@pytest.mark.parametrize("event_type", NON_USAGE_TYPES)
def test_non_usage_event_with_flags_off_constructs(event_type: EventType) -> None:
    event = ProviderEvent(type=event_type)
    assert (event.output_truncated, event.provisional) == (False, False)

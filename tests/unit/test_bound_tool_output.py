# SPDX-License-Identifier: MIT
"""Tests for :func:`agents.events.bound_tool_output`, the shared tool-result bound."""

from __future__ import annotations

from agents.events import (
    TOOL_OUTPUT_TRUNCATED_KEY,
    TOOL_RESULT_BYTE_CEILING,
    TRUNCATION_MARKER,
    bound_tool_output,
    record_list_field,
)
from agents.supervisor import describe_truncation

_MARKER: dict[str, object] = {TRUNCATION_MARKER: True}


def _size(value: object) -> int:
    return len(repr(value).encode("utf-8"))


def _records(prefix: str, count: int, pad: int = 700) -> list[dict[str, object]]:
    return [{"id": f"{prefix}-{n:04d}", "text": "x" * pad} for n in range(count)]


def _assert_record_boundary_trim(
    bounded_list: object, source: list[dict[str, object]]
) -> list[object]:
    assert isinstance(bounded_list, list)
    assert bounded_list[-1] == _MARKER
    kept = bounded_list[:-1]
    assert 0 < len(kept) < len(source)
    assert kept == source[: len(kept)]
    return kept


def test_the_truncation_key_is_the_truncated_field() -> None:
    assert TOOL_OUTPUT_TRUNCATED_KEY == "truncated"


def test_an_oversized_items_list_is_cut_at_a_record_boundary_and_flagged() -> None:
    items = _records("opp", 60)
    raw: dict[str, object] = {"items": items, "next_token": "t-1"}
    assert _size(raw) > TOOL_RESULT_BYTE_CEILING

    bounded, truncated = bound_tool_output(raw)

    assert truncated is True
    _assert_record_boundary_trim(bounded["items"], items)
    assert bounded[TOOL_OUTPUT_TRUNCATED_KEY] is True
    assert bounded["next_token"] == "t-1"
    assert _size(bounded) <= TOOL_RESULT_BYTE_CEILING
    assert describe_truncation(bounded, event_truncated=truncated) is not None
    assert raw["items"] is items and TOOL_OUTPUT_TRUNCATED_KEY not in raw


def test_items_is_chosen_over_a_larger_list() -> None:
    items = _records("opp", 60)
    container: dict[str, object] = {"items": items, "notes": _records("note", 80)}
    assert record_list_field(container) == ("items", items)


def test_without_items_the_largest_list_is_trimmed_and_the_rest_survive() -> None:
    activities = _records("act", 55)
    contacts = _records("cont", 2, pad=50)
    notes = _records("note", 3, pad=50)
    body: dict[str, object] = {
        "opportunity": {"opportunity_id": "opp-0000", "stage": "Proposal"},
        "contacts": contacts,
        "notes": notes,
        "activities": activities,
    }
    raw: dict[str, object] = {"statusCode": 200, "body": body}

    bounded, truncated = bound_tool_output(raw)

    assert truncated is True
    bounded_body = bounded["body"]
    assert isinstance(bounded_body, dict)
    _assert_record_boundary_trim(bounded_body["activities"], activities)
    assert bounded_body["contacts"] == contacts
    assert bounded_body["notes"] == notes
    assert bounded_body["opportunity"] == body["opportunity"]
    assert bounded_body[TOOL_OUTPUT_TRUNCATED_KEY] is True
    assert describe_truncation(bounded, event_truncated=truncated) is not None


def test_a_body_envelope_is_bounded_inside_the_body_and_keeps_the_envelope() -> None:
    items = _records("opp", 60)
    raw: dict[str, object] = {"statusCode": 200, "body": {"items": items}}

    bounded, truncated = bound_tool_output(raw)

    assert truncated is True
    assert bounded["statusCode"] == 200
    assert TOOL_OUTPUT_TRUNCATED_KEY not in bounded
    bounded_body = bounded["body"]
    assert isinstance(bounded_body, dict)
    _assert_record_boundary_trim(bounded_body["items"], items)
    assert bounded_body[TOOL_OUTPUT_TRUNCATED_KEY] is True
    assert _size(bounded_body) <= TOOL_RESULT_BYTE_CEILING


def test_an_output_under_the_ceiling_is_returned_unchanged() -> None:
    raw: dict[str, object] = {"statusCode": 200, "body": {"items": _records("opp", 5)}}

    bounded, truncated = bound_tool_output(raw)

    assert truncated is False
    assert bounded is raw
    assert describe_truncation(bounded, event_truncated=truncated) is None


def test_bounding_a_bounded_output_again_changes_nothing() -> None:
    raw: dict[str, object] = {"statusCode": 200, "body": {"items": _records("opp", 60)}}
    bounded, _ = bound_tool_output(raw)

    again, truncated = bound_tool_output(bounded)

    assert truncated is False
    assert again is bounded


def test_an_oversized_container_with_no_list_is_returned_unchanged() -> None:
    raw: dict[str, object] = {"body": {"blob": "x" * (TOOL_RESULT_BYTE_CEILING + 10)}}

    bounded, truncated = bound_tool_output(raw)

    assert truncated is False
    assert bounded is raw


def test_a_single_oversized_record_leaves_only_the_marker() -> None:
    raw: dict[str, object] = {"items": [{"id": "big", "text": "x" * (TOOL_RESULT_BYTE_CEILING)}]}

    bounded, truncated = bound_tool_output(raw)

    assert truncated is True
    assert bounded["items"] == [_MARKER]
    assert bounded[TOOL_OUTPUT_TRUNCATED_KEY] is True


def test_a_custom_ceiling_is_honoured() -> None:
    items = _records("opp", 10, pad=100)
    raw: dict[str, object] = {"items": items}

    bounded, truncated = bound_tool_output(raw, byte_ceiling=600)

    assert truncated is True
    _assert_record_boundary_trim(bounded["items"], items)
    assert _size(bounded) <= 600

# SPDX-License-Identifier: MIT
"""Tests for crm.paging: byte-bounded handler pages and the resume key after a kept record."""

from __future__ import annotations

import pytest

from agents.events import TOOL_RESULT_BYTE_CEILING, bound_tool_output
from crm.errors import CrmError
from crm.paging import HANDLER_PAGE_BYTE_BUDGET, bound_page, resume_key


def _record(index: int, *, padding: int = 400) -> dict[str, object]:
    return {"opportunity_id": f"opp-{index:05d}", "tenant": "demo", "notes": "x" * padding}


def _list_size(records: list[dict[str, object]]) -> int:
    return len(repr(records).encode("utf-8"))


def test_the_budget_leaves_headroom_under_the_agent_tool_result_ceiling() -> None:
    assert HANDLER_PAGE_BYTE_BUDGET == 28_672
    assert HANDLER_PAGE_BYTE_BUDGET < TOOL_RESULT_BYTE_CEILING


def test_a_page_within_the_budget_is_returned_whole_and_uncut() -> None:
    records = [_record(i) for i in range(5)]
    kept, cut = bound_page(records)
    assert kept == records
    assert cut is False


def test_an_empty_page_is_empty_and_uncut() -> None:
    assert bound_page([]) == ([], False)


def test_an_oversized_page_keeps_the_longest_prefix_that_fits() -> None:
    records = [_record(i) for i in range(200)]
    kept, cut = bound_page(records)
    assert cut is True
    assert kept == records[: len(kept)]
    assert _list_size(kept) <= HANDLER_PAGE_BYTE_BUDGET
    assert _list_size(records[: len(kept) + 1]) > HANDLER_PAGE_BYTE_BUDGET


def test_the_measure_is_the_exact_size_of_the_kept_list_repr() -> None:
    records = [_record(i, padding=10) for i in range(3)]
    exact = _list_size(records)
    assert bound_page(records, byte_budget=exact) == (records, False)
    kept, cut = bound_page(records, byte_budget=exact - 1)
    assert cut is True
    assert kept == records[:2]


def test_non_ascii_text_is_measured_in_utf8_bytes_not_characters() -> None:
    records = [{"id": str(i), "note": "é" * 100} for i in range(3)]
    char_length = len(repr(records))
    kept, cut = bound_page(records, byte_budget=char_length)
    assert cut is True
    assert len(kept) < len(records)


def test_a_single_record_over_the_budget_is_still_a_one_record_page() -> None:
    huge = _record(0, padding=HANDLER_PAGE_BYTE_BUDGET * 2)
    kept, cut = bound_page([huge, _record(1)])
    assert kept == [huge]
    assert cut is True
    alone, alone_cut = bound_page([huge])
    assert alone == [huge]
    assert alone_cut is False


def test_kept_records_are_copies_not_the_callers_mappings() -> None:
    record = _record(0)
    kept, _ = bound_page([record])
    kept[0]["notes"] = "changed"
    assert record["notes"] != "changed"


def test_a_page_at_the_budget_passes_the_agent_backstop_untrimmed() -> None:
    records = [_record(i) for i in range(500)]
    kept, cut = bound_page(records)
    assert cut is True
    payload = {
        "items": kept,
        "truncated": True,
        "examined_count": 2000,
        "next_cursor": "c" * 512,
    }
    bounded, trimmed = bound_tool_output(payload)
    assert trimmed is False
    assert bounded is payload


def test_resume_key_names_the_table_and_index_keys_with_their_types() -> None:
    item: dict[str, object] = {
        "opportunity_id": "opp-1",
        "tenant": "demo",
        "name_lower": "acme renewal",
        "name": "ignored",
    }
    key = resume_key("opportunities", "gsi_tenant", item)
    assert "name" not in key
    assert key["opportunity_id"] == {"S": "opp-1"}
    assert key["tenant"] == {"S": "demo"}
    assert key["name_lower"] == {"S": "acme renewal"}
    assert all(isinstance(value, dict) and len(value) == 1 for value in key.values())


def test_resume_key_renders_numbers_as_dynamodb_numbers() -> None:
    from crm.store.schema import table_schema

    schema = table_schema("opportunities")
    gsi = schema.gsi("gsi_tenant")
    names = {schema.partition_key, gsi.partition_key}
    names.update(n for n in (schema.sort_key, gsi.sort_key) if n is not None)
    item: dict[str, object] = dict.fromkeys(names, 7)
    key = resume_key("opportunities", "gsi_tenant", item)
    assert set(key) == names
    assert all(value == {"N": "7"} for value in key.values())


@pytest.mark.parametrize("bad", [None, True, ["list"]])
def test_resume_key_refuses_an_item_missing_or_mistyping_a_key_attribute(bad: object) -> None:
    item: dict[str, object] = {"opportunity_id": "opp-1", "name_lower": "acme", "tenant": bad}
    with pytest.raises(CrmError) as raised:
        resume_key("opportunities", "gsi_tenant", item)
    assert "tenant" in (raised.value.detail or "")

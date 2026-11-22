# SPDX-License-Identifier: MIT
"""The list handlers' byte-bounded pages, on DynamoDB Local.

Oversized pages followed to exhaustion reach every record exactly once, a single record over the
ceiling is a one-record page whose cursor moves past it, and a worst-case page at the budget passes
:func:`agents.events.bound_tool_output` untrimmed, for every cursor-bearing list handler. A
proposal decided between the index query and the base-item re-read still yields a usable cursor.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import pytest

from agents.events import TOOL_RESULT_BYTE_CEILING, bound_tool_output
from crm.paging import HANDLER_PAGE_BYTE_BUDGET
from crm.store.keys import tenant_prefixed
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.toolspec.definitions import CURSOR_MAX_LENGTH, LONG_TEXT_MAX_LENGTH
from tests.integration.conftest import _drop_prefixed_tables
from tests.unit.test_list_paging_handlers import (
    CASE_IDS,
    CASES,
    TENANT,
    ListCase,
    call,
    follow,
    list_size,
    seed_activities,
)

#: Unique per process, so concurrent xdist workers never share a table.
_TABLE_PREFIX = f"int-handler-paging-{uuid.uuid4().hex[:12]}-"


@pytest.fixture
def store(dynamodb_local_client: Any) -> Iterator[Store]:
    """A prefixed :class:`Store` on DynamoDB Local with its own tables, dropped after each test."""
    create_all_tables(dynamodb_local_client, table_prefix=_TABLE_PREFIX)
    try:
        yield Store(table_prefix=_TABLE_PREFIX, client=dynamodb_local_client)
    finally:
        _drop_prefixed_tables(dynamodb_local_client, _TABLE_PREFIX)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_oversized_pages_followed_to_exhaustion_reach_every_record_exactly_once(
    store: Store, case: ListCase
) -> None:
    ids = case.seed(store, 150, 400)

    pages = follow(store, case)

    seen = [str(r[case.id_key]) for page in pages for r in page[case.list_key]]
    assert len(seen) == len(set(seen))
    assert sorted(seen) == sorted(ids)
    assert sum(page.get("truncated") is True for page in pages) >= 2
    assert all(list_size(page[case.list_key]) <= HANDLER_PAGE_BYTE_BUDGET for page in pages)


def test_list_activities_returns_the_oldest_records_of_an_oversized_page(store: Store) -> None:
    ids = seed_activities(store, 50, 400)

    pages = follow(store, CASES[0])

    assert ids[0] in [r["activity_id"] for r in pages[0]["activities"]]
    for page in pages:
        page_ids = [r["activity_id"] for r in page["activities"]]
        assert page_ids == sorted(page_ids, reverse=True)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_record_over_the_ceiling_is_a_one_record_page_with_a_cursor_past_it(
    store: Store, case: ListCase
) -> None:
    ids = case.seed(store, 2, LONG_TEXT_MAX_LENGTH * 2)

    first = call(store, case)
    assert len(first[case.list_key]) == 1
    assert first["truncated"] is True

    rest = follow(store, case, cursor=first["next_cursor"])
    seen = [str(r[case.id_key]) for page in [first, *rest] for r in page[case.list_key]]
    assert sorted(seen) == sorted(ids)
    assert len(seen) == len(set(seen))


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_worst_case_page_at_the_budget_passes_bound_tool_output_untrimmed(
    store: Store, case: ListCase
) -> None:
    case.seed(store, 30, LONG_TEXT_MAX_LENGTH // 4)

    body = call(store, case)
    assert body["truncated"] is True
    worst = {**body, "next_cursor": "c" * CURSOR_MAX_LENGTH}

    bounded, trimmed = bound_tool_output({"statusCode": 200, "body": worst})

    assert trimmed is False
    assert len(repr(worst).encode("utf-8")) <= TOOL_RESULT_BYTE_CEILING
    assert bounded["body"] == worst


def test_a_proposal_decided_between_query_and_re_read_does_not_break_the_next_page(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = next(c for c in CASES if c.name == "list_proposals")
    ids = case.seed(store, 60, 400)
    real_batch_get = store.batch_get_items
    decided: list[str] = []

    def decide_then_re_read(
        table: str, keys: Sequence[Mapping[str, str]]
    ) -> list[dict[str, object]]:
        if not decided:
            for key in keys:
                item = store.get_item(table, key)
                assert item is not None
                store.put_item(
                    table,
                    {
                        **item,
                        "status": "approved",
                        "tenant_status": tenant_prefixed(TENANT, "approved"),
                    },
                )
                decided.append(str(key["proposal_id"]))
        return real_batch_get(table, keys)

    monkeypatch.setattr(store, "batch_get_items", decide_then_re_read)

    first = call(store, case)
    assert first["truncated"] is True
    assert all(p["status"] == "approved" for p in first["proposals"])

    second = call(store, case, cursor=first["next_cursor"])

    first_ids = {p["proposal_id"] for p in first["proposals"]}
    second_ids = {p["proposal_id"] for p in second["proposals"]}
    assert second_ids
    assert first_ids.isdisjoint(second_ids)
    assert second_ids <= set(ids) - set(decided)

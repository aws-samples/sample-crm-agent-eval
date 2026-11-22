# SPDX-License-Identifier: MIT
"""The three cursor-bearing list handlers bound each page to the handler byte budget.

Runs every handler against `moto`. A page over :data:`crm.paging.HANDLER_PAGE_BYTE_BUDGET` is cut in
query order, flagged ``truncated``, and its cursor resumes after the last record returned, so
following cursors reaches every record exactly once. The seeding helpers here are shared with the
DynamoDB Local tier (``tests/integration/test_handler_paging_ddb.py``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import boto3
import pytest
from moto import mock_aws

from agents.events import TOOL_OUTPUT_TRUNCATED_KEY, TOOL_RESULT_BYTE_CEILING, bound_tool_output
from crm.envelope import IDENTITY_KEY, identity_event
from crm.handlers import list_activities, list_audit_records, list_proposals
from crm.paging import HANDLER_PAGE_BYTE_BUDGET
from crm.store.keys import tenant_prefixed
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from crm.toolspec.definitions import CURSOR_MAX_LENGTH, LONG_TEXT_MAX_LENGTH, TEXT_MAX_LENGTH

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TENANT = "eval-paging"
OPPORTUNITY = "opp-paging"
TARGET = "acct-paging"
#: A character whose ``repr`` costs four UTF-8 bytes, the most any one character can.
WIDE = "\U0001f600"

Handler = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class ListCase:
    """One list handler: how to seed ``count`` records and read a page of them."""

    name: str
    handler: Handler
    list_key: str
    id_key: str
    seed: Callable[[Store, int, int], list[str]]
    args: dict[str, Any]


def _stamp(n: int) -> str:
    return f"2026-10-14T{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}+00:00"


def seed_activities(store: Store, count: int, body_chars: int) -> list[str]:
    """Seed the opportunity and ``count`` activities on it; return their ids oldest-first."""
    store.put_item(
        "opportunities",
        {
            "opportunity_id": OPPORTUNITY,
            "tenant": TENANT,
            "account_id": TARGET,
            "owner_rep_id": "rep-paging",
            "stage": "Discovery",
            "name": "Paging Deal",
            "name_lower": "paging deal",
            "close_date": "2026-12-01",
            "value": 1,
            "created_at": _stamp(0),
            "tenant_account_id": tenant_prefixed(TENANT, TARGET),
            "tenant_owner_rep_id": tenant_prefixed(TENANT, "rep-paging"),
            "tenant_stage": tenant_prefixed(TENANT, "Discovery"),
        },
    )
    ids = [f"act-{n:04d}" for n in range(count)]
    for n, activity_id in enumerate(ids):
        store.put_item(
            "activities",
            {
                "activity_id": activity_id,
                "tenant": TENANT,
                "opportunity_id": OPPORTUNITY,
                "tenant_opportunity_id": tenant_prefixed(TENANT, OPPORTUNITY),
                "occurred_at": _stamp(n),
                "created_at": _stamp(n),
                "kind": "call" if n % 2 == 0 else "email",
                "subject": f"subject {n}",
                "body": WIDE * body_chars,
            },
        )
    return ids


def seed_proposals(store: Store, count: int, body_chars: int) -> list[str]:
    """Seed ``count`` pending proposals; return their ids in creation order."""
    ids = [f"prop-{n:04d}" for n in range(count)]
    for n, proposal_id in enumerate(ids):
        store.put_item(
            "proposals",
            {
                "proposal_id": proposal_id,
                "tenant": TENANT,
                "status": "pending",
                "tenant_status": tenant_prefixed(TENANT, "pending"),
                "tenant_target_id": tenant_prefixed(TENANT, TARGET),
                "created_at": _stamp(n),
                "target_table": "accounts",
                "target_id": TARGET,
                "actor_rep_id": "rep-paging",
                "operation": "update",
                "reason": WIDE * body_chars,
                "before_snapshot": {"account_id": TARGET, "tenant": TENANT},
                "after_payload": {"notes": WIDE * body_chars},
            },
        )
    return ids


def seed_audit_records(store: Store, count: int, body_chars: int) -> list[str]:
    """Seed ``count`` audit records on one target; return their ids oldest-first.

    Key-bearing text is capped at its accepted length; ``body_chars`` beyond that pads the
    projected ``account_id`` so a single record can exceed the ceiling.
    """
    target_id = WIDE * min(body_chars, TEXT_MAX_LENGTH)
    account_id = TARGET if body_chars <= TEXT_MAX_LENGTH else TARGET + WIDE * body_chars
    ids = [f"aud-{n:04d}" for n in range(count)]
    for n, audit_id in enumerate(ids):
        store.put_item(
            "audit_records",
            {
                "audit_id": audit_id,
                "tenant": TENANT,
                "tenant_target_id": tenant_prefixed(TENANT, target_id),
                "created_at": _stamp(n),
                "target_table": "accounts",
                "target_id": target_id,
                "actor_rep_id": "rep-paging",
                "status": "active",
                "account_id": account_id,
                "before_snapshot": {},
                "after_snapshot": {"account_id": TARGET},
            },
        )
    return ids


CASES: tuple[ListCase, ...] = (
    ListCase(
        "list_activities",
        list_activities.handler,
        "activities",
        "activity_id",
        seed_activities,
        {"opportunity_id": OPPORTUNITY},
    ),
    ListCase(
        "list_proposals",
        list_proposals.handler,
        "proposals",
        "proposal_id",
        seed_proposals,
        {"status": "pending"},
    ),
    ListCase(
        "list_proposals_all",
        list_proposals.handler,
        "proposals",
        "proposal_id",
        seed_proposals,
        {},
    ),
    ListCase(
        "list_audit_records",
        list_audit_records.handler,
        "audit_records",
        "audit_id",
        seed_audit_records,
        {},
    ),
)
CASE_IDS = [case.name for case in CASES]


def call(store: Store, case: ListCase, **extra: Any) -> dict[str, Any]:
    """Invoke ``case``'s handler as a full-visibility caller and return the response body."""
    identity = {
        "rep_id": "rep-paging",
        "book": [],
        "full_visibility": True,
        "principal_type": "user",
        "session_id": "sess-paging",
        "source": "gateway",
        "tenant": TENANT,
    }
    result = case.handler(
        identity_event(IDENTITY_KEY, identity, {**case.args, **extra}), store=store
    )
    assert result["statusCode"] == 200, result
    return dict(result["body"])


def follow(
    store: Store, case: ListCase, *, cursor: str | None = None, **extra: Any
) -> list[dict[str, Any]]:
    """Follow ``next_cursor`` from ``cursor`` to exhaustion; return every page body."""
    pages: list[dict[str, Any]] = []
    for _ in range(500):
        body = call(store, case, **extra, **({"cursor": cursor} if cursor else {}))
        pages.append(body)
        cursor = body["next_cursor"]
        if cursor is None:
            return pages
    raise AssertionError("cursor never exhausted")


def list_size(records: list[dict[str, Any]]) -> int:
    return len(repr(records).encode("utf-8"))


@pytest.fixture
def store() -> Iterator[Store]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        create_all_tables(client)
        yield Store(client=client)


def test_the_handler_flag_is_the_key_the_agent_truncation_reader_names() -> None:
    assert TOOL_OUTPUT_TRUNCATED_KEY == "truncated"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_page_within_the_budget_is_unchanged_and_carries_no_truncated_flag(
    store: Store, case: ListCase
) -> None:
    ids = case.seed(store, 3, 10)

    body = call(store, case)

    assert sorted(r[case.id_key] for r in body[case.list_key]) == sorted(ids)
    assert "truncated" not in body
    assert body["next_cursor"] is None


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_an_oversized_page_is_cut_flagged_and_resumable(store: Store, case: ListCase) -> None:
    case.seed(store, 40, 400)

    body = call(store, case)

    assert body["truncated"] is True
    assert body["next_cursor"] is not None
    assert 0 < len(body[case.list_key]) < 40
    assert list_size(body[case.list_key]) <= HANDLER_PAGE_BYTE_BUDGET


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_following_cut_pages_reaches_every_record_exactly_once(
    store: Store, case: ListCase
) -> None:
    ids = case.seed(store, 120, 400)

    pages = follow(store, case)

    seen = [str(r[case.id_key]) for page in pages for r in page[case.list_key]]
    assert len(seen) == len(set(seen))
    assert sorted(seen) == sorted(ids)
    assert any(page.get("truncated") is True for page in pages)
    for page in pages:
        assert list_size(page[case.list_key]) <= HANDLER_PAGE_BYTE_BUDGET


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_single_record_over_the_ceiling_is_a_one_record_page_with_a_cursor_past_it(
    store: Store, case: ListCase
) -> None:
    ids = case.seed(store, 3, LONG_TEXT_MAX_LENGTH * 2)

    first = call(store, case)
    assert len(first[case.list_key]) == 1
    assert first["truncated"] is True
    assert first["next_cursor"] is not None

    pages = [first, *follow(store, case, cursor=first["next_cursor"])]
    seen = [str(r[case.id_key]) for page in pages for r in page[case.list_key]]
    assert sorted(seen) == sorted(ids)
    assert len(seen) == len(set(seen))


def test_list_activities_reaches_the_oldest_records_the_old_trim_dropped(store: Store) -> None:
    ids = seed_activities(store, 50, 400)

    first = call(store, CASES[0])
    first_ids = [r["activity_id"] for r in first["activities"]]

    assert first["truncated"] is True
    assert ids[0] in first_ids
    assert first_ids == sorted(first_ids, reverse=True)


def test_list_activities_bounds_after_the_kind_filter_and_resumes_past_filtered_rows(
    store: Store,
) -> None:
    ids = seed_activities(store, 120, 400)
    calls = [activity_id for n, activity_id in enumerate(ids) if n % 2 == 0]

    unfiltered_page = len(call(store, CASES[0])["activities"])
    pages = follow(store, CASES[0], kind="call")

    seen = [str(r["activity_id"]) for page in pages for r in page["activities"]]
    assert all(r["kind"] == "call" for page in pages for r in page["activities"])
    assert sorted(seen) == calls
    assert len(seen) == len(set(seen))
    cut_pages = [page for page in pages if page.get("truncated") is True]
    assert cut_pages
    assert all(len(page["activities"]) == unfiltered_page for page in cut_pages)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_a_worst_case_page_at_the_budget_passes_the_agent_backstop_untrimmed(
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

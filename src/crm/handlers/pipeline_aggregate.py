# SPDX-License-Identifier: MIT
"""``pipeline_aggregate`` action Lambda. Stage funnel, value rollup, and forecast by close
date, scoped to the caller's book. An empty-book caller without full visibility is denied.

Aggregates in process over the opportunities :func:`crm.authz.filter_to_scope` leaves visible
among at most :data:`crm.search.EXAMINATION_BUDGET` tenant index items; ``truncated`` reports a
cut-off examination.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from crm.authz import Identity, filter_to_scope, require_listing_scope
from crm.domain.stages import CLOSED_STAGES, STAGES
from crm.handlers._common import build_handler
from crm.search import EXAMINATION_BUDGET
from crm.store.store import Store

_TABLE = "opportunities"
_KEY = "opportunity_id"


def _candidate_opportunities(
    store: Store, identity: Identity
) -> tuple[list[dict[str, object]], bool]:
    """Return the caller's visible opportunities with full attributes, and whether examination
    stopped at the budget.

    Bodies are batch-fetched by primary key, since ``gsi_tenant`` does not project ``value``,
    ``owner_rep_id`` or ``close_date``; they are returned in index order.
    """
    stubs: list[dict[str, object]] = []
    cursor: dict[str, object] | None = None
    truncated = False
    while True:
        page, cursor = store.query_tenant_scan(
            _TABLE,
            tenant=identity.tenant,
            limit=EXAMINATION_BUDGET - len(stubs),
            exclusive_start_key=cursor,
        )
        stubs.extend(page)
        if cursor is None:
            break
        if len(stubs) >= EXAMINATION_BUDGET:
            truncated = True
            break
    visible_ids = [str(stub[_KEY]) for stub in filter_to_scope(identity, stubs)]
    bodies = store.batch_get_items(
        _TABLE, [{_KEY: opportunity_id} for opportunity_id in visible_ids]
    )
    by_id = {str(body[_KEY]): body for body in bodies}
    return [by_id[i] for i in visible_ids if i in by_id], truncated


def _pipeline_aggregate(
    store: Store, identity: Identity, arguments: dict[str, Any]
) -> dict[str, Any]:
    require_listing_scope(identity, operation="pipeline_aggregate")
    owner_rep_id = arguments.get("owner_rep_id")
    close_before = arguments.get("close_before")
    close_after = arguments.get("close_after")
    include_closed = bool(arguments.get("include_closed", False))

    opportunities, truncated = _candidate_opportunities(store, identity)

    stage_funnel: dict[str, int] = dict.fromkeys(STAGES, 0)
    value_rollup = 0.0
    forecast: dict[str, float] = defaultdict(float)

    for opp in opportunities:
        if owner_rep_id is not None and opp.get("owner_rep_id") != owner_rep_id:
            continue
        stage = str(opp.get("stage", ""))
        if not include_closed and stage in CLOSED_STAGES:
            continue

        close_date = opp.get("close_date")
        if (
            close_before is not None
            and isinstance(close_date, str)
            and close_date >= str(close_before)
        ):
            continue
        if (
            close_after is not None
            and isinstance(close_date, str)
            and close_date <= str(close_after)
        ):
            continue

        if stage in stage_funnel:
            stage_funnel[stage] += 1
        value = opp.get("value")
        if isinstance(value, (int, float)):
            value_rollup += float(value)
            if isinstance(close_date, str) and close_date:
                forecast[close_date] += float(value)

    return {
        "stage_funnel": stage_funnel,
        "value_rollup": value_rollup,
        "forecast_by_close_date": dict(sorted(forecast.items())),
        "truncated": truncated,
    }


handler = build_handler(_pipeline_aggregate)

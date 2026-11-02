# SPDX-License-Identifier: MIT
"""``search_opportunities`` action Lambda. Thin adapter over :mod:`crm.search`."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from crm.authz import Identity
from crm.handlers._common import build_handler
from crm.search import search_opportunities
from crm.store.store import Store


def _search_opportunities(
    store: Store, identity: Identity, arguments: dict[str, Any]
) -> dict[str, Any]:
    result = search_opportunities(
        store,
        identity,
        query=arguments.get("query"),
        stage=arguments.get("stage"),
        owner_rep_id=arguments.get("owner_rep_id"),
        account_id=arguments.get("account_id"),
        cursor=arguments.get("cursor"),
        limit=arguments.get("limit"),
    )
    return asdict(result)


handler = build_handler(_search_opportunities)

# SPDX-License-Identifier: MIT
"""``search_accounts`` action Lambda. Thin adapter over :mod:`crm.search`."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from crm.authz import Identity
from crm.handlers._common import build_handler
from crm.search import search_accounts
from crm.store.store import Store


def _search_accounts(store: Store, identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    result = search_accounts(
        store,
        identity,
        query=arguments.get("query"),
        owner_rep_id=arguments.get("owner_rep_id"),
        cursor=arguments.get("cursor"),
        limit=arguments.get("limit"),
    )
    return asdict(result)


handler = build_handler(_search_accounts)

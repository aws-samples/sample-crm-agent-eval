# SPDX-License-Identifier: MIT
"""``get_audit_record`` action Lambda. Thin adapter over :func:`crm.audit.get_audit_record`, which
enforces the list's record-scope predicate itself.

Returns ``{"audit_record": <full item with derived "action">}``.
"""

from __future__ import annotations

from typing import Any

from crm.audit import get_audit_record
from crm.authz import Identity
from crm.handlers._common import build_handler
from crm.store.store import Store


def _get_audit_record(
    store: Store, identity: Identity, arguments: dict[str, Any]
) -> dict[str, Any]:
    return {"audit_record": get_audit_record(identity, store, str(arguments["audit_id"]))}


handler = build_handler(_get_audit_record)

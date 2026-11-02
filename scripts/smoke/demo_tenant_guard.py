# SPDX-License-Identifier: MIT
"""Re-export of `eval.tenant_guard`, the tenant-delta assertions every smoke script imports.

Re-exports only: nothing here is re-implemented, wrapped, renamed, or defaulted differently.
"""

from __future__ import annotations

from eval.tenant_guard import (
    DEMO_TENANT,
    WATCHED_TABLES,
    TenantRowReader,
    TenantSnapshot,
    demo_tenant_untouched_check,
    read_tenant_snapshot,
    store_row_reader,
    writes_only_under_smoke_tenant_check,
)

__all__ = [
    "DEMO_TENANT",
    "WATCHED_TABLES",
    "TenantRowReader",
    "TenantSnapshot",
    "demo_tenant_untouched_check",
    "read_tenant_snapshot",
    "store_row_reader",
    "writes_only_under_smoke_tenant_check",
]

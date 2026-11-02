# SPDX-License-Identifier: MIT
"""DynamoDB store: entity-per-table persistence with tenant-prefixed GSI isolation.

* :class:`crm.store.store.Store` — the persistence class.
* :mod:`crm.store.schema` — declared table/GSI key schema.
* :mod:`crm.store.keys` — tenant-prefixed composite keys and derived index attributes.
* :mod:`crm.store.provisioning` — table creation from the declared schema, for tests and local
  DynamoDB engines.
"""

from __future__ import annotations

from crm.store.keys import (
    DERIVED_ATTRIBUTES,
    derive,
    derived_attribute_names,
    run_id_task_id,
    split_tenant_prefixed,
    stage_close_date,
    tenant_prefixed,
)
from crm.store.provisioning import create_all_tables, create_table
from crm.store.schema import (
    TABLE_NAMES,
    TABLES,
    TENANT_SCOPED_TABLES,
    GlobalSecondaryIndex,
    TableSchema,
    table_schema,
)
from crm.store.store import Store

__all__ = [
    "DERIVED_ATTRIBUTES",
    "TABLE_NAMES",
    "TABLES",
    "TENANT_SCOPED_TABLES",
    "GlobalSecondaryIndex",
    "Store",
    "TableSchema",
    "create_all_tables",
    "create_table",
    "derive",
    "derived_attribute_names",
    "run_id_task_id",
    "split_tenant_prefixed",
    "stage_close_date",
    "table_schema",
    "tenant_prefixed",
]

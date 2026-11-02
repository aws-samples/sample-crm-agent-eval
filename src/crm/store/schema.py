# SPDX-License-Identifier: MIT
"""Table and GSI key schema declarations for the eleven DynamoDB tables.

Declares, for every table, its primary key and every global secondary index with its key schema and
projection. On the tenant-scoped tables every GSI partition key is either ``tenant`` or a
tenant-prefixed composite (see :mod:`crm.store.keys`), and every GSI projects ``tenant``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class GlobalSecondaryIndex:
    """One GSI: its name, key schema, and projection.

    Attributes:
        name: Index name, unique within the table.
        partition_key: Attribute name serving as the index partition key.
        sort_key: Attribute name serving as the index sort key, or ``None`` for a partition-key-only
            index.
        projected_attributes: Extra attributes (beyond the table's own keys and the index's own
            keys) available on a query result. Must include ``"tenant"``.
    """

    name: str
    partition_key: str
    sort_key: str | None
    projected_attributes: frozenset[str]

    def __post_init__(self) -> None:
        """Reject a GSI declaration that does not project ``tenant``."""
        if "tenant" not in self.projected_attributes:
            raise ValueError(
                f"GSI {self.name!r} must project 'tenant': a query result missing it would fail "
                "crm.authz.filter_to_scope's tenant_of check and vanish from list results"
            )


@dataclass(frozen=True, slots=True)
class TableSchema:
    """One entity table: its primary key and its global secondary indexes.

    Attributes:
        name: Table name, one of :data:`TABLE_NAMES`.
        partition_key: Table primary partition key attribute.
        sort_key: Table primary sort key attribute, or ``None`` for a partition-key-only table.
        gsis: Every global secondary index declared on this table, keyed by index name.
    """

    name: str
    partition_key: str
    sort_key: str | None
    gsis: dict[str, GlobalSecondaryIndex]

    def gsi(self, name: str) -> GlobalSecondaryIndex:
        """Return the named GSI, raising if the table has no such index."""
        try:
            return self.gsis[name]
        except KeyError as exc:
            raise ValueError(
                f"table {self.name!r} has no GSI named {name!r}; known: {sorted(self.gsis)}"
            ) from exc


def _common_projection(*extra: str) -> frozenset[str]:
    """Every GSI projects ``tenant`` plus whatever else its own query path reads."""
    return frozenset({"tenant", *extra})


#: Every attribute that must be projected by each index :func:`crm.search._index_for` may select.
#: ``search_opportunities`` filters in process, so an unprojected attribute evaluates as "no match".
#:
#: * ``tenant`` / ``account_id`` — the two axes ``crm.authz.is_in_scope`` fails closed on.
#: * ``name_lower`` — ``_matches_query``'s only haystack; unprojected, every free-text search
#:   served by that index matches nothing.
#: * ``owner_rep_id`` / ``stage`` — ``_matches_owner`` / ``_matches_stage``.
#: * ``opportunity_id`` / ``name`` / ``value`` — dereferenced by the pipeline board on every card.
#: * ``close_date`` — returned on every search result.
OPPORTUNITY_SEARCH_PROJECTION: Final[frozenset[str]] = frozenset(
    {
        "account_id",
        "close_date",
        "name",
        "name_lower",
        "opportunity_id",
        "owner_rep_id",
        "stage",
        "tenant",
        "value",
    }
)


#: Every table's schema, keyed by table name.
TABLES: Final[dict[str, TableSchema]] = {
    "accounts": TableSchema(
        name="accounts",
        partition_key="account_id",
        sort_key=None,
        gsis={
            "gsi_owner": GlobalSecondaryIndex(
                name="gsi_owner",
                partition_key="tenant_owner_rep_id",
                sort_key="created_at",
                projected_attributes=_common_projection("account_id", "owner_rep_id", "name"),
            ),
            "gsi_tenant": GlobalSecondaryIndex(
                name="gsi_tenant",
                partition_key="tenant",
                sort_key="name_lower",
                projected_attributes=_common_projection("account_id", "name"),
            ),
        },
    ),
    "contacts": TableSchema(
        name="contacts",
        partition_key="contact_id",
        sort_key=None,
        gsis={
            "gsi_account": GlobalSecondaryIndex(
                name="gsi_account",
                partition_key="tenant_account_id",
                sort_key="created_at",
                projected_attributes=_common_projection(
                    "contact_id", "account_id", "name", "status"
                ),
            ),
            "gsi_tenant": GlobalSecondaryIndex(
                name="gsi_tenant",
                partition_key="tenant",
                sort_key="name_lower",
                projected_attributes=_common_projection("contact_id", "account_id", "name"),
            ),
        },
    ),
    "opportunities": TableSchema(
        name="opportunities",
        partition_key="opportunity_id",
        sort_key=None,
        gsis={
            "gsi_account": GlobalSecondaryIndex(
                name="gsi_account",
                partition_key="tenant_account_id",
                sort_key="created_at",
                projected_attributes=_common_projection(
                    "opportunity_id",
                    "account_id",
                    "name",
                    "name_lower",
                    "owner_rep_id",
                    "stage",
                    "close_date",
                    "value",
                ),
            ),
            # These four indexes are the ones `crm.search._index_for` selects among; each projects
            # OPPORTUNITY_SEARCH_PROJECTION in full.
            "gsi_owner_stage": GlobalSecondaryIndex(
                name="gsi_owner_stage",
                partition_key="tenant_owner_rep_id",
                sort_key="stage_close_date",
                projected_attributes=_common_projection(
                    "opportunity_id",
                    "account_id",
                    "name",
                    "name_lower",
                    "owner_rep_id",
                    "stage",
                    "close_date",
                    "value",
                ),
            ),
            "gsi_stage": GlobalSecondaryIndex(
                name="gsi_stage",
                partition_key="tenant_stage",
                sort_key="close_date",
                projected_attributes=_common_projection(
                    "opportunity_id",
                    "account_id",
                    "name",
                    "name_lower",
                    "owner_rep_id",
                    "stage",
                    "close_date",
                    "value",
                ),
            ),
            "gsi_tenant": GlobalSecondaryIndex(
                name="gsi_tenant",
                partition_key="tenant",
                sort_key="name_lower",
                projected_attributes=_common_projection(
                    "opportunity_id",
                    "account_id",
                    "name",
                    "owner_rep_id",
                    "stage",
                    "close_date",
                    "value",
                ),
            ),
        },
    ),
    "activities": TableSchema(
        name="activities",
        partition_key="activity_id",
        sort_key=None,
        gsis={
            "gsi_opportunity": GlobalSecondaryIndex(
                name="gsi_opportunity",
                partition_key="tenant_opportunity_id",
                sort_key="occurred_at",
                projected_attributes=_common_projection(
                    "activity_id", "opportunity_id", "kind", "subject", "body"
                ),
            ),
            "gsi_account": GlobalSecondaryIndex(
                name="gsi_account",
                partition_key="tenant_account_id",
                sort_key="occurred_at",
                projected_attributes=_common_projection(
                    "activity_id", "account_id", "opportunity_id", "kind", "subject"
                ),
            ),
            "gsi_tenant": GlobalSecondaryIndex(
                name="gsi_tenant",
                partition_key="tenant",
                sort_key="created_at",
                projected_attributes=_common_projection(
                    "activity_id", "opportunity_id", "kind", "subject"
                ),
            ),
        },
    ),
    "notes": TableSchema(
        name="notes",
        partition_key="note_id",
        sort_key=None,
        gsis={
            "gsi_opportunity": GlobalSecondaryIndex(
                name="gsi_opportunity",
                partition_key="tenant_opportunity_id",
                sort_key="created_at",
                projected_attributes=_common_projection("note_id", "opportunity_id", "body"),
            ),
            "gsi_tenant": GlobalSecondaryIndex(
                name="gsi_tenant",
                partition_key="tenant",
                sort_key="created_at",
                projected_attributes=_common_projection("note_id", "opportunity_id", "body"),
            ),
        },
    ),
    "proposals": TableSchema(
        name="proposals",
        partition_key="proposal_id",
        sort_key=None,
        gsis={
            "gsi_status": GlobalSecondaryIndex(
                name="gsi_status",
                partition_key="tenant_status",
                sort_key="created_at",
                projected_attributes=_common_projection(
                    "proposal_id", "status", "target_table", "target_id", "actor_rep_id"
                ),
            ),
            "gsi_actor": GlobalSecondaryIndex(
                name="gsi_actor",
                partition_key="tenant_actor_rep_id",
                sort_key="created_at",
                projected_attributes=_common_projection(
                    "proposal_id", "actor_rep_id", "status", "target_table", "target_id"
                ),
            ),
            "gsi_target": GlobalSecondaryIndex(
                name="gsi_target",
                partition_key="tenant_target_id",
                sort_key="created_at",
                projected_attributes=_common_projection(
                    "proposal_id", "target_table", "target_id", "status"
                ),
            ),
            # Tenant-wide scan path, used by scoped teardown.
            "gsi_tenant": GlobalSecondaryIndex(
                name="gsi_tenant",
                partition_key="tenant",
                sort_key="created_at",
                projected_attributes=_common_projection("proposal_id"),
            ),
        },
    ),
    "audit_records": TableSchema(
        name="audit_records",
        partition_key="audit_id",
        sort_key=None,
        gsis={
            "gsi_target": GlobalSecondaryIndex(
                name="gsi_target",
                partition_key="tenant_target_id",
                sort_key="created_at",
                projected_attributes=_common_projection(
                    "audit_id",
                    "target_table",
                    "target_id",
                    "actor_rep_id",
                    "reversed_at",
                    "account_id",
                    "status",
                ),
            ),
            "gsi_actor": GlobalSecondaryIndex(
                name="gsi_actor",
                partition_key="tenant_actor_rep_id",
                sort_key="created_at",
                projected_attributes=_common_projection(
                    "audit_id", "actor_rep_id", "target_table", "target_id"
                ),
            ),
            # Tenant-wide scan path for scoped teardown and for list_audit_records without a
            # target_id; projects the scope and revert-state fields that handler returns.
            "gsi_tenant": GlobalSecondaryIndex(
                name="gsi_tenant",
                partition_key="tenant",
                sort_key="created_at",
                projected_attributes=_common_projection(
                    "audit_id", "account_id", "target_id", "target_table", "status"
                ),
            ),
        },
    ),
    "usage_records": TableSchema(
        name="usage_records",
        partition_key="usage_id",
        sort_key=None,
        gsis={
            "gsi_correlation": GlobalSecondaryIndex(
                name="gsi_correlation",
                partition_key="tenant_correlation_id",
                sort_key="created_at",
                projected_attributes=_common_projection("usage_id", "correlation_id", "run_id"),
            ),
            "gsi_run": GlobalSecondaryIndex(
                name="gsi_run",
                partition_key="tenant_run_id",
                sort_key="created_at",
                projected_attributes=_common_projection("usage_id", "run_id", "correlation_id"),
            ),
            # Tenant-wide scan path, used by scoped teardown.
            "gsi_tenant": GlobalSecondaryIndex(
                name="gsi_tenant",
                partition_key="tenant",
                sort_key="created_at",
                projected_attributes=_common_projection("usage_id"),
            ),
        },
    ),
    "reps": TableSchema(
        name="reps",
        partition_key="rep_id",
        sort_key=None,
        gsis={
            "gsi_tenant": GlobalSecondaryIndex(
                name="gsi_tenant",
                partition_key="tenant",
                sort_key="rep_id",
                projected_attributes=_common_projection("rep_id"),
            ),
        },
    ),
    # eval_runs and eval_results record runs across all tenants; their GSIs are not
    # tenant-prefixed.
    "eval_runs": TableSchema(
        name="eval_runs",
        partition_key="run_id",
        sort_key=None,
        gsis={
            "gsi_status": GlobalSecondaryIndex(
                name="gsi_status",
                partition_key="status",
                sort_key="started_at",
                projected_attributes=frozenset({"tenant", "run_id", "status"}),
            ),
        },
    ),
    "eval_results": TableSchema(
        name="eval_results",
        partition_key="run_id",
        sort_key="result_id",
        gsis={
            "gsi_task": GlobalSecondaryIndex(
                name="gsi_task",
                partition_key="run_id_task_id",
                sort_key="arm",
                projected_attributes=frozenset({"tenant", "run_id", "result_id", "arm"}),
            ),
        },
    ),
}

#: The closed set of table names this store manages.
TABLE_NAMES: Final[frozenset[str]] = frozenset(TABLES)

#: Tenant-scoped tables: every table except ``eval_runs`` and ``eval_results``.
TENANT_SCOPED_TABLES: Final[frozenset[str]] = frozenset(
    name for name in TABLE_NAMES if name not in {"eval_runs", "eval_results"}
)


def table_schema(table: str) -> TableSchema:
    """Return the declared schema for ``table``, raising on an unrecognised name."""
    try:
        return TABLES[table]
    except KeyError as exc:
        raise ValueError(
            f"unrecognised table {table!r}; expected one of {sorted(TABLE_NAMES)}"
        ) from exc

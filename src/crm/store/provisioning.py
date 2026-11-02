# SPDX-License-Identifier: MIT
"""Table creation from the declared schema, for test fixtures and local DynamoDB engines.

Deployed tables are provisioned by Terraform; this module serves ``moto`` and DynamoDB Local only.
"""

from __future__ import annotations

from typing import Any

from crm.store.schema import TABLES, TableSchema


def create_all_tables(client: Any, *, table_prefix: str = "") -> None:
    """Create every table in :data:`crm.store.schema.TABLES` against ``client``.

    Args:
        client: A ``boto3`` DynamoDB low-level client pointed at a mock or local endpoint.
        table_prefix: Prefix applied to each physical table name, matching
            :class:`crm.store.store.Store`'s prefixing.
    """
    for schema in TABLES.values():
        create_table(client, schema, table_prefix=table_prefix)


def create_table(client: Any, schema: TableSchema, *, table_prefix: str = "") -> None:
    """Create one table from its declared schema, including every GSI and its projection."""
    attribute_definitions: dict[str, dict[str, str]] = {
        schema.partition_key: {"AttributeName": schema.partition_key, "AttributeType": "S"}
    }
    if schema.sort_key is not None:
        attribute_definitions[schema.sort_key] = {
            "AttributeName": schema.sort_key,
            "AttributeType": "S",
        }

    key_schema = [{"AttributeName": schema.partition_key, "KeyType": "HASH"}]
    if schema.sort_key is not None:
        key_schema.append({"AttributeName": schema.sort_key, "KeyType": "RANGE"})

    gsi_definitions = []
    for gsi in schema.gsis.values():
        attribute_definitions.setdefault(
            gsi.partition_key, {"AttributeName": gsi.partition_key, "AttributeType": "S"}
        )
        gsi_key_schema = [{"AttributeName": gsi.partition_key, "KeyType": "HASH"}]
        if gsi.sort_key is not None:
            attribute_definitions.setdefault(
                gsi.sort_key, {"AttributeName": gsi.sort_key, "AttributeType": "S"}
            )
            gsi_key_schema.append({"AttributeName": gsi.sort_key, "KeyType": "RANGE"})

        non_key_attrs = sorted(
            attr
            for attr in gsi.projected_attributes
            if attr not in {schema.partition_key, schema.sort_key, gsi.partition_key, gsi.sort_key}
        )
        # An empty non-key set means KEYS_ONLY, not ALL, matching the Terraform-created tables.
        projection: dict[str, Any] = (
            {"ProjectionType": "KEYS_ONLY"}
            if not non_key_attrs
            else {"ProjectionType": "INCLUDE", "NonKeyAttributes": non_key_attrs}
        )
        gsi_definitions.append(
            {
                "IndexName": gsi.name,
                "KeySchema": gsi_key_schema,
                "Projection": projection,
            }
        )

    kwargs: dict[str, Any] = {
        "TableName": f"{table_prefix}{schema.name}",
        "AttributeDefinitions": list(attribute_definitions.values()),
        "KeySchema": key_schema,
        "BillingMode": "PAY_PER_REQUEST",
    }
    if gsi_definitions:
        kwargs["GlobalSecondaryIndexes"] = gsi_definitions

    client.create_table(**kwargs)

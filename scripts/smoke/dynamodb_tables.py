# SPDX-License-Identifier: MIT
"""Layer 4 per-table DynamoDB smoke check.

For every table declared in :mod:`crm.store.schema`, asserts customer-managed-KMS encryption at
rest (not the AWS-owned key, and not merely the AWS-*managed* ``alias/aws/dynamodb`` key), point-in-
time recovery enabled, every declared GSI present, ``ACTIVE``, queryable, and projecting what
:mod:`crm.authz` needs (``tenant`` foremost), and every mandatory tag. Reconciles
the schema's table set against the deployed ``dynamodb_table_names`` Terraform output so a table
missing on either side is reported, not silently skipped.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from scripts.smoke.harness import CheckResult, SmokeContext, run_main

from crm.store.schema import TABLE_NAMES, GlobalSecondaryIndex, table_schema

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "dynamodb_tables"

#: The mandatory tags every resource must carry.
_MANDATORY_TAGS: Final[frozenset[str]] = frozenset(
    {"service", "environment", "owner", "cost-center", "data-classification", "AppName"}
)
#: Mandatory tags whose value is fixed, not merely present.
_MANDATORY_TAG_VALUES: Final[Mapping[str, str]] = {"AppName": "crm-agent-eval"}

#: AWS-managed (not customer-managed) DynamoDB key alias. A table on this key returns
#: `SSEType: "KMS"` with a `KMSMasterKeyArn`, which a naive `SSEType == "KMS"` check would
#: wrongly accept as satisfying the customer-managed-CMK requirement.
_AWS_MANAGED_KEY_ALIAS_FRAGMENT: Final[str] = "alias/aws/dynamodb"


def _physical_table_names(ctx: SmokeContext) -> dict[str, str]:
    """Return the deployed logical->physical table name map from the `dynamodb_table_names` output."""
    mapping = ctx.outputs.require_mapping("dynamodb_table_names")
    return {str(k): str(v) for k, v in mapping.items()}


def _check_table_reconciliation(deployed: Mapping[str, str]) -> Iterable[CheckResult]:
    """Assert the schema's table set and the deployed table set agree exactly."""
    deployed_names = set(deployed)
    missing_from_deployment = sorted(TABLE_NAMES - deployed_names)
    extra_in_deployment = sorted(deployed_names - TABLE_NAMES)

    if missing_from_deployment:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="schema_matches_deployment",
            status="fail",
            detail=f"table(s) declared in schema but absent from deployment: {missing_from_deployment}",
        )
    elif extra_in_deployment:
        yield CheckResult(
            resource=RESOURCE_CLASS,
            check="schema_matches_deployment",
            status="fail",
            detail=f"table(s) deployed but not declared in schema: {extra_in_deployment}",
        )
    else:
        yield CheckResult(resource=RESOURCE_CLASS, check="schema_matches_deployment", status="pass")


def _check_encryption(table_name: str, table_description: Mapping[str, Any]) -> CheckResult:
    """Assert `table_name` uses a customer-managed KMS CMK, not an AWS-owned or AWS-managed key."""
    check_name = "encryption_at_rest_kms_cmk"
    sse_description = table_description.get("SSEDescription")
    if not sse_description:
        return CheckResult(
            resource=table_name,
            check=check_name,
            status="fail",
            detail="SSEDescription absent: table is encrypted with the AWS-owned key, not a CMK",
        )

    sse_type = sse_description.get("SSEType")
    if sse_type != "KMS":
        return CheckResult(
            resource=table_name,
            check=check_name,
            status="fail",
            detail=f"expected SSEType='KMS', got {sse_type!r}",
        )

    key_arn = sse_description.get("KMSMasterKeyArn")
    if not key_arn:
        return CheckResult(
            resource=table_name,
            check=check_name,
            status="fail",
            detail="SSEType is KMS but KMSMasterKeyArn is absent",
        )
    if _AWS_MANAGED_KEY_ALIAS_FRAGMENT in key_arn:
        return CheckResult(
            resource=table_name,
            check=check_name,
            status="fail",
            detail=(
                f"KMSMasterKeyArn {key_arn!r} is the AWS-managed key "
                f"({_AWS_MANAGED_KEY_ALIAS_FRAGMENT}), not a customer-managed CMK"
            ),
        )
    return CheckResult(resource=table_name, check=check_name, status="pass", detail=key_arn)


def _check_pitr(ctx: SmokeContext, table_name: str) -> CheckResult:
    """Assert point-in-time recovery is `ENABLED` for `table_name`."""
    check_name = "point_in_time_recovery"
    dynamodb = ctx.client("dynamodb")
    response = dynamodb.describe_continuous_backups(TableName=table_name)
    pitr_status = (
        response.get("ContinuousBackupsDescription", {})
        .get("PointInTimeRecoveryDescription", {})
        .get("PointInTimeRecoveryStatus")
    )
    if pitr_status != "ENABLED":
        return CheckResult(
            resource=table_name,
            check=check_name,
            status="fail",
            detail=f"expected PointInTimeRecoveryStatus='ENABLED', got {pitr_status!r}",
        )
    return CheckResult(resource=table_name, check=check_name, status="pass")


def _check_tags(ctx: SmokeContext, table_name: str, table_arn: str) -> CheckResult:
    """Assert `table_name` carries every mandatory tag, at its fixed value where one is set."""
    check_name = "mandatory_tags"
    dynamodb = ctx.client("dynamodb")
    response = dynamodb.list_tags_of_resource(ResourceArn=table_arn)
    tags = {str(tag["Key"]): str(tag["Value"]) for tag in response.get("Tags", [])}
    missing = sorted(_MANDATORY_TAGS - tags.keys())
    if missing:
        return CheckResult(
            resource=table_name,
            check=check_name,
            status="fail",
            detail=f"missing tag(s): {missing}",
        )
    wrong = {k: tags[k] for k, v in _MANDATORY_TAG_VALUES.items() if tags[k] != v}
    if wrong:
        return CheckResult(
            resource=table_name,
            check=check_name,
            status="fail",
            detail=f"wrong tag value(s): {wrong}",
        )
    return CheckResult(resource=table_name, check=check_name, status="pass")


def _gsi_projection_missing(
    gsi: GlobalSecondaryIndex, deployed_projection: Mapping[str, Any]
) -> frozenset[str]:
    """The schema-declared projected attributes a deployed GSI's projection does not cover.

    `ProjectionType: "ALL"` covers everything. `"INCLUDE"` covers its `NonKeyAttributes` plus the
    index's key attributes. Any other projection type covers none of them.
    """
    projection_type = deployed_projection.get("ProjectionType")
    if projection_type == "ALL":
        return frozenset()
    if projection_type != "INCLUDE":
        return frozenset(gsi.projected_attributes)
    key_attrs = {gsi.partition_key} | ({gsi.sort_key} if gsi.sort_key else set())
    available = set(deployed_projection.get("NonKeyAttributes", [])) | key_attrs
    return frozenset(gsi.projected_attributes - available)


def _check_gsi(
    table_name: str,
    gsi: GlobalSecondaryIndex,
    deployed_gsis_by_name: Mapping[str, Mapping[str, Any]],
) -> CheckResult:
    """Assert `gsi` is present, `ACTIVE`, and its deployed projection covers what the schema needs."""
    check_name = f"gsi_present:{gsi.name}"
    deployed = deployed_gsis_by_name.get(gsi.name)
    if deployed is None:
        return CheckResult(
            resource=table_name,
            check=check_name,
            status="fail",
            detail=f"table {table_name!r} is missing declared GSI {gsi.name!r}",
        )

    status = deployed.get("IndexStatus")
    if status != "ACTIVE":
        return CheckResult(
            resource=table_name,
            check=check_name,
            status="fail",
            detail=f"table {table_name!r} GSI {gsi.name!r} is not ACTIVE (status={status!r})",
        )

    deployed_projection = deployed.get("Projection", {})
    missing = _gsi_projection_missing(gsi, deployed_projection)
    if missing:
        return CheckResult(
            resource=table_name,
            check=check_name,
            status="fail",
            detail=(
                f"table {table_name!r} GSI {gsi.name!r} projection {deployed_projection!r} does "
                f"not cover required attribute(s) {sorted(missing)!r}"
            ),
        )
    return CheckResult(resource=table_name, check=check_name, status="pass")


def _check_gsi_queryable(
    ctx: SmokeContext, table_name: str, gsi: GlobalSecondaryIndex
) -> CheckResult:
    """Issue a bounded, read-only `query` against `gsi` to prove it actually serves reads.

    Uses the smoke tenant (when supplied) as the partition-key probe value; a `ValidationException`
    or any other error querying the index is reported as a failure rather than raised, so sibling
    checks still run.
    """
    check_name = f"gsi_queryable:{gsi.name}"
    dynamodb = ctx.client("dynamodb")
    probe_value = ctx.tenant or "smoke-probe"
    try:
        dynamodb.query(
            TableName=table_name,
            IndexName=gsi.name,
            KeyConditionExpression="#pk = :pk",
            ExpressionAttributeNames={"#pk": gsi.partition_key},
            ExpressionAttributeValues={":pk": {"S": probe_value}},
            Limit=1,
        )
    except Exception as exc:  # noqa: BLE001 - report as a failing check, not a crash
        return CheckResult(
            resource=table_name,
            check=check_name,
            status="fail",
            detail=f"query against GSI {gsi.name!r} raised: {exc}",
        )
    return CheckResult(resource=table_name, check=check_name, status="pass")


def checks(ctx: SmokeContext) -> Iterable[CheckResult]:
    """Yield one `CheckResult` per (table, assertion) pair across every declared table.

    Never raises for a check failure: an absent CMK, disabled PITR, a missing/inactive/
    under-projected GSI, or a missing tag is a `fail` naming the table (and index, where applicable).
    """
    results: list[CheckResult] = []
    deployed = _physical_table_names(ctx)
    results.extend(_check_table_reconciliation(deployed))

    dynamodb = ctx.client("dynamodb")
    for logical_name in sorted(TABLE_NAMES):
        physical_name = deployed.get(logical_name)
        if physical_name is None:
            # Already reported by _check_table_reconciliation.
            continue

        schema = table_schema(logical_name)

        try:
            description = dynamodb.describe_table(TableName=physical_name)["Table"]
        except Exception as exc:  # noqa: BLE001
            results.append(
                CheckResult(
                    resource=physical_name,
                    check="describe_table",
                    status="fail",
                    detail=f"describe_table raised: {exc}",
                )
            )
            continue

        results.append(_check_encryption(physical_name, description))
        results.append(_check_pitr(ctx, physical_name))

        table_arn = description.get(
            "TableArn", f"arn:aws:dynamodb:{ctx.region}::table/{physical_name}"
        )
        results.append(_check_tags(ctx, physical_name, table_arn))

        deployed_gsis_by_name = {
            gsi["IndexName"]: gsi for gsi in description.get("GlobalSecondaryIndexes", [])
        }
        for gsi in schema.gsis.values():
            gsi_present_result = _check_gsi(physical_name, gsi, deployed_gsis_by_name)
            results.append(gsi_present_result)
            if gsi_present_result.status == "pass":
                results.append(_check_gsi_queryable(ctx, physical_name, gsi))

    return results


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the DynamoDB table smoke checks and return the process exit code."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

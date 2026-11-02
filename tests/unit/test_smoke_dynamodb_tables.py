# SPDX-License-Identifier: MIT
"""Offline unit tests for `scripts.smoke.dynamodb_tables`."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber
from scripts.smoke.dynamodb_tables import checks
from scripts.smoke.harness import context_for_test

from crm.store.schema import TABLE_NAMES, table_schema

#: Placeholder account id allowlisted by `scripts/check_no_account_id.sh`.
_PLACEHOLDER_ACCOUNT_ID = "123456789012"
#: Placeholder credential for a client whose calls never leave the process; not a secret.
_STUB_CRED = "test"

#: Every table's physical name is identical to its logical name for these tests.
_DEPLOYED_TABLE_NAMES = {name: name for name in TABLE_NAMES}

_MANDATORY_TAGS = {
    "service": "crm-agent-eval",
    "environment": "dev",
    "owner": "crm-team",
    "cost-center": "cc-1234",
    "data-classification": "confidential",
    "AppName": "crm-agent-eval",
}

_CMK_ARN = (
    f"arn:aws:kms:us-east-1:{_PLACEHOLDER_ACCOUNT_ID}:key/11111111-1111-1111-1111-111111111111"
)
_AWS_MANAGED_KEY_ARN = f"arn:aws:kms:us-east-1:{_PLACEHOLDER_ACCOUNT_ID}:key/alias/aws/dynamodb"


def _dynamodb_client() -> Any:
    """A real boto3 `dynamodb` client, never used for network I/O: `Stubber` intercepts first."""
    return boto3.client(
        "dynamodb",
        region_name="us-east-1",
        aws_access_key_id=_STUB_CRED,
        aws_secret_access_key=_STUB_CRED,
    )


def _table_arn(table_name: str) -> str:
    return f"arn:aws:dynamodb:us-east-1:{_PLACEHOLDER_ACCOUNT_ID}:table/{table_name}"


def _gsi_description(name: str, partition_key: str, sort_key: str | None) -> dict[str, Any]:
    key_schema = [{"AttributeName": partition_key, "KeyType": "HASH"}]
    if sort_key is not None:
        key_schema.append({"AttributeName": sort_key, "KeyType": "RANGE"})
    return {
        "IndexName": name,
        "KeySchema": key_schema,
        "Projection": {"ProjectionType": "ALL"},
        "IndexStatus": "ACTIVE",
    }


def _describe_table_response(
    table_name: str,
    *,
    sse_description: dict[str, Any] | None,
    gsi_overrides: dict[str, dict[str, Any]] | None = None,
    omit_gsis: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Build a `describe_table` response matching the schema's declared GSIs, minus overrides."""
    schema = table_schema(table_name)
    gsi_overrides = gsi_overrides or {}
    global_secondary_indexes = []
    for gsi in schema.gsis.values():
        if gsi.name in omit_gsis:
            continue
        description = _gsi_description(gsi.name, gsi.partition_key, gsi.sort_key)
        if gsi.name in gsi_overrides:
            description = {**description, **gsi_overrides[gsi.name]}
        global_secondary_indexes.append(description)

    table: dict[str, Any] = {
        "TableName": table_name,
        "TableArn": _table_arn(table_name),
        "TableStatus": "ACTIVE",
    }
    if sse_description is not None:
        table["SSEDescription"] = sse_description
    if global_secondary_indexes:
        table["GlobalSecondaryIndexes"] = global_secondary_indexes
    return {"Table": table}


def _pitr_response(*, status: str = "ENABLED") -> dict[str, Any]:
    return {
        "ContinuousBackupsDescription": {
            "ContinuousBackupsStatus": "ENABLED",
            "PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": status},
        }
    }


def _tags_response(tags: dict[str, str]) -> dict[str, Any]:
    return {"Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}


def _query_response() -> dict[str, Any]:
    return {"Items": [], "Count": 0, "ScannedCount": 0}


def _stub_happy_table(
    stubber: Stubber,
    table_name: str,
    *,
    sse_description: dict[str, Any] | None,
    pitr_status: str = "ENABLED",
    tags: dict[str, str] | None = None,
    gsi_overrides: dict[str, dict[str, Any]] | None = None,
    omit_gsis: frozenset[str] = frozenset(),
    skip_gsi_queries_for: frozenset[str] = frozenset(),
) -> None:
    """Queue the full sequence of stubbed responses `checks()` issues for one table.

    Call order matches `checks()`: describe_table, describe_continuous_backups,
    list_tags_of_resource, then one query per declared GSI that is present (in schema iteration
    order), skipping a GSI query when it's expected not to be issued (e.g. the GSI is missing).
    """
    schema = table_schema(table_name)
    stubber.add_response(
        "describe_table",
        _describe_table_response(
            table_name,
            sse_description=sse_description,
            gsi_overrides=gsi_overrides,
            omit_gsis=omit_gsis,
        ),
        {"TableName": table_name},
    )
    stubber.add_response(
        "describe_continuous_backups",
        _pitr_response(status=pitr_status),
        {"TableName": table_name},
    )
    stubber.add_response(
        "list_tags_of_resource",
        _tags_response(tags if tags is not None else _MANDATORY_TAGS),
        {"ResourceArn": _table_arn(table_name)},
    )
    for gsi in schema.gsis.values():
        if gsi.name in omit_gsis or gsi.name in skip_gsi_queries_for:
            continue
        override = (gsi_overrides or {}).get(gsi.name, {})
        if override.get("IndexStatus", "ACTIVE") != "ACTIVE":
            continue
        stubber.add_response(
            "query",
            _query_response(),
            {
                "TableName": table_name,
                "IndexName": gsi.name,
                "KeyConditionExpression": ANY,
                "ExpressionAttributeNames": ANY,
                "ExpressionAttributeValues": ANY,
                "Limit": 1,
            },
        )


def _outputs() -> dict[str, Any]:
    return {"dynamodb_table_names": dict(_DEPLOYED_TABLE_NAMES)}


# ---------------------------------------------------------------------------
# Happy path: every table passes every assertion.
# ---------------------------------------------------------------------------


def test_all_tables_pass_when_everything_is_correctly_configured() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    for table_name in sorted(TABLE_NAMES):
        _stub_happy_table(
            stubber,
            table_name,
            sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN},
        )
    stubber.activate()

    ctx = context_for_test(outputs=_outputs(), clients={"dynamodb": client})
    results = list(checks(ctx))
    stubber.deactivate()

    assert results, "expected at least one check result"
    failed = [r for r in results if r.status == "fail"]
    assert not failed, f"unexpected failures: {failed}"

    checked_tables = {r.resource for r in results if r.resource != "dynamodb_tables"}
    assert checked_tables == set(TABLE_NAMES)


# ---------------------------------------------------------------------------
# Encryption: AWS-owned key (SSEDescription absent) and AWS-managed key both fail.
# ---------------------------------------------------------------------------


def test_aws_owned_key_absent_sse_description_is_detected() -> None:
    """A table on the AWS-owned key (no `SSEDescription`) fails the CMK check."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    table_name = "accounts"
    _stub_happy_table(stubber, table_name, sse_description=None)
    # `checks()` walks the full deployed map, so every other table is stubbed too.
    for other in sorted(TABLE_NAMES - {table_name}):
        _stub_happy_table(
            stubber, other, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        )
    stubber.activate()

    ctx = context_for_test(outputs=_outputs(), clients={"dynamodb": client})
    results = list(checks(ctx))
    stubber.deactivate()

    encryption_result = next(
        r for r in results if r.resource == table_name and r.check == "encryption_at_rest_kms_cmk"
    )
    assert encryption_result.status == "fail"
    assert "AWS-owned" in encryption_result.detail

    # Every other table is still checked (inventory stays complete despite one failure).
    checked_tables = {r.resource for r in results if r.resource != "dynamodb_tables"}
    assert checked_tables == set(TABLE_NAMES)


def test_aws_managed_key_is_detected_as_not_customer_managed() -> None:
    """`SSEType=KMS` with the AWS-managed `alias/aws/dynamodb`-backed key must still fail."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    table_name = "accounts"
    _stub_happy_table(
        stubber,
        table_name,
        sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _AWS_MANAGED_KEY_ARN},
    )
    for other in sorted(TABLE_NAMES - {table_name}):
        _stub_happy_table(
            stubber, other, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        )
    stubber.activate()

    ctx = context_for_test(outputs=_outputs(), clients={"dynamodb": client})
    results = list(checks(ctx))
    stubber.deactivate()

    encryption_result = next(
        r for r in results if r.resource == table_name and r.check == "encryption_at_rest_kms_cmk"
    )
    assert encryption_result.status == "fail"
    assert "AWS-managed" in encryption_result.detail


def test_sse_type_not_kms_is_detected() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    table_name = "accounts"
    _stub_happy_table(stubber, table_name, sse_description={"SSEType": "AES256"})
    for other in sorted(TABLE_NAMES - {table_name}):
        _stub_happy_table(
            stubber, other, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        )
    stubber.activate()

    ctx = context_for_test(outputs=_outputs(), clients={"dynamodb": client})
    results = list(checks(ctx))
    stubber.deactivate()

    encryption_result = next(
        r for r in results if r.resource == table_name and r.check == "encryption_at_rest_kms_cmk"
    )
    assert encryption_result.status == "fail"
    assert "AES256" in encryption_result.detail


# ---------------------------------------------------------------------------
# PITR
# ---------------------------------------------------------------------------


def test_pitr_disabled_is_detected() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    table_name = "accounts"
    _stub_happy_table(
        stubber,
        table_name,
        sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN},
        pitr_status="DISABLED",
    )
    for other in sorted(TABLE_NAMES - {table_name}):
        _stub_happy_table(
            stubber, other, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        )
    stubber.activate()

    ctx = context_for_test(outputs=_outputs(), clients={"dynamodb": client})
    results = list(checks(ctx))
    stubber.deactivate()

    pitr_result = next(
        r for r in results if r.resource == table_name and r.check == "point_in_time_recovery"
    )
    assert pitr_result.status == "fail"
    assert "DISABLED" in pitr_result.detail


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def test_missing_data_classification_tag_is_detected() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    table_name = "accounts"
    incomplete_tags = {k: v for k, v in _MANDATORY_TAGS.items() if k != "data-classification"}
    _stub_happy_table(
        stubber,
        table_name,
        sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN},
        tags=incomplete_tags,
    )
    for other in sorted(TABLE_NAMES - {table_name}):
        _stub_happy_table(
            stubber, other, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        )
    stubber.activate()

    ctx = context_for_test(outputs=_outputs(), clients={"dynamodb": client})
    results = list(checks(ctx))
    stubber.deactivate()

    tags_result = next(
        r for r in results if r.resource == table_name and r.check == "mandatory_tags"
    )
    assert tags_result.status == "fail"
    assert "data-classification" in tags_result.detail


def _tags_result_for(tags: dict[str, str]) -> Any:
    client = _dynamodb_client()
    stubber = Stubber(client)
    table_name = "accounts"
    _stub_happy_table(
        stubber,
        table_name,
        sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN},
        tags=tags,
    )
    for other in sorted(TABLE_NAMES - {table_name}):
        _stub_happy_table(
            stubber, other, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        )
    stubber.activate()
    ctx = context_for_test(outputs=_outputs(), clients={"dynamodb": client})
    results = list(checks(ctx))
    stubber.deactivate()
    return next(r for r in results if r.resource == table_name and r.check == "mandatory_tags")


def test_missing_app_name_tag_is_detected() -> None:
    result = _tags_result_for({k: v for k, v in _MANDATORY_TAGS.items() if k != "AppName"})
    assert result.status == "fail"
    assert "AppName" in result.detail


def test_wrong_app_name_value_is_detected() -> None:
    result = _tags_result_for(_MANDATORY_TAGS | {"AppName": "other-app"})
    assert result.status == "fail"
    assert "other-app" in result.detail


def test_the_full_tag_set_passes() -> None:
    assert _tags_result_for(_MANDATORY_TAGS).status == "pass"


# ---------------------------------------------------------------------------
# GSIs: missing, inactive, under-projected.
# ---------------------------------------------------------------------------


def test_missing_gsi_fails_naming_table_and_index() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    table_name = "accounts"
    schema = table_schema(table_name)
    missing_index_name = next(iter(schema.gsis)).lower()
    # The omitted index is the schema's first declared GSI.
    missing_index_name = next(iter(schema.gsis.values())).name

    _stub_happy_table(
        stubber,
        table_name,
        sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN},
        omit_gsis=frozenset({missing_index_name}),
    )
    for other in sorted(TABLE_NAMES - {table_name}):
        _stub_happy_table(
            stubber, other, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        )
    stubber.activate()

    ctx = context_for_test(outputs=_outputs(), clients={"dynamodb": client})
    results = list(checks(ctx))
    stubber.deactivate()

    gsi_result = next(
        r
        for r in results
        if r.resource == table_name and r.check == f"gsi_present:{missing_index_name}"
    )
    assert gsi_result.status == "fail"
    assert table_name in gsi_result.detail
    assert missing_index_name in gsi_result.detail

    # No query check should have been issued for the missing index.
    assert not any(
        r.resource == table_name and r.check == f"gsi_queryable:{missing_index_name}"
        for r in results
    )


def test_gsi_not_active_fails_and_is_not_queried() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    table_name = "accounts"
    schema = table_schema(table_name)
    creating_index_name = next(iter(schema.gsis.values())).name

    _stub_happy_table(
        stubber,
        table_name,
        sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN},
        gsi_overrides={creating_index_name: {"IndexStatus": "CREATING"}},
    )
    for other in sorted(TABLE_NAMES - {table_name}):
        _stub_happy_table(
            stubber, other, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        )
    stubber.activate()

    ctx = context_for_test(outputs=_outputs(), clients={"dynamodb": client})
    results = list(checks(ctx))
    stubber.deactivate()

    gsi_result = next(
        r
        for r in results
        if r.resource == table_name and r.check == f"gsi_present:{creating_index_name}"
    )
    assert gsi_result.status == "fail"
    assert "CREATING" in gsi_result.detail
    assert not any(
        r.resource == table_name and r.check == f"gsi_queryable:{creating_index_name}"
        for r in results
    )


def test_gsi_projection_missing_tenant_is_detected() -> None:
    """A deployed GSI projection that omits `tenant` fails."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    table_name = "accounts"
    schema = table_schema(table_name)
    target_gsi = next(iter(schema.gsis.values()))

    _stub_happy_table(
        stubber,
        table_name,
        sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN},
        gsi_overrides={
            target_gsi.name: {
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": ["account_id"],
                }
            }
        },
    )
    for other in sorted(TABLE_NAMES - {table_name}):
        _stub_happy_table(
            stubber, other, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        )
    stubber.activate()

    ctx = context_for_test(outputs=_outputs(), clients={"dynamodb": client})
    results = list(checks(ctx))
    stubber.deactivate()

    gsi_result = next(
        r
        for r in results
        if r.resource == table_name and r.check == f"gsi_present:{target_gsi.name}"
    )
    assert gsi_result.status == "fail"
    assert "tenant" in gsi_result.detail


#: The scored new-row attributes the `gsi_tenant` projections must carry, by table.
_SCORED_NEW_ROW_PROJECTIONS = (("activities", "subject"), ("notes", "body"))


def _gsi_tenant_result(table_name: str, non_key_attributes: list[str]) -> Any:
    """`gsi_present:gsi_tenant` on `table_name` when its deployed projection is that INCLUDE list."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_happy_table(
        stubber,
        table_name,
        sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN},
        gsi_overrides={
            "gsi_tenant": {
                "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": non_key_attributes}
            }
        },
    )
    for other in sorted(TABLE_NAMES - {table_name}):
        _stub_happy_table(
            stubber, other, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        )
    stubber.activate()
    ctx = context_for_test(outputs=_outputs(), clients={"dynamodb": client})
    results = list(checks(ctx))
    stubber.deactivate()
    return next(
        r for r in results if r.resource == table_name and r.check == "gsi_present:gsi_tenant"
    )


@pytest.mark.parametrize(("table_name", "attribute"), _SCORED_NEW_ROW_PROJECTIONS)
def test_gsi_tenant_projection_without_the_scored_attribute_fails(
    table_name: str, attribute: str
) -> None:
    """A deployed `gsi_tenant` projecting everything the schema declares except the scored
    new-row attribute fails, naming exactly that attribute; the full projection passes."""
    gsi = table_schema(table_name).gsis["gsi_tenant"]
    assert attribute in gsi.projected_attributes
    keys = {gsi.partition_key} | ({gsi.sort_key} if gsi.sort_key else set())
    full = sorted(gsi.projected_attributes - keys)

    complete = _gsi_tenant_result(table_name, full)
    assert complete.status == "pass", complete.detail

    without = _gsi_tenant_result(table_name, [a for a in full if a != attribute])
    assert without.status == "fail"
    assert f"[{attribute!r}]" in without.detail


def test_keys_only_projection_still_fails() -> None:
    """A `KEYS_ONLY` projection covers no declared non-key attribute, so it fails."""
    client = _dynamodb_client()
    stubber = Stubber(client)
    _stub_happy_table(
        stubber,
        "activities",
        sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN},
        gsi_overrides={"gsi_tenant": {"Projection": {"ProjectionType": "KEYS_ONLY"}}},
    )
    for other in sorted(TABLE_NAMES - {"activities"}):
        _stub_happy_table(
            stubber, other, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        )
    stubber.activate()
    ctx = context_for_test(outputs=_outputs(), clients={"dynamodb": client})
    results = list(checks(ctx))
    stubber.deactivate()
    row = next(
        r for r in results if r.resource == "activities" and r.check == "gsi_present:gsi_tenant"
    )
    assert row.status == "fail"


def test_gsi_query_failure_is_detected() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    table_name = "accounts"
    schema = table_schema(table_name)
    target_gsi = next(iter(schema.gsis.values()))

    stubber.add_response(
        "describe_table",
        _describe_table_response(
            table_name, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        ),
        {"TableName": table_name},
    )
    stubber.add_response("describe_continuous_backups", _pitr_response(), {"TableName": table_name})
    stubber.add_response(
        "list_tags_of_resource",
        _tags_response(_MANDATORY_TAGS),
        {"ResourceArn": _table_arn(table_name)},
    )
    stubber.add_client_error(
        "query",
        service_error_code="ValidationException",
        service_message="index not queryable",
    )
    for remaining_gsi in list(schema.gsis.values())[1:]:
        stubber.add_response(
            "query",
            _query_response(),
            {
                "TableName": table_name,
                "IndexName": remaining_gsi.name,
                "KeyConditionExpression": ANY,
                "ExpressionAttributeNames": ANY,
                "ExpressionAttributeValues": ANY,
                "Limit": 1,
            },
        )
    for other in sorted(TABLE_NAMES - {table_name}):
        _stub_happy_table(
            stubber, other, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        )
    stubber.activate()

    ctx = context_for_test(outputs=_outputs(), clients={"dynamodb": client})
    results = list(checks(ctx))
    stubber.deactivate()

    query_result = next(
        r
        for r in results
        if r.resource == table_name and r.check == f"gsi_queryable:{target_gsi.name}"
    )
    assert query_result.status == "fail"
    assert target_gsi.name in query_result.detail


# ---------------------------------------------------------------------------
# Reconciliation between schema and deployment.
# ---------------------------------------------------------------------------


def test_table_missing_from_deployment_is_detected() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    incomplete_deployed = {k: v for k, v in _DEPLOYED_TABLE_NAMES.items() if k != "accounts"}
    for table_name in sorted(incomplete_deployed):
        _stub_happy_table(
            stubber, table_name, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        )
    stubber.activate()

    ctx = context_for_test(
        outputs={"dynamodb_table_names": incomplete_deployed}, clients={"dynamodb": client}
    )
    results = list(checks(ctx))
    stubber.deactivate()

    reconciliation_result = next(r for r in results if r.check == "schema_matches_deployment")
    assert reconciliation_result.status == "fail"
    assert "accounts" in reconciliation_result.detail

    # The missing table itself is never described (it has no physical name to describe).
    assert not any(r.resource == "accounts" for r in results)


def test_extra_table_in_deployment_is_detected() -> None:
    client = _dynamodb_client()
    stubber = Stubber(client)
    extra_deployed = {**_DEPLOYED_TABLE_NAMES, "mystery_table": "mystery_table"}
    for table_name in sorted(TABLE_NAMES):
        _stub_happy_table(
            stubber, table_name, sse_description={"SSEType": "KMS", "KMSMasterKeyArn": _CMK_ARN}
        )
    stubber.activate()

    ctx = context_for_test(
        outputs={"dynamodb_table_names": extra_deployed}, clients={"dynamodb": client}
    )
    results = list(checks(ctx))
    stubber.deactivate()

    reconciliation_result = next(r for r in results if r.check == "schema_matches_deployment")
    assert reconciliation_result.status == "fail"
    assert "mystery_table" in reconciliation_result.detail


# ---------------------------------------------------------------------------
# main() smoke: --json emits the pinned shape.
# ---------------------------------------------------------------------------


def test_main_missing_outputs_fails_cleanly(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.smoke.dynamodb_tables import main

    outputs_file = tmp_path / "tf-outputs.json"
    outputs_file.write_text("{}", encoding="utf-8")

    exit_code = main(["--outputs", str(outputs_file), "--json"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert '"layer": 4' in captured.out
    assert '"resource_class": "dynamodb_tables"' in captured.out

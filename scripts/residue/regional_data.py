# SPDX-License-Identifier: MIT
"""Residue scanners for regional data services.

`SCANNERS` maps each family key (s3, dynamodb, kms, logs, ecr, ssm, secretsmanager, events,
cloudtrail) to a `FamilyScanner` that lists project-owned resources through the read-only
`core.call`/`core.paginate` helpers. A failed list call surfaces as `core.Unmeasurable`.
"""

from __future__ import annotations

from typing import Any, cast

from scripts.residue.core import (
    ClientFactory,
    FamilyName,
    FamilyScanner,
    Resource,
    ScanConfig,
    call,
    has_project_tag,
    matches_prefix,
    paginate,
)

KMS_ALIAS_PREFIX = "alias/"


def _contains_prefix(name: str, config: ScanConfig) -> bool:
    return any(prefix in name for prefix in config.name_prefixes)


def scan_s3(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """List project buckets account-wide; each bucket's region goes in `detail["bucket_region"]`."""
    client = factory("s3", config.region)
    found: list[Resource] = []
    for bucket in paginate(client, "s3", "list_buckets", "Buckets"):
        name = bucket["Name"]
        if not matches_prefix(name, config):
            continue
        region = bucket.get("BucketRegion")
        if not region:
            location = call(client, "s3", "get_bucket_location", Bucket=name)
            region = location.get("LocationConstraint") or "us-east-1"
        found.append(
            Resource(
                family="s3",
                kind="bucket",
                name=name,
                arn=bucket.get("BucketArn") or f"arn:aws:s3:::{name}",
                region="global",
                detail={"bucket_region": region},
            )
        )
    return found


def scan_dynamodb(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """List project tables by name prefix, with the ARN from `describe_table`."""
    client = factory("dynamodb", config.region)
    found: list[Resource] = []
    for entry in paginate(client, "dynamodb", "list_tables", "TableNames"):
        name = cast(str, entry)
        if not matches_prefix(name, config):
            continue
        table = call(client, "dynamodb", "describe_table", TableName=name)["Table"]
        found.append(
            Resource(
                family="dynamodb",
                kind="table",
                name=name,
                arn=table.get("TableArn"),
                region=config.region,
                detail={"table_status": str(table.get("TableStatus", ""))},
            )
        )
    return found


def scan_kms(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """List project aliases, and customer-managed keys targeted by one or carrying the tag.

    Each key carries `detail["key_state"]` from `describe_key`.
    """
    client = factory("kms", config.region)
    found: list[Resource] = []
    aliased_keys: set[str] = set()
    for alias in paginate(client, "kms", "list_aliases", "Aliases"):
        alias_name = alias["AliasName"]
        if not matches_prefix(alias_name.removeprefix(KMS_ALIAS_PREFIX), config):
            continue
        target = alias.get("TargetKeyId")
        if target:
            aliased_keys.add(target)
        found.append(
            Resource(
                family="kms",
                kind="alias",
                name=alias_name,
                arn=alias.get("AliasArn"),
                region=config.region,
                detail={"target_key_id": target or ""},
            )
        )
    for key in paginate(client, "kms", "list_keys", "Keys"):
        key_id = key["KeyId"]
        metadata = call(client, "kms", "describe_key", KeyId=key_id)["KeyMetadata"]
        if metadata.get("KeyManager") != "CUSTOMER":
            continue
        if key_id not in aliased_keys and not _key_has_project_tag(client, key_id, config):
            continue
        found.append(
            Resource(
                family="kms",
                kind="key",
                name=key_id,
                arn=metadata.get("Arn") or key.get("KeyArn"),
                region=config.region,
                detail={"key_state": str(metadata.get("KeyState", ""))},
            )
        )
    return found


def _key_has_project_tag(client: Any, key_id: str, config: ScanConfig) -> bool:
    tags = paginate(client, "kms", "list_resource_tags", "Tags", KeyId=key_id)
    return has_project_tag({tag["TagKey"]: tag["TagValue"] for tag in tags}, config)


def scan_logs(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """List log groups whose name contains a project prefix anywhere."""
    client = factory("logs", config.region)
    found: dict[str, Resource] = {}
    for prefix in config.name_prefixes:
        groups = paginate(
            client, "logs", "describe_log_groups", "logGroups", logGroupNamePattern=prefix
        )
        for group in groups:
            name = group["logGroupName"]
            if name in found or not _contains_prefix(name, config):
                continue
            arn = group.get("logGroupArn") or str(group.get("arn", "")).removesuffix(":*")
            found[name] = Resource(
                family="logs",
                kind="log-group",
                name=name,
                arn=arn or None,
                region=config.region,
            )
    return list(found.values())


def scan_ecr(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """List ECR repositories by name prefix."""
    client = factory("ecr", config.region)
    return [
        Resource(
            family="ecr",
            kind="repository",
            name=repo["repositoryName"],
            arn=repo.get("repositoryArn"),
            region=config.region,
        )
        for repo in paginate(client, "ecr", "describe_repositories", "repositories")
        if matches_prefix(repo["repositoryName"], config)
    ]


def scan_ssm(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """List SSM parameters whose name, ignoring a leading `/`, starts with a project prefix."""
    client = factory("ssm", config.region)
    return [
        Resource(
            family="ssm",
            kind="parameter",
            name=param["Name"],
            arn=param.get("ARN"),
            region=config.region,
        )
        for param in paginate(client, "ssm", "describe_parameters", "Parameters")
        if matches_prefix(param["Name"].lstrip("/"), config)
    ]


def scan_secretsmanager(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """List secrets by name prefix or project tag."""
    client = factory("secretsmanager", config.region)
    found: list[Resource] = []
    for secret in paginate(client, "secretsmanager", "list_secrets", "SecretList"):
        tags = {tag["Key"]: tag["Value"] for tag in secret.get("Tags", [])}
        if not (matches_prefix(secret["Name"], config) or has_project_tag(tags, config)):
            continue
        found.append(
            Resource(
                family="secretsmanager",
                kind="secret",
                name=secret["Name"],
                arn=secret.get("ARN"),
                region=config.region,
            )
        )
    return found


def scan_events(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """List EventBridge rules on the default bus by name prefix."""
    client = factory("events", config.region)
    return [
        Resource(
            family="events",
            kind="rule",
            name=rule["Name"],
            arn=rule.get("Arn"),
            region=config.region,
        )
        for rule in paginate(client, "events", "list_rules", "Rules")
        if matches_prefix(rule["Name"], config)
    ]


def scan_cloudtrail(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """List CloudTrail trails visible in the region by name prefix."""
    client = factory("cloudtrail", config.region)
    return [
        Resource(
            family="cloudtrail",
            kind="trail",
            name=trail["Name"],
            arn=trail.get("TrailARN"),
            region=trail.get("HomeRegion") or config.region,
        )
        for trail in paginate(client, "cloudtrail", "list_trails", "Trails")
        if matches_prefix(trail["Name"], config)
    ]


SCANNERS: dict[FamilyName, FamilyScanner] = {
    "s3": scan_s3,
    "dynamodb": scan_dynamodb,
    "kms": scan_kms,
    "logs": scan_logs,
    "ecr": scan_ecr,
    "ssm": scan_ssm,
    "secretsmanager": scan_secretsmanager,
    "events": scan_events,
    "cloudtrail": scan_cloudtrail,
}

ENUMERATED_ARN_TYPES: dict[FamilyName, frozenset[str]] = {
    "s3": frozenset({""}),
    "dynamodb": frozenset({"table"}),
    "kms": frozenset({"key", "alias"}),
    "logs": frozenset({"log-group"}),
    "ecr": frozenset({"repository"}),
    "ssm": frozenset({"parameter"}),
    "secretsmanager": frozenset({"secret"}),
    "events": frozenset({"rule"}),
    "cloudtrail": frozenset({"trail"}),
}
"""Per family, the `core.arn_resource_type` values of the resources its scanner returns."""

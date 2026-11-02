# SPDX-License-Identifier: MIT
"""Residue scanners for account-global services (IAM, CloudFront) and the tag-index scan.

`SCANNERS` maps family name to a `FamilyScanner`; `ARN_SERVICE_TO_FAMILY` maps an ARN service
segment to the family that scans it, for every family in the residue package; `tag_index_scan`
lists every resource carrying the project tag through the Resource Groups Tagging API. Every AWS
call goes through `core.call`/`core.paginate`, so every failure surfaces as `core.Unmeasurable`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from scripts.residue.core import (
    TAG_INDEX_FAMILY,
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

GLOBAL_REGION: Final = "global"
GLOBAL_API_REGION: Final = "us-east-1"
SERVICE_LINKED_ROLE_PATH: Final = "/aws-service-role/"
AWS_MANAGED_POLICY_ARN_MARKER: Final = ":iam::aws:policy/"

ARN_SERVICE_TO_FAMILY: dict[str, FamilyName] = {
    "s3": "s3",
    "dynamodb": "dynamodb",
    "kms": "kms",
    "logs": "logs",
    "ecr": "ecr",
    "ssm": "ssm",
    "secretsmanager": "secretsmanager",
    "events": "events",
    "cloudtrail": "cloudtrail",
    "lambda": "lambda",
    "codebuild": "codebuild",
    "apigateway": "apigateway",
    "elasticloadbalancing": "elbv2",
    "ec2": "ec2",
    "cognito-idp": "cognito",
    "states": "stepfunctions",
    "bedrock-agentcore": "agentcore",
    "iam": "iam",
    "cloudfront": "cloudfront",
}


def _global(family: FamilyName, kind: str, name: str, arn: str | None) -> Resource:
    return Resource(family=family, kind=kind, name=name, arn=arn, region=GLOBAL_REGION)


def scan_iam(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """Return project IAM roles, customer-managed policies and instance profiles, by name prefix.

    Service-linked roles and AWS-managed policies are never returned.
    """
    client = factory("iam", GLOBAL_API_REGION)
    found: list[Resource] = []
    for role in paginate(client, "iam", "list_roles", "Roles"):
        if role.get("Path", "").startswith(SERVICE_LINKED_ROLE_PATH):
            continue
        if matches_prefix(role["RoleName"], config):
            found.append(_global("iam", "role", role["RoleName"], role["Arn"]))
    for policy in paginate(client, "iam", "list_policies", "Policies", Scope="Local"):
        if AWS_MANAGED_POLICY_ARN_MARKER in policy["Arn"]:
            continue
        if matches_prefix(policy["PolicyName"], config):
            found.append(_global("iam", "policy", policy["PolicyName"], policy["Arn"]))
    for profile in paginate(client, "iam", "list_instance_profiles", "InstanceProfiles"):
        if matches_prefix(profile["InstanceProfileName"], config):
            found.append(
                _global("iam", "instance-profile", profile["InstanceProfileName"], profile["Arn"])
            )
    return found


def _cloudfront_items(fetch: Callable[..., dict[str, Any]], list_key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    marker: str | None = None
    while True:
        page = fetch(Marker=marker) if marker is not None else fetch()
        listing = page.get(list_key, {})
        items.extend(listing.get("Items", []))
        marker = listing.get("NextMarker")
        if not marker:
            return items


def _project_distributions(client: Any, config: ScanConfig) -> list[Resource]:
    found: list[Resource] = []
    for dist in _cloudfront_items(
        lambda **kw: call(client, "cloudfront", "list_distributions", **kw), "DistributionList"
    ):
        tagged = call(client, "cloudfront", "list_tags_for_resource", Resource=dist["ARN"])
        tags = {t["Key"]: t.get("Value", "") for t in tagged.get("Tags", {}).get("Items", [])}
        if has_project_tag(tags, config):
            found.append(_global("cloudfront", "distribution", dist["Id"], dist["ARN"]))
    return found


def scan_cloudfront(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """Return project CloudFront resources.

    Distributions match on the project tag; VPC origins, origin access controls, custom
    origin-request/cache/response-headers policies and functions match on name prefix.
    """
    client = factory("cloudfront", GLOBAL_API_REGION)
    found = _project_distributions(client, config)
    for origin in _cloudfront_items(
        lambda **kw: call(client, "cloudfront", "list_vpc_origins", **kw), "VpcOriginList"
    ):
        if matches_prefix(origin["Name"], config):
            found.append(_global("cloudfront", "vpc-origin", origin["Name"], origin["Arn"]))
    for oac in _cloudfront_items(
        lambda **kw: call(client, "cloudfront", "list_origin_access_controls", **kw),
        "OriginAccessControlList",
    ):
        if matches_prefix(oac["Name"], config):
            found.append(_global("cloudfront", "origin-access-control", oac["Name"], None))
    custom_policy_lists = (
        (
            lambda **kw: call(
                client, "cloudfront", "list_origin_request_policies", Type="custom", **kw
            ),
            "OriginRequestPolicy",
            "origin-request-policy",
        ),
        (
            lambda **kw: call(client, "cloudfront", "list_cache_policies", Type="custom", **kw),
            "CachePolicy",
            "cache-policy",
        ),
        (
            lambda **kw: call(
                client, "cloudfront", "list_response_headers_policies", Type="custom", **kw
            ),
            "ResponseHeadersPolicy",
            "response-headers-policy",
        ),
    )
    for fetch, item_key, kind in custom_policy_lists:
        for item in _cloudfront_items(fetch, f"{item_key}List"):
            name = item[item_key][f"{item_key}Config"]["Name"]
            if matches_prefix(name, config):
                found.append(_global("cloudfront", kind, name, None))
    for function in _cloudfront_items(
        lambda **kw: call(client, "cloudfront", "list_functions", **kw), "FunctionList"
    ):
        if matches_prefix(function["Name"], config):
            arn = function["FunctionMetadata"]["FunctionARN"]
            found.append(_global("cloudfront", "function", function["Name"], arn))
    return found


SCANNERS: dict[FamilyName, FamilyScanner] = {
    "iam": scan_iam,
    "cloudfront": scan_cloudfront,
}

ENUMERATED_ARN_TYPES: dict[FamilyName, frozenset[str]] = {
    "iam": frozenset({"role", "policy", "instance-profile"}),
    "cloudfront": frozenset({"distribution", "vpcorigin", "function"}),
}
"""Per family, the `core.arn_resource_type` values of the resources its scanner returns with an ARN."""


def _tagged_resource(arn: str) -> Resource:
    parts = arn.split(":", 5)
    service = parts[2] if len(parts) > 2 else ""
    region = parts[3] if len(parts) > 3 and parts[3] else GLOBAL_REGION
    name = parts[5] if len(parts) > 5 else arn
    family = ARN_SERVICE_TO_FAMILY.get(service, service)
    return Resource(family=family, kind="tagged", name=name, arn=arn, region=region)


def _tag_index_query(
    factory: ClientFactory, config: ScanConfig, region: str, **kwargs: Any
) -> list[str]:
    client = factory("resourcegroupstaggingapi", region)
    mappings = paginate(
        client,
        TAG_INDEX_FAMILY,
        "get_resources",
        "ResourceTagMappingList",
        TagFilters=[{"Key": config.tag_key, "Values": [config.tag_value]}],
        **kwargs,
    )
    return [m["ResourceARN"] for m in mappings]


def tag_index_scan(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """Return every resource the tagging index lists with the project tag, as kind `tagged`.

    Queries the scan region and, when that is not us-east-1, us-east-1 for CloudFront ARNs.
    Each ARN appears once.
    """
    arns = _tag_index_query(factory, config, config.region)
    if config.region != GLOBAL_API_REGION:
        arns += _tag_index_query(
            factory, config, GLOBAL_API_REGION, ResourceTypeFilters=["cloudfront"]
        )
    return [_tagged_resource(arn) for arn in dict.fromkeys(arns)]

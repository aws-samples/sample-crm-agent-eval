# SPDX-License-Identifier: MIT
"""Tests for the global-service residue scanners (IAM, CloudFront) and the tag-index scan."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from scripts.residue import core, global_services
from scripts.residue.core import Resource, ScanConfig, Unmeasurable
from scripts.residue.global_services import (
    ARN_SERVICE_TO_FAMILY,
    ENUMERATED_ARN_TYPES,
    SCANNERS,
    scan_cloudfront,
    scan_iam,
    tag_index_scan,
)

ACCOUNT = "123456789012"
NOW = datetime(2026, 10, 19, tzinfo=UTC)
CONFIG = ScanConfig(region="us-east-1")
TAG_FILTERS = [{"Key": "service", "Values": ["crm-agent-eval"]}]
EXPECTED_FAMILIES = {
    "s3",
    "dynamodb",
    "kms",
    "logs",
    "ecr",
    "ssm",
    "secretsmanager",
    "events",
    "cloudtrail",
    "lambda",
    "codebuild",
    "apigateway",
    "elbv2",
    "ec2",
    "cognito",
    "stepfunctions",
    "agentcore",
    "iam",
    "cloudfront",
}
SIBLING_MODULES = ("scripts.residue.regional_data", "scripts.residue.compute_network")


class StubbedFactory:
    """A `ClientFactory` handing out Stubber-backed clients, one per (service, region)."""

    def __init__(self) -> None:
        self._session = boto3.Session(
            aws_access_key_id="testing",
            aws_secret_access_key="testing",  # nosec B106  # fake moto/stub credentials, not a credential
            region_name="us-east-1",
        )
        self.clients: dict[tuple[str, str], Any] = {}
        self.stubbers: dict[tuple[str, str], Stubber] = {}

    def stub(self, service: str, region: str) -> Stubber:
        key = (service, region)
        if key not in self.stubbers:
            client = self._session.client(service, region_name=region)
            self.clients[key] = client
            self.stubbers[key] = Stubber(client)
            self.stubbers[key].activate()
        return self.stubbers[key]

    def __call__(self, service: str, region: str) -> Any:
        key = (service, region)
        if key not in self.clients:
            raise AssertionError(f"unexpected client request {key}")
        return self.clients[key]

    def assert_drained(self) -> None:
        for stubber in self.stubbers.values():
            stubber.assert_no_pending_responses()


@pytest.fixture
def factory() -> Iterator[StubbedFactory]:
    built = StubbedFactory()
    yield built
    for stubber in built.stubbers.values():
        stubber.deactivate()


def _role(name: str, path: str = "/") -> dict[str, Any]:
    return {
        "Path": path,
        "RoleName": name,
        "RoleId": "test_role_id_00001",
        "Arn": f"arn:aws:iam::{ACCOUNT}:role{path}{name}",
        "CreateDate": NOW,
    }


def _policy(name: str, arn: str | None = None) -> dict[str, Any]:
    return {"PolicyName": name, "Arn": arn or f"arn:aws:iam::{ACCOUNT}:policy/{name}"}


def _profile(name: str) -> dict[str, Any]:
    return {
        "Path": "/",
        "InstanceProfileName": name,
        "InstanceProfileId": "test_instance_profile_id",
        "Arn": f"arn:aws:iam::{ACCOUNT}:instance-profile/{name}",
        "CreateDate": NOW,
        "Roles": [],
    }


def _stub_iam(
    stubber: Stubber,
    roles: list[dict[str, Any]],
    policies: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
) -> None:
    stubber.add_response("list_roles", {"Roles": roles, "IsTruncated": False}, {})
    stubber.add_response(
        "list_policies", {"Policies": policies, "IsTruncated": False}, {"Scope": "Local"}
    )
    stubber.add_response(
        "list_instance_profiles", {"InstanceProfiles": profiles, "IsTruncated": False}, {}
    )


def _names(resources: list[Resource], kind: str) -> list[str]:
    return [r.name for r in resources if r.kind == kind]


def test_iam_returns_project_role_policy_and_instance_profile(factory: StubbedFactory) -> None:
    _stub_iam(
        factory.stub("iam", "us-east-1"),
        roles=[_role("crm-agent-eval-crm-get"), _role("OwnerAdmin")],
        policies=[_policy("crm-agent-eval-runtime"), _policy("team-policy")],
        profiles=[_profile("crm-agent-eval-probe"), _profile("other-profile")],
    )
    found = scan_iam(factory, CONFIG)
    factory.assert_drained()
    assert _names(found, "role") == ["crm-agent-eval-crm-get"]
    assert _names(found, "policy") == ["crm-agent-eval-runtime"]
    assert _names(found, "instance-profile") == ["crm-agent-eval-probe"]
    assert {r.family for r in found} == {"iam"}
    assert {r.region for r in found} == {"global"}
    assert all(r.arn and r.arn.startswith("arn:aws:iam::") for r in found)


def test_iam_ignores_service_linked_role_even_with_project_prefix(
    factory: StubbedFactory,
) -> None:
    _stub_iam(
        factory.stub("iam", "us-east-1"),
        roles=[_role("crm-agent-eval-linked", path="/aws-service-role/bedrock.amazonaws.com/")],
        policies=[],
        profiles=[],
    )
    assert scan_iam(factory, CONFIG) == []


def test_iam_ignores_aws_managed_policy_even_with_project_prefix(
    factory: StubbedFactory,
) -> None:
    _stub_iam(
        factory.stub("iam", "us-east-1"),
        roles=[],
        policies=[_policy("crm-agent-eval-x", arn="arn:aws:iam::aws:policy/crm-agent-eval-x")],
        profiles=[],
    )
    assert scan_iam(factory, CONFIG) == []


def test_iam_follows_role_pagination(factory: StubbedFactory) -> None:
    stubber = factory.stub("iam", "us-east-1")
    stubber.add_response(
        "list_roles",
        {"Roles": [_role("crm-agent-eval-a")], "IsTruncated": True, "Marker": "m1"},
        {},
    )
    stubber.add_response(
        "list_roles", {"Roles": [_role("crm-agent-eval-b")], "IsTruncated": False}, {"Marker": "m1"}
    )
    stubber.add_response(
        "list_policies", {"Policies": [], "IsTruncated": False}, {"Scope": "Local"}
    )
    stubber.add_response("list_instance_profiles", {"InstanceProfiles": [], "IsTruncated": False})
    found = scan_iam(factory, CONFIG)
    factory.assert_drained()
    assert _names(found, "role") == ["crm-agent-eval-a", "crm-agent-eval-b"]


def test_iam_access_denied_is_unmeasurable(factory: StubbedFactory) -> None:
    factory.stub("iam", "us-east-1").add_client_error(
        "list_roles",
        service_error_code="AccessDenied",
        service_message="explicit deny",
        http_status_code=403,
    )
    with pytest.raises(Unmeasurable) as caught:
        scan_iam(factory, CONFIG)
    assert (caught.value.family, caught.value.operation, caught.value.code) == (
        "iam",
        "list_roles",
        "AccessDenied",
    )


def _distribution(dist_id: str) -> dict[str, Any]:
    return {
        "Id": dist_id,
        "ARN": f"arn:aws:cloudfront::{ACCOUNT}:distribution/{dist_id}",
        "Status": "Deployed",
        "LastModifiedTime": NOW,
        "DomainName": f"{dist_id.lower()}.cloudfront.net",
        "Aliases": {"Quantity": 0},
        "Origins": {
            "Quantity": 1,
            "Items": [{"Id": "o1", "DomainName": "origin.example.com"}],
        },
        "DefaultCacheBehavior": {"TargetOriginId": "o1", "ViewerProtocolPolicy": "https-only"},
        "CacheBehaviors": {"Quantity": 0},
        "CustomErrorResponses": {"Quantity": 0},
        "Comment": "",
        "PriceClass": "PriceClass_100",
        "Enabled": True,
        "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
        "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
        "WebACLId": "",
        "HttpVersion": "http2",
        "IsIPV6Enabled": True,
        "Staging": False,
    }


def _marker_list(items: list[dict[str, Any]], next_marker: str | None = None) -> dict[str, Any]:
    listing: dict[str, Any] = {
        "Marker": "",
        "MaxItems": 100,
        "IsTruncated": next_marker is not None,
        "Quantity": len(items),
        "Items": items,
    }
    if next_marker is not None:
        listing["NextMarker"] = next_marker
    return listing


def _policy_list(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"MaxItems": 100, "Quantity": len(items), "Items": items}


def _origin_request_policy(name: str) -> dict[str, Any]:
    return {
        "Type": "custom",
        "OriginRequestPolicy": {
            "Id": f"orp-{name}",
            "LastModifiedTime": NOW,
            "OriginRequestPolicyConfig": {
                "Name": name,
                "HeadersConfig": {"HeaderBehavior": "none"},
                "CookiesConfig": {"CookieBehavior": "none"},
                "QueryStringsConfig": {"QueryStringBehavior": "none"},
            },
        },
    }


def _cache_policy(name: str) -> dict[str, Any]:
    return {
        "Type": "custom",
        "CachePolicy": {
            "Id": f"cp-{name}",
            "LastModifiedTime": NOW,
            "CachePolicyConfig": {
                "Name": name,
                "MinTTL": 0,
                "ParametersInCacheKeyAndForwardedToOrigin": {
                    "EnableAcceptEncodingGzip": True,
                    "HeadersConfig": {"HeaderBehavior": "none"},
                    "CookiesConfig": {"CookieBehavior": "none"},
                    "QueryStringsConfig": {"QueryStringBehavior": "none"},
                },
            },
        },
    }


def _response_headers_policy(name: str) -> dict[str, Any]:
    return {
        "Type": "custom",
        "ResponseHeadersPolicy": {
            "Id": f"rhp-{name}",
            "LastModifiedTime": NOW,
            "ResponseHeadersPolicyConfig": {"Name": name},
        },
    }


def _function(name: str) -> dict[str, Any]:
    return {
        "Name": name,
        "FunctionConfig": {"Comment": "", "Runtime": "cloudfront-js-2.0"},
        "FunctionMetadata": {
            "FunctionARN": f"arn:aws:cloudfront::{ACCOUNT}:function/{name}",
            "LastModifiedTime": NOW,
        },
    }


def _tags(**tags: str) -> dict[str, Any]:
    return {"Tags": {"Items": [{"Key": k, "Value": v} for k, v in tags.items()]}}


def _stub_cloudfront_after_distributions(
    stubber: Stubber,
    vpc_origins: list[dict[str, Any]] | None = None,
    oacs: list[dict[str, Any]] | None = None,
    origin_request: list[dict[str, Any]] | None = None,
    cache: list[dict[str, Any]] | None = None,
    response_headers: list[dict[str, Any]] | None = None,
    functions: list[dict[str, Any]] | None = None,
) -> None:
    stubber.add_response("list_vpc_origins", {"VpcOriginList": _marker_list(vpc_origins or [])}, {})
    stubber.add_response(
        "list_origin_access_controls", {"OriginAccessControlList": _marker_list(oacs or [])}, {}
    )
    stubber.add_response(
        "list_origin_request_policies",
        {"OriginRequestPolicyList": _policy_list(origin_request or [])},
        {"Type": "custom"},
    )
    stubber.add_response(
        "list_cache_policies", {"CachePolicyList": _policy_list(cache or [])}, {"Type": "custom"}
    )
    stubber.add_response(
        "list_response_headers_policies",
        {"ResponseHeadersPolicyList": _policy_list(response_headers or [])},
        {"Type": "custom"},
    )
    stubber.add_response("list_functions", {"FunctionList": _policy_list(functions or [])}, {})


def test_cloudfront_distribution_matches_on_project_tag_across_pages(
    factory: StubbedFactory,
) -> None:
    stubber = factory.stub("cloudfront", "us-east-1")
    project, other = _distribution("EPROJECT"), _distribution("EOTHER")
    stubber.add_response(
        "list_distributions", {"DistributionList": _marker_list([other], next_marker="m1")}, {}
    )
    stubber.add_response(
        "list_distributions", {"DistributionList": _marker_list([project])}, {"Marker": "m1"}
    )
    stubber.add_response(
        "list_tags_for_resource", _tags(service="someone-else"), {"Resource": other["ARN"]}
    )
    stubber.add_response(
        "list_tags_for_resource", _tags(service="crm-agent-eval"), {"Resource": project["ARN"]}
    )
    _stub_cloudfront_after_distributions(stubber)
    found = scan_cloudfront(factory, CONFIG)
    factory.assert_drained()
    assert found == [Resource("cloudfront", "distribution", "EPROJECT", project["ARN"], "global")]


NAMED_KIND_CASES: list[tuple[str, dict[str, list[dict[str, Any]]]]] = [
    (
        "vpc-origin",
        {
            "vpc_origins": [
                {
                    "Id": f"vo-{n}",
                    "Name": n,
                    "Status": "Deployed",
                    "CreatedTime": NOW,
                    "LastModifiedTime": NOW,
                    "Arn": f"arn:aws:cloudfront::{ACCOUNT}:vpcorigin/vo-{n}",
                    "OriginEndpointArn": "arn:aws:elasticloadbalancing:us-east-1:"
                    f"{ACCOUNT}:loadbalancer/app/x/1",
                }
                for n in ("crm-agent-eval-api-origin", "other-origin")
            ]
        },
    ),
    (
        "origin-access-control",
        {
            "oacs": [
                {
                    "Id": f"oac-{n}",
                    "Description": "",
                    "Name": n,
                    "SigningProtocol": "sigv4",
                    "SigningBehavior": "always",
                    "OriginAccessControlOriginType": "s3",
                }
                for n in ("crm-agent-evalfrontend-cdn-oac", "other-oac")
            ]
        },
    ),
    (
        "origin-request-policy",
        {
            "origin_request": [
                _origin_request_policy(n)
                for n in ("crm-agent-evalagent-relay-origin-request", "other-orp")
            ]
        },
    ),
    (
        "cache-policy",
        {"cache": [_cache_policy(n) for n in ("crm-agent-eval-cache", "other-cache")]},
    ),
    (
        "response-headers-policy",
        {
            "response_headers": [
                _response_headers_policy(n) for n in ("crm-agent-eval-headers", "other")
            ]
        },
    ),
    (
        "function",
        {"functions": [_function(n) for n in ("crm-agent-evalapi-path-rewrite", "other-function")]},
    ),
]


@pytest.mark.parametrize(("kind", "stub_kwargs"), NAMED_KIND_CASES)
def test_cloudfront_named_kind_matches_on_prefix(
    factory: StubbedFactory, kind: str, stub_kwargs: dict[str, list[dict[str, Any]]]
) -> None:
    stubber = factory.stub("cloudfront", "us-east-1")
    stubber.add_response("list_distributions", {"DistributionList": _marker_list([])}, {})
    _stub_cloudfront_after_distributions(stubber, **stub_kwargs)
    found = scan_cloudfront(factory, CONFIG)
    factory.assert_drained()
    assert [r.kind for r in found] == [kind]
    assert found[0].name.startswith("crm-agent-eval")
    assert (found[0].family, found[0].region) == ("cloudfront", "global")


def test_cloudfront_access_denied_is_unmeasurable(factory: StubbedFactory) -> None:
    factory.stub("cloudfront", "us-east-1").add_client_error(
        "list_distributions", service_error_code="AccessDenied", http_status_code=403
    )
    with pytest.raises(Unmeasurable) as caught:
        scan_cloudfront(factory, CONFIG)
    assert (caught.value.family, caught.value.code) == ("cloudfront", "AccessDenied")


def _mappings(*arns: str) -> list[dict[str, Any]]:
    return [
        {"ResourceARN": arn, "Tags": [{"Key": "service", "Value": "crm-agent-eval"}]}
        for arn in arns
    ]


LAMBDA_ARN = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:crm-agent-eval-crm-get"
ALB_ARN = f"arn:aws:elasticloadbalancing:us-east-1:{ACCOUNT}:loadbalancer/app/crm-agent-eval/1"
QUEUE_ARN = f"arn:aws:sqs:us-east-1:{ACCOUNT}:crm-agent-eval-queue"
BUCKET_ARN = "arn:aws:s3:::crm-agent-eval-cdn-origin"
DIST_ARN = f"arn:aws:cloudfront::{ACCOUNT}:distribution/EPROJECT"
GONE_DIST_ARN = f"arn:aws:cloudfront::{ACCOUNT}:distribution/EGONE"
ANYCAST_ARN = f"arn:aws:cloudfront::{ACCOUNT}:anycast-ip-list/crm-agent-eval-ips"


def test_tag_index_maps_arn_service_to_family_across_pages(factory: StubbedFactory) -> None:
    stubber = factory.stub("resourcegroupstaggingapi", "us-east-1")
    stubber.add_response(
        "get_resources",
        {"ResourceTagMappingList": _mappings(LAMBDA_ARN, ALB_ARN), "PaginationToken": "t1"},
        {"TagFilters": TAG_FILTERS},
    )
    stubber.add_response(
        "get_resources",
        {"ResourceTagMappingList": _mappings(QUEUE_ARN, BUCKET_ARN), "PaginationToken": ""},
        {"TagFilters": TAG_FILTERS, "PaginationToken": "t1"},
    )
    found = tag_index_scan(factory, CONFIG)
    factory.assert_drained()
    assert [(r.family, r.kind, r.arn, r.region) for r in found] == [
        ("lambda", "tagged", LAMBDA_ARN, "us-east-1"),
        ("elbv2", "tagged", ALB_ARN, "us-east-1"),
        ("sqs", "tagged", QUEUE_ARN, "us-east-1"),
        ("s3", "tagged", BUCKET_ARN, "global"),
    ]
    assert found[0].name == "function:crm-agent-eval-crm-get"


def test_tag_index_outside_us_east_1_also_queries_us_east_1_for_cloudfront(
    factory: StubbedFactory,
) -> None:
    config = ScanConfig(region="us-west-2")
    factory.stub("resourcegroupstaggingapi", "us-west-2").add_response(
        "get_resources",
        {"ResourceTagMappingList": _mappings(LAMBDA_ARN.replace("us-east-1", "us-west-2"))},
        {"TagFilters": TAG_FILTERS},
    )
    factory.stub("resourcegroupstaggingapi", "us-east-1").add_response(
        "get_resources",
        {"ResourceTagMappingList": _mappings(DIST_ARN)},
        {"TagFilters": TAG_FILTERS, "ResourceTypeFilters": ["cloudfront"]},
    )
    found = tag_index_scan(factory, config)
    factory.assert_drained()
    assert [(r.family, r.region) for r in found] == [
        ("lambda", "us-west-2"),
        ("cloudfront", "global"),
    ]


def test_tag_index_reports_each_arn_once(factory: StubbedFactory) -> None:
    factory.stub("resourcegroupstaggingapi", "us-east-1").add_response(
        "get_resources",
        {"ResourceTagMappingList": _mappings(LAMBDA_ARN, LAMBDA_ARN)},
        {"TagFilters": TAG_FILTERS},
    )
    assert [r.arn for r in tag_index_scan(factory, CONFIG)] == [LAMBDA_ARN]


def test_tag_index_access_denied_is_unmeasurable_under_tag_index_family(
    factory: StubbedFactory,
) -> None:
    factory.stub("resourcegroupstaggingapi", "us-east-1").add_client_error(
        "get_resources", service_error_code="AccessDeniedException", http_status_code=400
    )
    with pytest.raises(Unmeasurable) as caught:
        tag_index_scan(factory, CONFIG)
    assert (caught.value.family, caught.value.code) == (
        core.TAG_INDEX_FAMILY,
        "AccessDeniedException",
    )


def test_run_scan_classifies_tag_hits_by_enumerated_arn_type(
    factory: StubbedFactory,
) -> None:
    cloudfront = factory.stub("cloudfront", "us-east-1")
    project = _distribution("EPROJECT")
    cloudfront.add_response("list_distributions", {"DistributionList": _marker_list([project])}, {})
    cloudfront.add_response(
        "list_tags_for_resource", _tags(service="crm-agent-eval"), {"Resource": DIST_ARN}
    )
    _stub_cloudfront_after_distributions(cloudfront)
    factory.stub("resourcegroupstaggingapi", "us-east-1").add_response(
        "get_resources",
        {"ResourceTagMappingList": _mappings(DIST_ARN, GONE_DIST_ARN, ANYCAST_ARN, QUEUE_ARN)},
        {"TagFilters": TAG_FILTERS},
    )
    result = core.run_scan(
        factory,
        CONFIG,
        {"cloudfront": SCANNERS["cloudfront"]},
        tag_index_scan,
        ARN_SERVICE_TO_FAMILY,
        enumerated_arn_types=ENUMERATED_ARN_TYPES,
    )
    factory.assert_drained()
    assert result.measured == ["cloudfront"]
    assert [(r.family, r.kind, r.arn) for r in result.residue] == [
        ("cloudfront", "distribution", DIST_ARN),
        ("cloudfront", "tagged-unscanned", ANYCAST_ARN),
        ("sqs", "tagged-unscanned", QUEUE_ARN),
    ]
    assert [r.arn for r in result.stale_tag_index] == [GONE_DIST_ARN]


def test_enumerated_arn_types_pinned() -> None:
    assert {
        "iam": frozenset({"role", "policy", "instance-profile"}),
        "cloudfront": frozenset({"distribution", "vpcorigin", "function"}),
    } == ENUMERATED_ARN_TYPES
    assert set(ENUMERATED_ARN_TYPES) == set(SCANNERS)


def _arn_types(found: list[Resource]) -> set[str]:
    return {core.arn_resource_type(r.arn) for r in found if r.arn is not None}


def test_enumerated_arn_types_equal_what_the_iam_scanner_returns(factory: StubbedFactory) -> None:
    _stub_iam(
        factory.stub("iam", "us-east-1"),
        roles=[_role("crm-agent-eval-a"), _role("crm-agent-eval-b", path="/service-role/")],
        policies=[_policy("crm-agent-eval-p")],
        profiles=[_profile("crm-agent-eval-i")],
    )
    assert _arn_types(scan_iam(factory, CONFIG)) == ENUMERATED_ARN_TYPES["iam"]


def test_enumerated_arn_types_equal_what_the_cloudfront_scanner_returns(
    factory: StubbedFactory,
) -> None:
    stubber = factory.stub("cloudfront", "us-east-1")
    project = _distribution("EPROJECT")
    stubber.add_response("list_distributions", {"DistributionList": _marker_list([project])}, {})
    stubber.add_response(
        "list_tags_for_resource", _tags(service="crm-agent-eval"), {"Resource": project["ARN"]}
    )
    merged: dict[str, list[dict[str, Any]]] = {}
    for _kind, stub_kwargs in NAMED_KIND_CASES:
        merged.update(stub_kwargs)
    _stub_cloudfront_after_distributions(stubber, **merged)
    found = scan_cloudfront(factory, CONFIG)
    factory.assert_drained()
    assert {r.kind for r in found} == {"distribution"} | {kind for kind, _ in NAMED_KIND_CASES}
    assert _arn_types(found) == ENUMERATED_ARN_TYPES["cloudfront"]


def test_scanners_registry_holds_iam_and_cloudfront() -> None:
    assert {"iam": scan_iam, "cloudfront": scan_cloudfront} == SCANNERS


def test_arn_service_map_covers_every_family() -> None:
    assert set(ARN_SERVICE_TO_FAMILY.values()) == EXPECTED_FAMILIES
    assert set(SCANNERS) <= set(ARN_SERVICE_TO_FAMILY.values())


@pytest.mark.parametrize("module_name", SIBLING_MODULES)
def test_arn_service_map_covers_sibling_module_families(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert set(module.SCANNERS) <= set(ARN_SERVICE_TO_FAMILY.values())


def test_global_scanners_ask_for_us_east_1_clients() -> None:
    requested: list[tuple[str, str]] = []

    def recording_factory(service: str, region: str) -> Any:
        requested.append((service, region))
        raise Unmeasurable(service, "list_x", "Stop", "recorded")

    for scanner in SCANNERS.values():
        with pytest.raises(Unmeasurable):
            scanner(recording_factory, ScanConfig(region="eu-west-1"))
    assert requested == [("iam", "us-east-1"), ("cloudfront", "us-east-1")]
    assert global_services.GLOBAL_API_REGION == "us-east-1"

# SPDX-License-Identifier: MIT
"""Unit tests for `scripts.residue.regional_data`: each family scanner against botocore Stubber,
which validates every stubbed request and response against the service model."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from scripts.residue import regional_data
from scripts.residue.core import Resource, ScanConfig, Unmeasurable, arn_resource_type

REGION = "us-east-1"
ACCOUNT = "123456789012"
CONFIG = ScanConfig(region=REGION)


class StubbedClients:
    """A `ClientFactory` handing out one Stubber-backed client per service."""

    def __init__(self) -> None:
        self._session = boto3.session.Session(
            aws_access_key_id="testing",
            aws_secret_access_key="testing",  # nosec B106  # fake moto/stub credentials, not a credential
            region_name=REGION,
        )
        self.clients: dict[str, Any] = {}
        self.stubbers: dict[str, Stubber] = {}

    def stub(self, service: str) -> Stubber:
        if service not in self.stubbers:
            client = self._session.client(service, region_name=REGION)
            stubber = Stubber(client)
            stubber.activate()
            self.clients[service] = client
            self.stubbers[service] = stubber
        return self.stubbers[service]

    def __call__(self, service: str, region: str) -> Any:
        assert region == REGION
        self.stub(service)
        return self.clients[service]

    def assert_consumed(self) -> None:
        for stubber in self.stubbers.values():
            stubber.assert_no_pending_responses()


@pytest.fixture
def clients() -> Iterator[StubbedClients]:
    stubbed = StubbedClients()
    yield stubbed
    for stubber in stubbed.stubbers.values():
        stubber.deactivate()


def _scan(family: str, clients: StubbedClients) -> list[Resource]:
    resources = regional_data.SCANNERS[family](clients, CONFIG)
    clients.assert_consumed()
    return resources


def _arn(service: str, resource: str) -> str:
    return f"arn:aws:{service}:{REGION}:{ACCOUNT}:{resource}"


# Per-family stub builders. Each `pages_*` stubs two list pages, project items on both and a
# non-project item on the first, and returns the expected project names in order. Each
# `foreign_*` stubs a single page holding only non-project items.


def pages_s3(c: StubbedClients) -> list[str]:
    s3 = c.stub("s3")
    s3.add_response(
        "list_buckets",
        {
            "Buckets": [
                {"Name": "someone-else", "BucketRegion": "us-west-2"},
                {"Name": "crm-agent-eval-cdn-origin", "BucketRegion": REGION},
            ],
            "ContinuationToken": "page2",
        },
        {},
    )
    s3.add_response(
        "list_buckets",
        {"Buckets": [{"Name": "crm-agent-eval-tfstate-logs"}]},
        {"ContinuationToken": "page2"},
    )
    s3.add_response(
        "get_bucket_location",
        {"LocationConstraint": "us-west-2"},
        {"Bucket": "crm-agent-eval-tfstate-logs"},
    )
    return ["crm-agent-eval-cdn-origin", "crm-agent-eval-tfstate-logs"]


def foreign_s3(c: StubbedClients) -> None:
    c.stub("s3").add_response(
        "list_buckets",
        {"Buckets": [{"Name": "someone-else"}, {"Name": "backup-crm-agent-eval"}]},
        {},
    )


def pages_dynamodb(c: StubbedClients) -> list[str]:
    ddb = c.stub("dynamodb")
    ddb.add_response(
        "list_tables",
        {"TableNames": ["crm-agent-evalaccounts", "orders"], "LastEvaluatedTableName": "orders"},
        {},
    )
    ddb.add_response(
        "list_tables",
        {"TableNames": ["crm-agent-evalproposals"]},
        {"ExclusiveStartTableName": "orders"},
    )
    for name in ("crm-agent-evalaccounts", "crm-agent-evalproposals"):
        ddb.add_response(
            "describe_table",
            {"Table": {"TableName": name, "TableArn": _arn("dynamodb", f"table/{name}")}},
            {"TableName": name},
        )
    return ["crm-agent-evalaccounts", "crm-agent-evalproposals"]


def foreign_dynamodb(c: StubbedClients) -> None:
    c.stub("dynamodb").add_response("list_tables", {"TableNames": ["orders", "agent-eval"]}, {})


def _kms_describe(c: StubbedClients, key_id: str, manager: str, state: str) -> None:
    c.stub("kms").add_response(
        "describe_key",
        {
            "KeyMetadata": {
                "KeyId": key_id,
                "Arn": _arn("kms", f"key/{key_id}"),
                "KeyManager": manager,
                "KeyState": state,
            }
        },
        {"KeyId": key_id},
    )


def pages_kms(c: StubbedClients) -> list[str]:
    kms = c.stub("kms")
    kms.add_response(
        "list_aliases",
        {
            "Aliases": [
                {"AliasName": "alias/other", "TargetKeyId": "k-other"},
                {
                    "AliasName": "alias/crm-agent-eval-tfstate",
                    "AliasArn": _arn("kms", "alias/crm-agent-eval-tfstate"),
                    "TargetKeyId": "k-state",
                },
            ],
            "Truncated": True,
            "NextMarker": "m1",
        },
        {},
    )
    kms.add_response(
        "list_aliases",
        {"Aliases": [{"AliasName": "alias/aws/s3", "TargetKeyId": "k-aws"}], "Truncated": False},
        {"Marker": "m1"},
    )
    kms.add_response(
        "list_keys",
        {
            "Keys": [{"KeyId": "k-state"}, {"KeyId": "k-aws"}],
            "Truncated": True,
            "NextMarker": "m2",
        },
        {},
    )
    kms.add_response(
        "list_keys",
        {"Keys": [{"KeyId": "k-tagged"}, {"KeyId": "k-other"}], "Truncated": False},
        {"Marker": "m2"},
    )
    _kms_describe(c, "k-state", "CUSTOMER", "Enabled")
    _kms_describe(c, "k-aws", "AWS", "Enabled")
    _kms_describe(c, "k-tagged", "CUSTOMER", "PendingDeletion")
    kms.add_response(
        "list_resource_tags",
        {"Tags": [{"TagKey": "service", "TagValue": "crm-agent-eval"}], "Truncated": False},
        {"KeyId": "k-tagged"},
    )
    _kms_describe(c, "k-other", "CUSTOMER", "Enabled")
    kms.add_response(
        "list_resource_tags",
        {"Tags": [{"TagKey": "service", "TagValue": "billing"}], "Truncated": False},
        {"KeyId": "k-other"},
    )
    return ["alias/crm-agent-eval-tfstate", "k-state", "k-tagged"]


def foreign_kms(c: StubbedClients) -> None:
    kms = c.stub("kms")
    kms.add_response(
        "list_aliases", {"Aliases": [{"AliasName": "alias/other"}], "Truncated": False}, {}
    )
    kms.add_response("list_keys", {"Keys": [{"KeyId": "k-other"}], "Truncated": False}, {})
    _kms_describe(c, "k-other", "CUSTOMER", "Enabled")
    kms.add_response("list_resource_tags", {"Tags": [], "Truncated": False}, {"KeyId": "k-other"})


def pages_logs(c: StubbedClients) -> list[str]:
    logs = c.stub("logs")
    lambda_group = "/aws/lambda/crm-agent-eval-eniprobe-1a2b"
    agentcore_group = "/aws/bedrock-agentcore/runtimes/crm_agent_eval_runtime-x-DEFAULT"
    logs.add_response(
        "describe_log_groups",
        {
            "logGroups": [
                {"logGroupName": "/aws/lambda/CRM-AGENT-EVAL-upper"},
                {
                    "logGroupName": lambda_group,
                    "logGroupArn": _arn("logs", f"log-group:{lambda_group}"),
                },
            ],
            "nextToken": "t1",
        },
        {"logGroupNamePattern": "crm-agent-eval"},
    )
    logs.add_response(
        "describe_log_groups",
        {"logGroups": [{"logGroupName": "/aws/codebuild/crm-agent-eval-agent"}]},
        {"logGroupNamePattern": "crm-agent-eval", "nextToken": "t1"},
    )
    logs.add_response(
        "describe_log_groups",
        {
            "logGroups": [
                {
                    "logGroupName": agentcore_group,
                    "arn": _arn("logs", f"log-group:{agentcore_group}:*"),
                }
            ]
        },
        {"logGroupNamePattern": "crm_agent_eval"},
    )
    return [lambda_group, "/aws/codebuild/crm-agent-eval-agent", agentcore_group]


def foreign_logs(c: StubbedClients) -> None:
    logs = c.stub("logs")
    logs.add_response(
        "describe_log_groups",
        {"logGroups": [{"logGroupName": "/aws/lambda/CRM-AGENT-EVAL-x"}]},
        {"logGroupNamePattern": "crm-agent-eval"},
    )
    logs.add_response(
        "describe_log_groups", {"logGroups": []}, {"logGroupNamePattern": "crm_agent_eval"}
    )


def pages_ecr(c: StubbedClients) -> list[str]:
    ecr = c.stub("ecr")
    ecr.add_response(
        "describe_repositories",
        {
            "repositories": [
                {"repositoryName": "base-images"},
                {
                    "repositoryName": "crm-agent-eval-agent",
                    "repositoryArn": _arn("ecr", "repository/crm-agent-eval-agent"),
                },
            ],
            "nextToken": "t1",
        },
        {},
    )
    ecr.add_response(
        "describe_repositories",
        {"repositories": [{"repositoryName": "crm-agent-eval-relay"}]},
        {"nextToken": "t1"},
    )
    return ["crm-agent-eval-agent", "crm-agent-eval-relay"]


def foreign_ecr(c: StubbedClients) -> None:
    c.stub("ecr").add_response(
        "describe_repositories", {"repositories": [{"repositoryName": "base-images"}]}, {}
    )


def pages_ssm(c: StubbedClients) -> list[str]:
    ssm = c.stub("ssm")
    ssm.add_response(
        "describe_parameters",
        {
            "Parameters": [
                {"Name": "/other/crm-agent-eval/x"},
                {
                    "Name": "/crm-agent-eval/relay/cognito-audience",
                    "ARN": _arn("ssm", "parameter/crm-agent-eval/relay/cognito-audience"),
                },
            ],
            "NextToken": "t1",
        },
        {},
    )
    ssm.add_response(
        "describe_parameters",
        {"Parameters": [{"Name": "crm-agent-eval-flat"}]},
        {"NextToken": "t1"},
    )
    return ["/crm-agent-eval/relay/cognito-audience", "crm-agent-eval-flat"]


def foreign_ssm(c: StubbedClients) -> None:
    c.stub("ssm").add_response(
        "describe_parameters", {"Parameters": [{"Name": "/other/crm-agent-eval/x"}]}, {}
    )


def pages_secretsmanager(c: StubbedClients) -> list[str]:
    sm = c.stub("secretsmanager")
    sm.add_response(
        "list_secrets",
        {
            "SecretList": [
                {"Name": "db-password", "Tags": [{"Key": "service", "Value": "billing"}]},
                {"Name": "crm-agent-eval-demo-user", "ARN": _arn("secretsmanager", "secret:x")},
            ],
            "NextToken": "t1",
        },
        {},
    )
    sm.add_response(
        "list_secrets",
        {
            "SecretList": [
                {"Name": "unprefixed", "Tags": [{"Key": "service", "Value": "crm-agent-eval"}]}
            ]
        },
        {"NextToken": "t1"},
    )
    return ["crm-agent-eval-demo-user", "unprefixed"]


def foreign_secretsmanager(c: StubbedClients) -> None:
    c.stub("secretsmanager").add_response(
        "list_secrets",
        {
            "SecretList": [
                {"Name": "db-password", "Tags": [{"Key": "owner", "Value": "crm-agent-eval"}]}
            ]
        },
        {},
    )


def pages_events(c: StubbedClients) -> list[str]:
    events = c.stub("events")
    events.add_response(
        "list_rules",
        {
            "Rules": [
                {"Name": "AutoScalingManagedRule"},
                {
                    "Name": "crm-agent-eval-sweep",
                    "Arn": _arn("events", "rule/crm-agent-eval-sweep"),
                },
            ],
            "NextToken": "t1",
        },
        {},
    )
    events.add_response(
        "list_rules", {"Rules": [{"Name": "crm_agent_eval_nightly"}]}, {"NextToken": "t1"}
    )
    return ["crm-agent-eval-sweep", "crm_agent_eval_nightly"]


def foreign_events(c: StubbedClients) -> None:
    c.stub("events").add_response("list_rules", {"Rules": [{"Name": "AutoScalingManagedRule"}]}, {})


def pages_cloudtrail(c: StubbedClients) -> list[str]:
    ct = c.stub("cloudtrail")
    ct.add_response(
        "list_trails",
        {
            "Trails": [
                {"Name": "org-trail", "HomeRegion": REGION},
                {
                    "Name": "crm-agent-eval-trail",
                    "TrailARN": _arn("cloudtrail", "trail/crm-agent-eval-trail"),
                    "HomeRegion": REGION,
                },
            ],
            "NextToken": "t1",
        },
        {},
    )
    ct.add_response(
        "list_trails",
        {"Trails": [{"Name": "crm-agent-eval-west", "HomeRegion": "us-west-2"}]},
        {"NextToken": "t1"},
    )
    return ["crm-agent-eval-trail", "crm-agent-eval-west"]


def foreign_cloudtrail(c: StubbedClients) -> None:
    c.stub("cloudtrail").add_response(
        "list_trails", {"Trails": [{"Name": "org-trail", "HomeRegion": REGION}]}, {}
    )


Builder = Callable[[StubbedClients], Any]

CASES: dict[str, tuple[Builder, Builder, str, str, str]] = {
    # family: (pages, foreign, service, first operation, AccessDenied code the service returns)
    "s3": (pages_s3, foreign_s3, "s3", "list_buckets", "AccessDenied"),
    "dynamodb": (
        pages_dynamodb,
        foreign_dynamodb,
        "dynamodb",
        "list_tables",
        "AccessDeniedException",
    ),
    "kms": (pages_kms, foreign_kms, "kms", "list_aliases", "AccessDeniedException"),
    "logs": (pages_logs, foreign_logs, "logs", "describe_log_groups", "AccessDeniedException"),
    "ecr": (pages_ecr, foreign_ecr, "ecr", "describe_repositories", "AccessDeniedException"),
    "ssm": (pages_ssm, foreign_ssm, "ssm", "describe_parameters", "AccessDeniedException"),
    "secretsmanager": (
        pages_secretsmanager,
        foreign_secretsmanager,
        "secretsmanager",
        "list_secrets",
        "AccessDeniedException",
    ),
    "events": (pages_events, foreign_events, "events", "list_rules", "AccessDeniedException"),
    "cloudtrail": (
        pages_cloudtrail,
        foreign_cloudtrail,
        "cloudtrail",
        "list_trails",
        "AccessDeniedException",
    ),
}


def test_scanners_cover_exactly_the_regional_families() -> None:
    assert set(regional_data.SCANNERS) == set(CASES)


@pytest.mark.parametrize("family", sorted(CASES))
def test_finds_project_items_across_pages(family: str, clients: StubbedClients) -> None:
    expected = CASES[family][0](clients)
    resources = _scan(family, clients)
    assert [r.name for r in resources] == expected
    assert {r.family for r in resources} == {family}


@pytest.mark.parametrize("family", sorted(CASES))
def test_ignores_non_project_items(family: str, clients: StubbedClients) -> None:
    CASES[family][1](clients)
    assert _scan(family, clients) == []


@pytest.mark.parametrize("family", sorted(CASES))
def test_access_denied_makes_family_unmeasurable(family: str, clients: StubbedClients) -> None:
    _, _, service, operation, code = CASES[family]
    clients.stub(service).add_client_error(
        operation, service_error_code=code, service_message="explicit deny", http_status_code=403
    )
    with pytest.raises(Unmeasurable) as caught:
        regional_data.SCANNERS[family](clients, CONFIG)
    assert (caught.value.family, caught.value.operation, caught.value.code) == (
        family,
        operation,
        code,
    )


def test_s3_records_bucket_region_and_global_scope(clients: StubbedClients) -> None:
    pages_s3(clients)
    by_name = {r.name: r for r in _scan("s3", clients)}
    origin = by_name["crm-agent-eval-cdn-origin"]
    assert (origin.region, origin.detail["bucket_region"]) == ("global", REGION)
    assert origin.arn == "arn:aws:s3:::crm-agent-eval-cdn-origin"
    assert by_name["crm-agent-eval-tfstate-logs"].detail["bucket_region"] == "us-west-2"


def test_s3_null_location_constraint_is_us_east_1(clients: StubbedClients) -> None:
    s3 = clients.stub("s3")
    s3.add_response("list_buckets", {"Buckets": [{"Name": "crm-agent-eval-x"}]}, {})
    s3.add_response("get_bucket_location", {}, {"Bucket": "crm-agent-eval-x"})
    [bucket] = _scan("s3", clients)
    assert bucket.detail["bucket_region"] == "us-east-1"


def test_dynamodb_tables_carry_their_arn(clients: StubbedClients) -> None:
    pages_dynamodb(clients)
    arns = [r.arn for r in _scan("dynamodb", clients)]
    assert arns == [
        _arn("dynamodb", "table/crm-agent-evalaccounts"),
        _arn("dynamodb", "table/crm-agent-evalproposals"),
    ]


def test_kms_keys_carry_state_and_aws_managed_keys_are_skipped(clients: StubbedClients) -> None:
    pages_kms(clients)
    resources = _scan("kms", clients)
    keys = {r.name: r for r in resources if r.kind == "key"}
    assert set(keys) == {"k-state", "k-tagged"}
    assert keys["k-state"].detail["key_state"] == "Enabled"
    assert keys["k-tagged"].detail["key_state"] == "PendingDeletion"
    assert keys["k-tagged"].arn == _arn("kms", "key/k-tagged")
    [alias] = [r for r in resources if r.kind == "alias"]
    assert alias.detail["target_key_id"] == "k-state"


def test_kms_tag_lookup_denied_makes_family_unmeasurable(clients: StubbedClients) -> None:
    kms = clients.stub("kms")
    kms.add_response("list_aliases", {"Aliases": [], "Truncated": False}, {})
    kms.add_response("list_keys", {"Keys": [{"KeyId": "k-1"}], "Truncated": False}, {})
    _kms_describe(clients, "k-1", "CUSTOMER", "Enabled")
    kms.add_client_error("list_resource_tags", service_error_code="AccessDeniedException")
    with pytest.raises(Unmeasurable) as caught:
        regional_data.SCANNERS["kms"](clients, CONFIG)
    assert (caught.value.operation, caught.value.code) == (
        "list_resource_tags",
        "AccessDeniedException",
    )


def test_log_group_arns_drop_the_stream_wildcard(clients: StubbedClients) -> None:
    pages_logs(clients)
    arns = {r.name: r.arn for r in _scan("logs", clients)}
    group = "/aws/bedrock-agentcore/runtimes/crm_agent_eval_runtime-x-DEFAULT"
    assert arns[group] == _arn("logs", f"log-group:{group}")
    assert arns["/aws/codebuild/crm-agent-eval-agent"] is None


def test_log_group_matched_by_both_prefixes_is_listed_once(clients: StubbedClients) -> None:
    logs = clients.stub("logs")
    both = "/aws/crm-agent-eval/crm_agent_eval"
    for prefix in CONFIG.name_prefixes:
        logs.add_response(
            "describe_log_groups",
            {"logGroups": [{"logGroupName": both}]},
            {"logGroupNamePattern": prefix},
        )
    assert [r.name for r in _scan("logs", clients)] == [both]


def test_cloudtrail_trail_region_is_its_home_region(clients: StubbedClients) -> None:
    pages_cloudtrail(clients)
    regions = {r.name: r.region for r in _scan("cloudtrail", clients)}
    assert regions == {"crm-agent-eval-trail": REGION, "crm-agent-eval-west": "us-west-2"}


def test_enumerated_arn_types_are_pinned() -> None:
    assert {
        "s3": frozenset({""}),
        "dynamodb": frozenset({"table"}),
        "kms": frozenset({"key", "alias"}),
        "logs": frozenset({"log-group"}),
        "ecr": frozenset({"repository"}),
        "ssm": frozenset({"parameter"}),
        "secretsmanager": frozenset({"secret"}),
        "events": frozenset({"rule"}),
        "cloudtrail": frozenset({"trail"}),
    } == regional_data.ENUMERATED_ARN_TYPES
    assert set(regional_data.ENUMERATED_ARN_TYPES) == set(regional_data.SCANNERS)


@pytest.mark.parametrize("family", sorted(CASES))
def test_returned_arn_types_are_enumerated_for_their_family(
    family: str, clients: StubbedClients
) -> None:
    CASES[family][0](clients)
    arns = [r.arn for r in _scan(family, clients) if r.arn is not None]
    assert arns
    assert {arn_resource_type(arn) for arn in arns} <= regional_data.ENUMERATED_ARN_TYPES[family]

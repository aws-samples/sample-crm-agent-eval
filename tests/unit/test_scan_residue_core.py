# SPDX-License-Identifier: MIT
"""Unit tests for `scripts/residue/core.py`: call helpers, ownership rules, accept rules, merge, exit."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import botocore.session
import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)
from botocore.stub import Stubber
from scripts.residue import core
from scripts.residue.core import (
    ACCEPTED_RESIDUE,
    TAG_INDEX_FAMILY,
    Resource,
    ScanConfig,
    ScanResult,
    Unmeasurable,
)

CONFIG = ScanConfig(region="us-east-1")
ACCOUNT = "123456789012"


def _client(service: str) -> Any:
    session = botocore.session.get_session()
    return session.create_client(
        service,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # nosec B106  # fake moto/stub credentials, not a credential
    )


@pytest.fixture
def logs() -> Iterator[tuple[Any, Stubber]]:
    client = _client("logs")
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


class _Raising:
    """A stand-in client whose read operations raise a given exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def describe_things(self, **_: Any) -> dict[str, Any]:
        raise self._exc

    def get_paginator(self, _operation: str) -> Any:
        raise self._exc


def _tagged(arn: str) -> Resource:
    return Resource(
        family="ignored", kind="tagged", name=arn.rsplit(":", 1)[-1], arn=arn, region="us-east-1"
    )


# --- call / paginate -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation",
    [
        "delete_bucket",
        "put_object",
        "create_table",
        "list",
        "List_buckets",
        "listbuckets",
        "tag_resource",
    ],
)
def test_call_refuses_non_read_only_operation(operation: str) -> None:
    with pytest.raises(ValueError, match="non-read-only"):
        core.call(object(), "s3", operation)


def test_paginate_refuses_non_read_only_operation_before_touching_client() -> None:
    with pytest.raises(ValueError, match="non-read-only"):
        core.paginate(_Raising(AssertionError("client touched")), "s3", "delete_objects", "Items")


def test_call_returns_the_response(logs: tuple[Any, Stubber]) -> None:
    client, stubber = logs
    stubber.add_response(
        "describe_log_groups", {"logGroups": [{"logGroupName": "/aws/lambda/crm-agent-eval-x"}]}
    )
    response = core.call(client, "logs", "describe_log_groups")
    assert response["logGroups"][0]["logGroupName"] == "/aws/lambda/crm-agent-eval-x"


def test_paginate_concatenates_items_across_pages(logs: tuple[Any, Stubber]) -> None:
    client, stubber = logs
    stubber.add_response(
        "describe_log_groups", {"logGroups": [{"logGroupName": "a"}], "nextToken": "t1"}
    )
    stubber.add_response(
        "describe_log_groups", {"logGroups": [{"logGroupName": "b"}]}, {"nextToken": "t1"}
    )
    items = core.paginate(client, "logs", "describe_log_groups", "logGroups")
    assert [i["logGroupName"] for i in items] == ["a", "b"]


def test_paginate_dotted_result_key_and_missing_key() -> None:
    client = _client("cloudfront")
    with Stubber(client) as stubber:
        stubber.add_response(
            "list_distributions",
            {
                "DistributionList": {
                    "Marker": "",
                    "MaxItems": 100,
                    "IsTruncated": False,
                    "Quantity": 0,
                }
            },
        )
        assert (
            core.paginate(client, "cloudfront", "list_distributions", "DistributionList.Items")
            == []
        )


@pytest.mark.parametrize(
    "code", ["AccessDenied", "AccessDeniedException", "ThrottlingException", "Throttling"]
)
def test_client_error_becomes_unmeasurable_with_its_code(
    logs: tuple[Any, Stubber], code: str
) -> None:
    client, stubber = logs
    stubber.add_client_error("describe_log_groups", service_error_code=code, service_message="nope")
    with pytest.raises(Unmeasurable) as info:
        core.call(client, "logs", "describe_log_groups")
    assert (info.value.family, info.value.operation, info.value.code) == (
        "logs",
        "describe_log_groups",
        code,
    )
    assert info.value.message == "nope"
    assert isinstance(info.value.__cause__, ClientError)


def test_paginate_client_error_on_second_page_is_unmeasurable(logs: tuple[Any, Stubber]) -> None:
    client, stubber = logs
    stubber.add_response("describe_log_groups", {"logGroups": [], "nextToken": "t1"})
    stubber.add_client_error("describe_log_groups", service_error_code="ThrottlingException")
    with pytest.raises(Unmeasurable) as info:
        core.paginate(client, "logs", "describe_log_groups", "logGroups")
    assert info.value.code == "ThrottlingException"


def test_exhausted_throttling_after_real_retries_is_unmeasurable() -> None:
    """A real client with standard retries: throttling on every attempt surfaces as Unmeasurable."""
    from botocore.config import Config

    session = botocore.session.get_session()
    client = session.create_client(
        "logs",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # nosec B106  # fake moto/stub credentials, not a credential
        config=Config(retries={"mode": "standard", "max_attempts": 2}),
    )
    attempts: list[int] = []

    def throttle(**kwargs: Any) -> Any:
        from botocore.awsrequest import AWSResponse

        attempts.append(1)
        body = b'{"__type":"ThrottlingException","message":"Rate exceeded"}'
        raw = type("Raw", (), {"stream": lambda _self, **_: iter([body])})()
        return AWSResponse(
            kwargs["request"].url, 400, {"x-amzn-ErrorType": "ThrottlingException"}, raw
        )

    client.meta.events.register("before-send.logs.DescribeLogGroups", throttle)
    with pytest.raises(Unmeasurable) as info:
        core.call(client, "logs", "describe_log_groups")
    assert info.value.code == "ThrottlingException"
    assert len(attempts) > 1


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (NoCredentialsError(), "NoCredentialsError"),
        (EndpointConnectionError(endpoint_url="https://logs.invalid"), "EndpointConnectionError"),
        (ReadTimeoutError(endpoint_url="https://logs.invalid"), "ReadTimeoutError"),
    ],
)
def test_botocore_failures_become_unmeasurable(exc: Exception, code: str) -> None:
    with pytest.raises(Unmeasurable) as info:
        core.call(_Raising(exc), "logs", "describe_things")
    assert info.value.code == code
    with pytest.raises(Unmeasurable) as info:
        core.paginate(_Raising(exc), "logs", "describe_things", "Items")
    assert info.value.code == code


def test_unrelated_exceptions_propagate() -> None:
    with pytest.raises(KeyError):
        core.call(_Raising(KeyError("bug")), "logs", "describe_things")


# --- ownership rules -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("crm-agent-eval-api", True),
        ("crm-agent-evalaccounts", True),
        ("crm_agent_eval_runtime", True),
        ("crm-agent-evaluator-x", True),
        ("other-crm-agent-eval", False),
        ("crm-agent-ev", False),
        ("", False),
    ],
)
def test_matches_prefix_is_a_plain_startswith(name: str, expected: bool) -> None:
    assert core.matches_prefix(name, CONFIG) is expected


def test_matches_prefix_honours_configured_prefixes() -> None:
    assert core.matches_prefix("acme-x", ScanConfig(region="us-east-1", name_prefixes=("acme",)))
    assert not core.matches_prefix(
        "crm-agent-eval-x", ScanConfig(region="r", name_prefixes=("acme",))
    )


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"service": "crm-agent-eval"}, True),
        ({"service": "crm-agent-eval", "env": "dev"}, True),
        ({"service": "other"}, False),
        ({"Service": "crm-agent-eval"}, False),
        ({}, False),
    ],
)
def test_has_project_tag(tags: dict[str, str], expected: bool) -> None:
    assert core.has_project_tag(tags, CONFIG) is expected


def test_arn_helpers() -> None:
    arn = f"arn:aws:ec2:us-east-1:{ACCOUNT}:vpc/vpc-1"
    assert core.arn_service(arn) == "ec2"
    assert core.arn_resource_type(arn) == "vpc"
    assert core.arn_resource_type(f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:f") == "function"
    assert core.arn_resource_type("arn:aws:s3:::bucket") == ""
    assert core.arn_service("not-an-arn") == ""


# --- accept rules --------------------------------------------------------------------------


def test_accepted_residue_is_exactly_the_three_design_rules() -> None:
    assert [(r.family, r.kind) for r in ACCEPTED_RESIDUE] == [
        ("kms", "key"),
        ("ec2", "security-group"),
        ("ec2", "tagged"),
    ]
    assert [r.reason.split(":", 1)[0] for r in ACCEPTED_RESIDUE] == [
        "kms-pending-deletion",
        "vpc-origins-service-sg",
        "adopted-network",
    ]
    assert all(r.reason.split(":", 1)[1].strip() for r in ACCEPTED_RESIDUE)


def _key(state: str) -> Resource:
    return Resource(
        "kms",
        "key",
        "k",
        f"arn:aws:kms:us-east-1:{ACCOUNT}:key/1",
        "us-east-1",
        {"key_state": state},
    )


def test_kms_pending_deletion_accepted_enabled_key_is_residue() -> None:
    reason = core.classify(_key("PendingDeletion"))
    assert reason is not None and reason.startswith("kms-pending-deletion")
    assert core.classify(_key("Enabled")) is None
    assert core.classify(Resource("kms", "key", "k", None, "us-east-1")) is None
    assert (
        core.classify(
            Resource("kms", "alias", "a", None, "us-east-1", {"key_state": "PendingDeletion"})
        )
        is None
    )


def test_vpc_origins_sg_accepted_other_sg_is_residue() -> None:
    sg = Resource("ec2", "security-group", "CloudFront-VPCOrigins-Service-SG", None, "us-east-1")
    reason = core.classify(sg)
    assert reason is not None and reason.startswith("vpc-origins-service-sg")
    assert (
        core.classify(
            Resource("ec2", "security-group", "crm-agent-eval-bootstrap", None, "us-east-1")
        )
        is None
    )
    assert (
        core.classify(
            Resource(
                "ec2", "network-interface", "CloudFront-VPCOrigins-Service-SG", None, "us-east-1"
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "kind", ["vpc", "subnet", "route-table", "internet-gateway", "network-acl"]
)
def test_adopted_network_tag_hits_accepted(kind: str) -> None:
    hit = Resource("ec2", "tagged", "x", f"arn:aws:ec2:us-east-1:{ACCOUNT}:{kind}/x-1", "us-east-1")
    reason = core.classify(hit)
    assert reason is not None and reason.startswith("adopted-network")


@pytest.mark.parametrize(
    "kind", ["natgateway", "elastic-ip", "vpc-endpoint", "security-group", "network-interface"]
)
def test_billable_network_tag_hits_not_accepted(kind: str) -> None:
    hit = Resource("ec2", "tagged", "x", f"arn:aws:ec2:us-east-1:{ACCOUNT}:{kind}/x-1", "us-east-1")
    assert core.classify(hit) is None


def test_adopted_network_requires_tag_index_kind() -> None:
    vpc = Resource("ec2", "vpc", "x", f"arn:aws:ec2:us-east-1:{ACCOUNT}:vpc/vpc-1", "us-east-1")
    assert core.classify(vpc) is None


# --- run_scan ------------------------------------------------------------------------------


def _scanner(*resources: Resource) -> core.FamilyScanner:
    return lambda _factory, _config: list(resources)


def _failing(family: str, code: str) -> core.FamilyScanner:
    def scan(_factory: core.ClientFactory, _config: ScanConfig) -> list[Resource]:
        raise Unmeasurable(family, "list_things", code, "denied")

    return scan


def _factory(_service: str, _region: str) -> Any:
    raise AssertionError("factory must not be called by run_scan itself")


FN_ARN = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:crm-agent-eval-api"
ARN_MAP = {"lambda": "lambda", "ec2": "ec2", "kms": "kms"}
ENUMERATED = {
    "lambda": frozenset({"function"}),
    "ec2": frozenset({"security-group", "network-interface", "natgateway"}),
    "stepfunctions": frozenset({"stateMachine"}),
    "s3": frozenset({""}),
}


def test_run_scan_splits_residue_and_accepted() -> None:
    fn = Resource("lambda", "function", "crm-agent-eval-api", FN_ARN, "us-east-1")
    result = core.run_scan(
        _factory,
        CONFIG,
        {"lambda": _scanner(fn), "kms": _scanner(_key("PendingDeletion"))},
        None,
        ARN_MAP,
        enumerated_arn_types=ENUMERATED,
    )
    assert result.measured == ["lambda", "kms"]
    assert result.residue == [fn]
    assert [r for r, _ in result.accepted] == [_key("PendingDeletion")]
    assert core.exit_code(result) == 1


def test_run_scan_records_unmeasured_family_and_keeps_others() -> None:
    fn = Resource("lambda", "function", "crm-agent-eval-api", FN_ARN, "us-east-1")
    result = core.run_scan(
        _factory,
        CONFIG,
        {"iam": _failing("iam", "AccessDenied"), "lambda": _scanner(fn)},
        None,
        ARN_MAP,
        enumerated_arn_types=ENUMERATED,
    )
    assert result.measured == ["lambda"]
    assert result.unmeasured["iam"].code == "AccessDenied"
    assert result.residue == [fn]
    assert core.exit_code(result) == 2


def test_tag_hit_seen_by_scanner_is_dropped() -> None:
    fn = Resource("lambda", "function", "crm-agent-eval-api", FN_ARN, "us-east-1")
    result = core.run_scan(
        _factory,
        CONFIG,
        {"lambda": _scanner(fn)},
        _scanner(_tagged(FN_ARN)),
        ARN_MAP,
        enumerated_arn_types=ENUMERATED,
    )
    assert result.residue == [fn]
    assert result.stale_tag_index == []


def test_tag_hit_matched_by_name_suffix_when_scanner_has_no_arn() -> None:
    fn = Resource("lambda", "function", "crm-agent-eval-api", None, "us-east-1")
    result = core.run_scan(
        _factory,
        CONFIG,
        {"lambda": _scanner(fn)},
        _scanner(_tagged(FN_ARN)),
        ARN_MAP,
        enumerated_arn_types=ENUMERATED,
    )
    assert result.stale_tag_index == []


def test_tag_hit_missed_by_scanned_family_is_stale_not_residue() -> None:
    result = core.run_scan(
        _factory,
        CONFIG,
        {"lambda": _scanner()},
        _scanner(_tagged(FN_ARN)),
        ARN_MAP,
        enumerated_arn_types=ENUMERATED,
    )
    assert [r.arn for r in result.stale_tag_index] == [FN_ARN]
    assert result.stale_tag_index[0].family == "lambda"
    assert result.residue == []
    assert core.exit_code(result) == 0


def test_tag_hit_for_unscanned_service_is_tagged_unscanned_residue() -> None:
    arn = f"arn:aws:sqs:us-east-1:{ACCOUNT}:crm-agent-eval-q"
    result = core.run_scan(
        _factory,
        CONFIG,
        {"lambda": _scanner()},
        _scanner(_tagged(arn)),
        ARN_MAP,
        enumerated_arn_types=ENUMERATED,
    )
    assert [(r.family, r.kind, r.arn) for r in result.residue] == [("sqs", "tagged-unscanned", arn)]
    assert core.exit_code(result) == 1


def test_tag_hit_family_comes_from_arn_service_map() -> None:
    arn = f"arn:aws:states:us-east-1:{ACCOUNT}:stateMachine:crm-agent-eval-sweep"
    result = core.run_scan(
        _factory,
        CONFIG,
        {"stepfunctions": _scanner()},
        _scanner(_tagged(arn)),
        {"states": "stepfunctions"},
        enumerated_arn_types=ENUMERATED,
    )
    assert [r.family for r in result.stale_tag_index] == ["stepfunctions"]


def test_tag_hit_for_unmeasured_family_is_residue() -> None:
    result = core.run_scan(
        _factory,
        CONFIG,
        {"lambda": _failing("lambda", "AccessDenied")},
        _scanner(_tagged(FN_ARN)),
        ARN_MAP,
        enumerated_arn_types=ENUMERATED,
    )
    assert [r.kind for r in result.residue] == ["tagged-unscanned"]
    assert core.exit_code(result) == 2


def test_adopted_network_tag_hit_is_accepted_and_nat_gateway_is_stale() -> None:
    vpc = f"arn:aws:ec2:us-east-1:{ACCOUNT}:vpc/vpc-1"
    nat = f"arn:aws:ec2:us-east-1:{ACCOUNT}:natgateway/nat-1"
    result = core.run_scan(
        _factory,
        CONFIG,
        {"ec2": _scanner()},
        _scanner(_tagged(vpc), _tagged(nat)),
        ARN_MAP,
        enumerated_arn_types=ENUMERATED,
    )
    assert [(r.arn, reason.split(":", 1)[0]) for r, reason in result.accepted] == [
        (vpc, "adopted-network")
    ]
    assert [r.arn for r in result.stale_tag_index] == [nat]


def test_failed_tag_scan_is_unmeasured() -> None:
    result = core.run_scan(
        _factory,
        CONFIG,
        {"lambda": _scanner()},
        _failing(TAG_INDEX_FAMILY, "AccessDeniedException"),
        ARN_MAP,
        enumerated_arn_types=ENUMERATED,
    )
    assert result.unmeasured[TAG_INDEX_FAMILY].code == "AccessDeniedException"
    assert core.exit_code(result) == 2


def test_no_scanners_executes_nothing_including_tag_scan() -> None:
    def tag_scan(_f: core.ClientFactory, _c: ScanConfig) -> list[Resource]:
        raise AssertionError("tag scan ran with no scanners")

    result = core.run_scan(_factory, CONFIG, {}, tag_scan, ARN_MAP, enumerated_arn_types=ENUMERATED)
    assert core.exit_code(result) == 3


def test_scanner_bug_propagates() -> None:
    def broken(_f: core.ClientFactory, _c: ScanConfig) -> list[Resource]:
        raise KeyError("bug")

    with pytest.raises(KeyError):
        core.run_scan(
            _factory, CONFIG, {"lambda": broken}, None, ARN_MAP, enumerated_arn_types=ENUMERATED
        )


# --- exit_code -----------------------------------------------------------------------------


def _result(**kwargs: Any) -> ScanResult:
    base: dict[str, Any] = {
        "measured": [],
        "unmeasured": {},
        "residue": [],
        "accepted": [],
        "stale_tag_index": [],
    }
    base.update(kwargs)
    return ScanResult(**base)


RESIDUE = Resource("lambda", "function", "crm-agent-eval-api", FN_ARN, "us-east-1")
DENIED = Unmeasurable("iam", "list_roles", "AccessDenied", "denied")


def test_exit_0_clean_with_accepted_and_stale_only() -> None:
    result = _result(
        measured=["kms"], accepted=[(_key("PendingDeletion"), "r")], stale_tag_index=[RESIDUE]
    )
    assert core.exit_code(result) == 0


def test_exit_1_residue() -> None:
    assert core.exit_code(_result(measured=["lambda"], residue=[RESIDUE])) == 1


def test_exit_2_unmeasured() -> None:
    assert core.exit_code(_result(measured=["lambda"], unmeasured={"iam": DENIED})) == 2


def test_exit_2_takes_precedence_over_residue() -> None:
    assert (
        core.exit_code(_result(measured=["lambda"], unmeasured={"iam": DENIED}, residue=[RESIDUE]))
        == 2
    )


def test_exit_2_when_every_family_unmeasured() -> None:
    assert core.exit_code(_result(unmeasured={"iam": DENIED})) == 2


def test_exit_3_nothing_executed() -> None:
    assert core.exit_code(_result()) == 3


# --- tag index classified per (family, ARN type) -------------------------------------------


LAYER_ARN = f"arn:aws:lambda:us-east-1:{ACCOUNT}:layer:crm-agent-eval-x:1"


def _merge(scanners: dict[str, core.FamilyScanner], *hits: str, **kwargs: Any) -> ScanResult:
    arn_map = kwargs.get("arn_map", ARN_MAP)
    enumerated = kwargs.get("enumerated", ENUMERATED)
    return core.run_scan(
        _factory,
        CONFIG,
        scanners,
        _scanner(*(_tagged(h) for h in hits)),
        arn_map,
        enumerated_arn_types=enumerated,
    )


def test_tagged_lambda_layer_with_empty_function_list_is_residue() -> None:
    result = _merge({"lambda": _scanner()}, LAYER_ARN)
    assert [(r.family, r.kind, r.arn) for r in result.residue] == [
        ("lambda", "tagged-unscanned", LAYER_ARN)
    ]
    assert result.stale_tag_index == []
    assert core.exit_code(result) == 1


def test_tagged_adopted_vpc_is_accepted_even_though_ec2_does_not_enumerate_vpcs() -> None:
    vpc = f"arn:aws:ec2:us-east-1:{ACCOUNT}:vpc/vpc-1"
    result = _merge({"ec2": _scanner()}, vpc)
    assert [(r.arn, reason.split(":", 1)[0]) for r, reason in result.accepted] == [
        (vpc, "adopted-network")
    ]
    assert result.residue == [] and result.stale_tag_index == []
    assert core.exit_code(result) == 0


def test_enumerated_type_missed_by_scanner_is_stale() -> None:
    sg = f"arn:aws:ec2:us-east-1:{ACCOUNT}:security-group/sg-1"
    result = _merge({"ec2": _scanner()}, sg)
    assert [r.arn for r in result.stale_tag_index] == [sg]
    assert core.exit_code(result) == 0


def test_non_enumerated_billable_type_in_scanned_family_is_residue() -> None:
    endpoint = f"arn:aws:ec2:us-east-1:{ACCOUNT}:vpc-endpoint/vpce-1"
    result = _merge({"ec2": _scanner()}, endpoint)
    assert [r.kind for r in result.residue] == ["tagged-unscanned"]
    assert core.exit_code(result) == 1


def test_type_enumerated_only_by_another_family_does_not_make_a_hit_stale() -> None:
    odd = f"arn:aws:lambda:us-east-1:{ACCOUNT}:security-group:x"
    result = _merge({"lambda": _scanner(), "ec2": _scanner()}, odd)
    assert [r.kind for r in result.residue] == ["tagged-unscanned"]


def test_family_absent_from_enumerated_map_never_yields_stale() -> None:
    result = _merge({"lambda": _scanner()}, FN_ARN, enumerated={})
    assert result.stale_tag_index == []
    assert [r.kind for r in result.residue] == ["tagged-unscanned"]


def test_typeless_s3_bucket_arn_is_stale_when_family_enumerates_empty_type() -> None:
    bucket = "arn:aws:s3:::crm-agent-eval-gone"
    result = _merge({"s3": _scanner()}, bucket, arn_map={"s3": "s3"})
    assert [r.arn for r in result.stale_tag_index] == [bucket]


@pytest.mark.parametrize(
    ("arn", "expected"),
    [
        (f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/lambda/crm-agent-eval-x", "log-group"),
        (f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/lambda/crm-agent-eval-x:*", "log-group"),
        ("arn:aws:apigateway:us-east-1::/restapis/abc123", "restapis"),
        ("arn:aws:apigateway:us-east-1::/restapis/abc123/stages/prod", "restapis"),
        ("arn:aws:apigateway:us-east-1::/apis/abc123", "apis"),
        (f"arn:aws:ec2:us-east-1:{ACCOUNT}:natgateway/nat-1", "natgateway"),
        (f"arn:aws:iam::{ACCOUNT}:role/service-role/crm-agent-eval-x", "role"),
        (f"arn:aws:iam::{ACCOUNT}:policy/crm-agent-eval-p", "policy"),
        (f"arn:aws:lambda:us-east-1:{ACCOUNT}:layer:crm-agent-eval-x:1", "layer"),
        (f"arn:aws:states:us-east-1:{ACCOUNT}:stateMachine:crm-agent-eval-sweep", "stateMachine"),
        (f"arn:aws:ssm:us-east-1:{ACCOUNT}:parameter/crm-agent-eval/x", "parameter"),
        (f"arn:aws:cloudfront::{ACCOUNT}:distribution/E123", "distribution"),
        ("arn:aws:s3:::crm-agent-eval-bucket", ""),
        ("arn:aws:sns:us-east-1:123456789012:crm-agent-eval-topic", ""),
        ("not-an-arn", ""),
    ],
)
def test_arn_resource_type_per_arn_shape(arn: str, expected: str) -> None:
    assert core.arn_resource_type(arn) == expected


def test_tagged_log_group_missed_by_logs_scanner_is_stale() -> None:
    group = f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/lambda/crm-agent-eval-gone"
    result = _merge(
        {"logs": _scanner()},
        group,
        arn_map={"logs": "logs"},
        enumerated={"logs": frozenset({"log-group"})},
    )
    assert [r.arn for r in result.stale_tag_index] == [group]

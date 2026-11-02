# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.resource_protection` against fake AWS clients and injected outputs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from botocore.exceptions import ClientError
from scripts import plan_fixtures
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.lambda_actions import function_name_for
from scripts.smoke.public_exposure import source_declared_function_names
from scripts.smoke.resource_protection import (
    ALB_ARN_OUTPUT,
    CHECK_ALB_DELETION_PROTECTION,
    CHECK_CODEBUILD_ENCRYPTION_KEY,
    CHECK_LOG_GROUP_RETENTION,
    CHECK_LOG_GROUPS_DISCOVERED,
    CHECK_TRAIL_LOGGING,
    CHECK_TRAIL_MULTI_REGION,
    CODEBUILD_PROJECT_NAME,
    EVAL_ARTIFACTS_BUCKET_OUTPUT,
    LOG_GROUP_PREFIXES,
    STATE_MACHINE_ARN_OUTPUT,
    TRAIL_BUCKET_OUTPUT,
    TRAIL_CHECKS,
    checks,
    expected_check_count,
    expected_log_groups,
)

from crm.toolspec.definitions import ALL_TOOLS

_BUCKET = "crm-cloudtrail-logs-123456789012"
_TRAIL_ARN = "arn:aws:cloudtrail:us-east-1:123456789012:trail/crm-trail"
_ALB_ARN = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/crm-api-origin/x"
_ARTIFACTS_BUCKET = "crm-eval-artifacts-123456789012"
_NAMED_GROUPS = {
    "agent_relay_log_group_name": "/aws/lambda/crm-agent-eval-agent-relay",
    "agentcore_runtime_log_group_name": "/aws/bedrock-agentcore/runtimes/crm_runtime-x-DEFAULT",
    "vpc_flow_log_group_name": "/aws/vpc/crm-agent-eval/flow-logs",
}
_EVAL_TRIGGER = "crm-agent-eval-eval-trigger"
_STATE_MACHINE_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:crm-agent-eval-eval-sweep"
_TOOL_GROUPS = [f"/aws/lambda/{function_name_for(tool)}" for tool in ALL_TOOLS]
_FUNCTION_GROUPS = [f"/aws/lambda/{name}" for name in source_declared_function_names()]
_OUTPUTS = {
    TRAIL_BUCKET_OUTPUT: _BUCKET,
    ALB_ARN_OUTPUT: _ALB_ARN,
    EVAL_ARTIFACTS_BUCKET_OUTPUT: _ARTIFACTS_BUCKET,
    "eval_trigger_function_name": _EVAL_TRIGGER,
    STATE_MACHINE_ARN_OUTPUT: _STATE_MACHINE_ARN,
    **_NAMED_GROUPS,
}
_TRAIL_LOG_GROUP = "/aws/cloudtrail/crm-trail"
_TRAIL_LOG_GROUP_ARN = f"arn:aws:logs:us-east-1:123456789012:log-group:{_TRAIL_LOG_GROUP}:*"
_CMK_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"
_CMK_ARN = f"arn:aws:kms:us-east-1:123456789012:key/{_CMK_ID}"
_CMK_ALIAS = "arn:aws:kms:us-east-1:123456789012:alias/crm-agent-eval-eval-artifacts"
_AWS_S3_ALIAS = "arn:aws:kms:us-east-1:123456789012:alias/aws/s3"
_AWS_KEY_ID = "ffffffff-12ab-34cd-56ef-1234567890ab"
_KEYS = {
    _CMK_ARN: {"KeyId": _CMK_ID, "KeyManager": "CUSTOMER"},
    _CMK_ALIAS: {"KeyId": _CMK_ID, "KeyManager": "CUSTOMER"},
    _AWS_S3_ALIAS: {"KeyId": _AWS_KEY_ID, "KeyManager": "AWS"},
}


def _trail(**overrides: Any) -> dict[str, Any]:
    trail: dict[str, Any] = {
        "Name": "crm-trail",
        "TrailARN": _TRAIL_ARN,
        "S3BucketName": _BUCKET,
        "IsMultiRegionTrail": True,
        "IncludeGlobalServiceEvents": True,
    }
    trail.update(overrides)
    return trail


_OTHER_TRAIL = {
    "Name": "account-trail",
    "TrailARN": "arn:aws:cloudtrail:us-east-1:123456789012:trail/account-trail",
    "S3BucketName": "some-other-bucket",
    "IsMultiRegionTrail": True,
    "IncludeGlobalServiceEvents": True,
}


def _client_error(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, operation)


class _FakeCloudTrail:
    def __init__(
        self,
        trails: list[dict[str, Any]] | Exception,
        status: Mapping[str, Any] | Exception | None = None,
    ) -> None:
        self._trails = trails
        self._status = {"IsLogging": True} if status is None else status
        self.status_names: list[str] = []
        self.describe_kwargs: list[dict[str, Any]] = []

    def describe_trails(self, **kwargs: Any) -> dict[str, Any]:
        self.describe_kwargs.append(kwargs)
        if isinstance(self._trails, Exception):
            raise self._trails
        return {"trailList": self._trails}

    def get_trail_status(self, **kwargs: Any) -> Mapping[str, Any]:
        self.status_names.append(kwargs["Name"])
        if isinstance(self._status, Exception):
            raise self._status
        return self._status


class _FakeElbv2:
    def __init__(self, attributes: list[dict[str, str]] | Exception | None = None) -> None:
        self._attributes = (
            [{"Key": "deletion_protection.enabled", "Value": "true"}]
            if attributes is None
            else attributes
        )
        self.arns: list[str] = []

    def describe_load_balancer_attributes(self, **kwargs: Any) -> dict[str, Any]:
        self.arns.append(kwargs["LoadBalancerArn"])
        if isinstance(self._attributes, Exception):
            raise self._attributes
        return {"Attributes": self._attributes}


def _groups(retention: int | None = 365) -> dict[str, dict[str, Any]]:
    names = [
        *_NAMED_GROUPS.values(),
        *_FUNCTION_GROUPS,
        "/aws/codebuild/crm-agent-eval-agent-image",
        "/aws/vendedlogs/states/crm-agent-eval-eval-sweep",
        "/aws/apigateway/crm-agent-eval-api",
    ]
    return {
        name: {
            "logGroupName": name,
            **({} if retention is None else {"retentionInDays": retention}),
        }
        for name in names
    }


class _FakePaginator:
    def __init__(self, logs: _FakeLogs) -> None:
        self._logs = logs

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        prefix = kwargs["logGroupNamePrefix"]
        self._logs.prefixes.append(prefix)
        if isinstance(self._logs.groups, Exception):
            raise self._logs.groups
        matching = [g for name, g in self._logs.groups.items() if name.startswith(prefix)]
        return [{"logGroups": matching[:1]}, {"logGroups": matching[1:]}]


class _FakeLogs:
    def __init__(self, groups: dict[str, dict[str, Any]] | Exception | None = None) -> None:
        self.groups = _groups() if groups is None else groups
        self.prefixes: list[str] = []

    def get_paginator(self, name: str) -> _FakePaginator:
        assert name == "describe_log_groups"
        return _FakePaginator(self)


class _FakeCodeBuild:
    def __init__(self, projects: list[dict[str, Any]] | Exception | None = None) -> None:
        self._projects = (
            [{"name": CODEBUILD_PROJECT_NAME, "encryptionKey": _CMK_ARN}]
            if projects is None
            else projects
        )
        self.names: list[list[str]] = []

    def batch_get_projects(self, **kwargs: Any) -> dict[str, Any]:
        self.names.append(kwargs["names"])
        if isinstance(self._projects, Exception):
            raise self._projects
        return {"projects": self._projects}


class _FakeKms:
    def describe_key(self, **kwargs: Any) -> dict[str, Any]:
        return {"KeyMetadata": _KEYS[kwargs["KeyId"]]}


class _FakeS3:
    def __init__(self, keys: list[str | None] | None = None) -> None:
        self._keys = [_CMK_ALIAS] if keys is None else keys

    def get_bucket_encryption(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["Bucket"] == _ARTIFACTS_BUCKET
        rules = [{"ApplyServerSideEncryptionByDefault": {"KMSMasterKeyID": k}} for k in self._keys]
        return {"ServerSideEncryptionConfiguration": {"Rules": rules}}


def _results(
    fake: _FakeCloudTrail | None = None,
    elbv2: _FakeElbv2 | None = None,
    outputs: Mapping[str, Any] | None = None,
    *,
    logs: _FakeLogs | None = None,
    codebuild: _FakeCodeBuild | None = None,
    s3: _FakeS3 | None = None,
) -> list[CheckResult]:
    ctx = context_for_test(
        outputs=_OUTPUTS if outputs is None else outputs,
        clients={
            "cloudtrail": _FakeCloudTrail([_trail()]) if fake is None else fake,
            "elbv2": _FakeElbv2() if elbv2 is None else elbv2,
            "logs": _FakeLogs() if logs is None else logs,
            "codebuild": _FakeCodeBuild() if codebuild is None else codebuild,
            "kms": _FakeKms(),
            "s3": _FakeS3() if s3 is None else s3,
        },
    )
    return list(checks(ctx))


def _run_all(
    fake: _FakeCloudTrail,
    elbv2: _FakeElbv2 | None = None,
    outputs: Mapping[str, Any] | None = None,
) -> dict[str, CheckResult]:
    """Every row except the per-group retention rows, keyed by check."""
    results = [r for r in _results(fake, elbv2, outputs) if r.check != CHECK_LOG_GROUP_RETENTION]
    by_check = {result.check: result for result in results}
    assert len(by_check) == len(results), results
    return by_check


def _run(fake: _FakeCloudTrail, outputs: Mapping[str, Any] | None = None) -> dict[str, CheckResult]:
    """The trail rows only."""
    results = _run_all(fake, outputs=outputs)
    return {check: result for check, result in results.items() if check in TRAIL_CHECKS}


def _alb(elbv2: _FakeElbv2, outputs: Mapping[str, Any] | None = None) -> CheckResult:
    return _run_all(_FakeCloudTrail([_trail()]), elbv2, outputs)[CHECK_ALB_DELETION_PROTECTION]


def test_a_multi_region_logging_trail_passes_both_rows() -> None:
    fake = _FakeCloudTrail([_OTHER_TRAIL, _trail()])
    results = _run(fake)
    assert {check: r.status for check, r in results.items()} == {
        CHECK_TRAIL_MULTI_REGION: "pass",
        CHECK_TRAIL_LOGGING: "pass",
    }
    assert fake.status_names == [_TRAIL_ARN]
    assert fake.describe_kwargs == [{"includeShadowTrails": False}]


@pytest.mark.parametrize(
    "overrides",
    [
        {"IsMultiRegionTrail": False},
        {"IncludeGlobalServiceEvents": False},
        {"IsMultiRegionTrail": None},
        {"IsMultiRegionTrail": "true"},
    ],
)
def test_a_single_region_or_no_global_events_trail_fails_the_multi_region_row(
    overrides: dict[str, Any],
) -> None:
    results = _run(_FakeCloudTrail([_trail(**overrides)]))
    assert results[CHECK_TRAIL_MULTI_REGION].status == "fail"
    assert results[CHECK_TRAIL_LOGGING].status == "pass"


@pytest.mark.parametrize("status", [{"IsLogging": False}, {}])
def test_a_trail_not_logging_fails_the_logging_row(status: Mapping[str, Any]) -> None:
    results = _run(_FakeCloudTrail([_trail()], status=status))
    assert results[CHECK_TRAIL_LOGGING].status == "fail"
    assert results[CHECK_TRAIL_MULTI_REGION].status == "pass"


def test_a_describe_trails_client_error_fails_every_trail_row() -> None:
    results = _run(_FakeCloudTrail(_client_error("DescribeTrails")))
    assert set(results) == set(TRAIL_CHECKS)
    assert all(r.status == "fail" for r in results.values())
    assert all("AccessDeniedException" in r.detail for r in results.values())


def test_a_get_trail_status_client_error_fails_the_logging_row() -> None:
    results = _run(_FakeCloudTrail([_trail()], status=_client_error("GetTrailStatus")))
    assert results[CHECK_TRAIL_LOGGING].status == "fail"
    assert "AccessDeniedException" in results[CHECK_TRAIL_LOGGING].detail


@pytest.mark.parametrize(
    "trails",
    [[_OTHER_TRAIL], [_trail(), _trail(Name="second", TrailARN=_TRAIL_ARN + "-2")], []],
)
def test_zero_or_several_trails_on_the_stack_bucket_fail_every_row(
    trails: list[dict[str, Any]],
) -> None:
    fake = _FakeCloudTrail(trails)
    results = _run(fake)
    assert set(results) == set(TRAIL_CHECKS)
    assert all(r.status == "fail" for r in results.values())
    assert fake.status_names == []


def test_a_missing_bucket_output_fails_every_row_without_calling_aws() -> None:
    fake = _FakeCloudTrail([_trail()])
    results = _run(fake, outputs={ALB_ARN_OUTPUT: _ALB_ARN})
    assert all(r.status == "fail" for r in results.values())
    assert set(results) == set(TRAIL_CHECKS)
    assert fake.describe_kwargs == []


def test_the_floor_is_met_by_a_healthy_run_and_counts_every_expected_group() -> None:
    ctx = context_for_test(outputs=_OUTPUTS)
    floor = expected_check_count(ctx).minimum
    results = _results()
    assert floor <= len(results)
    retention_rows = [r for r in results if r.check == CHECK_LOG_GROUP_RETENTION]
    expected = expected_log_groups(ctx)[0]
    assert floor == len(results) - len(retention_rows) + len(expected)
    assert len(retention_rows) == len(expected) == len(set(expected))


def test_the_floor_reds_a_run_that_loses_the_action_lambda_groups() -> None:
    """A prefix drift dropping the Lambda groups cannot pass on the named groups."""
    groups = {k: v for k, v in _groups().items() if not k.startswith("/aws/lambda/crm-agent-eval-")}
    groups[_NAMED_GROUPS["agent_relay_log_group_name"]] = {
        "logGroupName": _NAMED_GROUPS["agent_relay_log_group_name"],
        "retentionInDays": 365,
    }
    results = _results(logs=_FakeLogs(groups))
    assert len(results) < expected_check_count(context_for_test(outputs=_OUTPUTS)).minimum
    discovery = _row(results, CHECK_LOG_GROUPS_DISCOVERED)
    assert discovery.status == "fail"
    assert f"/aws/lambda/{function_name_for(ALL_TOOLS[0])}" in discovery.detail


@pytest.mark.parametrize(
    "prefix", [p for p in LOG_GROUP_PREFIXES if not p.startswith("/aws/lambda/")]
)
def test_a_prefix_matching_no_group_fails_discovery(prefix: str) -> None:
    """The Lambda prefix is covered by the action-Lambda floor cell above."""
    groups = {k: v for k, v in _groups().items() if not k.startswith(prefix)}
    result = _row(_results(logs=_FakeLogs(groups)), CHECK_LOG_GROUPS_DISCOVERED)
    assert result.status == "fail" and f"prefix {prefix!r} matches no log group" in result.detail


def test_one_missing_action_lambda_group_fails_discovery_naming_it() -> None:
    groups = _groups()
    missing = _TOOL_GROUPS[-1]
    del groups[missing]
    result = _row(_results(logs=_FakeLogs(groups)), CHECK_LOG_GROUPS_DISCOVERED)
    assert result.status == "fail" and missing in result.detail


def test_a_missing_function_output_fails_discovery() -> None:
    outputs = {k: v for k, v in _OUTPUTS.items() if k != "eval_trigger_function_name"}
    result = _row(_results(outputs=outputs), CHECK_LOG_GROUPS_DISCOVERED)
    assert result.status == "fail" and "eval_trigger_function_name" in result.detail


def test_the_trail_delivery_group_is_required_and_retention_checked() -> None:
    """A trail delivering to CloudWatch Logs must name an existing, retained group."""
    trail = _FakeCloudTrail([_trail(CloudWatchLogsLogGroupArn=_TRAIL_LOG_GROUP_ARN)])
    absent = _results(trail)
    discovery = _row(absent, CHECK_LOG_GROUPS_DISCOVERED)
    assert discovery.status == "fail" and _TRAIL_LOG_GROUP in discovery.detail

    for retention, status in ((365, "pass"), (90, "fail")):
        groups = _groups()
        groups[_TRAIL_LOG_GROUP] = {"logGroupName": _TRAIL_LOG_GROUP, "retentionInDays": retention}
        results = _results(
            _FakeCloudTrail([_trail(CloudWatchLogsLogGroupArn=_TRAIL_LOG_GROUP_ARN)]),
            logs=_FakeLogs(groups),
        )
        assert _row(results, CHECK_LOG_GROUPS_DISCOVERED).status == "pass"
        [row] = [r for r in results if r.resource == _TRAIL_LOG_GROUP]
        assert row.status == status


def test_a_trail_group_without_delivery_is_still_retention_checked() -> None:
    groups = _groups()
    groups[_TRAIL_LOG_GROUP] = {"logGroupName": _TRAIL_LOG_GROUP, "retentionInDays": 30}
    results = _results(logs=_FakeLogs(groups))
    assert _row(results, CHECK_LOG_GROUPS_DISCOVERED).status == "pass"
    [row] = [r for r in results if r.resource == _TRAIL_LOG_GROUP]
    assert row.status == "fail"


def test_no_trail_group_is_required_when_the_trail_delivers_nowhere() -> None:
    result = _row(_results(), CHECK_LOG_GROUPS_DISCOVERED)
    assert result.status == "pass" and "none configured on the trail" in result.detail


def test_every_row_passes_on_a_protected_stack() -> None:
    results = _results()
    assert {r.status for r in results} == {"pass"}, [r for r in results if r.status != "pass"]
    assert {r.check for r in results} == {
        CHECK_TRAIL_MULTI_REGION,
        CHECK_TRAIL_LOGGING,
        CHECK_ALB_DELETION_PROTECTION,
        CHECK_LOG_GROUPS_DISCOVERED,
        CHECK_LOG_GROUP_RETENTION,
        CHECK_CODEBUILD_ENCRYPTION_KEY,
    }
    assert {r.resource for r in results if r.check == CHECK_LOG_GROUP_RETENTION} == set(_groups())


def test_the_alb_row_reads_the_output_arn() -> None:
    elbv2 = _FakeElbv2()
    assert _alb(elbv2).status == "pass"
    assert elbv2.arns == [_ALB_ARN]


@pytest.mark.parametrize(
    "attributes",
    [
        [{"Key": "deletion_protection.enabled", "Value": "false"}],
        [{"Key": "idle_timeout.timeout_seconds", "Value": "60"}],
        [],
        [
            {"Key": "deletion_protection.enabled", "Value": "true"},
            {"Key": "deletion_protection.enabled", "Value": "false"},
        ],
    ],
)
def test_an_unprotected_or_unreadable_alb_attribute_fails(attributes: list[dict[str, str]]) -> None:
    assert _alb(_FakeElbv2(attributes)).status == "fail"


def test_a_describe_attributes_client_error_fails_the_alb_row() -> None:
    result = _alb(_FakeElbv2(_client_error("DescribeLoadBalancerAttributes")))
    assert result.status == "fail"
    assert "AccessDeniedException" in result.detail


def test_a_missing_alb_output_fails_the_alb_row_without_calling_aws() -> None:
    elbv2 = _FakeElbv2()
    result = _alb(elbv2, outputs={TRAIL_BUCKET_OUTPUT: _BUCKET})
    assert result.status == "fail"
    assert elbv2.arns == []


def test_the_alb_row_names_the_load_balancer_without_the_account() -> None:
    result = _alb(_FakeElbv2())
    assert result.resource == "alb:app/crm-api-origin/x"
    assert "123456789012" not in result.resource


def _row(results: list[CheckResult], check: str) -> CheckResult:
    rows = [r for r in results if r.check == check]
    assert len(rows) == 1, rows
    return rows[0]


@pytest.mark.parametrize("retention", [30, 364, None])
def test_a_short_or_unset_retention_fails_every_affected_group(retention: int | None) -> None:
    rows = [
        r
        for r in _results(logs=_FakeLogs(_groups(retention)))
        if r.check == CHECK_LOG_GROUP_RETENTION
    ]
    assert len(rows) == len(_groups()) and {r.status for r in rows} == {"fail"}


def test_one_short_group_fails_only_its_own_row() -> None:
    groups = _groups()
    groups[_TOOL_GROUPS[0]]["retentionInDays"] = 30
    rows = {
        r.resource: r.status
        for r in _results(logs=_FakeLogs(groups))
        if r.check == CHECK_LOG_GROUP_RETENTION
    }
    assert rows.pop(_TOOL_GROUPS[0]) == "fail"
    assert set(rows.values()) == {"pass"}


def test_an_empty_discovery_set_fails() -> None:
    results = _results(logs=_FakeLogs({}))
    assert _row(results, CHECK_LOG_GROUPS_DISCOVERED).status == "fail"
    assert [r for r in results if r.check == CHECK_LOG_GROUP_RETENTION] == []


def test_a_missing_output_named_group_fails_discovery() -> None:
    groups = _groups()
    del groups[_NAMED_GROUPS["vpc_flow_log_group_name"]]
    result = _row(_results(logs=_FakeLogs(groups)), CHECK_LOG_GROUPS_DISCOVERED)
    assert result.status == "fail" and "flow-logs" in result.detail


def test_a_missing_log_group_output_fails_discovery() -> None:
    outputs = {k: v for k, v in _OUTPUTS.items() if k != "agentcore_runtime_log_group_name"}
    result = _row(_results(outputs=outputs), CHECK_LOG_GROUPS_DISCOVERED)
    assert result.status == "fail" and "agentcore_runtime_log_group_name" in result.detail


def test_a_named_group_query_does_not_admit_a_longer_sibling() -> None:
    groups = _groups()
    flow = _NAMED_GROUPS["vpc_flow_log_group_name"]
    groups[flow + "-old"] = {"logGroupName": flow + "-old"}
    resources = {
        r.resource for r in _results(logs=_FakeLogs(groups)) if r.check == CHECK_LOG_GROUP_RETENTION
    }
    assert flow + "-old" not in resources


def test_a_describe_log_groups_error_fails_discovery() -> None:
    result = _row(
        _results(logs=_FakeLogs(_client_error("DescribeLogGroups"))), CHECK_LOG_GROUPS_DISCOVERED
    )
    assert result.status == "fail" and "AccessDeniedException" in result.detail


def test_the_codebuild_row_passes_on_the_eval_artifacts_cmk_and_names_the_project() -> None:
    codebuild = _FakeCodeBuild()
    result = _row(_results(codebuild=codebuild), CHECK_CODEBUILD_ENCRYPTION_KEY)
    assert result.status == "pass"
    assert codebuild.names == [[CODEBUILD_PROJECT_NAME]]


@pytest.mark.parametrize(
    ("projects", "s3_keys"),
    [
        ([{"name": CODEBUILD_PROJECT_NAME, "encryptionKey": _AWS_S3_ALIAS}], None),
        ([{"name": CODEBUILD_PROJECT_NAME}], None),
        ([], None),
        (None, [_AWS_S3_ALIAS]),
        (None, []),
        (None, [None]),
    ],
)
def test_a_codebuild_key_that_is_not_the_eval_artifacts_cmk_fails(
    projects: list[dict[str, Any]] | None, s3_keys: list[str | None] | None
) -> None:
    result = _row(
        _results(codebuild=_FakeCodeBuild(projects), s3=_FakeS3(s3_keys)),
        CHECK_CODEBUILD_ENCRYPTION_KEY,
    )
    assert result.status == "fail"


def test_a_batch_get_projects_error_fails_the_codebuild_row() -> None:
    result = _row(
        _results(codebuild=_FakeCodeBuild(_client_error("BatchGetProjects"))),
        CHECK_CODEBUILD_ENCRYPTION_KEY,
    )
    assert result.status == "fail" and "AccessDeniedException" in result.detail


_EXPECTED = expected_log_groups(context_for_test(outputs=_OUTPUTS))[0]


@pytest.mark.parametrize("dropped", _EXPECTED)
def test_dropping_any_expected_group_reds_discovery(dropped: str) -> None:
    """Every IaC-declared group is expected by name, not floored by a prefix."""
    groups = _groups()
    del groups[dropped]
    result = _row(_results(logs=_FakeLogs(groups)), CHECK_LOG_GROUPS_DISCOVERED)
    assert result.status == "fail"
    assert repr(dropped) in result.detail


def test_the_expected_set_is_every_log_group_the_plan_declares() -> None:
    """The expected names equal the `aws_cloudwatch_log_group` names in the committed root plan.

    The runtime group's name is unknown at plan time, so the plan's unnamed group is matched by the
    `agentcore_runtime_log_group_name` output.
    """
    plan = plan_fixtures.build("compliant")
    groups = [c for c in plan["resource_changes"] if c.get("type") == "aws_cloudwatch_log_group"]
    named = {c["change"]["after"]["name"] for c in groups if c["change"]["after"].get("name")}
    unnamed = [c["address"] for c in groups if not c["change"]["after"].get("name")]
    assert unnamed == ["module.agentcore.aws_cloudwatch_log_group.runtime"]
    runtime = _NAMED_GROUPS["agentcore_runtime_log_group_name"]
    assert set(_EXPECTED) - {runtime} == named
    assert runtime in _EXPECTED


def test_a_missing_state_machine_output_fails_discovery() -> None:
    outputs = {k: v for k, v in _OUTPUTS.items() if k != STATE_MACHINE_ARN_OUTPUT}
    result = _row(_results(outputs=outputs), CHECK_LOG_GROUPS_DISCOVERED)
    assert result.status == "fail" and STATE_MACHINE_ARN_OUTPUT in result.detail


def test_a_malformed_state_machine_arn_fails_discovery() -> None:
    outputs = {**_OUTPUTS, STATE_MACHINE_ARN_OUTPUT: "crm-agent-eval-eval-sweep"}
    result = _row(_results(outputs=outputs), CHECK_LOG_GROUPS_DISCOVERED)
    assert result.status == "fail" and STATE_MACHINE_ARN_OUTPUT in result.detail

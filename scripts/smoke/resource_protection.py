# SPDX-License-Identifier: MIT
"""Layer 4: audit-trail coverage and deletion-protection posture, read from the live account.

The trail is the one whose `S3BucketName` is the `cloudtrail_s3_bucket_name` output; exactly one
trail must match. Two rows for it:

1. **`trail_multi_region`** — `describe-trails` reports `IsMultiRegionTrail: true` and
   `IncludeGlobalServiceEvents: true`.
2. **`trail_logging`** — `get-trail-status` reports `IsLogging: true`.

One row for the ALB named by the `api_origin_alb_arn` output:

3. **`alb_deletion_protection`** — `describe-load-balancer-attributes` reports
   `deletion_protection.enabled` as `"true"`.

Log groups: the groups under this stack's prefixes (`LOG_GROUP_PREFIXES`), the groups named by
outputs (`LOG_GROUP_OUTPUTS`), the `/aws/lambda/<name>` group of every function in
`public_exposure.source_declared_function_names()` and `LAMBDA_FUNCTION_OUTPUTS`, the state
machine's group (from `eval_state_machine_arn`), the CodeBuild and API access-log groups, and
the trail's CloudWatch Logs group (its `CloudWatchLogsLogGroupArn`, plus
`/aws/cloudtrail/<trail name>` when that exists):

4. **`log_groups_discovered`** — every prefix matches at least one group, every expected group
   exists, and so does the trail's delivery group when the trail names one.
5. **`log_group_retention`**, one per group — `retentionInDays >= 365`; absent (never expire) fails.

The CodeBuild project `<prefix>-agent-image`:

6. **`codebuild_encryption_key`** — `encryptionKey` resolves through `kms describe-key` to a
   customer-managed key, the same key as the eval-artefacts bucket's default SSE-KMS key.

Any API error, a missing output, or zero or several matching trails is a `fail` on every row it
prevents, never a skip.

Usage (Layer 4, requires credentials):

    .venv/bin/python3 -m scripts.smoke.resource_protection --json

Required Terraform outputs: `cloudtrail_s3_bucket_name`, `api_origin_alb_arn`,
`eval_artifacts_bucket_name`, `eval_state_machine_arn`, the `LOG_GROUP_OUTPUTS` and the
`LAMBDA_FUNCTION_OUTPUTS`.
Required IAM: `cloudtrail:DescribeTrails`,
`cloudtrail:GetTrailStatus`, `elasticloadbalancing:DescribeLoadBalancerAttributes`,
`logs:DescribeLogGroups`, `codebuild:BatchGetProjects`, `kms:DescribeKey`,
`s3:GetEncryptionConfiguration`.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import Any, Final

from scripts.smoke.harness import CheckResult, ExpectedCheckCount, SmokeContext, run_main
from scripts.smoke.lambda_actions import FUNCTION_NAME_PREFIX
from scripts.smoke.public_exposure import source_declared_function_names

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "resource_protection"

CHECK_TRAIL_MULTI_REGION: Final[str] = "trail_multi_region"
CHECK_TRAIL_LOGGING: Final[str] = "trail_logging"

TRAIL_CHECKS: Final[tuple[str, ...]] = (CHECK_TRAIL_MULTI_REGION, CHECK_TRAIL_LOGGING)

TRAIL_BUCKET_OUTPUT: Final[str] = "cloudtrail_s3_bucket_name"

CHECK_ALB_DELETION_PROTECTION: Final[str] = "alb_deletion_protection"
ALB_ARN_OUTPUT: Final[str] = "api_origin_alb_arn"

CHECK_LOG_GROUPS_DISCOVERED: Final[str] = "log_groups_discovered"
CHECK_LOG_GROUP_RETENTION: Final[str] = "log_group_retention"
MINIMUM_RETENTION_DAYS: Final[int] = 365

#: Log-group name prefixes owned by this stack, each followed by `FUNCTION_NAME_PREFIX`.
LOG_GROUP_PREFIXES: Final[tuple[str, ...]] = (
    f"/aws/lambda/{FUNCTION_NAME_PREFIX}",
    f"/aws/codebuild/{FUNCTION_NAME_PREFIX}",
    f"/aws/vendedlogs/states/{FUNCTION_NAME_PREFIX}",
    f"/aws/apigateway/{FUNCTION_NAME_PREFIX}",
)

#: Outputs naming log groups outside `LOG_GROUP_PREFIXES` (or that must be present regardless).
LOG_GROUP_OUTPUTS: Final[tuple[str, ...]] = (
    "agent_relay_log_group_name",
    "agentcore_runtime_log_group_name",
    "vpc_flow_log_group_name",
)

#: Outputs naming Lambda functions whose `/aws/lambda/<name>` group must exist.
LAMBDA_FUNCTION_OUTPUTS: Final[tuple[str, ...]] = ("eval_trigger_function_name",)

TRAIL_LOG_GROUP_PREFIX: Final[str] = "/aws/cloudtrail/"

STATE_MACHINE_ARN_OUTPUT: Final[str] = "eval_state_machine_arn"
API_ACCESS_LOG_GROUP: Final[str] = f"/aws/apigateway/{FUNCTION_NAME_PREFIX}api"

CHECK_CODEBUILD_ENCRYPTION_KEY: Final[str] = "codebuild_encryption_key"
CODEBUILD_PROJECT_NAME: Final[str] = f"{FUNCTION_NAME_PREFIX}agent-image"
EVAL_ARTIFACTS_BUCKET_OUTPUT: Final[str] = "eval_artifacts_bucket_name"


def _fail(resource: str, check: str, detail: str) -> CheckResult:
    return CheckResult(resource=resource, check=check, status="fail", detail=detail)


def _stack_trail(ctx: SmokeContext, bucket: str) -> Mapping[str, Any] | str:
    """The one trail delivering to `bucket`, or why it could not be identified."""
    try:
        response = ctx.client("cloudtrail").describe_trails(includeShadowTrails=False)
    except Exception as exc:  # noqa: BLE001 - reported as a fail row, never aborts the run
        return f"describe-trails failed: {type(exc).__name__}: {exc}"
    matching = [
        trail for trail in response.get("trailList", []) if trail.get("S3BucketName") == bucket
    ]
    if len(matching) != 1:
        names = sorted(str(trail.get("Name")) for trail in matching)
        return (
            f"expected exactly one trail delivering to s3://{bucket}, found {len(matching)} {names}"
        )
    trail: Mapping[str, Any] = matching[0]
    return trail


def _check_multi_region(resource: str, trail: Mapping[str, Any]) -> CheckResult:
    multi_region = trail.get("IsMultiRegionTrail")
    global_events = trail.get("IncludeGlobalServiceEvents")
    observed = f"IsMultiRegionTrail={multi_region!r}, IncludeGlobalServiceEvents={global_events!r}"
    if multi_region is True and global_events is True:
        return CheckResult(
            resource=resource, check=CHECK_TRAIL_MULTI_REGION, status="pass", detail=observed
        )
    return _fail(resource, CHECK_TRAIL_MULTI_REGION, f"{observed}; both must be true")


def _check_logging(ctx: SmokeContext, resource: str, trail: Mapping[str, Any]) -> CheckResult:
    name = str(trail.get("TrailARN") or trail.get("Name"))
    try:
        status = ctx.client("cloudtrail").get_trail_status(Name=name)
    except Exception as exc:  # noqa: BLE001 - reported as a fail row, never aborts the run
        return _fail(
            resource, CHECK_TRAIL_LOGGING, f"get-trail-status failed: {type(exc).__name__}: {exc}"
        )
    is_logging = status.get("IsLogging")
    if is_logging is True:
        return CheckResult(
            resource=resource, check=CHECK_TRAIL_LOGGING, status="pass", detail="IsLogging=True"
        )
    return _fail(resource, CHECK_TRAIL_LOGGING, f"IsLogging={is_logging!r}; must be true")


def resolve_trail(ctx: SmokeContext) -> tuple[str, Mapping[str, Any] | str]:
    """`(resource, trail)` for the stack's trail, or `(resource, reason)` if it is unidentified."""
    try:
        bucket = ctx.outputs.require_str(TRAIL_BUCKET_OUTPUT)
    except Exception as exc:  # noqa: BLE001 - a missing output is a reportable failure
        return "cloudtrail", f"cannot locate the trail: {exc}"
    trail = _stack_trail(ctx, bucket)
    if isinstance(trail, str):
        return f"cloudtrail:{bucket}", trail
    return f"cloudtrail:{trail.get('Name')}", trail


def trail_checks(
    ctx: SmokeContext, resolved: tuple[str, Mapping[str, Any] | str] | None = None
) -> list[CheckResult]:
    """The trail rows, one per `TRAIL_CHECKS` entry whatever the outcome."""
    resource, trail = resolve_trail(ctx) if resolved is None else resolved
    if isinstance(trail, str):
        return [_fail(resource, check, trail) for check in TRAIL_CHECKS]
    return [_check_multi_region(resource, trail), _check_logging(ctx, resource, trail)]


def alb_check(ctx: SmokeContext) -> CheckResult:
    """The ALB deletion-protection row."""
    try:
        arn = ctx.outputs.require_str(ALB_ARN_OUTPUT)
    except Exception as exc:  # noqa: BLE001 - a missing output is a reportable failure
        return _fail("alb", CHECK_ALB_DELETION_PROTECTION, f"cannot locate the ALB: {exc}")
    resource = f"alb:{arn.partition(':loadbalancer/')[2] or arn}"
    try:
        response = ctx.client("elbv2").describe_load_balancer_attributes(LoadBalancerArn=arn)
    except Exception as exc:  # noqa: BLE001 - reported as a fail row, never aborts the run
        return _fail(
            resource,
            CHECK_ALB_DELETION_PROTECTION,
            f"describe-load-balancer-attributes failed: {type(exc).__name__}: {exc}",
        )
    values = [
        attribute.get("Value")
        for attribute in response.get("Attributes", [])
        if attribute.get("Key") == "deletion_protection.enabled"
    ]
    if values == ["true"]:
        return CheckResult(
            resource=resource,
            check=CHECK_ALB_DELETION_PROTECTION,
            status="pass",
            detail="deletion_protection.enabled=true",
        )
    return _fail(
        resource,
        CHECK_ALB_DELETION_PROTECTION,
        f"deletion_protection.enabled values {values!r}; expected exactly ['true']",
    )


def _log_groups(ctx: SmokeContext, prefix: str) -> list[Mapping[str, Any]]:
    groups: list[Mapping[str, Any]] = []
    paginator = ctx.client("logs").get_paginator("describe_log_groups")
    for page in paginator.paginate(logGroupNamePrefix=prefix):
        groups.extend(page.get("logGroups", []))
    return groups


def expected_log_groups(ctx: SmokeContext) -> tuple[list[str], list[str]]:
    """`(names, problems)`: every log group the IaC declares that must exist on the stack.

    Lambda groups come from `public_exposure.source_declared_function_names`; the state machine's
    from the `eval_state_machine_arn` output; the rest from outputs or the IaC's naming of them.
    """
    names: list[str] = []
    problems: list[str] = []
    for output in LOG_GROUP_OUTPUTS:
        try:
            names.append(ctx.outputs.require_str(output))
        except Exception as exc:  # noqa: BLE001 - a missing output is a reportable failure
            problems.append(f"output {output!r} unavailable: {exc}")
    functions = list(source_declared_function_names())
    for output in LAMBDA_FUNCTION_OUTPUTS:
        try:
            functions.append(ctx.outputs.require_str(output))
        except Exception as exc:  # noqa: BLE001 - a missing output is a reportable failure
            problems.append(f"output {output!r} unavailable: {exc}")
    names += [f"/aws/lambda/{function}" for function in functions]
    try:
        state_machine = ctx.outputs.require_str(STATE_MACHINE_ARN_OUTPUT)
        _, marker, state_machine_name = state_machine.partition(":stateMachine:")
        if not marker or not state_machine_name:
            raise ValueError(f"{state_machine!r} is not a state machine ARN")
        names.append(f"/aws/vendedlogs/states/{state_machine_name}")
    except Exception as exc:  # noqa: BLE001 - a missing output is a reportable failure
        problems.append(f"output {STATE_MACHINE_ARN_OUTPUT!r} unusable: {exc}")
    names += [f"/aws/codebuild/{CODEBUILD_PROJECT_NAME}", API_ACCESS_LOG_GROUP]
    return list(dict.fromkeys(names)), problems


def _trail_log_group(trail: Mapping[str, Any] | str) -> tuple[str | None, str | None]:
    """`(delivery group, candidate group)` for the trail: the group its `CloudWatchLogsLogGroupArn`
    names, and `/aws/cloudtrail/<trail name>`, which is checked when it exists."""
    if isinstance(trail, str):
        return None, None
    arn = trail.get("CloudWatchLogsLogGroupArn")
    delivery = None
    if isinstance(arn, str) and ":log-group:" in arn:
        delivery = arn.split(":log-group:", 1)[1].removesuffix(":*")
    return delivery, f"{TRAIL_LOG_GROUP_PREFIX}{trail.get('Name')}"


def log_group_checks(
    ctx: SmokeContext, trail: Mapping[str, Any] | str = "trail not resolved"
) -> list[CheckResult]:
    """One discovery row, then one retention row per discovered or expected group."""
    named, problems = expected_log_groups(ctx)
    delivery, candidate = _trail_log_group(trail)
    if delivery is not None:
        named.append(delivery)
    exact = [*named, *([candidate] if candidate else [])]
    found: dict[str, Mapping[str, Any]] = {}
    unmatched_prefixes: list[str] = []
    try:
        for prefix in LOG_GROUP_PREFIXES:
            groups = _log_groups(ctx, prefix)
            if not groups:
                unmatched_prefixes.append(prefix)
            for group in groups:
                found[str(group.get("logGroupName"))] = group
        for name in exact:
            for group in _log_groups(ctx, name):
                if group.get("logGroupName") == name:
                    found[name] = group
    except Exception as exc:  # noqa: BLE001 - reported as a fail row, never aborts the run
        return [
            _fail(
                "log_groups",
                CHECK_LOG_GROUPS_DISCOVERED,
                f"describe-log-groups failed: {type(exc).__name__}: {exc}",
            )
        ]
    problems += [f"prefix {prefix!r} matches no log group" for prefix in unmatched_prefixes]
    problems += [f"expected group {name!r} does not exist" for name in named if name not in found]
    results = [
        CheckResult(
            resource="log_groups",
            check=CHECK_LOG_GROUPS_DISCOVERED,
            status="fail" if problems else "pass",
            detail="; ".join(problems)
            or (
                f"{len(found)} group(s); every prefix matched, all {len(named)} expected group(s) "
                "present; trail delivery group: "
                + (delivery if delivery is not None else "none configured on the trail")
            ),
        )
    ]
    for name in sorted(found):
        retention = found[name].get("retentionInDays")
        ok = isinstance(retention, int) and retention >= MINIMUM_RETENTION_DAYS
        results.append(
            CheckResult(
                resource=name,
                check=CHECK_LOG_GROUP_RETENTION,
                status="pass" if ok else "fail",
                detail=f"retentionInDays={retention!r}; must be >= {MINIMUM_RETENTION_DAYS}",
            )
        )
    return results


def _key_identity(ctx: SmokeContext, key: str) -> Mapping[str, Any]:
    metadata: Mapping[str, Any] = ctx.client("kms").describe_key(KeyId=key)["KeyMetadata"]
    return metadata


def codebuild_check(ctx: SmokeContext) -> CheckResult:
    """The CodeBuild project's artefact key is the eval-artefacts customer-managed key."""
    resource = f"codebuild:{CODEBUILD_PROJECT_NAME}"
    try:
        bucket = ctx.outputs.require_str(EVAL_ARTIFACTS_BUCKET_OUTPUT)
        projects = ctx.client("codebuild").batch_get_projects(names=[CODEBUILD_PROJECT_NAME])
        found = projects.get("projects", [])
        if len(found) != 1:
            return _fail(
                resource,
                CHECK_CODEBUILD_ENCRYPTION_KEY,
                f"batch-get-projects returned {len(found)} project(s)",
            )
        key = found[0].get("encryptionKey")
        if not isinstance(key, str) or not key:
            return _fail(resource, CHECK_CODEBUILD_ENCRYPTION_KEY, "no encryptionKey")
        project_key = _key_identity(ctx, key)
        rules = ctx.client("s3").get_bucket_encryption(Bucket=bucket)[
            "ServerSideEncryptionConfiguration"
        ]["Rules"]
        bucket_keys = [
            rule.get("ApplyServerSideEncryptionByDefault", {}).get("KMSMasterKeyID")
            for rule in rules
        ]
        if len(bucket_keys) != 1 or not bucket_keys[0]:
            return _fail(
                resource,
                CHECK_CODEBUILD_ENCRYPTION_KEY,
                f"eval-artefacts bucket default KMS keys {bucket_keys!r}; expected exactly one",
            )
        bucket_key = _key_identity(ctx, str(bucket_keys[0]))
    except Exception as exc:  # noqa: BLE001 - reported as a fail row, never aborts the run
        return _fail(resource, CHECK_CODEBUILD_ENCRYPTION_KEY, f"{type(exc).__name__}: {exc}")
    observed = (
        f"encryptionKey resolves to KeyManager={project_key.get('KeyManager')!r}; "
        f"same key as the eval-artefacts bucket: {project_key.get('KeyId') == bucket_key.get('KeyId')}"
    )
    if project_key.get("KeyManager") == "CUSTOMER" and project_key.get("KeyId") == bucket_key.get(
        "KeyId"
    ):
        return CheckResult(
            resource=resource, check=CHECK_CODEBUILD_ENCRYPTION_KEY, status="pass", detail=observed
        )
    return _fail(resource, CHECK_CODEBUILD_ENCRYPTION_KEY, observed)


def checks(ctx: SmokeContext) -> Sequence[CheckResult]:
    resolved = resolve_trail(ctx)
    return [
        *trail_checks(ctx, resolved),
        alb_check(ctx),
        *log_group_checks(ctx, resolved[1]),
        codebuild_check(ctx),
    ]


def expected_check_count(ctx: SmokeContext) -> ExpectedCheckCount:
    expected_groups = len(expected_log_groups(ctx)[0])
    minimum = len(TRAIL_CHECKS) + 1 + 1 + expected_groups + 1
    return ExpectedCheckCount(
        minimum=minimum,
        basis=(
            f"{len(TRAIL_CHECKS)} trail rows + 1 ALB row + 1 log-group discovery row + at least "
            f"{expected_groups} retention rows (one per IaC-declared group, expected_log_groups) "
            "+ 1 CodeBuild row"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the posture checks and return the process exit code."""
    return run_main(
        argv,
        layer=LAYER,
        resource_class=RESOURCE_CLASS,
        description=__doc__ or RESOURCE_CLASS,
        checks=checks,
        expected=expected_check_count,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

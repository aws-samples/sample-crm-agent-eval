# SPDX-License-Identifier: MIT
"""Layer 4 access-log delivery smoke check.

Asserts that log objects appear in each access-log destination bucket under each expected prefix.
Delivery is asynchronous, so a prefix with no objects yet is reported `defer` with
`no_log_objects_delivered_yet`, never `pass`.

Each destination's target is asserted first: `check_destination_is_the_sources_log_target` reads the
source's `BucketLoggingStatus` (or the distribution's `Logging` block) and fails when the destination
is not what the source writes to. The delivery check runs only for a destination that passed.

Usage (Layer 4, requires credentials, run after a real request has been made through the stack):

    .venv/bin/python3 -m scripts.smoke.access_log_delivery --outputs results/outputs.json
    .venv/bin/python3 -m scripts.smoke.access_log_delivery --json

Required Terraform outputs: `frontend_cdn_logs_bucket_name`, `cloudtrail_access_logs_bucket_name`,
`frontend_origin_bucket_name`, `eval_artifacts_bucket_name`, `cloudtrail_s3_bucket_name`,
`cloudfront_distribution_id`. Required IAM: `s3:ListBucket` and `s3:GetBucketLogging` on the
destinations and sources, and `cloudfront:GetDistributionConfig`.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from scripts.smoke.harness import CheckResult, ExpectedCheckCount, SmokeContext, run_main

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "access_log_delivery"

#: Machine-readable defer token for an empty prefix: delivery not yet observed.
DEFER_NO_OBJECTS: Final[str] = "no_log_objects_delivered_yet"

#: Machine-readable defer token for an empty CloudFront standard-logging prefix.
DEFER_CLOUDFRONT_MECHANISM: Final[str] = "cloudfront_log_delivery_mechanism_unverified"

#: The two checks each destination carries, in the order they run. The second runs only when the
#: first passed.
CHECK_TARGET: Final[str] = "destination_is_the_sources_log_target"
CHECK_DELIVERED: Final[str] = "log_objects_delivered"


@dataclass(frozen=True)
class LogDestination:
    """One (bucket output, prefix) pair that must be receiving log objects from a named source.

    `source_bucket_output` names the Terraform output for the bucket whose S3 server access logs land
    here; `source_distribution_output` names the CloudFront distribution id output when the source is
    a distribution rather than a bucket. Exactly one of the two is set.
    """

    output_name: str
    prefix: str
    source_description: str
    source_bucket_output: str | None = None
    source_distribution_output: str | None = None
    defer_reason: str = DEFER_NO_OBJECTS

    def __post_init__(self) -> None:
        """Refuse a row whose target cannot be asserted against a named source."""
        named = [self.source_bucket_output, self.source_distribution_output]
        if sum(1 for value in named if value) != 1:
            raise ValueError(
                f"destination {self.output_name}:{self.prefix} must name exactly one source - "
                "either source_bucket_output or source_distribution_output - so that the bucket "
                "this check lists can be asserted to be the one the source logs to"
            )


#: Every prefix the infrastructure configures a log source to write to, each paired with that source,
#: from the `target_bucket`/`target_prefix` and `logging_config` values in `infra/`.
LOG_DESTINATIONS: Final[tuple[LogDestination, ...]] = (
    LogDestination(
        output_name="frontend_cdn_logs_bucket_name",
        prefix="s3-access-logs/origin/",
        source_description="S3 server access logs for the CloudFront origin bucket",
        source_bucket_output="frontend_origin_bucket_name",
    ),
    LogDestination(
        output_name="frontend_cdn_logs_bucket_name",
        prefix="s3-access-logs/eval-artifacts/",
        source_description="S3 server access logs for the eval-artefacts bucket",
        source_bucket_output="eval_artifacts_bucket_name",
    ),
    LogDestination(
        output_name="cloudtrail_access_logs_bucket_name",
        prefix="s3-access-logs/cloudtrail/",
        source_description="S3 server access logs for the CloudTrail delivery bucket",
        # `cloudtrail_s3_bucket_name` is this row's source, not its destination.
        source_bucket_output="cloudtrail_s3_bucket_name",
    ),
    LogDestination(
        output_name="frontend_cdn_logs_bucket_name",
        prefix="cloudfront-access-logs/",
        source_description="CloudFront standard access logs",
        source_distribution_output="cloudfront_distribution_id",
        defer_reason=DEFER_CLOUDFRONT_MECHANISM,
    ),
)


def _fail(resource: str, check: str, detail: str) -> CheckResult:
    """Build a `fail` result for `resource`."""
    return CheckResult(resource=resource, check=check, status="fail", detail=detail)


def _bucket_log_target(ctx: SmokeContext, source_bucket: str) -> tuple[str, str] | str:
    """Return the source bucket's configured `(target bucket, target prefix)`, or an error string.

    A source with no `LoggingEnabled` at all is an error.
    """
    try:
        response = ctx.client("s3").get_bucket_logging(Bucket=source_bucket)
    except Exception as exc:  # noqa: BLE001 - reported per destination, never aborts the run
        return f"{type(exc).__name__}: {exc}"
    enabled = response.get("LoggingEnabled")
    if not enabled:
        return (
            f"s3://{source_bucket} has no BucketLoggingStatus.LoggingEnabled at all, so it delivers "
            "no access logs anywhere"
        )
    return (str(enabled.get("TargetBucket", "")), str(enabled.get("TargetPrefix", "")))


def _distribution_log_target(ctx: SmokeContext, distribution_id: str) -> tuple[str, str] | str:
    """Return the distribution's configured `(target bucket domain, prefix)`, or an error string."""
    try:
        response = ctx.client("cloudfront").get_distribution_config(Id=distribution_id)
    except Exception as exc:  # noqa: BLE001 - reported per destination, never aborts the run
        return f"{type(exc).__name__}: {exc}"
    config: Any = response.get("DistributionConfig") or {}
    logging_block: Any = config.get("Logging") or {}
    if not logging_block.get("Enabled"):
        return (
            f"CloudFront distribution {distribution_id} has Logging.Enabled false, so it delivers "
            "no standard access logs anywhere"
        )
    return (str(logging_block.get("Bucket", "")), str(logging_block.get("Prefix", "")))


def _target_matches(destination: LogDestination, bucket: str, observed_target: str) -> bool:
    """True when `observed_target` designates `bucket`.

    S3 records the plain bucket name; CloudFront records a bucket *domain name*
    (`<bucket>.s3.amazonaws.com`), so the distribution row matches on the leading label.
    """
    if destination.source_distribution_output is not None:
        return observed_target == bucket or observed_target.startswith(f"{bucket}.")
    return observed_target == bucket


def _check_target(ctx: SmokeContext, destination: LogDestination, bucket: str) -> CheckResult:
    """Assert `bucket` is what this destination's source is configured to log to, under `prefix`."""
    resource = f"{destination.output_name}:{destination.prefix}"

    if destination.source_bucket_output is not None:
        try:
            source = ctx.outputs.require_str(destination.source_bucket_output)
        except Exception as exc:  # noqa: BLE001 - a missing output is a reportable failure
            return _fail(
                resource,
                CHECK_TARGET,
                f"Terraform output {destination.source_bucket_output!r} names this destination's "
                f"log source and is absent, so the destination cannot be asserted to be the right "
                f"bucket: {exc}",
            )
        if source == bucket:
            return _fail(
                resource,
                CHECK_TARGET,
                f"Terraform output {destination.output_name!r} resolves to {bucket!r}, which is "
                f"also this row's log SOURCE ({destination.source_bucket_output!r}). A bucket is "
                "not its own access-log destination, so the output is mis-wired; listing it would "
                "defer forever while reading as merely early.",
            )
        outcome = _bucket_log_target(ctx, source)
    else:
        if destination.source_distribution_output is None:
            raise RuntimeError("source_distribution_output is unset; __post_init__ enforces it")
        try:
            source = ctx.outputs.require_str(destination.source_distribution_output)
        except Exception as exc:  # noqa: BLE001 - a missing output is a reportable failure
            return _fail(
                resource,
                CHECK_TARGET,
                f"Terraform output {destination.source_distribution_output!r} names this "
                f"destination's log source and is absent, so the destination cannot be asserted to "
                f"be the right bucket: {exc}",
            )
        outcome = _distribution_log_target(ctx, source)

    if isinstance(outcome, str):
        return _fail(
            resource,
            CHECK_TARGET,
            f"could not establish where {source!r} delivers its logs ({outcome}). An unassertable "
            "target is a failure: without it an empty prefix cannot be told apart from the wrong "
            "bucket.",
        )

    observed_target, observed_prefix = outcome
    if not _target_matches(destination, bucket, observed_target):
        return _fail(
            resource,
            CHECK_TARGET,
            f"{source!r} delivers its logs to {observed_target!r}, but Terraform output "
            f"{destination.output_name!r} resolves to {bucket!r}. This check would have listed the "
            "wrong bucket and deferred indefinitely on an empty prefix.",
        )
    if observed_prefix != destination.prefix:
        return _fail(
            resource,
            CHECK_TARGET,
            f"{source!r} delivers its logs under prefix {observed_prefix!r}, but this row lists "
            f"{destination.prefix!r}. The bucket is right and the prefix is not, so nothing would "
            "ever appear where this check looks.",
        )
    return CheckResult(
        resource=resource,
        check=CHECK_TARGET,
        status="pass",
        detail=(
            f"{source!r} is configured to deliver to s3://{bucket}/{destination.prefix}, which is "
            f"what Terraform output {destination.output_name!r} resolves to -- so an empty prefix "
            "below means 'not delivered yet' rather than 'wrong bucket'"
        ),
    )


def _delivered_object_count(ctx: SmokeContext, bucket: str, prefix: str) -> int | str:
    """Objects present under `prefix`, or an error string when the listing itself failed."""
    try:
        response = ctx.client("s3").list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    except Exception as exc:  # noqa: BLE001 - reported per destination, never aborts the run
        return f"{type(exc).__name__}: {exc}"
    return int(response.get("KeyCount", 0))


def _check_delivered(ctx: SmokeContext, destination: LogDestination, bucket: str) -> CheckResult:
    """Assert at least one log object has been delivered under this destination's prefix."""
    resource = f"{destination.output_name}:{destination.prefix}"
    outcome = _delivered_object_count(ctx, bucket, destination.prefix)
    if isinstance(outcome, str):
        return _fail(
            resource,
            CHECK_DELIVERED,
            f"could not list s3://{bucket}/{destination.prefix} ({outcome}). An unlistable log "
            "destination is a failure, not an absence of evidence.",
        )
    if outcome > 0:
        return CheckResult(
            resource=resource,
            check=CHECK_DELIVERED,
            status="pass",
            detail=(
                f"{destination.source_description}: at least one object present under "
                f"s3://{bucket}/{destination.prefix} -- delivery observed, not inferred from "
                "configuration"
            ),
        )
    return CheckResult(
        resource=resource,
        check=CHECK_DELIVERED,
        status="defer",
        defer_reason=destination.defer_reason,
        detail=(
            f"{destination.source_description}: no object under "
            f"s3://{bucket}/{destination.prefix}. The source is confirmed to deliver here, and "
            "delivery is asynchronous, so this is unverified rather than proven broken -- re-run "
            "after a real request. It is NOT a pass: the configuration being present is not "
            "evidence of delivery."
        ),
    )


def checks(ctx: SmokeContext) -> Sequence[CheckResult]:
    """Assert each destination is its source's real log target, then that objects arrived there."""
    results: list[CheckResult] = []

    for destination in LOG_DESTINATIONS:
        resource = f"{destination.output_name}:{destination.prefix}"
        try:
            bucket = ctx.outputs.require_str(destination.output_name)
        except Exception as exc:  # noqa: BLE001 - a missing output is a reportable failure
            results.append(
                _fail(
                    resource,
                    CHECK_TARGET,
                    f"Terraform output {destination.output_name!r} is absent, so this destination "
                    f"cannot be located: {exc}",
                )
            )
            continue

        target = _check_target(ctx, destination, bucket)
        results.append(target)
        if target.status != "pass":
            continue
        results.append(_check_delivered(ctx, destination, bucket))

    return results


def expected_check_count(_ctx: SmokeContext) -> ExpectedCheckCount:
    """The fewest checks this class can legitimately emit: one target assertion per destination.

    The delivery check runs only for a destination whose target assertion passed, so a correct run
    emits between `len(LOG_DESTINATIONS)` and twice that.
    """
    return ExpectedCheckCount(
        minimum=len(LOG_DESTINATIONS),
        basis=(
            f"one target assertion per destination in LOG_DESTINATIONS ({len(LOG_DESTINATIONS)}); the "
            "delivery check is additional and conditional on the target assertion passing"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the access-log delivery checks and return the process exit code."""
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

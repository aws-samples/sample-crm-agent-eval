# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.access_log_delivery`: the Layer 4 log-delivery smoke check.

Exercises `checks()` against fake `s3`/`cloudfront` clients and injected outputs, and asserts the
Terraform outputs the check reads are wired at the root.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from scripts.smoke.access_log_delivery import (
    CHECK_DELIVERED,
    CHECK_TARGET,
    DEFER_CLOUDFRONT_MECHANISM,
    DEFER_NO_OBJECTS,
    LOG_DESTINATIONS,
    LogDestination,
    checks,
)
from scripts.smoke.harness import CheckResult, context_for_test

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ROOT_OUTPUTS_TF = REPO_ROOT / "infra" / "outputs.tf"
OBSERVABILITY_OUTPUTS_TF = REPO_ROOT / "infra" / "modules" / "observability" / "outputs.tf"

_LOGS_BUCKET = "crm-frontend-cdn-logs-123456789012"
_CLOUDTRAIL_ACCESS_LOGS_BUCKET = "crm-cloudtrail-access-logs-123456789012"
_CLOUDTRAIL_DELIVERY_BUCKET = "crm-cloudtrail-123456789012"
_ORIGIN_BUCKET = "crm-frontend-origin-123456789012"
_EVAL_ARTIFACTS_BUCKET = "crm-eval-artifacts-123456789012"
_DISTRIBUTION_ID = "E1EXAMPLE1EXAM"

_OUTPUTS = {
    "frontend_cdn_logs_bucket_name": _LOGS_BUCKET,
    "cloudtrail_access_logs_bucket_name": _CLOUDTRAIL_ACCESS_LOGS_BUCKET,
    "cloudtrail_s3_bucket_name": _CLOUDTRAIL_DELIVERY_BUCKET,
    "frontend_origin_bucket_name": _ORIGIN_BUCKET,
    "eval_artifacts_bucket_name": _EVAL_ARTIFACTS_BUCKET,
    "cloudfront_distribution_id": _DISTRIBUTION_ID,
}

#: What each log source is really configured to deliver to, matching `infra/`. Keyed by source bucket
#: name; the value is the `(TargetBucket, TargetPrefix)` `get_bucket_logging` would return.
_SOURCE_TARGETS: dict[str, tuple[str, str] | None] = {
    _ORIGIN_BUCKET: (_LOGS_BUCKET, "s3-access-logs/origin/"),
    _EVAL_ARTIFACTS_BUCKET: (_LOGS_BUCKET, "s3-access-logs/eval-artifacts/"),
    _CLOUDTRAIL_DELIVERY_BUCKET: (
        _CLOUDTRAIL_ACCESS_LOGS_BUCKET,
        "s3-access-logs/cloudtrail/",
    ),
}

#: What the distribution is really configured to deliver to. CloudFront records a bucket *domain*.
_DISTRIBUTION_LOGGING: dict[str, Any] = {
    "Enabled": True,
    "Bucket": f"{_LOGS_BUCKET}.s3.amazonaws.com",
    "Prefix": "cloudfront-access-logs/",
}


class _FakeS3:
    def __init__(
        self,
        counts: dict[tuple[str, str], int | Exception],
        targets: Mapping[str, tuple[str, str] | None | Exception] | None = None,
    ) -> None:
        self._counts = counts
        self._targets = dict(_SOURCE_TARGETS) if targets is None else targets
        self.calls: list[tuple[str, str]] = []
        self.logging_calls: list[str] = []

    def get_bucket_logging(self, **kwargs: Any) -> dict[str, Any]:
        bucket = kwargs["Bucket"]
        self.logging_calls.append(bucket)
        target = self._targets.get(bucket)
        if isinstance(target, Exception):
            raise target
        if target is None:
            return {}
        return {"LoggingEnabled": {"TargetBucket": target[0], "TargetPrefix": target[1]}}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["Bucket"], kwargs["Prefix"])
        self.calls.append(key)
        outcome = self._counts.get(key, 0)
        if isinstance(outcome, Exception):
            raise outcome
        return {"KeyCount": outcome}


class _FakeCloudFront:
    def __init__(self, logging: dict[str, Any] | Exception | None = None) -> None:
        self._logging = _DISTRIBUTION_LOGGING if logging is None else logging
        self.calls: list[str] = []

    def get_distribution_config(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs["Id"])
        if isinstance(self._logging, Exception):
            raise self._logging
        return {"DistributionConfig": {"Logging": self._logging}}


def _run(
    counts: dict[tuple[str, str], int | Exception],
    outputs: dict[str, str] | None = None,
    targets: Mapping[str, tuple[str, str] | None | Exception] | None = None,
    distribution_logging: dict[str, Any] | Exception | None = None,
) -> tuple[list[CheckResult], _FakeS3, _FakeCloudFront]:
    s3 = _FakeS3(counts, targets)
    cloudfront = _FakeCloudFront(distribution_logging)
    ctx = context_for_test(
        outputs=_OUTPUTS if outputs is None else outputs,
        clients={"s3": s3, "cloudfront": cloudfront},
    )
    return list(checks(ctx)), s3, cloudfront


def _all_delivered() -> dict[tuple[str, str], int | Exception]:
    return {(_OUTPUTS[d.output_name], d.prefix): 1 for d in LOG_DESTINATIONS}


def _by_prefix(results: list[CheckResult], check: str) -> dict[str, CheckResult]:
    return {r.resource.split(":", 1)[1]: r for r in results if r.check == check}


class TestDestinationRows:
    def test_every_configured_prefix_is_covered_by_a_destination_row(self) -> None:
        """Every configured log prefix has a destination row."""
        prefixes = {d.prefix for d in LOG_DESTINATIONS}
        assert prefixes == {
            "s3-access-logs/origin/",
            "s3-access-logs/eval-artifacts/",
            "s3-access-logs/cloudtrail/",
            "cloudfront-access-logs/",
        }

    def test_every_row_names_exactly_one_log_source(self) -> None:
        """Every destination row names exactly one log source."""
        for destination in LOG_DESTINATIONS:
            named = [destination.source_bucket_output, destination.source_distribution_output]
            assert sum(1 for value in named if value) == 1, destination

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"source_bucket_output": "a", "source_distribution_output": "b"},
        ],
        ids=["no_source", "two_sources"],
    )
    def test_a_row_without_exactly_one_source_is_refused_at_construction(
        self, kwargs: dict[str, str]
    ) -> None:
        with pytest.raises(ValueError, match="exactly one source"):
            LogDestination(output_name="o", prefix="p/", source_description="d", **kwargs)


class TestAccessLogDelivery:
    def test_objects_present_everywhere_passes_both_checks_for_every_destination(self) -> None:
        results, s3, cloudfront = _run(_all_delivered())
        assert [r.status for r in results] == ["pass"] * (2 * len(LOG_DESTINATIONS))
        assert len(_by_prefix(results, CHECK_TARGET)) == len(LOG_DESTINATIONS)
        assert len(s3.calls) == len(LOG_DESTINATIONS)
        assert cloudfront.calls == [_DISTRIBUTION_ID]
        assert all("delivery observed" in r.detail for r in results if r.check == CHECK_DELIVERED)

    def test_an_empty_prefix_defers_and_never_passes(self) -> None:
        """An empty prefix is `defer` with a reason and never `pass`."""
        results, _, _ = _run({})
        delivered = _by_prefix(results, CHECK_DELIVERED)
        assert len(delivered) == len(LOG_DESTINATIONS)
        assert all(r.status == "defer" for r in delivered.values())
        assert all(r.defer_reason for r in delivered.values())
        assert all(r.status == "pass" for r in _by_prefix(results, CHECK_TARGET).values())

    def test_the_cloudfront_prefix_defers_with_its_own_reason(self) -> None:
        """An empty CloudFront prefix defers with `DEFER_CLOUDFRONT_MECHANISM`."""
        delivered = _by_prefix(_run({})[0], CHECK_DELIVERED)
        assert delivered["cloudfront-access-logs/"].defer_reason == DEFER_CLOUDFRONT_MECHANISM
        assert delivered["s3-access-logs/origin/"].defer_reason == DEFER_NO_OBJECTS

    def test_a_listing_error_is_a_failure_not_an_absence_of_evidence(self) -> None:
        counts = _all_delivered()
        counts[(_CLOUDTRAIL_ACCESS_LOGS_BUCKET, "s3-access-logs/cloudtrail/")] = RuntimeError(
            "AccessDenied"
        )
        results, _, _ = _run(counts)
        failed = [r for r in results if r.status == "fail"]
        assert len(failed) == 1, results
        assert failed[0].check == CHECK_DELIVERED
        assert "AccessDenied" in failed[0].detail
        delivered_passes = [r for r in results if r.check == CHECK_DELIVERED and r.status == "pass"]
        assert len(delivered_passes) == len(LOG_DESTINATIONS) - 1

    def test_a_missing_destination_output_fails_rather_than_skipping_the_destination(self) -> None:
        outputs = {k: v for k, v in _OUTPUTS.items() if k != "cloudtrail_access_logs_bucket_name"}
        results, s3, _ = _run(_all_delivered(), outputs=outputs)
        failed = [r for r in results if r.status == "fail"]
        assert len(failed) == 1, results
        assert "cloudtrail_access_logs_bucket_name" in failed[0].detail
        assert (_CLOUDTRAIL_ACCESS_LOGS_BUCKET, "s3-access-logs/cloudtrail/") not in s3.calls

    def test_a_missing_source_output_fails_and_lists_nothing_for_that_destination(self) -> None:
        """A missing source output fails the target check and lists nothing for that destination."""
        outputs = {k: v for k, v in _OUTPUTS.items() if k != "cloudtrail_s3_bucket_name"}
        results, s3, _ = _run(_all_delivered(), outputs=outputs)
        failed = [r for r in results if r.status == "fail"]
        assert len(failed) == 1, results
        assert failed[0].check == CHECK_TARGET
        assert "cloudtrail_s3_bucket_name" in failed[0].detail
        assert (_CLOUDTRAIL_ACCESS_LOGS_BUCKET, "s3-access-logs/cloudtrail/") not in s3.calls

    @pytest.mark.parametrize("destination", LOG_DESTINATIONS, ids=lambda d: d.prefix)
    def test_each_destination_independently_detects_its_own_emptiness(
        self, destination: LogDestination
    ) -> None:
        """One empty prefix defers exactly that destination while the others pass."""
        counts = _all_delivered()
        counts[(_OUTPUTS[destination.output_name], destination.prefix)] = 0
        results, _, _ = _run(counts)
        deferred = [r for r in results if r.status == "defer"]
        assert len(deferred) == 1, results
        assert deferred[0].resource.endswith(destination.prefix)


class TestTheCheckAssertsItsOwnTarget:
    """The mis-wiring class: a wrong destination must be a loud `fail`, never a patient `defer`."""

    def test_the_cloudtrail_name_trap_fails_rather_than_deferring_forever(self) -> None:
        """Wiring the trail delivery bucket (the row's source) as its destination is a `fail`."""
        outputs = dict(_OUTPUTS)
        outputs["cloudtrail_access_logs_bucket_name"] = _CLOUDTRAIL_DELIVERY_BUCKET
        results, s3, _ = _run(_all_delivered(), outputs=outputs)

        trap = _by_prefix(results, CHECK_TARGET)["s3-access-logs/cloudtrail/"]
        assert trap.status == "fail", results
        assert "log SOURCE" in trap.detail
        assert not any(r.status == "defer" for r in results), results
        assert (_CLOUDTRAIL_DELIVERY_BUCKET, "s3-access-logs/cloudtrail/") not in s3.calls
        assert sum(1 for r in results if r.status == "pass") == 2 * (len(LOG_DESTINATIONS) - 1)

    def test_a_destination_the_source_does_not_log_to_fails_and_names_both_buckets(self) -> None:
        targets = dict(_SOURCE_TARGETS)
        targets[_ORIGIN_BUCKET] = ("some-other-bucket", "s3-access-logs/origin/")
        results, s3, _ = _run(_all_delivered(), targets=targets)
        failed = [r for r in results if r.status == "fail"]
        assert len(failed) == 1, results
        assert failed[0].check == CHECK_TARGET
        assert "some-other-bucket" in failed[0].detail
        assert _LOGS_BUCKET in failed[0].detail
        assert (_LOGS_BUCKET, "s3-access-logs/origin/") not in s3.calls

    def test_a_prefix_the_source_does_not_write_to_fails(self) -> None:
        """A destination prefix the source does not write to is a `fail`."""
        targets = dict(_SOURCE_TARGETS)
        targets[_EVAL_ARTIFACTS_BUCKET] = (_LOGS_BUCKET, "s3-access-logs/somewhere-else/")
        results, _, _ = _run(_all_delivered(), targets=targets)
        failed = [r for r in results if r.status == "fail"]
        assert len(failed) == 1, results
        assert "s3-access-logs/somewhere-else/" in failed[0].detail

    def test_a_source_that_logs_nowhere_fails_rather_than_deferring(self) -> None:
        targets: dict[str, tuple[str, str] | None | Exception] = dict(_SOURCE_TARGETS)
        targets[_ORIGIN_BUCKET] = None
        results, _, _ = _run(_all_delivered(), targets=targets)
        failed = [r for r in results if r.status == "fail"]
        assert len(failed) == 1, results
        assert "LoggingEnabled" in failed[0].detail

    def test_an_unreadable_source_logging_status_fails(self) -> None:
        targets: dict[str, tuple[str, str] | None | Exception] = dict(_SOURCE_TARGETS)
        targets[_ORIGIN_BUCKET] = RuntimeError("AccessDenied")
        results, _, _ = _run(_all_delivered(), targets=targets)
        failed = [r for r in results if r.status == "fail"]
        assert len(failed) == 1, results
        assert "AccessDenied" in failed[0].detail
        assert "unassertable target" in failed[0].detail

    def test_the_cloudfront_row_accepts_a_bucket_domain_and_rejects_a_different_bucket(
        self,
    ) -> None:
        """The CloudFront row accepts `<bucket>.s3.amazonaws.com` and rejects a bucket that only
        shares the prefix."""
        results, _, _ = _run(_all_delivered())
        assert _by_prefix(results, CHECK_TARGET)["cloudfront-access-logs/"].status == "pass"

        results, _, _ = _run(
            _all_delivered(),
            distribution_logging={
                **_DISTRIBUTION_LOGGING,
                "Bucket": f"{_LOGS_BUCKET}-other.s3.amazonaws.com",
            },
        )
        failed = [r for r in results if r.status == "fail"]
        assert len(failed) == 1, results
        assert f"{_LOGS_BUCKET}-other.s3.amazonaws.com" in failed[0].detail

    def test_a_distribution_with_logging_disabled_fails(self) -> None:
        results, _, _ = _run(_all_delivered(), distribution_logging={"Enabled": False})
        failed = [r for r in results if r.status == "fail"]
        assert len(failed) == 1, results
        assert "Logging.Enabled false" in failed[0].detail

    def test_an_unreadable_distribution_config_fails(self) -> None:
        results, _, _ = _run(_all_delivered(), distribution_logging=RuntimeError("AccessDenied"))
        failed = [r for r in results if r.status == "fail"]
        assert len(failed) == 1, results
        assert "AccessDenied" in failed[0].detail

    def test_no_destination_ever_reports_delivery_when_its_target_check_failed(self) -> None:
        """A destination whose target check failed reports no delivery row at all."""
        targets = dict(_SOURCE_TARGETS)
        targets[_ORIGIN_BUCKET] = ("some-other-bucket", "s3-access-logs/origin/")
        results, _, _ = _run({}, targets=targets)
        by_check = {(r.resource, r.check) for r in results}
        origin = "frontend_cdn_logs_bucket_name:s3-access-logs/origin/"
        assert (origin, CHECK_TARGET) in by_check
        assert (origin, CHECK_DELIVERED) not in by_check


def _declared_outputs(path: Path) -> dict[str, str]:
    """Map every `output "name" { ... value = <expr> }` in `path` to its `value` expression."""
    text = path.read_text(encoding="utf-8")
    declared: dict[str, str] = {}
    for match in re.finditer(r'^output\s+"([^"]+)"\s*\{(.*?)^\}', text, re.DOTALL | re.MULTILINE):
        name, body = match.group(1), match.group(2)
        value = re.search(r"^\s*value\s*=\s*(.+)$", body, re.MULTILINE)
        assert value is not None, f"output {name!r} in {path} declares no value"
        declared[name] = value.group(1).strip()
    return declared


class TestTheCompositionPassesTheseOutputs:
    """The root and observability outputs the check reads are declared and wired correctly."""

    def test_every_output_the_check_requires_is_declared_at_the_root(self) -> None:
        required = set()
        for destination in LOG_DESTINATIONS:
            required.add(destination.output_name)
            required.update(
                name
                for name in (
                    destination.source_bucket_output,
                    destination.source_distribution_output,
                )
                if name
            )
        declared = _declared_outputs(ROOT_OUTPUTS_TF)
        missing = sorted(required - set(declared))
        assert missing == [], (
            f"{ROOT_OUTPUTS_TF} declares no output for {missing}; the check reports each as a "
            "`fail`, so this is a wiring gap and not a coverage gap"
        )

    def test_the_cloudtrail_destination_is_wired_to_the_access_logs_bucket_not_the_trail_bucket(
        self,
    ) -> None:
        """The root CloudTrail access-logs output names the access-logs bucket, not the trail."""
        declared = _declared_outputs(ROOT_OUTPUTS_TF)
        assert (
            declared["cloudtrail_access_logs_bucket_name"]
            == "module.observability.cloudtrail_access_logs_bucket_name"
        )
        assert (
            declared["cloudtrail_s3_bucket_name"]
            == "module.observability.cloudtrail_s3_bucket_name"
        )
        assert declared["frontend_cdn_logs_bucket_name"] == "module.frontend_cdn.logs_bucket_name"

    def test_the_observability_module_exports_the_access_logs_bucket_distinctly(self) -> None:
        declared = _declared_outputs(OBSERVABILITY_OUTPUTS_TF)
        assert declared["cloudtrail_access_logs_bucket_name"] == (
            "aws_s3_bucket.cloudtrail_access_logs.id"
        )
        assert declared["cloudtrail_s3_bucket_name"] == "aws_s3_bucket.cloudtrail.id"

# SPDX-License-Identifier: MIT
"""Lifecycle coverage of every versioned S3 bucket, read from the committed plan fixture.

Each lifecycle configuration has a whole-bucket rule that expires noncurrent versions and aborts
incomplete uploads, and the frontend origin's never expires a current object. Read from
`plan_fixtures.build("compliant")` because `frontend_cdn` and `observability` cannot be planned
offline; `plan_freshness` reports whether that fixture matches the current `infra/`.
"""

from __future__ import annotations

from typing import Any

import pytest
from scripts import plan_fixtures

ORIGIN_LIFECYCLE = "module.frontend_cdn.aws_s3_bucket_lifecycle_configuration.origin"

#: Every lifecycle configuration the stack declares.
EXPECTED_LIFECYCLES = {
    "module.eval_artifacts.aws_s3_bucket_lifecycle_configuration.this",
    "module.frontend_cdn.aws_s3_bucket_lifecycle_configuration.logs",
    ORIGIN_LIFECYCLE,
    "module.observability.aws_s3_bucket_lifecycle_configuration.cloudtrail",
    "module.observability.aws_s3_bucket_lifecycle_configuration.cloudtrail_access_logs",
}


def _lifecycles() -> dict[str, list[dict[str, Any]]]:
    document = plan_fixtures.build("compliant")
    return {
        change["address"]: change["change"]["after"]["rule"]
        for change in document["resource_changes"]
        if change["type"] == "aws_s3_bucket_lifecycle_configuration"
    }


def _is_whole_bucket(rule: dict[str, Any]) -> bool:
    filters = rule.get("filter") or []
    return all(
        not f.get("prefix") and not f.get("tag") and not f.get("and") for f in filters
    ) and not rule.get("prefix")


def test_the_fixture_holds_exactly_the_expected_lifecycles() -> None:
    assert set(_lifecycles()) == EXPECTED_LIFECYCLES


@pytest.mark.parametrize("address", sorted(EXPECTED_LIFECYCLES))
def test_a_whole_bucket_rule_expires_noncurrent_versions_and_aborts_uploads(address: str) -> None:
    """Some enabled whole-bucket rule carries both `noncurrent_version_expiration` and an abort."""
    covering = [
        rule
        for rule in _lifecycles()[address]
        if rule["status"] == "Enabled"
        and _is_whole_bucket(rule)
        and rule["noncurrent_version_expiration"]
        and rule["noncurrent_version_expiration"][0]["noncurrent_days"] > 0
        and rule["abort_incomplete_multipart_upload"]
        and rule["abort_incomplete_multipart_upload"][0]["days_after_initiation"] > 0
    ]
    assert covering, _lifecycles()[address]


def test_the_origin_never_expires_the_current_build() -> None:
    """No origin rule carries a current-version `expiration`."""
    rules = _lifecycles()[ORIGIN_LIFECYCLE]
    assert rules
    assert all(not rule["expiration"] for rule in rules), rules

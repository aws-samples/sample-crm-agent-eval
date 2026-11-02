# SPDX-License-Identifier: MIT
"""Source assertions on S3 lifecycle and ownership hardenings that cannot be planned offline.

`frontend_cdn` and `observability` read live AWS data, so the offline plan harness cannot reach them
and `test_infra_s3_lifecycle.py` reads a captured plan. These cells read the committed HCL instead,
so reverting a hardening in `infra/` fails here whether or not the fixture is current. Comments and
heredocs are blanked first (`scripts.hcl_text`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts.hcl_text import blank_comments, blank_literals

INFRA_MODULES = Path(__file__).resolve().parents[2] / "infra" / "modules"
FRONTEND_S3_TF = INFRA_MODULES / "frontend_cdn" / "s3.tf"
CLOUDTRAIL_TF = INFRA_MODULES / "observability" / "cloudtrail.tf"


def _uncommented(path: Path) -> str:
    return blank_comments(path.read_text(encoding="utf-8"))


def _body(text: str, open_brace: int) -> str:
    """The text inside the brace at `open_brace` and its match, matched on blanked literals."""
    blanked = blank_literals(text)
    depth = 0
    for index in range(open_brace, len(blanked)):
        if blanked[index] == "{":
            depth += 1
        elif blanked[index] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : index]
    raise AssertionError(f"unbalanced braces from offset {open_brace}")


def _resource(path: Path, resource_type: str, name: str) -> str:
    text = _uncommented(path)
    match = re.search(rf'(?m)^resource\s+"{resource_type}"\s+"{name}"\s*\{{', text)
    assert match is not None, f'resource "{resource_type}" "{name}" not found in {path.name}'
    return _body(text, match.end() - 1)


def _blocks(body: str, header: str) -> list[str]:
    """Every body of a block introduced on its own line by `header {`."""
    return [
        _body(body, match.end() - 1)
        for match in re.finditer(rf"(?m)^\s*{re.escape(header)}\s*\{{", body)
    ]


def _whole_bucket_rules(lifecycle: str) -> list[str]:
    """The `rule` bodies whose `filter {}` is empty."""
    return [
        rule
        for rule in _blocks(lifecycle, "rule")
        if any(not filter_body.strip() for filter_body in _blocks(rule, "filter"))
    ]


def _days(block: str, argument: str) -> int:
    match = re.search(rf"(?m)^\s*{argument}\s*=\s*(\S+)\s*$", block)
    assert match is not None, f"no `{argument}` in {block!r}"
    value = match.group(1)
    return int(value) if value.isdigit() else 1


def _covers_noncurrent_and_abort(rule: str) -> bool:
    noncurrent = _blocks(rule, "noncurrent_version_expiration")
    abort = _blocks(rule, "abort_incomplete_multipart_upload")
    return (
        len(noncurrent) == 1
        and _days(noncurrent[0], "noncurrent_days") > 0
        and len(abort) == 1
        and _days(abort[0], "days_after_initiation") > 0
    )


def test_the_origin_lifecycle_never_expires_the_current_build() -> None:
    """No rule on the origin bucket carries a current-version `expiration` block."""
    lifecycle = _resource(FRONTEND_S3_TF, "aws_s3_bucket_lifecycle_configuration", "origin")
    rules = _blocks(lifecycle, "rule")
    assert rules, "the origin lifecycle declares no rule"
    for rule in rules:
        assert _blocks(rule, "expiration") == [], rule


def test_the_origin_lifecycle_expires_superseded_builds_and_aborts_uploads() -> None:
    lifecycle = _resource(FRONTEND_S3_TF, "aws_s3_bucket_lifecycle_configuration", "origin")
    assert any(_covers_noncurrent_and_abort(rule) for rule in _whole_bucket_rules(lifecycle))


@pytest.mark.parametrize("name", ["cloudtrail", "cloudtrail_access_logs"])
def test_cloudtrail_buckets_expire_noncurrent_versions_and_abort_uploads(name: str) -> None:
    """A whole-bucket rule carries both `noncurrent_version_expiration` and an abort."""
    lifecycle = _resource(CLOUDTRAIL_TF, "aws_s3_bucket_lifecycle_configuration", name)
    rules = _whole_bucket_rules(lifecycle)
    assert rules, f"{name}: no whole-bucket rule"
    assert any(_covers_noncurrent_and_abort(rule) for rule in rules), rules


def test_the_cloudtrail_access_log_bucket_disables_acls() -> None:
    """`cloudtrail_access_logs` is `BucketOwnerEnforced`; delivery is by bucket policy."""
    ownership = _resource(
        CLOUDTRAIL_TF, "aws_s3_bucket_ownership_controls", "cloudtrail_access_logs"
    )
    values = re.findall(r'(?m)^\s*object_ownership\s*=\s*"([^"]+)"', ownership)
    assert values == ["BucketOwnerEnforced"], values

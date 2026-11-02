# SPDX-License-Identifier: MIT
"""Source assertions on the CloudTrail trail: multi-region, global service events, and delivery
policies that admit every region.

`observability` reads live AWS data, so the offline plan harness cannot plan it; these cells read the
committed HCL with comments and heredocs blanked (`scripts.hcl_text`). Each predicate also runs
against a mutated scratch copy, so a cell that stops discriminating fails here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from scripts import plan_fixtures
from scripts.hcl_text import blank_comments, blank_literals

CLOUDTRAIL_TF = (
    Path(__file__).resolve().parents[2] / "infra" / "modules" / "observability" / "cloudtrail.tf"
)

#: The region-wildcarded trail ARN pattern both CMK statements condition on.
_ANY_REGION_TRAIL_ARN = (
    '"arn:aws:cloudtrail:*:${data.aws_caller_identity.current.account_id}:trail/*"'
)


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


def _block(text: str, kind: str, resource_type: str, name: str) -> str:
    match = re.search(rf'(?m)^{kind}\s+"{resource_type}"\s+"{name}"\s*\{{', text)
    assert match is not None, f'{kind} "{resource_type}" "{name}" not found'
    return _body(text, match.end() - 1)


def _top_level_values(body: str, argument: str) -> list[str]:
    """Every value assigned to `argument` at the top level of `body` (nested blocks dropped)."""
    blanked = blank_literals(body)
    top: list[str] = []
    depth = 0
    for char, original in zip(blanked, body, strict=True):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif depth == 0:
            top.append(original)
    return re.findall(rf"(?m)^\s*{argument}\s*=\s*(\S+)\s*$", "".join(top))


def trail_flags(text: str) -> dict[str, list[str]]:
    """The trail's `is_multi_region_trail` and `include_global_service_events` values."""
    trail = _block(blank_comments(text), "resource", "aws_cloudtrail", "this")
    return {
        argument: _top_level_values(trail, argument)
        for argument in ("is_multi_region_trail", "include_global_service_events")
    }


def is_multi_region(text: str) -> bool:
    flags = trail_flags(text)
    return flags["is_multi_region_trail"] == ["true"] and flags[
        "include_global_service_events"
    ] == ["true"]


def skips_multi_region_check(text: str) -> bool:
    """Whether any `checkov:skip` for CKV_AWS_67 remains in the file."""
    return re.search(r"checkov:skip\s*=\s*CKV_AWS_67\b", text) is not None


def _statement(document: str, sid: str) -> str:
    for match in re.finditer(r"(?m)^\s*statement\s*\{", document):
        body = _body(document, match.end() - 1)
        if re.search(rf'(?m)^\s*sid\s*=\s*"{re.escape(sid)}"\s*$', body):
            return body
    raise AssertionError(f"no statement with sid {sid!r}")


def cmk_admits_every_region(text: str) -> bool:
    """Both CloudTrail CMK statements condition on a region-wildcarded trail ARN."""
    policy = _block(blank_comments(text), "data", "aws_iam_policy_document", "cloudtrail_cmk")
    for sid in ("AllowCloudTrailToEncryptLogs", "AllowPrincipalsInAccountToDecryptLogFiles"):
        body = _statement(policy, sid)
        if f"values   = [{_ANY_REGION_TRAIL_ARN}]" not in body:
            return False
    return True


def bucket_write_admits_every_region(text: str) -> bool:
    """`AWSCloudTrailWrite` covers the whole account prefix and pins the one trail ARN."""
    uncommented = blank_comments(text)
    policy = _block(uncommented, "data", "aws_iam_policy_document", "cloudtrail_bucket")
    body = _statement(policy, "AWSCloudTrailWrite")
    resource = (
        'resources = ["${aws_s3_bucket.cloudtrail.arn}/AWSLogs/'
        '${data.aws_caller_identity.current.account_id}/*"]'
    )
    return resource in body and "values   = [local.cloudtrail_arn]" in body


def _mutated(tmp_path: Path, old: str, new: str) -> str:
    text = CLOUDTRAIL_TF.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"mutation anchor {old!r} is not unique"
    scratch = tmp_path / "cloudtrail.tf"
    scratch.write_text(text.replace(old, new), encoding="utf-8")
    return scratch.read_text(encoding="utf-8")


def test_the_trail_is_multi_region_with_global_service_events() -> None:
    assert is_multi_region(CLOUDTRAIL_TF.read_text(encoding="utf-8")), trail_flags(
        CLOUDTRAIL_TF.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("is_multi_region_trail         = true", "is_multi_region_trail         = false"),
        ("include_global_service_events = true", "include_global_service_events = false"),
        ("  is_multi_region_trail         = true\n", ""),
        (
            "  is_multi_region_trail         = true\n",
            "  # is_multi_region_trail         = true\n",
        ),
    ],
)
def test_a_single_region_or_global_events_off_mutation_is_detected(
    tmp_path: Path, old: str, new: str
) -> None:
    assert not is_multi_region(_mutated(tmp_path, old, new))


def test_no_multi_region_checkov_skip_remains() -> None:
    assert not skips_multi_region_check(CLOUDTRAIL_TF.read_text(encoding="utf-8"))


def test_a_reintroduced_multi_region_skip_is_detected(tmp_path: Path) -> None:
    text = _mutated(
        tmp_path,
        'resource "aws_cloudtrail" "this" {\n',
        'resource "aws_cloudtrail" "this" {\n  # checkov:skip=CKV_AWS_67: x\n',
    )
    assert skips_multi_region_check(text)


def test_no_single_region_wording_remains() -> None:
    assert re.search(r"(?i)single[- ]region", CLOUDTRAIL_TF.read_text(encoding="utf-8")) is None


def test_the_cmk_admits_cloudtrail_from_every_region() -> None:
    assert cmk_admits_every_region(CLOUDTRAIL_TF.read_text(encoding="utf-8"))


def test_a_region_pinned_cmk_condition_is_detected(tmp_path: Path) -> None:
    text = CLOUDTRAIL_TF.read_text(encoding="utf-8")
    pinned = text.replace(
        _ANY_REGION_TRAIL_ARN,
        '"arn:aws:cloudtrail:us-east-1:${data.aws_caller_identity.current.account_id}:trail/*"',
        1,
    )
    assert pinned != text
    scratch = tmp_path / "cloudtrail.tf"
    scratch.write_text(pinned, encoding="utf-8")
    assert not cmk_admits_every_region(scratch.read_text(encoding="utf-8"))


def test_the_bucket_policy_admits_delivery_from_every_region() -> None:
    assert bucket_write_admits_every_region(CLOUDTRAIL_TF.read_text(encoding="utf-8"))


def test_a_region_scoped_bucket_write_resource_is_detected(tmp_path: Path) -> None:
    text = _mutated(
        tmp_path,
        "/AWSLogs/${data.aws_caller_identity.current.account_id}/*",
        "/AWSLogs/${data.aws_caller_identity.current.account_id}/CloudTrail/us-east-1/*",
    )
    assert not bucket_write_admits_every_region(text)


def plan_trails_multi_region(document: dict[str, Any]) -> list[str]:
    """Why the plan's trails are not all multi-region with global events; empty when they are."""
    trails = [c for c in document.get("resource_changes", []) if c.get("type") == "aws_cloudtrail"]
    if len(trails) != 1:
        return [f"expected one aws_cloudtrail in the plan, found {len(trails)}"]
    after = trails[0].get("change", {}).get("after") or {}
    return [
        f"{trails[0].get('address')}: {name} = {after.get(name)!r}"
        for name in ("is_multi_region_trail", "include_global_service_events")
        if after.get(name) is not True
    ]


def test_the_root_plan_fixture_trail_is_multi_region() -> None:
    document = plan_fixtures.build("compliant")
    assert plan_trails_multi_region(document) == []


@pytest.mark.parametrize("attribute", ["is_multi_region_trail", "include_global_service_events"])
def test_a_single_region_plan_fixture_is_detected(attribute: str) -> None:
    document = plan_fixtures.build("compliant")
    for change in document["resource_changes"]:
        if change.get("type") == "aws_cloudtrail":
            change["change"]["after"][attribute] = False
    assert plan_trails_multi_region(document) != []

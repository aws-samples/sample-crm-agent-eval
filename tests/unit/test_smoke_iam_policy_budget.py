# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.iam_policy_budget`: the IAM inline and managed policy budgets.

Uses a hand-written fake `iam` client; each cell plants a role at a chosen usage and asserts the
resulting status and detail marker.
"""

from __future__ import annotations

import json
import math
from typing import Any, Final

from scripts.smoke.harness import context_for_test
from scripts.smoke.iam_policy_budget import (
    ADVISORY_USAGE_FRACTION,
    AWS_MANAGED_POLICY_ARN_PREFIX,
    BUDGET_ADVISORY_DETAIL_PREFIX,
    FAIL_USAGE_FRACTION,
    INLINE_POLICY_AGGREGATE_LIMIT_CHARS,
    MANAGED_POLICIES_PER_ROLE_QUOTA,
    MANAGED_POLICY_DOCUMENT_LIMIT_CHARS,
    checks,
    expected_check_count,
)
from scripts.smoke.iam_roles import _NAME_PREFIX, _function_grants, _load_matrix, _role_name

from eval.verification import CheckResult

#: Every role the matrix declares, so the coverage floor is satisfied by default.
_DECLARED_ROLES: Final[tuple[str, ...]] = tuple(
    sorted(_role_name(function) for function in _function_grants(_load_matrix()))
)


def _document_of_size(chars: int) -> dict[str, Any]:
    """A syntactically real policy document whose COMPACT serialisation is exactly `chars` long."""
    scaffold: dict[str, Any] = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Deny", "Action": "dynamodb:Scan", "Resource": "", "Sid": "Pad"}],
    }
    base = len(json.dumps(scaffold, separators=(",", ":"), sort_keys=True))
    assert chars >= base, f"cannot build a document smaller than its own scaffold ({base})"
    scaffold["Statement"][0]["Resource"] = "x" * (chars - base)
    built = len(json.dumps(scaffold, separators=(",", ":"), sort_keys=True))
    assert built == chars, f"scaffold arithmetic is wrong: wanted {chars}, built {built}"
    return scaffold


class _FakeIam:
    """`list_roles` / `list_role_policies` / `get_role_policy` / `list_attached_role_policies` /
    `get_policy` / `get_policy_version`.

    Paginates `list_roles` and `list_attached_role_policies` one entry per page by default.
    """

    def __init__(
        self,
        *,
        inline: dict[str, dict[str, int]] | None = None,
        managed: dict[str, dict[str, int]] | None = None,
        extra_roles: tuple[str, ...] = (),
        omit_roles: tuple[str, ...] = (),
        page_size: int = 1,
    ) -> None:
        self.roles = tuple(r for r in (*_DECLARED_ROLES, *extra_roles) if r not in omit_roles) + (
            "someone-elses-role",
        )
        #: `{role: {policy_name: compact_char_count}}`. A role absent here has no inline policy.
        self.inline = inline or {}
        #: `{role: {policy_arn: compact_char_count}}`. A role absent here has no attachment.
        self.managed = managed or {}
        self.documents_read: list[str] = []
        self.page_size = page_size

    def list_roles(self, **kwargs: Any) -> dict[str, Any]:
        start = int(kwargs.get("Marker", 0))
        chunk = self.roles[start : start + self.page_size]
        end = start + len(chunk)
        page: dict[str, Any] = {"Roles": [{"RoleName": name} for name in chunk]}
        if end < len(self.roles):
            page["IsTruncated"] = True
            page["Marker"] = str(end)
        return page

    def list_role_policies(self, *, RoleName: str, **kwargs: Any) -> dict[str, Any]:  # noqa: N803
        return {"PolicyNames": sorted(self.inline.get(RoleName, {}))}

    def get_role_policy(self, *, RoleName: str, PolicyName: str) -> dict[str, Any]:  # noqa: N803
        return {"PolicyDocument": _document_of_size(self.inline[RoleName][PolicyName])}

    def list_attached_role_policies(self, *, RoleName: str, **kwargs: Any) -> dict[str, Any]:  # noqa: N803
        arns = sorted(self.managed.get(RoleName, {}))
        start = int(kwargs.get("Marker", 0))
        chunk = arns[start : start + self.page_size]
        end = start + len(chunk)
        page: dict[str, Any] = {
            "AttachedPolicies": [
                {"PolicyName": arn.rsplit("/", 1)[-1], "PolicyArn": arn} for arn in chunk
            ]
        }
        if end < len(arns):
            page["IsTruncated"] = True
            page["Marker"] = str(end)
        return page

    def _chars(self, arn: str) -> int:
        sizes = [policies[arn] for policies in self.managed.values() if arn in policies]
        assert sizes, f"no planted policy {arn}"
        return sizes[0]

    def get_policy(self, *, PolicyArn: str) -> dict[str, Any]:  # noqa: N803
        self._chars(PolicyArn)
        return {"Policy": {"Arn": PolicyArn, "DefaultVersionId": "v3"}}

    def get_policy_version(self, *, PolicyArn: str, VersionId: str) -> dict[str, Any]:  # noqa: N803
        assert VersionId == "v3", f"read version {VersionId!r}, not the default"
        self.documents_read.append(PolicyArn)
        return {"PolicyVersion": {"Document": _document_of_size(self._chars(PolicyArn))}}


def _run(iam: _FakeIam) -> list[CheckResult]:
    return list(checks(context_for_test(clients={"iam": iam}, outputs={})))


def _row(results: list[CheckResult], role: str, check: str = "inline_policy_budget") -> CheckResult:
    matches = [r for r in results if r.resource == role and r.check == check]
    assert len(matches) == 1, f"expected one budget row for {role!r}, got {len(matches)}"
    return matches[0]


#: A small document size that is still above `_document_of_size`'s scaffold floor.
_SMALL_POLICY_CHARS: Final[int] = 200


def _chars_at(fraction: float) -> int:
    return int(INLINE_POLICY_AGGREGATE_LIMIT_CHARS * fraction)


_SUBJECT: Final[str] = _DECLARED_ROLES[0]


def test_the_limit_is_an_aws_service_quota_not_a_tunable() -> None:
    """The limit is the fixed 10,240-character IAM quota and the advisory fraction sits below the
    fail one.

    https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html
    """
    assert INLINE_POLICY_AGGREGATE_LIMIT_CHARS == 10240
    assert 0 < ADVISORY_USAGE_FRACTION < FAIL_USAGE_FRACTION < 1, (
        "the advisory threshold must sit below the failing one and both inside (0, 1), or a role can "
        f"fail without ever having been advised: advisory={ADVISORY_USAGE_FRACTION}, "
        f"fail={FAIL_USAGE_FRACTION}"
    )


def test_a_role_well_under_the_advisory_threshold_passes_quietly() -> None:
    results = _run(_FakeIam(inline={_SUBJECT: {"p": _chars_at(0.5)}}))
    row = _row(results, _SUBJECT)
    assert row.status == "pass"
    assert BUDGET_ADVISORY_DETAIL_PREFIX not in row.detail
    assert "50.0%" in row.detail, row.detail


def test_a_role_at_the_advisory_threshold_passes_but_is_marked() -> None:
    """A role at the advisory threshold passes with a `BUDGET_ADVISORY_DETAIL_PREFIX` detail."""
    results = _run(_FakeIam(inline={_SUBJECT: {"p": _chars_at(ADVISORY_USAGE_FRACTION)}}))
    row = _row(results, _SUBJECT)
    assert row.status == "pass"
    assert row.detail.startswith(BUDGET_ADVISORY_DETAIL_PREFIX), row.detail


def test_a_role_at_the_failing_threshold_fails() -> None:
    """A role at the failing threshold is a `fail` naming `PutRolePolicy`."""
    results = _run(_FakeIam(inline={_SUBJECT: {"p": _chars_at(FAIL_USAGE_FRACTION)}}))
    row = _row(results, _SUBJECT)
    assert row.status == "fail"
    assert "PutRolePolicy" in row.detail, row.detail


def test_the_aggregate_sums_every_inline_policy_not_just_the_largest() -> None:
    """The measured usage is the sum of every inline policy on the role."""
    each = 4000
    results = _run(_FakeIam(inline={_SUBJECT: {"a": each, "b": each, "c": each}}))
    row = _row(results, _SUBJECT)
    assert row.status == "fail", row.detail
    assert f"{3 * each} of {INLINE_POLICY_AGGREGATE_LIMIT_CHARS}" in row.detail, row.detail


def test_whitespace_is_not_counted_because_iam_does_not_count_it() -> None:
    """Indentation is not counted, while whitespace inside a string value is."""
    from scripts.smoke.iam_policy_budget import _policy_document_chars

    document = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Sid": "has spaces inside"}],
    }
    compact = _policy_document_chars(document)
    assert compact == _policy_document_chars(json.loads(json.dumps(document, indent=4)))
    assert "has spaces inside" in json.dumps(document), "the fixture lost its in-string whitespace"
    # The in-string spaces ARE counted: removing them shortens the measurement.
    narrowed = json.loads(json.dumps(document).replace("has spaces inside", "hasspacesinside"))
    assert _policy_document_chars(narrowed) < compact


def test_a_newly_added_role_gets_a_row_and_raises_the_floor() -> None:
    """A newly added role gets a budget row and raises the expected-check floor by two: one inline
    and one managed row.
    """
    new_role = f"{_NAME_PREFIX}-brand-new-role"
    without = _FakeIam(inline={_SUBJECT: {"p": _SMALL_POLICY_CHARS}})
    with_new = _FakeIam(inline={_SUBJECT: {"p": _SMALL_POLICY_CHARS}}, extra_roles=(new_role,))

    assert not [r for r in _run(without) if r.resource == new_role]
    assert _row(_run(with_new), new_role).status == "pass"

    ctx_without = context_for_test(clients={"iam": without}, outputs={})
    ctx_with = context_for_test(clients={"iam": with_new}, outputs={})
    assert (
        expected_check_count(ctx_with).minimum == expected_check_count(ctx_without).minimum + 2
    ), "the floor did not move with the role set, so it does not bound the loop it claims to bound"


def test_a_role_the_matrix_declares_but_the_account_lacks_is_a_failure() -> None:
    """A declared role missing from the account fails `derivation_covers_every_declared_role`."""
    omitted = _DECLARED_ROLES[1]
    results = _run(_FakeIam(inline={_SUBJECT: {"p": _SMALL_POLICY_CHARS}}, omit_roles=(omitted,)))
    floor = [r for r in results if r.check == "derivation_covers_every_declared_role"]
    assert len(floor) == 1
    assert floor[0].status == "fail"
    assert omitted in floor[0].detail


def test_an_empty_role_set_fails_rather_than_reporting_a_clean_budget() -> None:
    """An empty role set fails `inline_policy_budget_subject_is_non_empty` and yields no budget
    rows.
    """
    empty = _FakeIam(omit_roles=_DECLARED_ROLES)
    empty.roles = ()
    results = _run(empty)
    statuses = {r.check: r.status for r in results}
    assert statuses["inline_policy_budget_subject_is_non_empty"] == "fail"
    assert not [r for r in results if r.check == "inline_policy_budget"]


def test_pagination_is_followed_so_later_roles_are_not_silently_unmeasured() -> None:
    """A breaching role on a later `list_roles` page is still measured and fails."""
    breaching = _DECLARED_ROLES[-1]
    iam = _FakeIam(inline={breaching: {"p": _chars_at(0.99)}}, page_size=1)
    assert len(iam.roles) > 1, (
        "the fixture must span more than one page for this to assert anything"
    )
    assert _row(_run(iam), breaching).status == "fail"


def _customer_arn(name: str) -> str:
    return f"arn:aws:iam::123456789012:policy/{name}"


def test_managed_policy_count_is_reported_as_the_remediation_path() -> None:
    """The budget row's detail reports the role's attached managed-policy count."""
    managed = {_SUBJECT: {_customer_arn(f"m{i}"): _SMALL_POLICY_CHARS for i in range(3)}}
    results = _run(_FakeIam(inline={_SUBJECT: {"p": _SMALL_POLICY_CHARS}}, managed=managed))
    assert "3 managed policy(ies) attached" in _row(results, _SUBJECT).detail


def test_every_deployed_role_gets_exactly_one_budget_row() -> None:
    iam = _FakeIam(inline={_SUBJECT: {"p": _SMALL_POLICY_CHARS}})
    results = _run(iam)
    rows = [r for r in results if r.check == "inline_policy_budget"]
    assert len(rows) == len([r for r in iam.roles if r.startswith(f"{_NAME_PREFIX}-")])
    assert len(rows) == len({r.resource for r in rows}), "a role was measured twice"
    ctx = context_for_test(clients={"iam": iam}, outputs={})
    assert len(results) >= expected_check_count(ctx).minimum


# --- managed_policy_budget ---


def _managed_chars_at(fraction: float) -> int:
    return math.ceil(MANAGED_POLICY_DOCUMENT_LIMIT_CHARS * fraction)


def test_the_managed_limits_are_aws_service_quotas() -> None:
    """6,144 chars per managed policy and 10 attachments per role, the IAM defaults.

    https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html
    """
    assert MANAGED_POLICY_DOCUMENT_LIMIT_CHARS == 6144
    assert MANAGED_POLICIES_PER_ROLE_QUOTA == 10


def test_every_deployed_role_gets_exactly_one_managed_budget_row() -> None:
    iam = _FakeIam(inline={_SUBJECT: {"p": _SMALL_POLICY_CHARS}})
    results = _run(iam)
    rows = [r for r in results if r.check == "managed_policy_budget"]
    deployed = {r for r in iam.roles if r.startswith(f"{_NAME_PREFIX}-")}
    assert {r.resource for r in rows} == deployed
    assert len(rows) == len(deployed)


def test_the_floor_equals_the_rows_emitted() -> None:
    """Two rows per role plus the floor: a dropped managed row is a count shortfall."""
    iam = _FakeIam()
    ctx = context_for_test(clients={"iam": iam}, outputs={})
    assert len(_run(iam)) == expected_check_count(ctx).minimum


def test_a_small_managed_document_passes_quietly_and_is_measured() -> None:
    arn = _customer_arn("crm-agent-eval-sweep-control-dynamodb-allow")
    iam = _FakeIam(managed={_SUBJECT: {arn: _managed_chars_at(0.5)}})
    row = _row(_run(iam), _SUBJECT, "managed_policy_budget")
    assert row.status == "pass"
    assert BUDGET_ADVISORY_DETAIL_PREFIX not in row.detail
    assert f"{_managed_chars_at(0.5)} of 6144 chars (50.0%)" in row.detail, row.detail
    assert iam.documents_read == [arn]


def test_a_managed_document_at_the_advisory_threshold_passes_but_is_marked() -> None:
    iam = _FakeIam(
        managed={_SUBJECT: {_customer_arn("p"): _managed_chars_at(ADVISORY_USAGE_FRACTION)}}
    )
    row = _row(_run(iam), _SUBJECT, "managed_policy_budget")
    assert row.status == "pass"
    assert row.detail.startswith(BUDGET_ADVISORY_DETAIL_PREFIX), row.detail


def test_a_managed_document_at_the_failing_threshold_fails() -> None:
    """The largest document decides, not the sum or the first one read."""
    managed = {
        _SUBJECT: {
            _customer_arn("a-small"): _SMALL_POLICY_CHARS,
            _customer_arn("b-large"): _managed_chars_at(FAIL_USAGE_FRACTION),
        }
    }
    row = _row(_run(_FakeIam(managed=managed)), _SUBJECT, "managed_policy_budget")
    assert row.status == "fail", row.detail
    assert "CreatePolicyVersion" in row.detail, row.detail


def test_managed_documents_are_judged_one_by_one_not_summed() -> None:
    """Two documents each under the advisory line pass, though their sum exceeds 6,144."""
    each = _managed_chars_at(0.6)
    managed = {_SUBJECT: {_customer_arn("a"): each, _customer_arn("b"): each}}
    row = _row(_run(_FakeIam(managed=managed)), _SUBJECT, "managed_policy_budget")
    assert row.status == "pass", row.detail
    assert BUDGET_ADVISORY_DETAIL_PREFIX not in row.detail


def test_an_attachment_count_at_the_quota_fails() -> None:
    managed = {
        _SUBJECT: {
            _customer_arn(f"p{i}"): _SMALL_POLICY_CHARS
            for i in range(MANAGED_POLICIES_PER_ROLE_QUOTA)
        }
    }
    row = _row(_run(_FakeIam(managed=managed)), _SUBJECT, "managed_policy_budget")
    assert row.status == "fail", row.detail
    assert f"10 of {MANAGED_POLICIES_PER_ROLE_QUOTA} managed policy attachment(s)" in row.detail


def test_an_attachment_count_at_the_advisory_threshold_is_marked() -> None:
    managed = {_SUBJECT: {_customer_arn(f"p{i}"): _SMALL_POLICY_CHARS for i in range(9)}}
    row = _row(_run(_FakeIam(managed=managed)), _SUBJECT, "managed_policy_budget")
    assert row.status == "pass"
    assert row.detail.startswith(BUDGET_ADVISORY_DETAIL_PREFIX), row.detail


def test_aws_managed_policies_count_toward_the_quota_but_are_not_sized() -> None:
    aws_arn = f"{AWS_MANAGED_POLICY_ARN_PREFIX}service-role/AWSLambdaBasicExecutionRole"
    customer = _customer_arn("mine")
    iam = _FakeIam(managed={_SUBJECT: {aws_arn: 99999, customer: _SMALL_POLICY_CHARS}})
    row = _row(_run(iam), _SUBJECT, "managed_policy_budget")
    assert row.status == "pass", row.detail
    assert "2 of 10 managed policy attachment(s)" in row.detail
    assert iam.documents_read == [customer]


def test_attachment_pagination_is_followed() -> None:
    """A breaching document on a later `list_attached_role_policies` page is still measured."""
    managed = {
        _SUBJECT: {
            _customer_arn("a"): _SMALL_POLICY_CHARS,
            _customer_arn("z"): _managed_chars_at(0.99),
        }
    }
    iam = _FakeIam(managed=managed, page_size=1)
    row = _row(_run(iam), _SUBJECT, "managed_policy_budget")
    assert row.status == "fail", row.detail
    assert sorted(iam.documents_read) == sorted(managed[_SUBJECT])

# SPDX-License-Identifier: MIT
"""Layer 4: every deployment role's IAM policy-size and attachment quotas, measured on the account.

Two rows per deployed role. `inline_policy_budget` measures the aggregate of the role's inline
policies against the 10,240-char cap. `managed_policy_budget` measures each attached
customer-managed policy's default version against the 6,144-char per-policy cap, and the role's
attachment count against the managed-policies-per-role quota. Both caps exclude whitespace
(https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html). The same advisory and
fail fractions apply to every measurement.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final

from scripts.smoke.harness import ExpectedCheckCount, SmokeContext, run_main
from scripts.smoke.iam_roles import _NAME_PREFIX, _function_grants, _load_matrix, _role_name

from eval.verification import CheckResult

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "iam_policy_budget"

#: AWS service quota: the aggregate size of all inline policies on one role.
INLINE_POLICY_AGGREGATE_LIMIT_CHARS: Final[int] = 10240

#: AWS service quota: the size of one managed policy version.
MANAGED_POLICY_DOCUMENT_LIMIT_CHARS: Final[int] = 6144

#: AWS service quota: managed policies attached to one role, at its default (adjustable) value. A
#: raised account quota makes this conservative, never permissive.
MANAGED_POLICIES_PER_ROLE_QUOTA: Final[int] = 10

#: The ARN prefix of AWS-managed policies, whose documents this class does not size.
AWS_MANAGED_POLICY_ARN_PREFIX: Final[str] = "arn:aws:iam::aws:policy/"

#: Chosen threshold: at or above this fraction the role's detail carries
#: `BUDGET_ADVISORY_DETAIL_PREFIX`.
ADVISORY_USAGE_FRACTION: Final[float] = 0.85

#: Chosen threshold: at or above this fraction the role fails.
FAIL_USAGE_FRACTION: Final[float] = 0.95

#: Machine-readable marker opening the `detail` of a role at or above the advisory threshold; the
#: row's status stays `pass`.
BUDGET_ADVISORY_DETAIL_PREFIX: Final[str] = "ADVISORY: "


def _policy_document_chars(document: Mapping[str, Any]) -> int:
    """The character count IAM would attribute to `document`.

    Serialised without separator whitespace; whitespace inside string values is preserved. An
    approximation of IAM's unpublished serialisation, not a byte-exact prediction.
    """
    return len(json.dumps(document, separators=(",", ":"), sort_keys=True))


def _deployed_role_names(ctx: SmokeContext, name_prefix: str) -> list[str]:
    """Every deployed role whose name starts with `name_prefix`, from the account."""
    iam = ctx.client("iam")
    names: list[str] = []
    marker: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Marker": marker} if marker else {}
        page = iam.list_roles(**kwargs)
        for role in page.get("Roles", []):
            name = role.get("RoleName")
            if isinstance(name, str) and name.startswith(name_prefix):
                names.append(name)
        marker = page.get("Marker") if page.get("IsTruncated") else None
        if not marker:
            return sorted(names)


def _inline_policy_chars(ctx: SmokeContext, role_name: str) -> tuple[int, int]:
    """`(aggregate_chars, policy_count)` across every inline policy on `role_name`."""
    iam = ctx.client("iam")
    total = 0
    count = 0
    marker: str | None = None
    while True:
        kwargs: dict[str, Any] = {"RoleName": role_name}
        if marker:
            kwargs["Marker"] = marker
        page = iam.list_role_policies(**kwargs)
        for policy_name in page.get("PolicyNames", []):
            document = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)[
                "PolicyDocument"
            ]
            total += _policy_document_chars(document)
            count += 1
        marker = page.get("Marker") if page.get("IsTruncated") else None
        if not marker:
            return total, count


def _attached_policy_arns(ctx: SmokeContext, role_name: str) -> list[str]:
    """Every managed policy ARN attached to `role_name`, AWS-managed included."""
    iam = ctx.client("iam")
    arns: list[str] = []
    marker: str | None = None
    while True:
        kwargs: dict[str, Any] = {"RoleName": role_name}
        if marker:
            kwargs["Marker"] = marker
        page = iam.list_attached_role_policies(**kwargs)
        arns.extend(str(policy["PolicyArn"]) for policy in page.get("AttachedPolicies", []))
        marker = page.get("Marker") if page.get("IsTruncated") else None
        if not marker:
            return sorted(arns)


def _managed_policy_chars(ctx: SmokeContext, policy_arn: str) -> int:
    """The character count of `policy_arn`'s default version."""
    iam = ctx.client("iam")
    version_id = iam.get_policy(PolicyArn=policy_arn)["Policy"]["DefaultVersionId"]
    version = iam.get_policy_version(PolicyArn=policy_arn, VersionId=version_id)
    return _policy_document_chars(version["PolicyVersion"]["Document"])


def _managed_budget_row(ctx: SmokeContext, role_name: str, attached: Sequence[str]) -> CheckResult:
    """The role's `managed_policy_budget` row: every customer-managed document, and the count."""
    sizes = {
        arn: _managed_policy_chars(ctx, arn)
        for arn in attached
        if not arn.startswith(AWS_MANAGED_POLICY_ARN_PREFIX)
    }
    count_usage = len(attached) / MANAGED_POLICIES_PER_ROLE_QUOTA
    usages = {arn: chars / MANAGED_POLICY_DOCUMENT_LIMIT_CHARS for arn, chars in sizes.items()}
    largest = max(usages.values(), default=0.0)
    documents = "; ".join(
        f"{arn.rsplit('/', 1)[-1]} {sizes[arn]} of {MANAGED_POLICY_DOCUMENT_LIMIT_CHARS} chars "
        f"({usages[arn]:.1%})"
        for arn in sorted(sizes)
    )
    detail = (
        f"{len(attached)} of {MANAGED_POLICIES_PER_ROLE_QUOTA} managed policy attachment(s) "
        f"({count_usage:.1%}); {len(sizes)} customer-managed document(s)"
        + (f": {documents}" if documents else "")
    )
    if max(count_usage, largest) >= FAIL_USAGE_FRACTION:
        return CheckResult(
            resource=role_name,
            check="managed_policy_budget",
            status="fail",
            detail=(
                f"{detail}. At or above the chosen {FAIL_USAGE_FRACTION:.0%} threshold: IAM refuses "
                "the CreatePolicyVersion that crosses 6,144 chars and the AttachRolePolicy past the "
                "quota, so the next apply that grows this role fails"
            ),
        )
    return CheckResult(
        resource=role_name,
        check="managed_policy_budget",
        status="pass",
        detail=(
            f"{BUDGET_ADVISORY_DETAIL_PREFIX}{detail}. At or above the chosen "
            f"{ADVISORY_USAGE_FRACTION:.0%} advisory threshold — under the cap, but the margin is "
            "worth knowing before moving more statements into managed policies on this role"
            if max(count_usage, largest) >= ADVISORY_USAGE_FRACTION
            else detail
        ),
    )


def _matrix_declared_roles() -> set[str]:
    """The roles the table x action x role matrix declares, via `iam_roles`' own naming convention."""
    return {_role_name(function) for function in _function_grants(_load_matrix())}


def checks(ctx: SmokeContext) -> Iterator[CheckResult]:
    """Two budget checks per deployed role, plus the derivation's own coverage floor."""
    deployed = _deployed_role_names(ctx, f"{_NAME_PREFIX}-")

    # The account-derived set must cover every role the matrix declares.
    declared = _matrix_declared_roles()
    missing = sorted(declared - set(deployed))
    yield CheckResult(
        resource="role_set",
        check="derivation_covers_every_declared_role",
        status="pass" if not missing else "fail",
        detail=(
            f"{len(deployed)} deployed role(s) with prefix {_NAME_PREFIX!r} cover all "
            f"{len(declared)} role(s) the matrix declares"
            if not missing
            else f"{missing} are declared in the matrix but absent from the account, so this class "
            "would report a clean budget for roles it never measured. A role that failed to deploy is "
            "a finding, not a gap"
        ),
    )

    if not deployed:
        # Zero discovered roles is a failure, not an empty pass.
        yield CheckResult(
            resource="role_set",
            check="inline_policy_budget_subject_is_non_empty",
            status="fail",
            detail=(
                f"no deployed role starts with {_NAME_PREFIX!r}, so no budget was measured. This class "
                "PASSES when it finds nothing under the cap, so an empty subject would report a clean "
                "budget having read nothing"
            ),
        )
        return

    for role_name in deployed:
        aggregate, policies = _inline_policy_chars(ctx, role_name)
        attached = _attached_policy_arns(ctx, role_name)
        managed = len(attached)
        yield _managed_budget_row(ctx, role_name, attached)
        usage = aggregate / INLINE_POLICY_AGGREGATE_LIMIT_CHARS
        headroom = INLINE_POLICY_AGGREGATE_LIMIT_CHARS - aggregate
        detail = (
            f"{aggregate} of {INLINE_POLICY_AGGREGATE_LIMIT_CHARS} chars "
            f"({usage:.1%}) across {policies} inline policy(ies), {headroom} chars headroom; "
            f"{managed} managed policy(ies) attached"
        )
        if usage >= FAIL_USAGE_FRACTION:
            yield CheckResult(
                resource=role_name,
                check="inline_policy_budget",
                status="fail",
                detail=(
                    f"{detail}. At or above the chosen {FAIL_USAGE_FRACTION:.0%} threshold: IAM refuses "
                    "the PutRolePolicy that crosses 10,240, so the next apply that adds a statement to "
                    "this role fails and names the role rather than the change. Move statements into a "
                    "customer-managed policy (6,144 chars each, separate quota)"
                ),
            )
            continue
        yield CheckResult(
            resource=role_name,
            check="inline_policy_budget",
            status="pass",
            detail=(
                f"{BUDGET_ADVISORY_DETAIL_PREFIX}{detail}. At or above the chosen "
                f"{ADVISORY_USAGE_FRACTION:.0%} advisory threshold — under the cap, but the margin is "
                "worth knowing before adding explicit Deny statements to this role"
                if usage >= ADVISORY_USAGE_FRACTION
                else detail
            ),
        )


def expected_check_count(ctx: SmokeContext) -> ExpectedCheckCount:
    """Two rows per deployed role plus the coverage floor, derived from the account.

    The count is read from the same helper the loop iterates.
    """
    roles = _deployed_role_names(ctx, f"{_NAME_PREFIX}-")
    return ExpectedCheckCount(
        minimum=2 * len(roles) + 1,
        basis=(
            f"{len(roles)} deployed role(s) with prefix {_NAME_PREFIX!r} (an inline and a managed "
            "budget row each) + 1 derivation coverage floor"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the IAM policy budget checks and return the process exit code."""
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

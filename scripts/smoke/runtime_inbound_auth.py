# SPDX-License-Identifier: MIT
"""Layer 4 check: the deployed AgentCore Runtime carries no custom JWT authorizer.

The Runtime's inbound authorization is IAM, expressed as the absence of a `customJWTAuthorizer` in
`authorizerConfiguration`. Because the pass condition is an absent field, the check first asserts
the unprojected `GetAgentRuntime` response carries members the resource always has
(`agentRuntimeArn`, `roleArn`, `networkConfiguration`); if they are missing the result is `fail`.
A present `customJWTAuthorizer` is a `fail`.

Then the resource policy is exercised with a real call: the runner's own same-account
principal, which is not in `local.runtime_invoker_role_arns`, calls `InvokeAgentRuntime` and must get
an `AccessDeniedException` from an explicit deny in this Runtime's resource-based policy;
any other denial, an implicit deny included, is a `fail`, and the row quotes the error message. A
premise row first confirms the caller is not one of the invoker roles. The
payload is `{}`, so even an unexpected admission reaches no model. The runner calls from outside the
VPC, so the refusal it observes may come from the `aws:SourceVpce` Deny as well as from the principal
list.

The same observation is then judged against the `aws:SourceVpce` Deny state the deployment declares
(`runtime_network_posture.declared_source_vpce_deny`) in `outside_vpc_invoke_refused_under_declared_deny`:
declared enabled, an explicit resource-policy refusal passes and an admission fails; declared
disabled or unknown, or refused with another error, the row defers with a reason.

The principal list itself is read account-side: `GetResourcePolicy` on the runtime ARN and
on its DEFAULT endpoint ARN, whose Allow statements' AWS principals must equal exactly the role ARNs
`local.runtime_invoker_role_arns` names. Where the installed botocore does not model
`get_resource_policy`, the policy is read with `aws bedrock-agentcore-control get-resource-policy`
under the resolved `--profile`/`--region` instead. Only when neither supports the operation is the
row a `defer`, never a pass.

Usage:
    python3 -m scripts.smoke.runtime_inbound_auth [--profile P] [--region R] [--outputs PATH] [--json]
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, Final

from scripts import proc_run
from scripts.smoke.harness import CheckResult, ExpectedCheckCount, SmokeContext, run_main
from scripts.smoke.runtime_network_posture import (
    DeclaredDeny,
    DeclaredDenyState,
    declared_source_vpce_deny,
)

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "runtime_inbound_auth"

#: Response members the Runtime resource always carries, asserted before an absence is interpreted.
GUARD_MEMBERS: Final[tuple[str, ...]] = ("agentRuntimeArn", "roleArn", "networkConfiguration")

#: The member whose absence is IAM inbound auth, and the sub-member whose presence is a failure.
AUTHORIZER_MEMBER: Final[str] = "authorizerConfiguration"
CUSTOM_JWT_MEMBER: Final[str] = "customJWTAuthorizer"

#: The row that reads both Runtime resource policies' principal sets.
PRINCIPAL_SET_CHECK: Final[str] = "resource_policy_principals_are_the_invokers"

#: Appended to a passing denial: the call is made from outside the VPC, so it bounds nothing more.
OUTSIDE_VPC_BOUND: Final[str] = (
    "; outside-VPC refusal only: the aws:SourceVpce Deny alone could produce it, so this is not "
    f"proof the principal list is enforced ({PRINCIPAL_SET_CHECK} reads that list)"
)

#: Appended to an `AccessDeniedException` that is not this Runtime's explicit resource-based deny.
NOT_RESOURCE_POLICY_DENY: Final[str] = (
    "; expected an explicit deny in this Runtime's resource-based policy. An implicit deny (the "
    "runner holds no identity grant) or an identity, SCP or boundary deny refuses the call "
    "before the resource policy is exercised, so it proves nothing about that policy"
)

#: `(argv) -> (returncode, stdout, stderr)`; raises `FileNotFoundError` when `aws` is not on PATH.
CliRunner = Callable[[Sequence[str]], "tuple[int, str, str]"]

#: How the AWS CLI reports an operation it does not model.
_CLI_UNSUPPORTED_MARKER: Final[str] = "invalid choice"

#: The error code an unlisted principal's `InvokeAgentRuntime` must be refused with.
ACCESS_DENIED_CODE: Final[str] = "AccessDeniedException"

#: The row judging the outside-VPC invoke against the declared `aws:SourceVpce` Deny state.
DECLARED_DENY_CHECK: Final[str] = "outside_vpc_invoke_refused_under_declared_deny"

__all__ = [
    "AUTHORIZER_MEMBER",
    "CUSTOM_JWT_MEMBER",
    "DECLARED_DENY_CHECK",
    "GUARD_MEMBERS",
    "LAYER",
    "NOT_RESOURCE_POLICY_DENY",
    "PRINCIPAL_SET_CHECK",
    "RESOURCE_CLASS",
    "checks",
    "main",
    "runtime_id_from_arn",
]


def runtime_id_from_arn(runtime_arn: str) -> str:
    """The runtime id `GetAgentRuntime` takes, parsed from the deployed ARN's final path segment.

    An ARN with no `/` path segment yields the empty string, which the caller reports as a failure.
    """
    head, separator, tail = runtime_arn.rpartition("/")
    return tail if separator and head else ""


def _describe(ctx: SmokeContext, runtime_id: str) -> Mapping[str, Any]:
    """The whole, unprojected `GetAgentRuntime` response."""
    response = ctx.client("bedrock-agentcore-control").get_agent_runtime(agentRuntimeId=runtime_id)
    return response if isinstance(response, Mapping) else {}


def _invoker_role_names() -> frozenset[str]:
    """The role names `local.runtime_invoker_role_arns` admits, read from the root IaC."""
    from scripts.smoke.iam_roles import _role_name
    from scripts.smoke.relay_iam import _runtime_invoker_functions

    return frozenset(_role_name(function) for function in _runtime_invoker_functions())


def _caller_role_name(caller_arn: str) -> str:
    """The role name of an `assumed-role/<role>/<session>` or `role/<role>` ARN, else the ARN."""
    resource = caller_arn.partition(":assumed-role/")[2] or caller_arn.partition(":role/")[2]
    return resource.split("/", 1)[0] if resource else caller_arn


def _refusal_result(exc: BaseException, *, runtime_arn: str, role: str) -> CheckResult:
    """Classify the unlisted caller's refused `InvokeAgentRuntime`; the detail quotes the message.

    Only an `AccessDeniedException` from an explicit deny in this Runtime's resource-based policy
    passes. Any other `AccessDeniedException` (an implicit deny, an identity-policy, SCP or
    boundary deny) and any other error is a `fail`.
    """
    from scripts.smoke.runtime_invoke import is_explicit_inbound_deny

    error = getattr(exc, "response", {}).get("Error", {})
    code = str(error.get("Code", type(exc).__name__))
    message = str(error.get("Message", exc))
    observed = f"InvokeAgentRuntime as {role!r} -> {code}: {message!r}"
    passed = is_explicit_inbound_deny(exc, runtime_arn=runtime_arn)
    if passed:
        suffix = OUTSIDE_VPC_BOUND
    elif code == ACCESS_DENIED_CODE:
        suffix = NOT_RESOURCE_POLICY_DENY
    else:
        suffix = f"; expected {ACCESS_DENIED_CODE}"
    return CheckResult(
        resource=runtime_arn,
        check="unlisted_principal_denied_invoking_runtime",
        status="pass" if passed else "fail",
        detail=observed + suffix,
    )


def declared_deny_result(
    declared: DeclaredDenyState, *, refused_explicitly: bool, admitted: bool, runtime_arn: str
) -> CheckResult:
    """Judge the outside-VPC `InvokeAgentRuntime` against the declared `aws:SourceVpce` Deny state.

    Declared enabled: an explicit resource-policy refusal is a `pass`, an admission a `fail`, any
    other error a `defer`. Declared disabled or unknown: a `defer` naming that state.
    """
    declared_text = (
        f"the aws:SourceVpce Deny is declared {declared.state.value} ({declared.source})"
    )
    if declared.state is DeclaredDeny.DISABLED:
        return CheckResult(
            resource=runtime_arn,
            check=DECLARED_DENY_CHECK,
            status="defer",
            defer_reason="inbound_deny_disabled_by_config",
            detail=f"{declared_text}, so the outside-VPC invoke has no declared Deny to be judged by",
        )
    if declared.state is DeclaredDeny.UNKNOWN:
        return CheckResult(
            resource=runtime_arn,
            check=DECLARED_DENY_CHECK,
            status="defer",
            defer_reason="inbound_deny_state_unknown",
            detail=f"{declared_text}, so the outside-VPC invoke cannot be judged against it",
        )
    if admitted:
        return CheckResult(
            resource=runtime_arn,
            check=DECLARED_DENY_CHECK,
            status="fail",
            detail=f"{declared_text}, yet an InvokeAgentRuntime from outside the VPC endpoint was "
            "ADMITTED: the deployed resource policy does not enforce the Deny the deployment declares",
        )
    if refused_explicitly:
        return CheckResult(
            resource=runtime_arn,
            check=DECLARED_DENY_CHECK,
            status="pass",
            detail=f"{declared_text}, and the InvokeAgentRuntime from outside the VPC endpoint was "
            "refused by an explicit deny in the Runtime's resource-based policy, as declared"
            + OUTSIDE_VPC_BOUND,
        )
    return CheckResult(
        resource=runtime_arn,
        check=DECLARED_DENY_CHECK,
        status="defer",
        defer_reason="inbound_deny_not_observed_other_error",
        detail=f"{declared_text}, but the outside-VPC invoke failed with an error other than an "
        "explicit resource-policy deny, so the Deny was neither observed nor contradicted",
    )


def _unlisted_principal_results(
    ctx: SmokeContext, runtime_arn: str, declared: DeclaredDenyState
) -> Iterator[CheckResult]:
    """The premise row, the `InvokeAgentRuntime` the runner must be refused, and its declared-Deny
    verdict."""
    try:
        caller = str(ctx.client("sts").get_caller_identity()["Arn"])
        invokers = _invoker_role_names()
    except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
        yield CheckResult(
            resource=runtime_arn,
            check="caller_is_not_a_runtime_invoker",
            status="fail",
            detail=f"could not establish the caller or the invoker list: {type(exc).__name__}: {exc}",
        )
        return
    role = _caller_role_name(caller)
    unlisted = role not in invokers
    yield CheckResult(
        resource=runtime_arn,
        check="caller_is_not_a_runtime_invoker",
        status="pass" if unlisted else "fail",
        detail=f"caller role {role!r}; invoker roles {sorted(invokers)}"
        + (
            ""
            if unlisted
            else " -- the caller is admitted, so its refusal below would prove nothing"
        ),
    )
    if not unlisted:
        return
    try:
        response = ctx.client("bedrock-agentcore").invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=f"smoke-inbound-unlisted-{uuid.uuid4().hex}",
            contentType="application/json",
            accept="application/json",
            payload=b"{}",
        )
    except Exception as exc:  # noqa: BLE001 - classified below
        refusal = _refusal_result(exc, runtime_arn=runtime_arn, role=role)
        yield refusal
        yield declared_deny_result(
            declared,
            refused_explicitly=refusal.status == "pass",
            admitted=False,
            runtime_arn=runtime_arn,
        )
        return
    body = response.get("response")
    if body is not None and hasattr(body, "close"):
        body.close()
    yield CheckResult(
        resource=runtime_arn,
        check="unlisted_principal_denied_invoking_runtime",
        status="fail",
        detail=f"InvokeAgentRuntime as {role!r}, a principal outside runtime_invoker_role_arns, "
        "was ADMITTED. Severity: Critical -- the resource policy does not confine the Runtime to "
        "its three invokers",
    )
    yield declared_deny_result(
        declared, refused_explicitly=False, admitted=True, runtime_arn=runtime_arn
    )


def resource_policy_arns(runtime_arn: str) -> tuple[str, str]:
    """The runtime ARN and its DEFAULT endpoint ARN, each carrying its own resource policy."""
    return (runtime_arn, f"{runtime_arn}/runtime-endpoint/DEFAULT")


def _allowed_aws_principals(document: Mapping[str, Any]) -> tuple[set[str], list[str]]:
    """Every AWS principal `document`'s Allow statements name, and why any Allow is not AWS-only.

    An Allow is acceptable only with a `Principal` of exactly `{"AWS": ...}` and no `NotPrincipal`;
    a `Service`, `Federated` or `CanonicalUser` key, a `*` principal, or a `NotPrincipal` names the
    statement in the second element.
    """
    statements = document.get("Statement", [])
    if isinstance(statements, Mapping):
        statements = [statements]
    principals: set[str] = set()
    problems: list[str] = []
    for index, statement in enumerate(statements if isinstance(statements, list) else []):
        if not isinstance(statement, Mapping) or statement.get("Effect") != "Allow":
            continue
        sid = str(statement.get("Sid") or f"<statement {index}>")
        if "NotPrincipal" in statement:
            problems.append(f"{sid}: NotPrincipal {json.dumps(statement['NotPrincipal'])}")
            continue
        principal = statement.get("Principal")
        if not isinstance(principal, Mapping) or set(principal) != {"AWS"}:
            problems.append(
                f"{sid}: Principal {json.dumps(principal)} is not exactly {{'AWS': ...}}"
            )
            continue
        aws = principal["AWS"]
        principals.update([aws] if isinstance(aws, str) else [str(a) for a in aws or []])
    return principals, problems


def _run_cli(argv: Sequence[str]) -> tuple[int, str, str]:
    completed = proc_run.run(list(argv), capture_output=True, text=True, check=False, timeout=60)
    return completed.returncode, completed.stdout, completed.stderr


def resource_policy_cli_argv(ctx: SmokeContext, resource_arn: str) -> list[str]:
    """The read-only CLI call for one resource policy, with the resolved profile and region."""
    return [
        "aws",
        "bedrock-agentcore-control",
        "get-resource-policy",
        "--resource-arn",
        resource_arn,
        "--profile",
        ctx.profile,
        "--region",
        ctx.region,
        "--output",
        "json",
        "--no-cli-pager",
    ]


class _PolicyReadUnsupportedError(Exception):
    """Neither botocore nor the AWS CLI supports `get-resource-policy`; the message says which."""


def _read_policy_text(ctx: SmokeContext, client: Any, arn: str, runner: CliRunner) -> str:
    """The policy document text, from botocore when it models the operation, else from the CLI.

    Raises:
        _PolicyReadUnsupportedError: Neither path supports the operation.
        RuntimeError: The supported read failed.
    """
    if hasattr(client, "get_resource_policy"):
        return str(client.get_resource_policy(resourceArn=arn)["policy"])
    try:
        code, out, err = runner(resource_policy_cli_argv(ctx, arn))
    except FileNotFoundError as exc:
        raise _PolicyReadUnsupportedError(
            "botocore does not model get_resource_policy and no `aws` CLI is on PATH"
        ) from exc
    if code != 0:
        if _CLI_UNSUPPORTED_MARKER in err.lower():
            raise _PolicyReadUnsupportedError(
                "neither botocore nor the installed aws CLI supports "
                f"bedrock-agentcore-control get-resource-policy ({err.strip()[:160]})"
            )
        raise RuntimeError(f"aws CLI exit {code}: {err.strip()[:240]}")
    return str(json.loads(out)["policy"])


def _principal_set_results(
    ctx: SmokeContext, runtime_arn: str, runner: CliRunner
) -> Iterator[CheckResult]:
    """One row per resource policy: its Allow principals equal exactly the invoker role ARNs."""
    try:
        account = str(ctx.client("sts").get_caller_identity()["Account"])
        expected = {f"arn:aws:iam::{account}:role/{name}" for name in _invoker_role_names()}
    except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
        yield CheckResult(
            resource=runtime_arn,
            check=PRINCIPAL_SET_CHECK,
            status="fail",
            detail=f"could not derive the invoker role ARNs: {type(exc).__name__}: {exc}",
        )
        return
    client = ctx.client("bedrock-agentcore-control")
    for arn in resource_policy_arns(runtime_arn):
        try:
            document = json.loads(_read_policy_text(ctx, client, arn, runner))
            principals, problems = _allowed_aws_principals(document)
        except _PolicyReadUnsupportedError as exc:
            yield CheckResult(
                resource=arn,
                check=PRINCIPAL_SET_CHECK,
                status="defer",
                defer_reason="get_resource_policy_unsupported",
                detail=f"{exc}, so the principal set is NOT verified here; expected "
                f"{sorted(expected)}",
            )
            continue
        except Exception as exc:  # noqa: BLE001 - a failing row, not a crash
            yield CheckResult(
                resource=arn,
                check=PRINCIPAL_SET_CHECK,
                status="fail",
                detail=f"the resource policy could not be read: {type(exc).__name__}: {exc}",
            )
            continue
        exact = bool(expected) and principals == expected and not problems
        yield CheckResult(
            resource=arn,
            check=PRINCIPAL_SET_CHECK,
            status="pass" if exact else "fail",
            detail=f"Allow AWS principals {sorted(principals)}; invoker roles {sorted(expected)}"
            + (f"; non-AWS Allow: {'; '.join(problems)}" if problems else "")
            + (
                ""
                if principals == expected
                else f"; extra {sorted(principals - expected)}, missing "
                f"{sorted(expected - principals)} (an AROA id means a role was recreated)"
            ),
        )


def checks(
    ctx: SmokeContext,
    *,
    cli_runner: CliRunner | None = None,
    declared_deny: DeclaredDenyState | None = None,
) -> Iterator[CheckResult]:
    """Yield the guard result, the authorizer-absence result it licenses, and the denied call.

    `declared_deny` defaults to `declared_source_vpce_deny()`, read from this host's tfvars.
    """
    runner = cli_runner or _run_cli
    declared = declared_deny if declared_deny is not None else declared_source_vpce_deny()
    try:
        runtime_arn = ctx.outputs.require_str("agentcore_runtime_arn")
    except Exception as exc:  # noqa: BLE001 - reported as a failing check, not a crash
        yield CheckResult(
            resource="agentcore_runtime_arn",
            check="terraform_output_present",
            status="fail",
            detail=(
                f"required Terraform output 'agentcore_runtime_arn' is absent ({exc}); the "
                "Runtime to describe cannot be identified. A missing output is a failure, not "
                "a skipped resource class"
            ),
        )
        return

    runtime_id = runtime_id_from_arn(runtime_arn)
    if not runtime_id:
        yield CheckResult(
            resource=runtime_arn,
            check="runtime_id_resolves",
            status="fail",
            detail=(
                "the deployed runtime ARN has no final path segment, so no runtime id could be "
                "parsed from it. Calling GetAgentRuntime with an empty id would describe "
                "nothing and its absent authorizer field would read as a pass"
            ),
        )
        return

    response = _describe(ctx, runtime_id)
    missing_guards = [member for member in GUARD_MEMBERS if member not in response]
    guard_ok = not missing_guards
    yield CheckResult(
        resource=runtime_arn,
        check="response_shape_guard",
        status="pass" if guard_ok else "fail",
        detail=(
            f"GetAgentRuntime returned all of {list(GUARD_MEMBERS)}, so an absent "
            f"{AUTHORIZER_MEMBER!r} is a property of the resource rather than of the response "
            "shape"
            if guard_ok
            else f"GetAgentRuntime returned {sorted(response)} and is missing "
            f"{missing_guards} -- the response is not describing this Runtime's full "
            f"configuration, so an absent {AUTHORIZER_MEMBER!r} proves nothing and must not be "
            "read as IAM inbound auth"
        ),
    )

    authorizer = response.get(AUTHORIZER_MEMBER)
    custom_jwt = authorizer.get(CUSTOM_JWT_MEMBER) if isinstance(authorizer, Mapping) else None
    if custom_jwt:
        # Member names only, never their values.
        observed = (
            sorted(custom_jwt) if isinstance(custom_jwt, Mapping) else type(custom_jwt).__name__
        )
        yield CheckResult(
            resource=runtime_arn,
            check="no_custom_jwt_authorizer",
            status="fail",
            detail=(
                f"the deployed Runtime carries a {CUSTOM_JWT_MEMBER!r} (members: {observed}). "
                "Severity: Critical -- the v1 browser-token path "
                "to the Runtime is live, bypassing the relay and its JWT verification. Layer 4 "
                "must stop here rather than run T4 journeys against it"
            ),
        )
        yield from _unlisted_principal_results(ctx, runtime_arn, declared)
        yield from _principal_set_results(ctx, runtime_arn, runner)
        return

    absent = not authorizer
    yield CheckResult(
        resource=runtime_arn,
        check="no_custom_jwt_authorizer",
        status="pass" if (absent and guard_ok) else "fail",
        detail=(
            f"{AUTHORIZER_MEMBER!r} is absent/empty ({authorizer!r}), which IS IAM "
            "inbound authorization, and the shape guard above establishes the absence is the "
            "resource's"
            if absent and guard_ok
            else f"{AUTHORIZER_MEMBER!r}={authorizer!r} with shape guard "
            f"{'passed' if guard_ok else 'FAILED'}: an absence this check cannot attribute to "
            "the resource is not evidence of IAM inbound auth"
        ),
    )
    yield from _unlisted_principal_results(ctx, runtime_arn, declared)
    yield from _principal_set_results(ctx, runtime_arn, runner)


def expected_check_count(ctx: SmokeContext) -> ExpectedCheckCount:
    """Two checks -- the shape guard and the authorizer verdict -- once the runtime is addressable,
    and one before that.
    """
    try:
        addressable = bool(runtime_id_from_arn(ctx.outputs.require_str("agentcore_runtime_arn")))
    except Exception as exc:  # noqa: BLE001 - the reason belongs in the basis, not in a traceback
        return ExpectedCheckCount(
            minimum=1,
            basis=(
                f"the runtime ARN output could not be read ({type(exc).__name__}), so `checks` emits "
                "its single terraform_output_present failure and returns"
            ),
        )
    if not addressable:
        return ExpectedCheckCount(
            minimum=1,
            basis=(
                "the deployed runtime ARN has no final path segment, so `checks` emits its single "
                "runtime_id_resolves failure and returns"
            ),
        )
    return ExpectedCheckCount(
        minimum=4,
        basis=(
            "the runtime ARN output resolves to a parseable id, so the describe runs and both the "
            "response_shape_guard and the no_custom_jwt_authorizer verdict are emitted -- including "
            "on the custom-JWT path, where the authorizer verdict is the second check -- plus the "
            "caller_is_not_a_runtime_invoker premise row and at least one "
            f"{PRINCIPAL_SET_CHECK} row on every path"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the Runtime inbound-auth check and return the process exit code."""
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

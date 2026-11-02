# SPDX-License-Identifier: MIT
"""Layer 4 check: the deployed AgentCore Runtime's VPC-mode network posture.

Read-only, against the live account:

* **Ready** — a bounded poll of `get-agent-runtime` and the `DEFAULT` endpoint until both are `READY`
  and the endpoint's `liveVersion` is the runtime's version; any `*_FAILED` status fails at once.
* **Platform** — `platformVersion` equals :data:`EXPECTED_PLATFORM_VERSION`.
* **networkMode** — `get-agent-runtime` reports `networkMode == "VPC"`, its `agentRuntimeArn` equals
  the deployed output, its subnets are the stack's Runtime subnets in the region's AgentCore-supported
  AZs from `infra/regions.json` (a region missing from the map fails), and its
  security group is the dedicated Runtime SG, not the Lambda SG.
* **Endpoints** — in the stack's VPC (the `vpc_id` output), exactly one interface endpoint each for
  `bedrock-runtime`, `ecr.api` and `ecr.dkr`, `available` with private DNS on, and the endpoint SG
  admits 443 from the Runtime SG. An unresolvable VPC id, or more than one endpoint for a service in
  that VPC, fails the service's rows.
* **Egress** — the Runtime SG's egress carries no `0.0.0.0/0`.
* **Deny state** — both runtime resource policies are read for the `aws:SourceVpce` Deny. When it is
  absent this is reported `defer` with a reason, never `pass`.

`declared_source_vpce_deny` reads whether the deployment declares that Deny enabled, from the
gitignored tfvars; `scripts/smoke/runtime_inbound_auth.py` fails an accepted outside-VPC invoke
against it (`outside_vpc_invoke_refused_under_declared_deny`).

`get-agent-runtime` must carry the members the resource always carries before an absent field is
read as the resource's property.

Usage:
    python3 -m scripts.smoke.runtime_network_posture [--profile P] [--region R] [--outputs PATH] [--json]
"""

from __future__ import annotations

import enum
import json
import re
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from scripts import region_map
from scripts.smoke.harness import CheckResult, ExpectedCheckCount, SmokeContext, run_main

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "runtime_network_posture"

NAME_PREFIX: Final[str] = "crm-agent-eval"
RUNTIME_SG_NAME: Final[str] = f"{NAME_PREFIX}-agent-runtime"
ENDPOINT_SG_NAME: Final[str] = f"{NAME_PREFIX}-vpc-endpoints"

#: The three interface endpoints serving the Runtime's egress.
D2_INTERFACE_ENDPOINT_SERVICES: Final[tuple[str, ...]] = ("bedrock-runtime", "ecr.api", "ecr.dkr")

#: Members `get-agent-runtime` always carries; their presence makes an absent field the resource's.
GUARD_MEMBERS: Final[tuple[str, ...]] = ("agentRuntimeArn", "roleArn", "networkConfiguration")

#: The `platformVersion` `infra/modules/agentcore` declares as `var.platform_version`'s default.
EXPECTED_PLATFORM_VERSION: Final[str] = "V2"

#: The endpoint the relay invokes (`local.relay_runtime_qualifier` in `infra/main.tf`).
DEFAULT_ENDPOINT_NAME: Final[str] = "DEFAULT"

#: Bound and interval of the READY poll.
READY_WAIT_SECONDS: Final[float] = 900.0
READY_POLL_SECONDS: Final[float] = 15.0

READY_STATUS: Final[str] = "READY"
TRANSITIONAL_STATUSES: Final[frozenset[str]] = frozenset({"CREATING", "UPDATING"})

_PLATFORM_VERSION_KEY: Final[str] = "platformVersion"
_AFTER_CALL_EVENT: Final[str] = "after-call.bedrock-agentcore-control.GetAgentRuntime"
_HOOK_ID: Final[str] = "crm-smoke-copy-platform-version"

__all__ = [
    "D2_INTERFACE_ENDPOINT_SERVICES",
    "DEFAULT_ENDPOINT_NAME",
    "DENY_VARIABLE",
    "DeclaredDeny",
    "DeclaredDenyState",
    "EXPECTED_PLATFORM_VERSION",
    "LAYER",
    "READY_POLL_SECONDS",
    "READY_WAIT_SECONDS",
    "RESOURCE_CLASS",
    "RUNTIME_SG_NAME",
    "ReadyWait",
    "checks",
    "copy_platform_version",
    "declared_source_vpce_deny",
    "main",
    "runtime_id_from_arn",
    "wait_until_ready",
]

#: The Terraform variable gating both runtime resource policies' `aws:SourceVpce` Deny.
DENY_VARIABLE: Final[str] = "runtime_source_vpce_deny_enabled"

#: The Terraform root whose variable files declare the deployment's inputs.
INFRA_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "infra"

_ASSIGNMENT: Final[re.Pattern[str]] = re.compile(
    rf"^\s*{DENY_VARIABLE}\s*=\s*(?P<value>[^\s#]+)\s*(?:(?:#|//).*)?$", re.MULTILINE
)
_DEFAULT: Final[re.Pattern[str]] = re.compile(
    rf'variable\s+"{DENY_VARIABLE}"\s*{{.*?^\s*default\s*=\s*(?P<value>\S+)\s*$',
    re.MULTILINE | re.DOTALL,
)


class DeclaredDeny(enum.Enum):
    """Whether the deployment's variable files declare the inbound Deny enabled."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DeclaredDenyState:
    """A `DeclaredDeny` and the file or reason it was read from."""

    state: DeclaredDeny
    source: str


def declared_source_vpce_deny(infra_dir: Path = INFRA_DIR) -> DeclaredDenyState:
    """Read `runtime_source_vpce_deny_enabled` as the deployment declares it.

    Reads `terraform.tfvars`, then `*.auto.tfvars` in lexical order, the later assignment winning as
    in Terraform. With variable files present but no assignment, the `variables.tf` default applies.
    `UNKNOWN` when no variable file exists on this host or a value is not a literal `true`/`false`.
    A `-var` or `TF_VAR_` override given at apply time is not visible here.
    """
    tfvars = infra_dir / "terraform.tfvars"
    files = ([tfvars] if tfvars.is_file() else []) + sorted(infra_dir.glob("*.auto.tfvars"))
    if not files:
        return DeclaredDenyState(
            DeclaredDeny.UNKNOWN,
            f"no terraform.tfvars or *.auto.tfvars under {infra_dir} on this host",
        )
    declared: DeclaredDenyState | None = None
    for path in files:
        values = [match["value"] for match in _ASSIGNMENT.finditer(path.read_text("utf-8"))]
        if not values:
            continue
        if len(values) > 1 or values[0] not in ("true", "false"):
            return DeclaredDenyState(
                DeclaredDeny.UNKNOWN, f"{path.name} assigns {DENY_VARIABLE} as {values!r}"
            )
        state = DeclaredDeny.ENABLED if values[0] == "true" else DeclaredDeny.DISABLED
        declared = DeclaredDenyState(state, f"{path.name}: {DENY_VARIABLE} = {values[0]}")
    if declared is not None:
        return declared
    variables = infra_dir / "variables.tf"
    match = _DEFAULT.search(variables.read_text("utf-8")) if variables.is_file() else None
    if match is None or match["value"] not in ("true", "false"):
        return DeclaredDenyState(
            DeclaredDeny.UNKNOWN, f"no assignment and no readable {DENY_VARIABLE} default"
        )
    state = DeclaredDeny.ENABLED if match["value"] == "true" else DeclaredDeny.DISABLED
    return DeclaredDenyState(state, f"variables.tf default: {DENY_VARIABLE} = {match['value']}")


def runtime_id_from_arn(runtime_arn: str) -> str:
    """The runtime id `GetAgentRuntime` takes, parsed from the deployed ARN's final path segment.

    The separator is checked rather than the tail taken: `rpartition` on a string with no `/` returns
    the whole string, which would name nothing.
    """
    head, separator, tail = runtime_arn.rpartition("/")
    return tail if separator and head else ""


def copy_platform_version(http_response: Any, parsed: Any, **_: Any) -> None:
    """botocore `after-call` handler: copy `platformVersion` from the raw body into `parsed`.

    A no-op when botocore already parses the member, as the pinned one does; it fills the member
    only under an older botocore whose parser drops it.
    """
    if not isinstance(parsed, dict) or _PLATFORM_VERSION_KEY in parsed:
        return
    try:
        body = json.loads(getattr(http_response, "content", b"") or b"{}")
    except (ValueError, TypeError):
        return
    if isinstance(body, Mapping) and isinstance(body.get(_PLATFORM_VERSION_KEY), str):
        parsed[_PLATFORM_VERSION_KEY] = body[_PLATFORM_VERSION_KEY]


def _register_platform_version_hook(client: Any) -> None:
    """Register :func:`copy_platform_version` on a real botocore client; a fake has no `meta`."""
    events = getattr(getattr(client, "meta", None), "events", None)
    if events is not None:
        events.register(_AFTER_CALL_EVENT, copy_platform_version, unique_id=_HOOK_ID)


@dataclass(frozen=True, slots=True)
class ReadyWait:
    """The outcome of :func:`wait_until_ready`: the verdict, the last runtime read, and why."""

    ready: bool
    runtime: Mapping[str, Any]
    detail: str


def wait_until_ready(
    ctx: SmokeContext,
    runtime_id: str,
    *,
    timeout_seconds: float = READY_WAIT_SECONDS,
    poll_seconds: float = READY_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ReadyWait:
    """Poll the runtime and its `DEFAULT` endpoint until both are READY on the same version.

    Not ready at once on any status outside READY and :data:`TRANSITIONAL_STATUSES` (a `*_FAILED`,
    `DELETING` or missing status), and when `timeout_seconds` elapses first.
    """
    client = ctx.client("bedrock-agentcore-control")
    _register_platform_version_hook(client)
    deadline = clock() + timeout_seconds
    polls = 0
    while True:
        polls += 1
        runtime = client.get_agent_runtime(agentRuntimeId=runtime_id)
        runtime = runtime if isinstance(runtime, Mapping) else {}
        endpoint = client.get_agent_runtime_endpoint(
            agentRuntimeId=runtime_id, endpointName=DEFAULT_ENDPOINT_NAME
        )
        endpoint = endpoint if isinstance(endpoint, Mapping) else {}
        status = runtime.get("status")
        endpoint_status = endpoint.get("status")
        version = runtime.get("agentRuntimeVersion")
        live_version = endpoint.get("liveVersion")
        observed = (
            f"runtime status={status!r} version={version!r}; {DEFAULT_ENDPOINT_NAME} endpoint "
            f"status={endpoint_status!r} liveVersion={live_version!r}; {polls} poll(s)"
        )
        for subject, value in (("runtime", status), ("endpoint", endpoint_status)):
            if value != READY_STATUS and value not in TRANSITIONAL_STATUSES:
                return ReadyWait(
                    False, runtime, f"{subject} is in terminal status {value!r}: {observed}"
                )
        if status == READY_STATUS and endpoint_status == READY_STATUS and live_version == version:
            return ReadyWait(True, runtime, observed)
        if clock() >= deadline:
            return ReadyWait(False, runtime, f"not READY within {timeout_seconds:.0f}s: {observed}")
        sleep(poll_seconds)


def _network_config(response: Mapping[str, Any]) -> Mapping[str, Any]:
    value = response.get("networkConfiguration")
    return value if isinstance(value, Mapping) else {}


def _network_mode_config(response: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _network_config(response).get("networkModeConfig")
    return value if isinstance(value, Mapping) else {}


def _supported_az_row(
    runtime_arn: str, region: str, subnet_ids: Sequence[str], az_ids: Mapping[str, str]
) -> CheckResult:
    """The `runtime_subnets_supported_az` row against `region`'s zones in `infra/regions.json`.

    A region missing from the map, or an unreadable or invalid map, fails.
    """
    try:
        supported = region_map.load().entry(region).agentcore_az_ids
    except region_map.RegionMapError as exc:
        return CheckResult(
            resource=runtime_arn,
            check="runtime_subnets_supported_az",
            status="fail",
            detail=f"no AgentCore-supported AZ set for region {region!r}: {exc}",
        )
    bad = {sid: az_ids.get(sid, "?") for sid in subnet_ids if az_ids.get(sid) not in supported}
    return CheckResult(
        resource=runtime_arn,
        check="runtime_subnets_supported_az",
        status="pass" if not bad else "fail",
        detail=(
            f"all {len(subnet_ids)} Runtime subnet(s) in supported AZs {sorted(supported)} ({region})"
            if not bad
            else f"Runtime subnet(s) in AZs unsupported in {region} {sorted(supported)}: {bad}"
        ),
    )


def _subnet_az_ids(ctx: SmokeContext, subnet_ids: Sequence[str]) -> dict[str, str]:
    """Map each subnet id to its `AvailabilityZoneId` via one describe-subnets call."""
    if not subnet_ids:
        return {}
    response = ctx.client("ec2").describe_subnets(SubnetIds=list(subnet_ids))
    return {
        subnet["SubnetId"]: subnet.get("AvailabilityZoneId", "")
        for subnet in response.get("Subnets", [])
        if isinstance(subnet, Mapping)
    }


def _runtime_sg_id(ctx: SmokeContext) -> str | None:
    """The dedicated Runtime SG's id, discovered by its `Name` tag. None when not found."""
    response = ctx.client("ec2").describe_security_groups(
        Filters=[{"Name": "tag:Name", "Values": [RUNTIME_SG_NAME]}]
    )
    groups = response.get("SecurityGroups", [])
    return groups[0]["GroupId"] if groups and isinstance(groups[0], Mapping) else None


class DenyReadResult(enum.Enum):
    """Outcome of reading a runtime resource policy for the stage-2 aws:SourceVpce Deny.

    The caller maps each to a distinct row.
    """

    PRESENT = "present"  # a Deny statement conditioned on aws:SourceVpce is on the policy
    ABSENT = "absent"  # policy read cleanly; no such Deny
    OP_UNMODELED = "op_unmodeled"  # get_resource_policy is not modeled in the installed botocore
    READ_FAILED = "read_failed"  # the operation is modeled but the read itself failed


def _resource_policy_has_source_vpce_deny(ctx: SmokeContext, resource_arn: str) -> DenyReadResult:
    """Classify the runtime resource policy's stage-2 aws:SourceVpce Deny state.

    `OP_UNMODELED` when the installed botocore does not model
    `bedrock-agentcore-control.get_resource_policy`; the caller defers. `READ_FAILED` is a modeled
    operation whose call failed; the caller reports a failure.
    """
    client = ctx.client("bedrock-agentcore-control")
    # A botocore client raises AttributeError from __getattr__ for an unmodeled operation, so hasattr
    # is False; distinguish that SDK-capability gap from a genuine read failure of a modeled op.
    if not hasattr(client, "get_resource_policy"):
        return DenyReadResult.OP_UNMODELED
    try:
        response = client.get_resource_policy(resourceArn=resource_arn)
    except Exception:  # noqa: BLE001 - a modeled op that failed; surfaced as READ_FAILED, never absent
        return DenyReadResult.READ_FAILED
    policy_text = response.get("policy") if isinstance(response, Mapping) else None
    if not isinstance(policy_text, str):
        return DenyReadResult.READ_FAILED
    try:
        document = json.loads(policy_text)
    except (ValueError, TypeError):
        return DenyReadResult.READ_FAILED
    for statement in document.get("Statement", []):
        if not isinstance(statement, Mapping):
            continue
        if statement.get("Effect") != "Deny":
            continue
        condition = statement.get("Condition", {})
        rendered = json.dumps(condition) if isinstance(condition, Mapping) else ""
        if "aws:SourceVpce" in rendered or "aws:sourcevpce" in rendered.lower():
            return DenyReadResult.PRESENT
    return DenyReadResult.ABSENT


def checks(ctx: SmokeContext) -> Iterator[CheckResult]:  # noqa: C901 - one linear sequence of rows
    """Yield the network-mode, endpoint and egress posture rows and the Deny-state row."""
    try:
        runtime_arn = ctx.outputs.require_str("agentcore_runtime_arn")
    except Exception as exc:  # noqa: BLE001 - a missing output is a failing check, not a crash
        yield CheckResult(
            resource="agentcore_runtime_arn",
            check="terraform_output_present",
            status="fail",
            detail=f"required Terraform output 'agentcore_runtime_arn' is absent ({exc})",
        )
        return

    runtime_id = runtime_id_from_arn(runtime_arn)
    if not runtime_id:
        yield CheckResult(
            resource=runtime_arn,
            check="runtime_id_resolves",
            status="fail",
            detail="the deployed runtime ARN has no final path segment; no runtime id could be parsed",
        )
        return

    try:
        wait = wait_until_ready(ctx, runtime_id)
    except Exception as exc:  # noqa: BLE001 - an unreadable status is a failing check, not a crash
        yield CheckResult(
            resource=runtime_arn,
            check="runtime_ready",
            status="fail",
            detail=f"the runtime or its endpoint status could not be read: {type(exc).__name__}: {exc}",
        )
        return
    yield CheckResult(
        resource=runtime_arn,
        check="runtime_ready",
        status="pass" if wait.ready else "fail",
        detail=wait.detail,
    )
    response = wait.runtime
    missing_guards = [member for member in GUARD_MEMBERS if member not in response]
    guard_ok = not missing_guards
    yield CheckResult(
        resource=runtime_arn,
        check="response_shape_guard",
        status="pass" if guard_ok else "fail",
        detail=(
            f"GetAgentRuntime returned all of {list(GUARD_MEMBERS)}"
            if guard_ok
            else f"GetAgentRuntime is missing {missing_guards}; posture assertions below would be "
            "vacuous, so this is a fail"
        ),
    )
    if not guard_ok:
        return

    platform = response.get(_PLATFORM_VERSION_KEY)
    yield CheckResult(
        resource=runtime_arn,
        check="platform_version_expected",
        status="pass" if platform == EXPECTED_PLATFORM_VERSION else "fail",
        detail=(
            f"platformVersion={platform!r} equals the declared {EXPECTED_PLATFORM_VERSION!r}"
            if platform == EXPECTED_PLATFORM_VERSION
            else f"platformVersion={platform!r} != the declared {EXPECTED_PLATFORM_VERSION!r}"
            + (" (absent from the raw GetAgentRuntime body)" if platform is None else "")
        ),
    )

    # networkMode is VPC.
    mode = _network_config(response).get("networkMode")
    yield CheckResult(
        resource=runtime_arn,
        check="network_mode_vpc",
        status="pass" if mode == "VPC" else "fail",
        detail=f"networkMode={mode!r} (VPC is required; PUBLIC is inbound-reachable by any grant-holder)",
    )

    # The ARN is the deployed one.
    observed_arn = response.get("agentRuntimeArn")
    yield CheckResult(
        resource=runtime_arn,
        check="runtime_arn_unchanged",
        status="pass" if observed_arn == runtime_arn else "fail",
        detail=(
            f"agentRuntimeArn={observed_arn!r} equals the deployed output (in-place update, not a "
            "replace)"
            if observed_arn == runtime_arn
            else f"agentRuntimeArn={observed_arn!r} != output {runtime_arn!r}: the runtime was REPLACED"
        ),
    )

    # Subnets present and in AgentCore-supported AZs.
    subnets = _network_mode_config(response).get("subnets") or []
    subnet_ids = [s for s in subnets if isinstance(s, str)]
    if not subnet_ids:
        yield CheckResult(
            resource=runtime_arn,
            check="runtime_subnets_present",
            status="fail",
            detail="networkModeConfig carries no subnets; VPC mode is not actually configured",
        )
    else:
        yield _supported_az_row(
            runtime_arn, ctx.region, subnet_ids, _subnet_az_ids(ctx, subnet_ids)
        )

    # The security group is the dedicated Runtime SG, not the Lambda SG.
    runtime_sgs = [
        s
        for s in (_network_mode_config(response).get("securityGroups") or [])
        if isinstance(s, str)
    ]
    expected_sg = _runtime_sg_id(ctx)
    if expected_sg is None:
        yield CheckResult(
            resource=RUNTIME_SG_NAME,
            check="runtime_security_group_is_dedicated",
            status="fail",
            detail=f"no security group tagged Name={RUNTIME_SG_NAME!r} found in the account",
        )
    else:
        yield CheckResult(
            resource=runtime_arn,
            check="runtime_security_group_is_dedicated",
            status="pass" if runtime_sgs == [expected_sg] else "fail",
            detail=(
                f"Runtime SG(s)={runtime_sgs} equal the dedicated {RUNTIME_SG_NAME} ({expected_sg})"
                if runtime_sgs == [expected_sg]
                else f"Runtime SG(s)={runtime_sgs} != the dedicated {RUNTIME_SG_NAME} ({expected_sg}); "
                "the Runtime must not share the Lambda group's 0.0.0.0/0 JWKS egress"
            ),
        )
        # The Runtime SG's egress has no 0.0.0.0/0.
        yield from _runtime_sg_egress_checks(ctx, expected_sg)

    # The three interface endpoints are available with private DNS on.
    yield from _endpoint_checks(ctx, expected_sg)

    # Deny state: aws:SourceVpce Deny on both resource policies (defer when absent).
    endpoint_arn = f"{runtime_arn}/runtime-endpoint/DEFAULT"
    for arn in (runtime_arn, endpoint_arn):
        deny_state = _resource_policy_has_source_vpce_deny(ctx, arn)
        if deny_state is DenyReadResult.OP_UNMODELED:
            yield CheckResult(
                resource=arn,
                check="source_vpce_deny_state",
                status="defer",
                defer_reason="get_resource_policy_not_modeled_in_botocore",
                detail="bedrock-agentcore-control.get_resource_policy is not modeled in the installed "
                "botocore, so the Deny state cannot be read here. This is a check-capability "
                "gap, not the Deny being absent. The account-side gate is runtime_inbound_auth's "
                "outside_vpc_invoke_refused_under_declared_deny, which fails when the deployment "
                f"declares {DENY_VARIABLE} = true and the outside-VPC invoke is accepted. Read "
                "out-of-band with `aws bedrock-agentcore-control get-resource-policy` if the row "
                "is needed.",
            )
        elif deny_state is DenyReadResult.READ_FAILED:
            yield CheckResult(
                resource=arn,
                check="source_vpce_deny_state",
                status="fail",
                detail="the resource policy could not be read; the Deny state is unknown, not absent",
            )
        elif deny_state is DenyReadResult.PRESENT:
            yield CheckResult(
                resource=arn,
                check="source_vpce_deny_state",
                status="pass",
                detail="stage-2 aws:SourceVpce Deny present on the resource policy (inbound "
                "boundary authored)",
            )
        else:
            yield CheckResult(
                resource=arn,
                check="source_vpce_deny_state",
                status="defer",
                defer_reason="stage_2_source_vpce_deny_not_applied",
                detail="stage-2 aws:SourceVpce Deny NOT yet applied (default). The inbound boundary is "
                "not enforced yet — never a pass. Apply runtime_source_vpce_deny_enabled=true after "
                "the report-only stage is verified, then re-run",
            )


def _runtime_sg_egress_checks(ctx: SmokeContext, sg_id: str) -> Iterator[CheckResult]:
    """The Runtime SG's egress rules carry no 0.0.0.0/0 or ::/0."""
    response = ctx.client("ec2").describe_security_group_rules(
        Filters=[{"Name": "group-id", "Values": [sg_id]}]
    )
    open_rules = [
        rule
        for rule in response.get("SecurityGroupRules", [])
        if isinstance(rule, Mapping)
        and rule.get("IsEgress")
        and (rule.get("CidrIpv4") == "0.0.0.0/0" or rule.get("CidrIpv6") == "::/0")
    ]
    yield CheckResult(
        resource=sg_id,
        check="runtime_sg_egress_no_open_cidr",
        status="pass" if not open_rules else "fail",
        detail=(
            "Runtime SG egress names only the endpoint group / prefix lists, no 0.0.0.0/0"
            if not open_rules
            else f"Runtime SG has {len(open_rules)} open-CIDR egress rule(s); the Runtime must have no "
            "internet path"
        ),
    )


def _endpoint_sg_admits_runtime(ctx: SmokeContext, endpoint_sg_id: str, runtime_sg_id: str) -> bool:
    """Whether the endpoint SG has an ingress rule admitting the Runtime SG on 443."""
    rules = (
        ctx.client("ec2")
        .describe_security_group_rules(Filters=[{"Name": "group-id", "Values": [endpoint_sg_id]}])
        .get("SecurityGroupRules", [])
    )
    return any(
        isinstance(rule, Mapping)
        and not rule.get("IsEgress")
        and (rule.get("ReferencedGroupInfo") or {}).get("GroupId") == runtime_sg_id
        and int(rule.get("FromPort", -1)) <= 443 <= int(rule.get("ToPort", -1))
        for rule in rules
    )


def _endpoint_group_ids(endpoint: Mapping[str, Any]) -> set[str]:
    """The security-group ids attached to a VPC endpoint's ENIs (`Groups[].GroupId`)."""
    return {
        group["GroupId"]
        for group in endpoint.get("Groups", [])
        if isinstance(group, Mapping) and group.get("GroupId")
    }


def _failed_endpoint_rows(short: str, detail: str) -> Iterator[CheckResult]:
    for check in ("d2_interface_endpoint_available", "d2_endpoint_reachable_from_runtime_sg"):
        yield CheckResult(resource=short, check=check, status="fail", detail=detail)


def _endpoint_checks(ctx: SmokeContext, runtime_sg_id: str | None) -> Iterator[CheckResult]:
    """In the stack's VPC, each of the three interface endpoints is available with private DNS and
    reachable from the Runtime: its ENIs sit on the shared endpoints SG, and that SG admits the
    Runtime SG on 443."""
    region = ctx.region
    wanted = {
        f"com.amazonaws.{region}.{service}": service for service in D2_INTERFACE_ENDPOINT_SERVICES
    }
    vpc_id = ctx.outputs.get("vpc_id")
    if not isinstance(vpc_id, str) or not vpc_id.startswith("vpc-"):
        for short in sorted(wanted.values()):
            yield from _failed_endpoint_rows(
                short,
                f"the stack's VPC id is unresolvable (Terraform output 'vpc_id' = {vpc_id!r}), so no "
                "endpoint can be attributed to this stack",
            )
        return
    response = ctx.client("ec2").describe_vpc_endpoints(
        Filters=[
            {"Name": "service-name", "Values": list(wanted)},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )
    by_service: dict[str, list[Mapping[str, Any]]] = {}
    for endpoint in response.get("VpcEndpoints", []):
        if isinstance(endpoint, Mapping) and endpoint.get("VpcId") == vpc_id:
            by_service.setdefault(str(endpoint.get("ServiceName")), []).append(endpoint)

    # A None runtime SG or a missing endpoint SG makes every reachability verdict a fail.
    endpoint_sg_id: str | None = None
    admits_runtime = False
    if runtime_sg_id is not None:
        groups = (
            ctx.client("ec2")
            .describe_security_groups(
                Filters=[
                    {"Name": "tag:Name", "Values": [ENDPOINT_SG_NAME]},
                    {"Name": "vpc-id", "Values": [vpc_id]},
                ]
            )
            .get("SecurityGroups", [])
        )
        endpoint_sg_id = groups[0]["GroupId"] if groups and isinstance(groups[0], Mapping) else None
        if endpoint_sg_id is not None:
            admits_runtime = _endpoint_sg_admits_runtime(ctx, endpoint_sg_id, runtime_sg_id)

    for service_name, short in sorted(wanted.items()):
        endpoints = by_service.get(service_name, [])
        if len(endpoints) > 1:
            ids = sorted(str(e.get("VpcEndpointId")) for e in endpoints)
            yield from _failed_endpoint_rows(
                short,
                f"{len(endpoints)} endpoints for {service_name} in {vpc_id} ({ids}); exactly one is "
                "expected, so none is graded",
            )
            continue
        if not endpoints:
            yield CheckResult(
                resource=short,
                check="d2_interface_endpoint_available",
                status="fail",
                detail=f"no VPC endpoint for {service_name} in {vpc_id}; the Runtime's egress to it "
                "has no route",
            )
            continue
        endpoint = endpoints[0]
        state = endpoint.get("State")
        private_dns = endpoint.get("PrivateDnsEnabled")
        available = state == "available" and private_dns is True
        yield CheckResult(
            resource=short,
            check="d2_interface_endpoint_available",
            status="pass" if available else "fail",
            detail=f"state={state!r} privateDnsEnabled={private_dns!r} (expect available + true)",
        )

        # Reachability: the endpoint's ENIs must be on the shared endpoints SG, and that SG must admit
        # the Runtime SG on 443.
        on_endpoint_sg = endpoint_sg_id is not None and endpoint_sg_id in _endpoint_group_ids(
            endpoint
        )
        reachable = on_endpoint_sg and admits_runtime
        yield CheckResult(
            resource=short,
            check="d2_endpoint_reachable_from_runtime_sg",
            status="pass" if reachable else "fail",
            detail=(
                f"endpoint ENIs are on the shared endpoints SG {endpoint_sg_id}, which admits the "
                f"Runtime SG {runtime_sg_id} on 443"
                if reachable
                else f"NOT reachable from the Runtime: on_endpoint_sg={on_endpoint_sg} "
                f"(endpoint SG={endpoint_sg_id}), endpoint_sg_admits_runtime_443={admits_runtime} "
                f"(Runtime SG={runtime_sg_id}). The Runtime's egress to this endpoint would be refused"
            ),
        )


def expected_check_count(ctx: SmokeContext) -> ExpectedCheckCount:
    """At least the shape guard plus the network-mode row once the runtime is addressable, else 1."""
    try:
        addressable = bool(runtime_id_from_arn(ctx.outputs.require_str("agentcore_runtime_arn")))
    except Exception as exc:  # noqa: BLE001 - the reason belongs in the basis
        return ExpectedCheckCount(
            minimum=1,
            basis=f"the runtime ARN output could not be read ({type(exc).__name__}); `checks` emits its "
            "single terraform_output_present failure and returns",
        )
    if not addressable:
        return ExpectedCheckCount(
            minimum=1,
            basis="the runtime ARN has no path segment; `checks` emits its single runtime_id_resolves "
            "failure and returns",
        )
    return ExpectedCheckCount(
        minimum=2,
        basis="the runtime is addressable, so the READY poll runs and at least the runtime_ready and "
        "response_shape_guard rows are emitted",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the Runtime network-posture checks and return the process exit code."""
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

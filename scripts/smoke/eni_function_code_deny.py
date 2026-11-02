# SPDX-License-Identifier: MIT
"""Layer 4 T3 smoke: function code is denied the Hyperplane ENI actions; Lambda itself is not.

Checks `DenyEniActionsToFunctionCode` on the deployed roles:

1. Per deployed `crm-agent-eval-*` function: `State=Active` and `LastUpdateStatus=Successful`.
2. Per distinct execution role of those functions, `iam simulate-principal-policy` on `*`:
   - no context: the six ENI actions and the two X-Ray write actions are `allowed`;
   - with `lambda:SourceFunctionArn` set to that function's ARN: the six ENI actions are
     `explicitDeny`, and the X-Ray actions are still `allowed`.
3. `stale_probes_absent`: probes tagged `smoke-run` by an interrupted earlier run are deleted.
4. A live probe: a run-scoped function `crm-agent-eval-eniprobe-<run>` on one existing action role,
   with that function's subnets and security groups (refused if either is empty). The probe reaching
   `Active` with that same `VpcConfig` proves the service's ENI path accepts a role carrying the
   Deny. The handler calls `ec2:DescribeSubnets` and a dry-run `ec2:DeleteNetworkInterface`. Both
   must return `UnauthorizedOperation` in the payload, and `FunctionError` must be absent. Deletion
   is attempted in a `finally` whenever creation was attempted, and SIGTERM is turned into
   `SystemExit` so it runs. Absence is then confirmed with `get_function`.

Lambda's asynchronous ENI work (rebalancing, idle reclaim, delete on destroy) cannot be exercised
here. It rests on AWS's statement that service calls carry no `lambda:SourceFunctionArn`.
"""

from __future__ import annotations

import io
import json
import signal
import sys
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, Final

from scripts.smoke.harness import ExpectedCheckCount, SmokeContext, error_code, run_main

from eval.verification import CheckResult

LAYER: Final[int] = 4
RESOURCE_CLASS: Final[str] = "eni_function_code_deny"

FUNCTION_PREFIX: Final[str] = "crm-agent-eval-"
PROBE_PREFIX: Final[str] = f"{FUNCTION_PREFIX}eniprobe-"

#: The action role the probe borrows; it has no reach beyond its own log group and its table grants.
PROBE_SOURCE_FUNCTION: Final[str] = f"{FUNCTION_PREFIX}capabilities"

ENI_ACTIONS: Final[tuple[str, ...]] = (
    "ec2:CreateNetworkInterface",
    "ec2:DescribeNetworkInterfaces",
    "ec2:DescribeSubnets",
    "ec2:DeleteNetworkInterface",
    "ec2:AssignPrivateIpAddresses",
    "ec2:UnassignPrivateIpAddresses",
)
XRAY_ACTIONS: Final[tuple[str, ...]] = ("xray:PutTraceSegments", "xray:PutTelemetryRecords")

#: Tag every probe carries; only tagged functions are reaped.
PROBE_TAG: Final[str] = "smoke-run"

#: The error both probe calls must return.
EXPECTED_PROBE_ERROR: Final[str] = "UnauthorizedOperation"

ACTIVE_TIMEOUT_SECONDS: Final[float] = 300.0
_POLL_SECONDS: Final[float] = 5.0

#: The probe's handler. Its ENI id is built at runtime, so no literal id appears in tracked content.
PROBE_SOURCE: Final[str] = """
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_EC2 = boto3.client("ec2", config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}))


def _outcome(call):
    try:
        call()
    except ClientError as exc:
        return exc.response["Error"]["Code"]
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__
    return "Succeeded"


def handler(event, context):
    eni_id = "eni-" + "0" * 17
    return {
        "describe_subnets": _outcome(lambda: _EC2.describe_subnets(MaxResults=5)),
        "delete_network_interface": _outcome(
            lambda: _EC2.delete_network_interface(NetworkInterfaceId=eni_id, DryRun=True)
        ),
    }
"""


def _result(resource: str, check: str, ok: bool, detail: str) -> CheckResult:
    return CheckResult(
        resource=resource, check=check, status="pass" if ok else "fail", detail=detail
    )


def _deployed_functions(lambda_client: Any) -> list[dict[str, Any]]:
    """Every deployed function named `crm-agent-eval-*`, probes excluded, sorted by name."""
    functions: list[dict[str, Any]] = []
    marker: str | None = None
    while True:
        page = lambda_client.list_functions(**({"Marker": marker} if marker else {}))
        functions.extend(
            f
            for f in page.get("Functions", [])
            if str(f.get("FunctionName", "")).startswith(FUNCTION_PREFIX)
            and not str(f.get("FunctionName", "")).startswith(PROBE_PREFIX)
        )
        marker = page.get("NextMarker")
        if not marker:
            return sorted(functions, key=lambda f: str(f["FunctionName"]))


def check_lifecycle(lambda_client: Any, function_name: str) -> CheckResult:
    config = lambda_client.get_function_configuration(FunctionName=function_name)
    state, update = config.get("State"), config.get("LastUpdateStatus")
    return _result(
        function_name,
        "function_lifecycle_healthy",
        state == "Active" and update == "Successful",
        f"State={state!r}, LastUpdateStatus={update!r}",
    )


def _decisions(
    iam_client: Any, role_arn: str, context: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    kwargs: dict[str, Any] = {
        "PolicySourceArn": role_arn,
        "ActionNames": [*ENI_ACTIONS, *XRAY_ACTIONS],
        "ResourceArns": ["*"],
    }
    if context:
        kwargs["ContextEntries"] = list(context)
    results = iam_client.simulate_principal_policy(**kwargs)["EvaluationResults"]
    return {str(r["EvalActionName"]): str(r["EvalDecision"]) for r in results}


def check_role_simulation(
    iam_client: Any, *, role_arn: str, function_arn: str
) -> Iterator[CheckResult]:
    """The service path is allowed everything; function code is denied ENI actions only."""
    service = _decisions(iam_client, role_arn, ())
    wrong = {a: d for a, d in service.items() if d != "allowed"}
    yield _result(
        role_arn,
        "service_path_allowed_eni_and_xray",
        not wrong and len(service) == len(ENI_ACTIONS) + len(XRAY_ACTIONS),
        f"without lambda:SourceFunctionArn: {service}",
    )

    code = _decisions(
        iam_client,
        role_arn,
        [
            {
                "ContextKeyName": "lambda:SourceFunctionArn",
                "ContextKeyValues": [function_arn],
                "ContextKeyType": "string",
            }
        ],
    )
    expected = {
        **dict.fromkeys(ENI_ACTIONS, "explicitDeny"),
        **dict.fromkeys(XRAY_ACTIONS, "allowed"),
    }
    yield _result(
        role_arn,
        "function_code_denied_eni_not_xray",
        code == expected,
        f"with lambda:SourceFunctionArn={function_arn}: {code} (expected {expected})",
    )


def _probe_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("probe.py", PROBE_SOURCE)
    return buffer.getvalue()


def _wait_active(
    lambda_client: Any,
    name: str,
    *,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    deadline = clock() + ACTIVE_TIMEOUT_SECONDS
    config: dict[str, Any] = {}
    while clock() < deadline:
        config = lambda_client.get_function_configuration(FunctionName=name)
        if config.get("State") in ("Active", "Failed"):
            return config
        sleeper(_POLL_SECONDS)
    return config


def _function_is_gone(lambda_client: Any, name: str) -> bool:
    try:
        lambda_client.get_function(FunctionName=name)
    except Exception as exc:  # noqa: BLE001 - only the not-found code means absent
        return error_code(exc) == "ResourceNotFoundException"
    return False


def reap_stale_probes(lambda_client: Any) -> CheckResult:
    """Delete probes an interrupted earlier run left behind; only those this module tagged."""
    stale: list[dict[str, Any]] = []
    marker: str | None = None
    while True:
        page = lambda_client.list_functions(**({"Marker": marker} if marker else {}))
        stale.extend(
            f
            for f in page.get("Functions", [])
            if str(f.get("FunctionName", "")).startswith(PROBE_PREFIX)
        )
        marker = page.get("NextMarker")
        if not marker:
            break
    reaped: list[str] = []
    refused: list[str] = []
    for function in stale:
        name = str(function["FunctionName"])
        tags = lambda_client.list_tags(Resource=function["FunctionArn"]).get("Tags") or {}
        if PROBE_TAG not in tags:
            refused.append(name)
            continue
        _delete_if_present(lambda_client, name)
        reaped.append(name)
    left = [name for name in reaped if not _function_is_gone(lambda_client, name)]
    ok = not refused and not left
    return _result(
        RESOURCE_CLASS,
        "stale_probes_absent",
        ok,
        f"reaped {reaped or 'none'}"
        + (f"; still present after delete: {left}" if left else "")
        + (
            f"; not tagged {PROBE_TAG!r}, so not deleted: {refused}. Check what created them"
            if refused
            else ""
        ),
    )


def _delete_if_present(lambda_client: Any, name: str) -> None:
    """Delete `name`; a function that is already gone counts as deleted."""
    try:
        lambda_client.delete_function(FunctionName=name)
    except Exception as exc:
        if error_code(exc) != "ResourceNotFoundException":
            sys.stderr.write(f"{RESOURCE_CLASS}: delete_function({name}) raised {exc}\n")


def check_live_probe(
    lambda_client: Any,
    *,
    source_config: Mapping[str, Any],
    run_id: str,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> Iterator[CheckResult]:
    """Create, invoke and delete the probe; yield created, denied, and torn-down rows."""
    name = f"{PROBE_PREFIX}{run_id}"
    vpc = source_config.get("VpcConfig") or {}
    subnets = sorted(vpc.get("SubnetIds") or [])
    groups = sorted(vpc.get("SecurityGroupIds") or [])
    if not subnets or not groups:
        yield _result(
            name,
            "probe_created_active",
            False,
            f"{PROBE_SOURCE_FUNCTION} has SubnetIds={subnets} SecurityGroupIds={groups}; refusing to "
            "create a probe outside a VPC, which would exercise no ENI path",
        )
        return
    attempted = False
    try:
        try:
            attempted = True
            lambda_client.create_function(
                FunctionName=name,
                Runtime="python3.13",
                Role=source_config["Role"],
                Handler="probe.handler",
                Code={"ZipFile": _probe_zip()},
                Timeout=30,
                MemorySize=128,
                VpcConfig={"SubnetIds": subnets, "SecurityGroupIds": groups},
                Description="Run-scoped smoke probe for the ENI function-code Deny; deleted by the run.",
                Tags={"data-classification": "internal", PROBE_TAG: run_id},
            )
        except Exception as exc:  # noqa: BLE001 - reported as the failing row
            yield _result(name, "probe_created_active", False, f"create_function failed: {exc}")
            return
        config = _wait_active(lambda_client, name, clock=clock, sleeper=sleeper)
        probe_vpc = config.get("VpcConfig") or {}
        in_vpc = (
            sorted(probe_vpc.get("SubnetIds") or []) == subnets
            and sorted(probe_vpc.get("SecurityGroupIds") or []) == groups
        )
        active = config.get("State") == "Active" and in_vpc
        yield _result(
            name,
            "probe_created_active",
            active,
            f"State={config.get('State')!r} StateReasonCode={config.get('StateReasonCode')!r} "
            f"on role {source_config['Role']}; VpcConfig matches {PROBE_SOURCE_FUNCTION}'s: {in_vpc}",
        )
        if not active:
            return

        response = lambda_client.invoke(FunctionName=name, InvocationType="RequestResponse")
        function_error = response.get("FunctionError")
        raw = response["Payload"].read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        outcomes = payload if isinstance(payload, dict) else {}
        ok = function_error is None and all(
            outcomes.get(call) == EXPECTED_PROBE_ERROR
            for call in ("describe_subnets", "delete_network_interface")
        )
        yield _result(
            name,
            "probe_code_denied_eni_actions",
            ok,
            f"FunctionError={function_error!r}, outcomes={outcomes or raw[:200]!r} "
            f"(expected {EXPECTED_PROBE_ERROR} for both)",
        )
    finally:
        if attempted:
            _delete_if_present(lambda_client, name)
        gone = _function_is_gone(lambda_client, name)
        yield _result(
            name,
            "probe_torn_down",
            gone,
            "get_function returns ResourceNotFoundException"
            if gone
            else f"{name} still exists; delete it by hand (aws lambda delete-function)",
        )


def checks(
    ctx: SmokeContext,
    *,
    run_id: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> Iterator[CheckResult]:
    lambda_client = ctx.client("lambda")
    iam_client = ctx.client("iam")
    functions = _deployed_functions(lambda_client)
    yield _result(
        RESOURCE_CLASS,
        "function_set_non_empty",
        bool(functions),
        f"{len(functions)} deployed function(s) named {FUNCTION_PREFIX}*",
    )
    simulated: set[str] = set()
    for function in functions:
        yield check_lifecycle(lambda_client, str(function["FunctionName"]))
        role = str(function["Role"])
        if role in simulated:
            continue
        simulated.add(role)
        yield from check_role_simulation(
            iam_client, role_arn=role, function_arn=str(function["FunctionArn"])
        )

    yield reap_stale_probes(lambda_client)
    source = lambda_client.get_function_configuration(FunctionName=PROBE_SOURCE_FUNCTION)
    yield from check_live_probe(
        lambda_client,
        source_config=source,
        run_id=run_id or uuid.uuid4().hex[:12],
        clock=clock,
        sleeper=sleeper,
    )


def expected_check_count(ctx: SmokeContext) -> ExpectedCheckCount:
    """1 + one lifecycle row per function + two rows per distinct role + four probe rows."""
    functions = _deployed_functions(ctx.client("lambda"))
    roles = {str(f["Role"]) for f in functions}
    return ExpectedCheckCount(
        minimum=1 + len(functions) + 2 * len(roles) + 4,
        basis=f"{len(functions)} function(s), {len(roles)} distinct role(s), 4 probe rows, 1 floor",
    )


def _exit_on_sigterm(signum: int, frame: object) -> None:
    """Turn SIGTERM into `SystemExit`, so the probe's `finally` teardown runs."""
    del frame
    raise SystemExit(128 + signum)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the ENI function-code Deny checks and return the process exit code."""
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
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

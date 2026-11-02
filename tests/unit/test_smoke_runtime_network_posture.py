# SPDX-License-Identifier: MIT
"""Tests for `scripts.smoke.runtime_network_posture`, against fake control-plane and EC2 clients.

Each test flips one input and asserts the matching row's disposition; the SourceVpce Deny rows
`defer` (never `pass`) while the Deny is absent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from scripts import region_map
from scripts.smoke.harness import CheckResult, context_for_test
from scripts.smoke.runtime_network_posture import (
    DEFAULT_ENDPOINT_NAME,
    DENY_VARIABLE,
    EXPECTED_PLATFORM_VERSION,
    DeclaredDeny,
    checks,
    copy_platform_version,
    declared_source_vpce_deny,
    runtime_id_from_arn,
    wait_until_ready,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/crm_agent_eval_runtime-abc123"
)
_ENDPOINT_ARN = f"{_RUNTIME_ARN}/runtime-endpoint/DEFAULT"
_VPC_ID = "vpc-stackfixture"
_FOREIGN_VPC_ID = "vpc-foreignfixture"
_OUTPUTS = {"agentcore_runtime_arn": _RUNTIME_ARN, "vpc_id": _VPC_ID}
_RUNTIME_SG_ID = "sg-runtimefixture"
_SUBNET_ID = "subnet-runtimefixture"

_COMPLIANT_RUNTIME: dict[str, Any] = {
    "agentRuntimeArn": _RUNTIME_ARN,
    "agentRuntimeId": "crm_agent_eval_runtime-abc123",
    "roleArn": "arn:aws:iam::123456789012:role/crm-agent-eval-runtime-role",
    "networkConfiguration": {
        "networkMode": "VPC",
        "networkModeConfig": {"subnets": [_SUBNET_ID], "securityGroups": [_RUNTIME_SG_ID]},
    },
    "status": "READY",
    "agentRuntimeVersion": "39",
    "platformVersion": "V2",
}

_READY_ENDPOINT: dict[str, Any] = {"status": "READY", "liveVersion": "39"}

_ALLOW_ONLY_POLICY = (
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":"arn:...:role/relay"},'
    '"Action":"bedrock-agentcore:InvokeAgentRuntime","Resource":"*"}]}'
)
_DENY_POLICY = (
    '{"Version":"2012-10-17","Statement":['
    '{"Effect":"Allow","Principal":{"AWS":"arn:...:role/relay"},"Action":"bedrock-agentcore:InvokeAgentRuntime","Resource":"*"},'
    '{"Effect":"Deny","Principal":"*","Action":"bedrock-agentcore:InvokeAgentRuntime","Resource":"*",'
    '"Condition":{"StringNotEquals":{"aws:SourceVpce":"vpce-abc"},"Bool":{"aws:ViaAWSService":"false"}}}]}'
)


class _FakeControlPlane:
    """Serves `runtimes` / `endpoints` in order, repeating the last; records the endpoint names."""

    def __init__(
        self,
        runtime: dict[str, Any],
        policy: str,
        *,
        runtimes: list[dict[str, Any]] | None = None,
        endpoints: list[dict[str, Any]] | None = None,
    ) -> None:
        self._runtimes = runtimes if runtimes is not None else [runtime]
        self._endpoints = endpoints if endpoints is not None else [_READY_ENDPOINT]
        self._policy = policy
        self.runtime_reads = 0
        self.endpoint_names: list[str] = []

    def get_agent_runtime(self, **_: Any) -> dict[str, Any]:
        self.runtime_reads += 1
        return self._runtimes[min(self.runtime_reads, len(self._runtimes)) - 1]

    def get_agent_runtime_endpoint(self, *, endpointName: str, **_: Any) -> dict[str, Any]:  # noqa: N803
        self.endpoint_names.append(endpointName)
        return self._endpoints[min(len(self.endpoint_names), len(self._endpoints)) - 1]

    def get_resource_policy(self, **_: Any) -> dict[str, Any]:
        return {"policy": self._policy}


_ENDPOINT_SG_ID = "sg-endpointfixture"


class _FakeEc2:
    def __init__(
        self,
        *,
        az: str = "use1-az2",
        sg_found: bool = True,
        open_egress: bool = False,
        endpoints_ok: bool = True,
        endpoint_ingress_from_runtime: bool = True,
        endpoints_on_endpoint_sg: bool = True,
        foreign_endpoints: bool = False,
        duplicate_in_vpc: str = "",
    ) -> None:
        self._az = az
        self._sg_found = sg_found
        self._open_egress = open_egress
        self._endpoints_ok = endpoints_ok
        self._endpoint_ingress_from_runtime = endpoint_ingress_from_runtime
        self._endpoints_on_endpoint_sg = endpoints_on_endpoint_sg
        self._foreign_endpoints = foreign_endpoints
        self._duplicate_in_vpc = duplicate_in_vpc
        self.endpoint_filters: list[list[dict[str, Any]]] = []

    def describe_subnets(self, **_: Any) -> dict[str, Any]:
        return {"Subnets": [{"SubnetId": _SUBNET_ID, "AvailabilityZoneId": self._az}]}

    def describe_security_groups(self, **kwargs: Any) -> dict[str, Any]:
        # Distinguish the two lookups by the Name tag the caller filters on.
        filters = kwargs.get("Filters") or []
        wanted = filters[0]["Values"][0] if filters else ""
        if "vpc-endpoints" in wanted:
            return {"SecurityGroups": [{"GroupId": _ENDPOINT_SG_ID}]}
        return {"SecurityGroups": [{"GroupId": _RUNTIME_SG_ID}] if self._sg_found else []}

    def describe_security_group_rules(self, **kwargs: Any) -> dict[str, Any]:
        filters = kwargs.get("Filters") or []
        group = filters[0]["Values"][0] if filters else ""
        if group == _ENDPOINT_SG_ID:
            rules: list[dict[str, Any]] = []
            if self._endpoint_ingress_from_runtime:
                rules.append(
                    {
                        "IsEgress": False,
                        "ReferencedGroupInfo": {"GroupId": _RUNTIME_SG_ID},
                        "FromPort": 443,
                        "ToPort": 443,
                    }
                )
            return {"SecurityGroupRules": rules}
        egress: list[dict[str, Any]] = [{"IsEgress": True, "CidrIpv4": "10.0.0.0/16"}]
        if self._open_egress:
            egress.append({"IsEgress": True, "CidrIpv4": "0.0.0.0/0"})
        return {"SecurityGroupRules": egress}

    def describe_vpc_endpoints(self, **kwargs: Any) -> dict[str, Any]:
        """Honours the `vpc-id` filter; a foreign VPC's broken endpoints follow the stack's own."""
        filters = kwargs.get("Filters") or []
        self.endpoint_filters.append(filters)
        vpc_filter = [f["Values"] for f in filters if f["Name"] == "vpc-id"]
        state = "available" if self._endpoints_ok else "pending"
        groups = [{"GroupId": _ENDPOINT_SG_ID}] if self._endpoints_on_endpoint_sg else []
        services = ("bedrock-runtime", "ecr.api", "ecr.dkr")
        endpoints = [
            {
                "VpcEndpointId": f"vpce-{svc.replace('.', '')}",
                "VpcId": _VPC_ID,
                "ServiceName": f"com.amazonaws.us-east-1.{svc}",
                "State": state,
                "PrivateDnsEnabled": self._endpoints_ok,
                "Groups": groups,
            }
            for svc in services
        ]
        if self._duplicate_in_vpc:
            endpoints.append(
                {
                    **endpoints[0],
                    "ServiceName": f"com.amazonaws.us-east-1.{self._duplicate_in_vpc}",
                    "VpcEndpointId": "vpce-duplicate",
                }
            )
        if self._foreign_endpoints:
            endpoints += [
                {
                    "VpcEndpointId": f"vpce-foreign{svc.replace('.', '')}",
                    "VpcId": _FOREIGN_VPC_ID,
                    "ServiceName": f"com.amazonaws.us-east-1.{svc}",
                    "State": "available",
                    "PrivateDnsEnabled": True,
                    "Groups": [{"GroupId": "sg-foreignfixture"}],
                }
                for svc in services
            ]
        if vpc_filter:
            endpoints = [e for e in endpoints if e["VpcId"] in vpc_filter[0]]
        return {"VpcEndpoints": endpoints}


def _run(
    *,
    runtime: dict[str, Any] | None = None,
    policy: str = _ALLOW_ONLY_POLICY,
    ec2: _FakeEc2 | None = None,
    outputs: dict[str, Any] | None = None,
    region: str = "us-east-1",
) -> dict[str, CheckResult]:
    ctx = context_for_test(
        region=region,
        outputs=_OUTPUTS if outputs is None else outputs,
        clients={
            "bedrock-agentcore-control": _FakeControlPlane(
                _COMPLIANT_RUNTIME if runtime is None else runtime, policy
            ),
            "ec2": _FakeEc2() if ec2 is None else ec2,
        },
    )
    return {f"{r.resource}|{r.check}": r for r in checks(ctx)}


def _statuses(results: dict[str, CheckResult], check: str) -> list[str]:
    return [r.status for key, r in results.items() if key.endswith(f"|{check}")]


def test_runtime_id_parses_from_arn() -> None:
    assert runtime_id_from_arn(_RUNTIME_ARN) == "crm_agent_eval_runtime-abc123"
    assert runtime_id_from_arn("arn:aws:...:runtime") == ""  # no path segment


def test_compliant_stage_one_posture() -> None:
    """A compliant stage-one posture passes every row, and the absent Deny's rows defer."""
    r = _run()
    assert _statuses(r, "network_mode_vpc") == ["pass"]
    assert _statuses(r, "runtime_arn_unchanged") == ["pass"]
    assert _statuses(r, "runtime_subnets_supported_az") == ["pass"]
    assert _statuses(r, "runtime_security_group_is_dedicated") == ["pass"]
    assert _statuses(r, "runtime_sg_egress_no_open_cidr") == ["pass"]
    assert _statuses(r, "d2_interface_endpoint_available") == ["pass", "pass", "pass"]
    assert _statuses(r, "d2_endpoint_reachable_from_runtime_sg") == ["pass", "pass", "pass"]
    # Two policies (runtime + endpoint), both defer while the Deny is absent.
    assert _statuses(r, "source_vpce_deny_state") == ["defer", "defer"]


def test_public_network_mode_fails() -> None:
    runtime = {**_COMPLIANT_RUNTIME, "networkConfiguration": {"networkMode": "PUBLIC"}}
    r = _run(runtime=runtime)
    assert _statuses(r, "network_mode_vpc") == ["fail"]


def test_missing_guard_member_fails_and_returns() -> None:
    """An absent guard member makes the whole posture unverifiable — fail, and no further rows."""
    runtime = {k: v for k, v in _COMPLIANT_RUNTIME.items() if k != "roleArn"}
    r = _run(runtime=runtime)
    assert _statuses(r, "response_shape_guard") == ["fail"]
    assert "network_mode_vpc" not in {key.split("|", 1)[1] for key in r}


def test_replaced_runtime_arn_fails() -> None:
    runtime = {**_COMPLIANT_RUNTIME, "agentRuntimeArn": _RUNTIME_ARN + "-REPLACED"}
    r = _run(runtime=runtime)
    assert _statuses(r, "runtime_arn_unchanged") == ["fail"]


def test_unsupported_az_fails() -> None:
    r = _run(ec2=_FakeEc2(az="use1-az6"))
    assert _statuses(r, "runtime_subnets_supported_az") == ["fail"]


@pytest.mark.parametrize("az", ["use1-az1", "use1-az2", "use1-az4"])
def test_every_us_east_1_supported_az_passes(az: str) -> None:
    r = _run(ec2=_FakeEc2(az=az))
    assert _statuses(r, "runtime_subnets_supported_az") == ["pass"]


def test_a_us_west_2_subnet_in_a_mapped_zone_passes() -> None:
    r = _run(ec2=_FakeEc2(az="usw2-az1"), region="us-west-2")
    assert _statuses(r, "runtime_subnets_supported_az") == ["pass"]


@pytest.mark.parametrize("az", ["usw2-az4", "use1-az1"])
def test_a_us_west_2_subnet_outside_its_mapped_zones_fails(az: str) -> None:
    r = _run(ec2=_FakeEc2(az=az), region="us-west-2")
    assert _statuses(r, "runtime_subnets_supported_az") == ["fail"]


@pytest.mark.parametrize("region", ["eu-west-1", "us-west-3"])
def test_a_region_missing_from_the_map_fails_naming_the_map(region: str) -> None:
    r = _run(region=region)
    (row,) = [v for k, v in r.items() if k.endswith("|runtime_subnets_supported_az")]
    assert row.status == "fail"
    assert row.defer_reason == ""
    assert "infra/regions.json" in row.detail and region in row.detail


def _synthetic_map(path: Path, regions: dict[str, Any]) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": {
                    "agentcore_vpc_zones_url": "https://docs.aws.amazon.com/a",
                    "agentcore_regions_url": "https://docs.aws.amazon.com/b",
                    "checked": "2026-10-22",
                },
                "regions": regions,
            }
        ),
        encoding="utf-8",
    )
    return path


def _entry(zones: list[str]) -> dict[str, Any]:
    return {
        "agentcore_az_ids": zones,
        "bootstrap": {
            "private_az_ids": zones[:2],
            "runtime_az_ids": zones[:2],
            "nat_az_id": zones[0],
        },
    }


def test_the_supported_set_is_read_from_the_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A map that narrows us-east-1 to az1/az4 refuses a use1-az2 subnet the real map accepts."""
    synthetic = _synthetic_map(
        tmp_path / "regions.json", {"us-east-1": _entry(["use1-az1", "use1-az4"])}
    )
    assert _statuses(_run(), "runtime_subnets_supported_az") == ["pass"]
    monkeypatch.setattr(region_map, "MAP_PATH", synthetic)
    assert _statuses(_run(), "runtime_subnets_supported_az") == ["fail"]
    assert _statuses(_run(ec2=_FakeEc2(az="use1-az4")), "runtime_subnets_supported_az") == ["pass"]


def test_a_mapless_deployed_region_fails_against_a_synthetic_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synthetic = _synthetic_map(
        tmp_path / "regions.json", {"us-east-1": _entry(["use1-az1", "use1-az2"])}
    )
    monkeypatch.setattr(region_map, "MAP_PATH", synthetic)
    r = _run(ec2=_FakeEc2(az="usw2-az1"), region="us-west-2")
    assert _statuses(r, "runtime_subnets_supported_az") == ["fail"]


@pytest.mark.parametrize("content", ["{not json", '{"schema_version": 2}'])
def test_an_invalid_or_unreadable_map_fails_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    bad = tmp_path / "regions.json"
    bad.write_text(content, encoding="utf-8")
    monkeypatch.setattr(region_map, "MAP_PATH", bad)
    r = _run()
    assert _statuses(r, "runtime_subnets_supported_az") == ["fail"]
    monkeypatch.setattr(region_map, "MAP_PATH", tmp_path / "absent.json")
    assert _statuses(_run(), "runtime_subnets_supported_az") == ["fail"]


def test_wrong_security_group_fails() -> None:
    runtime = {
        **_COMPLIANT_RUNTIME,
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {
                "subnets": [_SUBNET_ID],
                "securityGroups": ["sg-somethingelse00"],
            },
        },
    }
    r = _run(runtime=runtime)
    assert _statuses(r, "runtime_security_group_is_dedicated") == ["fail"]


def test_open_egress_fails() -> None:
    r = _run(ec2=_FakeEc2(open_egress=True))
    assert _statuses(r, "runtime_sg_egress_no_open_cidr") == ["fail"]


def test_unavailable_endpoint_fails() -> None:
    r = _run(ec2=_FakeEc2(endpoints_ok=False))
    assert _statuses(r, "d2_interface_endpoint_available") == ["fail", "fail", "fail"]


def test_endpoint_available_but_sg_denies_runtime_fails() -> None:
    """An available endpoint whose SG does not admit the Runtime SG on 443 fails reachability."""
    r = _run(ec2=_FakeEc2(endpoint_ingress_from_runtime=False))
    assert _statuses(r, "d2_interface_endpoint_available") == ["pass", "pass", "pass"]
    assert _statuses(r, "d2_endpoint_reachable_from_runtime_sg") == ["fail", "fail", "fail"]


def test_endpoint_not_on_shared_endpoint_sg_fails() -> None:
    """An endpoint whose ENIs are not on the shared endpoints SG fails reachability."""
    r = _run(ec2=_FakeEc2(endpoints_on_endpoint_sg=False))
    assert _statuses(r, "d2_endpoint_reachable_from_runtime_sg") == ["fail", "fail", "fail"]


def test_a_foreign_vpc_endpoint_is_ignored() -> None:
    """Another VPC's endpoints for the same services are neither graded nor counted."""
    ec2 = _FakeEc2(foreign_endpoints=True)
    r = _run(ec2=ec2)
    assert _statuses(r, "d2_interface_endpoint_available") == ["pass", "pass", "pass"]
    assert _statuses(r, "d2_endpoint_reachable_from_runtime_sg") == ["pass", "pass", "pass"]
    assert {"Name": "vpc-id", "Values": [_VPC_ID]} in ec2.endpoint_filters[0]


def test_a_foreign_vpc_endpoint_is_ignored_even_if_the_filter_is_not_applied() -> None:
    """The `VpcId` of each returned endpoint is re-checked, so an unfiltered answer is safe too."""

    class _Unfiltered(_FakeEc2):
        def describe_vpc_endpoints(self, **_: Any) -> dict[str, Any]:
            return super().describe_vpc_endpoints(Filters=[])

    r = _run(ec2=_Unfiltered(foreign_endpoints=True))
    assert _statuses(r, "d2_endpoint_reachable_from_runtime_sg") == ["pass", "pass", "pass"]


def test_two_endpoints_for_one_service_in_the_vpc_fail_that_service() -> None:
    r = _run(ec2=_FakeEc2(duplicate_in_vpc="ecr.api"))
    rows = {k: v for k, v in r.items() if k.startswith("ecr.api|d2_")}
    assert {v.status for v in rows.values()} == {"fail"} and len(rows) == 2
    assert all("2 endpoints" in v.detail for v in rows.values())
    assert _statuses(r, "d2_interface_endpoint_available").count("pass") == 2


@pytest.mark.parametrize("vpc_id", [None, "", "not-a-vpc"])
def test_an_unresolvable_vpc_id_fails_every_endpoint_row(vpc_id: str | None) -> None:
    outputs: dict[str, Any] = {"agentcore_runtime_arn": _RUNTIME_ARN}
    if vpc_id is not None:
        outputs["vpc_id"] = vpc_id
    ec2 = _FakeEc2()
    r = _run(outputs=outputs, ec2=ec2)
    assert _statuses(r, "d2_interface_endpoint_available") == ["fail", "fail", "fail"]
    assert _statuses(r, "d2_endpoint_reachable_from_runtime_sg") == ["fail", "fail", "fail"]
    assert ec2.endpoint_filters == [], "no endpoint may be read without the stack's VPC id"


def test_deny_present_passes_the_deny_state() -> None:
    r = _run(policy=_DENY_POLICY)
    assert _statuses(r, "source_vpce_deny_state") == ["pass", "pass"]


class _ControlPlaneWithoutResourcePolicyOp:
    """A control-plane client with `get_agent_runtime` but no `get_resource_policy` operation."""

    def __init__(self, runtime: dict[str, Any]) -> None:
        self._runtime = runtime

    def get_agent_runtime(self, **_: Any) -> dict[str, Any]:
        return self._runtime

    def get_agent_runtime_endpoint(self, **_: Any) -> dict[str, Any]:
        return _READY_ENDPOINT


class _ControlPlaneResourcePolicyRaises:
    """get_resource_policy is modeled but the call fails (permission/network) — a real read failure."""

    def __init__(self, runtime: dict[str, Any]) -> None:
        self._runtime = runtime

    def get_agent_runtime(self, **_: Any) -> dict[str, Any]:
        return self._runtime

    def get_agent_runtime_endpoint(self, **_: Any) -> dict[str, Any]:
        return _READY_ENDPOINT

    def get_resource_policy(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("AccessDeniedException: not authorized to GetResourcePolicy")


def test_deny_state_defers_when_get_resource_policy_is_not_modeled() -> None:
    """An unmodeled `get_resource_policy` defers the Deny rows with a named reason, never fails."""
    ctx = context_for_test(
        outputs=_OUTPUTS,
        clients={
            "bedrock-agentcore-control": _ControlPlaneWithoutResourcePolicyOp(_COMPLIANT_RUNTIME),
            "ec2": _FakeEc2(),
        },
    )
    rows = [r for r in checks(ctx) if r.check == "source_vpce_deny_state"]
    assert [r.status for r in rows] == ["defer", "defer"]
    assert all(r.defer_reason == "get_resource_policy_not_modeled_in_botocore" for r in rows)


def test_deny_state_fails_when_a_modeled_read_fails() -> None:
    """A modeled `get_resource_policy` call that raises fails the Deny rows, not defers them."""
    ctx = context_for_test(
        outputs=_OUTPUTS,
        clients={
            "bedrock-agentcore-control": _ControlPlaneResourcePolicyRaises(_COMPLIANT_RUNTIME),
            "ec2": _FakeEc2(),
        },
    )
    assert [r.status for r in checks(ctx) if r.check == "source_vpce_deny_state"] == [
        "fail",
        "fail",
    ]


def test_missing_output_fails() -> None:
    r = _run(outputs={})
    assert _statuses(r, "terraform_output_present") == ["fail"]


def test_the_unmodeled_deny_row_names_the_account_gate_rather_than_pointing_back() -> None:
    """The defer names agent_stream's row and the condition under which that row fails."""
    ctx = context_for_test(
        outputs=_OUTPUTS,
        clients={
            "bedrock-agentcore-control": _ControlPlaneWithoutResourcePolicyOp(_COMPLIANT_RUNTIME),
            "ec2": _FakeEc2(),
        },
    )
    for row in (r for r in checks(ctx) if r.check == "source_vpce_deny_state"):
        assert "outside_vpc_invoke_refused_under_declared_deny" in row.detail
        assert f"{DENY_VARIABLE} = true" in row.detail
        assert "authoritative" not in row.detail


# ------------------------------------------------------------------------------------------------
# declared_source_vpce_deny: the Deny state the deployment declares
# ------------------------------------------------------------------------------------------------

_VARIABLES_DEFAULT_FALSE = (
    f'variable "{DENY_VARIABLE}" {{\n'
    "  description = <<-EOT\n    Defaults to false.\n  EOT\n"
    "  type        = bool\n  default     = false\n}\n"
)


def _infra(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("tfvars", "state"),
    [
        (f"{DENY_VARIABLE} = true\n", DeclaredDeny.ENABLED),
        (f"{DENY_VARIABLE} = false\n", DeclaredDeny.DISABLED),
        (f'vpc_id = "x"\n{DENY_VARIABLE}   =   true   # stage 2\n', DeclaredDeny.ENABLED),
        (f"# {DENY_VARIABLE} = true\n{DENY_VARIABLE} = false\n", DeclaredDeny.DISABLED),
        (f"{DENY_VARIABLE} = var.other\n", DeclaredDeny.UNKNOWN),
        (f"{DENY_VARIABLE} = true\n{DENY_VARIABLE} = false\n", DeclaredDeny.UNKNOWN),
    ],
)
def test_the_tfvars_assignment_is_the_declared_state(
    tmp_path: Path, tfvars: str, state: DeclaredDeny
) -> None:
    infra = _infra(tmp_path, {"terraform.tfvars": tfvars, "variables.tf": _VARIABLES_DEFAULT_FALSE})
    assert declared_source_vpce_deny(infra).state is state


def test_an_auto_tfvars_assignment_overrides_terraform_tfvars(tmp_path: Path) -> None:
    infra = _infra(
        tmp_path,
        {
            "terraform.tfvars": f"{DENY_VARIABLE} = false\n",
            "stage2.auto.tfvars": f"{DENY_VARIABLE} = true\n",
        },
    )
    declared = declared_source_vpce_deny(infra)
    assert declared.state is DeclaredDeny.ENABLED
    assert "stage2.auto.tfvars" in declared.source


def test_no_variable_file_on_this_host_is_unknown(tmp_path: Path) -> None:
    infra = _infra(tmp_path, {"variables.tf": _VARIABLES_DEFAULT_FALSE})
    assert declared_source_vpce_deny(infra).state is DeclaredDeny.UNKNOWN


def test_an_unassigned_variable_takes_the_real_variables_tf_default(tmp_path: Path) -> None:
    """Contract with `infra/variables.tf`: its default is what an unassigned deployment declares."""
    real = (Path(__file__).resolve().parents[2] / "infra" / "variables.tf").read_text("utf-8")
    infra = _infra(tmp_path, {"terraform.tfvars": 'vpc_id = "x"\n', "variables.tf": real})
    declared = declared_source_vpce_deny(infra)
    assert declared.state is DeclaredDeny.DISABLED
    assert "variables.tf default" in declared.source


def test_an_unreadable_default_is_unknown(tmp_path: Path) -> None:
    infra = _infra(tmp_path, {"terraform.tfvars": 'vpc_id = "x"\n', "variables.tf": "# nothing\n"})
    assert declared_source_vpce_deny(infra).state is DeclaredDeny.UNKNOWN


# ---------------------------------------------------------------------------
# READY wait and platform version.
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _wait(plane: _FakeControlPlane, *, timeout: float = 60.0) -> tuple[Any, _FakeClock]:
    clock = _FakeClock()
    ctx = context_for_test(outputs=_OUTPUTS, clients={"bedrock-agentcore-control": plane})
    result = wait_until_ready(
        ctx,
        "crm_agent_eval_runtime-abc123",
        timeout_seconds=timeout,
        poll_seconds=10.0,
        sleep=clock.sleep,
        clock=clock,
    )
    return result, clock


def test_a_ready_runtime_and_endpoint_pass_without_sleeping() -> None:
    plane = _FakeControlPlane(_COMPLIANT_RUNTIME, _ALLOW_ONLY_POLICY)
    result, clock = _wait(plane)
    assert result.ready is True
    assert clock.sleeps == []
    assert plane.endpoint_names == [DEFAULT_ENDPOINT_NAME]


def test_an_updating_runtime_is_polled_until_ready() -> None:
    updating = {**_COMPLIANT_RUNTIME, "status": "UPDATING"}
    plane = _FakeControlPlane(
        _COMPLIANT_RUNTIME,
        _ALLOW_ONLY_POLICY,
        runtimes=[updating, updating, _COMPLIANT_RUNTIME],
    )
    result, clock = _wait(plane)
    assert result.ready is True
    assert plane.runtime_reads == 3
    assert clock.sleeps == [10.0, 10.0]


def test_an_endpoint_still_on_the_previous_version_is_polled() -> None:
    lagging = {"status": "UPDATING", "liveVersion": "38", "targetVersion": "39"}
    ready_on_old = {"status": "READY", "liveVersion": "38"}
    plane = _FakeControlPlane(
        _COMPLIANT_RUNTIME,
        _ALLOW_ONLY_POLICY,
        endpoints=[lagging, ready_on_old, _READY_ENDPOINT],
    )
    result, _ = _wait(plane)
    assert result.ready is True
    assert len(plane.endpoint_names) == 3


@pytest.mark.parametrize(
    ("runtime_status", "endpoint_status"),
    [
        ("UPDATE_FAILED", "READY"),
        ("CREATE_FAILED", "READY"),
        ("READY", "UPDATE_FAILED"),
        ("DELETING", "READY"),
    ],
)
def test_a_terminal_status_fails_at_once(runtime_status: str, endpoint_status: str) -> None:
    plane = _FakeControlPlane(
        {**_COMPLIANT_RUNTIME, "status": runtime_status},
        _ALLOW_ONLY_POLICY,
        endpoints=[{**_READY_ENDPOINT, "status": endpoint_status}],
    )
    result, clock = _wait(plane)
    assert result.ready is False
    assert clock.sleeps == []
    assert "terminal status" in result.detail


def test_a_runtime_that_never_becomes_ready_times_out() -> None:
    plane = _FakeControlPlane({**_COMPLIANT_RUNTIME, "status": "UPDATING"}, _ALLOW_ONLY_POLICY)
    result, clock = _wait(plane, timeout=30.0)
    assert result.ready is False
    assert "not READY within 30s" in result.detail
    assert sum(clock.sleeps) == 30.0


def test_the_ready_and_platform_rows_pass_on_a_ready_v2_runtime() -> None:
    r = _run()
    assert _statuses(r, "runtime_ready") == ["pass"]
    assert _statuses(r, "platform_version_expected") == ["pass"]


def test_a_failed_update_fails_the_ready_row_and_still_reads_the_posture() -> None:
    r = _run(runtime={**_COMPLIANT_RUNTIME, "status": "UPDATE_FAILED"})
    assert _statuses(r, "runtime_ready") == ["fail"]
    assert _statuses(r, "network_mode_vpc") == ["pass"]


@pytest.mark.parametrize("platform", ["V1", None])
def test_a_v1_or_unreported_platform_version_fails(platform: str | None) -> None:
    runtime = {k: v for k, v in _COMPLIANT_RUNTIME.items() if k != "platformVersion"}
    if platform is not None:
        runtime["platformVersion"] = platform
    r = _run(runtime=runtime)
    assert _statuses(r, "platform_version_expected") == ["fail"]


def test_an_unreadable_status_is_one_failing_row() -> None:
    class _Raising(_FakeControlPlane):
        def get_agent_runtime_endpoint(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDeniedException")

    ctx = context_for_test(
        outputs=_OUTPUTS,
        clients={
            "bedrock-agentcore-control": _Raising(_COMPLIANT_RUNTIME, _ALLOW_ONLY_POLICY),
            "ec2": _FakeEc2(),
        },
    )
    rows = list(checks(ctx))
    assert [(r.check, r.status) for r in rows] == [("runtime_ready", "fail")]


class _RawResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self.content = json.dumps(body).encode()


def test_the_hook_copies_platform_version_the_parser_dropped() -> None:
    parsed: dict[str, Any] = {"status": "READY"}
    copy_platform_version(_RawResponse({"status": "READY", "platformVersion": "V2"}), parsed)
    assert parsed["platformVersion"] == "V2"


def test_the_hook_leaves_a_parsed_member_and_an_absent_raw_member_alone() -> None:
    parsed: dict[str, Any] = {"platformVersion": "V1"}
    copy_platform_version(_RawResponse({"platformVersion": "V2"}), parsed)
    assert parsed["platformVersion"] == "V1"
    bare: dict[str, Any] = {}
    copy_platform_version(_RawResponse({"status": "READY"}), bare)
    assert "platformVersion" not in bare


def test_the_expected_platform_version_is_the_module_variable_default() -> None:
    text = (_REPO_ROOT / "infra" / "modules" / "agentcore" / "variables.tf").read_text("utf-8")
    match = re.search(
        r'variable\s+"platform_version"\s*\{.*?^\s*default\s*=\s*"(?P<value>[^"]+)"',
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    assert match["value"] == EXPECTED_PLATFORM_VERSION == "V2"

# SPDX-License-Identifier: MIT
"""Offline tests for `scripts.residue.compute_network`, using `botocore.stub.Stubber`.

Each family is driven through real-shaped, model-validated responses: a project item is found,
a non-project item is ignored, results span two pages, and AccessDenied becomes `Unmeasurable`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from scripts.residue import compute_network
from scripts.residue.core import (
    VPC_ORIGINS_SERVICE_SG,
    FamilyScanner,
    Resource,
    ScanConfig,
    Unmeasurable,
    arn_resource_type,
)

REGION = "us-east-1"
OWNER = "123456789012"
CONFIG = ScanConfig(region=REGION)

EC2_OPS = (
    ("describe_security_groups", "SecurityGroups"),
    ("describe_network_interfaces", "NetworkInterfaces"),
    ("describe_vpc_endpoints", "VpcEndpoints"),
    ("describe_nat_gateways", "NatGateways"),
    ("describe_addresses", "Addresses"),
    ("describe_flow_logs", "FlowLogs"),
)


class StubbedClients:
    """A `ClientFactory` handing out one stubbed client per service."""

    def __init__(self) -> None:
        self._session = boto3.session.Session(
            aws_access_key_id="testing",
            aws_secret_access_key="testing",  # nosec B106  # fake moto/stub credentials, not a credential
            region_name=REGION,
        )
        self.clients: dict[str, Any] = {}
        self.stubbers: dict[str, Stubber] = {}

    def stub(self, service: str) -> Stubber:
        if service not in self.stubbers:
            client = self._session.client(service)
            self.clients[service] = client
            self.stubbers[service] = Stubber(client)
            self.stubbers[service].activate()
        return self.stubbers[service]

    def __call__(self, service: str, region: str) -> Any:
        assert region == REGION
        self.stub(service)
        return self.clients[service]

    def assert_drained(self) -> None:
        for stubber in self.stubbers.values():
            stubber.assert_no_pending_responses()


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> Iterator[StubbedClients]:
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    stubbed = StubbedClients()
    yield stubbed
    for stubber in stubbed.stubbers.values():
        stubber.deactivate()


@dataclass(frozen=True)
class PagedOp:
    """One list operation of a family: its stub shape and a project/other item pair."""

    family: str
    service: str
    operation: str
    result_key: str
    token_out: str
    token_in: str
    project_item: Any
    other_item: Any
    expected: tuple[str, str]
    base_params: dict[str, Any] = field(default_factory=dict)
    preceding: tuple[tuple[str, str], ...] = ()
    following: tuple[tuple[str, str], ...] = ()
    per_item: tuple[tuple[str, dict[str, Any], dict[str, Any]], ...] = ()


def _fn(name: str) -> dict[str, Any]:
    return {"FunctionName": name, "FunctionArn": f"arn:aws:lambda:{REGION}:{OWNER}:function:{name}"}


def _lb(name: str) -> dict[str, Any]:
    arn = f"arn:aws:elasticloadbalancing:{REGION}:{OWNER}:loadbalancer/app/{name}/abc"
    return {"LoadBalancerName": name, "LoadBalancerArn": arn, "Type": "application"}


def _tg(name: str) -> dict[str, Any]:
    arn = f"arn:aws:elasticloadbalancing:{REGION}:{OWNER}:targetgroup/{name}/abc"
    return {"TargetGroupName": name, "TargetGroupArn": arn}


def _sg(name: str, group_id: str, tags: dict[str, str] | None = None) -> dict[str, Any]:
    sg: dict[str, Any] = {"GroupName": name, "GroupId": group_id, "OwnerId": OWNER}
    if tags:
        sg["Tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]
    return sg


def _tags(**tags: str) -> list[dict[str, str]]:
    return [{"Key": k, "Value": v} for k, v in tags.items()]


PROJECT_TAGS = _tags(service="crm-agent-eval")
OTHER_TAGS = _tags(service="someone-else", Name="other")

PAGED_OPS: tuple[PagedOp, ...] = (
    PagedOp(
        "lambda",
        "lambda",
        "list_functions",
        "Functions",
        "NextMarker",
        "Marker",
        _fn("crm-agent-eval-create-account"),
        _fn("other-app-fn"),
        ("function", "crm-agent-eval-create-account"),
    ),
    PagedOp(
        "codebuild",
        "codebuild",
        "list_projects",
        "projects",
        "nextToken",
        "nextToken",
        "crm-agent-eval-agent-image",
        "other-project",
        ("project", "crm-agent-eval-agent-image"),
    ),
    PagedOp(
        "apigateway",
        "apigateway",
        "get_rest_apis",
        "items",
        "position",
        "position",
        {"id": "a1", "name": "crm-agent-eval-api"},
        {"id": "a2", "name": "other-api"},
        ("rest-api", "crm-agent-eval-api"),
        following=(("apigatewayv2", "get_apis"),),
    ),
    PagedOp(
        "apigateway",
        "apigatewayv2",
        "get_apis",
        "Items",
        "NextToken",
        "NextToken",
        {
            "ApiId": "b1",
            "Name": "unprefixed-http",
            "ProtocolType": "HTTP",
            "RouteSelectionExpression": "$request.method $request.path",
            "Tags": {"service": "crm-agent-eval"},
        },
        {
            "ApiId": "b2",
            "Name": "other-http",
            "ProtocolType": "HTTP",
            "RouteSelectionExpression": "$request.method $request.path",
        },
        ("api-v2", "unprefixed-http"),
        preceding=(("apigateway", "get_rest_apis"),),
    ),
    PagedOp(
        "elbv2",
        "elbv2",
        "describe_load_balancers",
        "LoadBalancers",
        "NextMarker",
        "Marker",
        _lb("crm-agent-eval-api-origin"),
        _lb("other-alb"),
        ("load-balancer", "crm-agent-eval-api-origin"),
        following=(("elbv2", "describe_target_groups"),),
    ),
    PagedOp(
        "elbv2",
        "elbv2",
        "describe_target_groups",
        "TargetGroups",
        "NextMarker",
        "Marker",
        _tg("crm-agent-eval-api-origin"),
        _tg("other-tg"),
        ("target-group", "crm-agent-eval-api-origin"),
        preceding=(("elbv2", "describe_load_balancers"),),
    ),
    PagedOp(
        "ec2",
        "ec2",
        "describe_security_groups",
        "SecurityGroups",
        "NextToken",
        "NextToken",
        _sg("crm-agent-eval-lambda", "sg-0001"),
        _sg("other-sg", "sg-0002"),
        ("security-group", "crm-agent-eval-lambda"),
        following=tuple(("ec2", op) for op, _ in EC2_OPS[1:]),
    ),
    PagedOp(
        "ec2",
        "ec2",
        "describe_vpc_endpoints",
        "VpcEndpoints",
        "NextToken",
        "NextToken",
        {"VpcEndpointId": "vpce-0001", "State": "available", "Tags": PROJECT_TAGS},
        {"VpcEndpointId": "vpce-0002", "State": "available", "Tags": OTHER_TAGS},
        ("vpc-endpoint", "vpce-0001"),
        preceding=tuple(("ec2", op) for op, _ in EC2_OPS[:2]),
        following=tuple(("ec2", op) for op, _ in EC2_OPS[3:]),
    ),
    PagedOp(
        "ec2",
        "ec2",
        "describe_nat_gateways",
        "NatGateways",
        "NextToken",
        "NextToken",
        {"NatGatewayId": "nat-0001", "State": "available", "Tags": PROJECT_TAGS},
        {"NatGatewayId": "nat-0002", "State": "available", "Tags": OTHER_TAGS},
        ("nat-gateway", "nat-0001"),
        preceding=tuple(("ec2", op) for op, _ in EC2_OPS[:3]),
        following=tuple(("ec2", op) for op, _ in EC2_OPS[4:]),
    ),
    PagedOp(
        "ec2",
        "ec2",
        "describe_flow_logs",
        "FlowLogs",
        "NextToken",
        "NextToken",
        {"FlowLogId": "fl-0001", "LogGroupName": "/aws/vpc/crm-agent-eval-flow-logs"},
        {"FlowLogId": "fl-0002", "LogGroupName": "/aws/vpc/other"},
        ("flow-log", "fl-0001"),
        preceding=tuple(("ec2", op) for op, _ in EC2_OPS[:5]),
    ),
    PagedOp(
        "cognito",
        "cognito-idp",
        "list_user_pools",
        "UserPools",
        "NextToken",
        "NextToken",
        {"Id": "us-east-1_aaa", "Name": "crm-agent-eval-users"},
        {"Id": "us-east-1_bbb", "Name": "other-pool"},
        ("user-pool", "crm-agent-eval-users"),
        base_params={"MaxResults": 60},
        per_item=(
            (
                "describe_user_pool",
                {
                    "UserPool": {
                        "Id": "us-east-1_aaa",
                        "Name": "crm-agent-eval-users",
                        "Arn": f"arn:aws:cognito-idp:{REGION}:{OWNER}:userpool/us-east-1_aaa",
                    }
                },
                {"UserPoolId": "us-east-1_aaa"},
            ),
        ),
    ),
    PagedOp(
        "stepfunctions",
        "stepfunctions",
        "list_state_machines",
        "stateMachines",
        "nextToken",
        "nextToken",
        {
            "name": "crm-agent-eval-eval-sweep",
            "stateMachineArn": f"arn:aws:states:{REGION}:{OWNER}:stateMachine:crm-agent-eval-eval-sweep",
            "type": "STANDARD",
            "creationDate": "2026-10-01T00:00:00Z",
        },
        {
            "name": "other-sm",
            "stateMachineArn": f"arn:aws:states:{REGION}:{OWNER}:stateMachine:other-sm",
            "type": "STANDARD",
            "creationDate": "2026-10-01T00:00:00Z",
        },
        ("state-machine", "crm-agent-eval-eval-sweep"),
    ),
    PagedOp(
        "agentcore",
        "bedrock-agentcore-control",
        "list_agent_runtimes",
        "agentRuntimes",
        "nextToken",
        "nextToken",
        {
            "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{REGION}:{OWNER}:runtime/crm_agent_eval_runtime-x",
            "agentRuntimeId": "crm_agent_eval_runtime-x",
            "agentRuntimeVersion": "1",
            "description": "runtime",
            "agentRuntimeName": "crm_agent_eval_runtime",
            "lastUpdatedAt": "2026-10-01T00:00:00Z",
            "status": "READY",
        },
        {
            "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{REGION}:{OWNER}:runtime/other_rt-y",
            "agentRuntimeId": "other_rt-y",
            "agentRuntimeVersion": "1",
            "description": "runtime",
            "agentRuntimeName": "other_rt",
            "lastUpdatedAt": "2026-10-01T00:00:00Z",
            "status": "READY",
        },
        ("agent-runtime", "crm_agent_eval_runtime"),
        following=(
            ("bedrock-agentcore-control", "list_gateways"),
            ("bedrock-agentcore-control", "list_workload_identities"),
        ),
    ),
    PagedOp(
        "agentcore",
        "bedrock-agentcore-control",
        "list_gateways",
        "items",
        "nextToken",
        "nextToken",
        {
            "gatewayId": "crm-agent-eval-gateway-abc",
            "name": "crm-agent-eval-gateway",
            "status": "READY",
            "createdAt": "2026-10-01T00:00:00Z",
            "updatedAt": "2026-10-01T00:00:00Z",
            "authorizerType": "CUSTOM_JWT",
            "protocolType": "MCP",
        },
        {
            "gatewayId": "other-gw-abc",
            "name": "other-gw",
            "status": "READY",
            "createdAt": "2026-10-01T00:00:00Z",
            "updatedAt": "2026-10-01T00:00:00Z",
            "authorizerType": "CUSTOM_JWT",
            "protocolType": "MCP",
        },
        ("gateway", "crm-agent-eval-gateway"),
        preceding=(("bedrock-agentcore-control", "list_agent_runtimes"),),
        per_item=(
            (
                "get_gateway",
                {
                    "gatewayArn": f"arn:aws:bedrock-agentcore:{REGION}:{OWNER}:gateway/crm-agent-eval-gateway-abc",
                    "gatewayId": "crm-agent-eval-gateway-abc",
                    "name": "crm-agent-eval-gateway",
                    "status": "READY",
                    "createdAt": "2026-10-01T00:00:00Z",
                    "updatedAt": "2026-10-01T00:00:00Z",
                    "authorizerType": "CUSTOM_JWT",
                    "protocolType": "MCP",
                },
                {"gatewayIdentifier": "crm-agent-eval-gateway-abc"},
            ),
        ),
        following=(("bedrock-agentcore-control", "list_workload_identities"),),
    ),
    PagedOp(
        "agentcore",
        "bedrock-agentcore-control",
        "list_workload_identities",
        "workloadIdentities",
        "nextToken",
        "nextToken",
        {
            "name": "crm_agent_eval_runtime-wi",
            "workloadIdentityArn": f"arn:aws:bedrock-agentcore:{REGION}:{OWNER}:workload-identity-directory/default/workload-identity/crm_agent_eval_runtime-wi",
        },
        {
            "name": "other-wi",
            "workloadIdentityArn": f"arn:aws:bedrock-agentcore:{REGION}:{OWNER}:workload-identity-directory/default/workload-identity/other-wi",
        },
        ("workload-identity", "crm_agent_eval_runtime-wi"),
        preceding=(
            ("bedrock-agentcore-control", "list_agent_runtimes"),
            ("bedrock-agentcore-control", "list_gateways"),
        ),
    ),
)

_RESULT_KEYS: dict[tuple[str, str], str] = {
    (p.service, p.operation): p.result_key for p in PAGED_OPS
}
_RESULT_KEYS.update({("ec2", op): key for op, key in EC2_OPS})
_BASE_PARAMS: dict[tuple[str, str], dict[str, Any]] = {
    ("cognito-idp", "list_user_pools"): {"MaxResults": 60},
}


def _stub_empty(clients: StubbedClients, ops: tuple[tuple[str, str], ...]) -> None:
    for service, operation in ops:
        clients.stub(service).add_response(
            operation,
            {_RESULT_KEYS[(service, operation)]: []},
            dict(_BASE_PARAMS.get((service, operation), {})),
        )


def _scanner(family: str) -> FamilyScanner:
    return compute_network.SCANNERS[family]


def _names(resources: list[Resource]) -> list[tuple[str, str]]:
    return [(r.kind, r.name) for r in resources]


def test_scanners_registers_exactly_the_compute_network_families() -> None:
    assert set(compute_network.SCANNERS) == {
        "lambda",
        "codebuild",
        "apigateway",
        "elbv2",
        "ec2",
        "cognito",
        "stepfunctions",
        "agentcore",
    }


def test_botocore_ships_the_agentcore_control_service_model() -> None:
    assert "bedrock-agentcore-control" in boto3.session.Session().get_available_services()


@pytest.mark.parametrize("op", PAGED_OPS, ids=lambda p: f"{p.family}-{p.operation}")
def test_project_item_found_and_other_ignored_across_two_pages(
    clients: StubbedClients, op: PagedOp
) -> None:
    _stub_empty(clients, op.preceding)
    stubber = clients.stub(op.service)
    stubber.add_response(
        op.operation,
        {op.result_key: [op.other_item], op.token_out: "page-2"},
        dict(op.base_params),
    )
    stubber.add_response(
        op.operation,
        {op.result_key: [op.project_item]},
        {**op.base_params, op.token_in: "page-2"},
    )
    for operation, response, params in op.per_item:
        stubber.add_response(operation, response, params)
    _stub_empty(clients, op.following)

    found = _scanner(op.family)(clients, CONFIG)

    assert [(r.family, r.kind, r.name) for r in found] == [(op.family, *op.expected)]
    assert found[0].region == REGION
    clients.assert_drained()


@pytest.mark.parametrize("op", PAGED_OPS, ids=lambda p: f"{p.family}-{p.operation}")
def test_returned_arn_type_is_enumerated_for_its_family(
    clients: StubbedClients, op: PagedOp
) -> None:
    _stub_empty(clients, op.preceding)
    stubber = clients.stub(op.service)
    stubber.add_response(op.operation, {op.result_key: [op.project_item]}, dict(op.base_params))
    for operation, response, params in op.per_item:
        stubber.add_response(operation, response, params)
    _stub_empty(clients, op.following)

    [resource] = _scanner(op.family)(clients, CONFIG)

    if resource.arn is not None:
        enumerated = compute_network.ENUMERATED_ARN_TYPES[op.family]
        assert arn_resource_type(resource.arn) in enumerated


def test_enumerated_arn_types_are_pinned() -> None:
    assert {
        "lambda": frozenset({"function"}),
        "codebuild": frozenset({"project"}),
        "apigateway": frozenset({"restapis", "apis"}),
        "elbv2": frozenset({"loadbalancer", "targetgroup"}),
        "ec2": frozenset(
            {
                "security-group",
                "network-interface",
                "vpc-endpoint",
                "natgateway",
                "elastic-ip",
                "vpc-flow-log",
            }
        ),
        "cognito": frozenset({"userpool"}),
        "stepfunctions": frozenset({"stateMachine"}),
        "agentcore": frozenset({"runtime", "gateway", "workload-identity-directory"}),
    } == compute_network.ENUMERATED_ARN_TYPES
    assert set(compute_network.ENUMERATED_ARN_TYPES) == set(compute_network.SCANNERS)


FIRST_OPS: dict[str, tuple[str, str]] = {
    "lambda": ("lambda", "list_functions"),
    "codebuild": ("codebuild", "list_projects"),
    "apigateway": ("apigateway", "get_rest_apis"),
    "elbv2": ("elbv2", "describe_load_balancers"),
    "ec2": ("ec2", "describe_security_groups"),
    "cognito": ("cognito-idp", "list_user_pools"),
    "stepfunctions": ("stepfunctions", "list_state_machines"),
    "agentcore": ("bedrock-agentcore-control", "list_agent_runtimes"),
}


@pytest.mark.parametrize("family", sorted(FIRST_OPS))
def test_access_denied_makes_the_family_unmeasurable(clients: StubbedClients, family: str) -> None:
    service, operation = FIRST_OPS[family]
    clients.stub(service).add_client_error(
        operation, service_error_code="AccessDenied", service_message="denied", http_status_code=403
    )

    with pytest.raises(Unmeasurable) as caught:
        _scanner(family)(clients, CONFIG)

    assert caught.value.family == family
    assert caught.value.operation == operation
    assert caught.value.code == "AccessDenied"


def test_access_denied_on_a_later_ec2_call_is_not_reported_as_empty(
    clients: StubbedClients,
) -> None:
    _stub_empty(clients, (("ec2", "describe_security_groups"),))
    clients.stub("ec2").add_client_error(
        "describe_network_interfaces", service_error_code="AccessDenied", http_status_code=403
    )

    with pytest.raises(Unmeasurable) as caught:
        compute_network.scan_ec2(clients, CONFIG)

    assert (caught.value.family, caught.value.code) == ("ec2", "AccessDenied")


def test_eniprobe_function_is_residue(clients: StubbedClients) -> None:
    clients.stub("lambda").add_response(
        "list_functions", {"Functions": [_fn("crm-agent-eval-eniprobe-x")]}, {}
    )

    found = compute_network.scan_lambda(clients, CONFIG)

    assert _names(found) == [("function", "crm-agent-eval-eniprobe-x")]
    assert found[0].arn == _fn("crm-agent-eval-eniprobe-x")["FunctionArn"]


def _run_ec2(clients: StubbedClients, responses: dict[str, list[dict[str, Any]]]) -> list[Resource]:
    stubber = clients.stub("ec2")
    for operation, key in EC2_OPS:
        stubber.add_response(operation, {key: responses.get(operation, [])}, {})
    found = compute_network.scan_ec2(clients, CONFIG)
    clients.assert_drained()
    return found


def test_ec2_security_groups_project_and_vpc_origins_only(clients: StubbedClients) -> None:
    found = _run_ec2(
        clients,
        {
            "describe_security_groups": [
                _sg("default", "sg-def", {"service": "crm-agent-eval"}),
                _sg("crm-agent-eval-bootstrap", "sg-boot"),
                _sg("unprefixed-but-tagged", "sg-tag", {"service": "crm-agent-eval"}),
                _sg(VPC_ORIGINS_SERVICE_SG, "sg-vpco"),
                _sg("other-sg", "sg-other"),
            ]
        },
    )

    assert _names(found) == [
        ("security-group", "crm-agent-eval-bootstrap"),
        ("security-group", "unprefixed-but-tagged"),
        ("security-group", VPC_ORIGINS_SERVICE_SG),
    ]
    assert found[0].arn == f"arn:aws:ec2:{REGION}:{OWNER}:security-group/sg-boot"
    assert found[0].detail["group_id"] == "sg-boot"


def test_ec2_network_interfaces_by_project_group_description_or_tag(
    clients: StubbedClients,
) -> None:
    found = _run_ec2(
        clients,
        {
            "describe_security_groups": [
                _sg("crm-agent-eval-lambda", "sg-proj"),
                _sg(VPC_ORIGINS_SERVICE_SG, "sg-vpco"),
            ],
            "describe_network_interfaces": [
                {"NetworkInterfaceId": "eni-bygroup", "Groups": [{"GroupId": "sg-proj"}]},
                {
                    "NetworkInterfaceId": "eni-bydesc",
                    "Description": "AWS Lambda VPC ENI-crm-agent-eval-eniprobe-x-1234",
                    "Groups": [{"GroupId": "sg-unrelated"}],
                },
                {"NetworkInterfaceId": "eni-bytag", "TagSet": PROJECT_TAGS},
                {"NetworkInterfaceId": "eni-vpco", "Groups": [{"GroupId": "sg-vpco"}]},
                {"NetworkInterfaceId": "eni-other", "Description": "someone else's"},
            ],
        },
    )

    enis = [name for kind, name in _names(found) if kind == "network-interface"]
    assert enis == ["eni-bygroup", "eni-bydesc", "eni-bytag"]


def test_ec2_skips_deleted_endpoints_and_nat_gateways(clients: StubbedClients) -> None:
    found = _run_ec2(
        clients,
        {
            "describe_vpc_endpoints": [
                {"VpcEndpointId": "vpce-gone", "State": "Deleted", "Tags": PROJECT_TAGS}
            ],
            "describe_nat_gateways": [
                {"NatGatewayId": "nat-gone", "State": "deleted", "Tags": PROJECT_TAGS}
            ],
        },
    )

    assert found == []


@pytest.mark.parametrize("state", ["pending", "available", "deleting", "failed"])
def test_ec2_nat_gateway_kept_in_every_state_but_deleted(
    clients: StubbedClients, state: str
) -> None:
    found = _run_ec2(
        clients,
        {
            "describe_nat_gateways": [
                {"NatGatewayId": "nat-1", "State": state, "Tags": PROJECT_TAGS}
            ]
        },
    )

    assert [(r.kind, r.name, r.detail["state"]) for r in found] == [("nat-gateway", "nat-1", state)]


@pytest.mark.parametrize("state", ["PendingAcceptance", "Pending", "Available", "Deleting"])
def test_ec2_vpc_endpoint_kept_in_every_state_but_deleted(
    clients: StubbedClients, state: str
) -> None:
    found = _run_ec2(
        clients,
        {
            "describe_vpc_endpoints": [
                {"VpcEndpointId": "vpce-1", "State": state, "Tags": PROJECT_TAGS}
            ]
        },
    )

    assert [(r.kind, r.name, r.detail["state"]) for r in found] == [
        ("vpc-endpoint", "vpce-1", state.lower())
    ]


def test_ec2_vpc_origins_service_sg_carries_its_eni_count(clients: StubbedClients) -> None:
    found = _run_ec2(
        clients,
        {
            "describe_security_groups": [
                _sg("crm-agent-eval-lambda", "sg-proj"),
                _sg(VPC_ORIGINS_SERVICE_SG, "sg-vpco"),
            ],
            "describe_network_interfaces": [
                {"NetworkInterfaceId": "eni-a", "Groups": [{"GroupId": "sg-vpco"}]},
                {
                    "NetworkInterfaceId": "eni-b",
                    "Groups": [{"GroupId": "sg-other"}, {"GroupId": "sg-vpco"}],
                },
                {"NetworkInterfaceId": "eni-c", "Groups": [{"GroupId": "sg-proj"}]},
            ],
        },
    )

    groups = {r.name: r.detail for r in found if r.kind == "security-group"}
    assert groups[VPC_ORIGINS_SERVICE_SG]["eni_count"] == "2"
    assert "eni_count" not in groups["crm-agent-eval-lambda"]


def test_ec2_vpc_origins_service_sg_with_no_enis_counts_zero(clients: StubbedClients) -> None:
    found = _run_ec2(clients, {"describe_security_groups": [_sg(VPC_ORIGINS_SERVICE_SG, "sg-v")]})

    assert [r.detail["eni_count"] for r in found] == ["0"]


def test_ec2_elastic_ip_by_tag_or_name_prefix(clients: StubbedClients) -> None:
    found = _run_ec2(
        clients,
        {
            "describe_addresses": [
                {"AllocationId": "eipalloc-tag", "Tags": PROJECT_TAGS},
                {"AllocationId": "eipalloc-name", "Tags": _tags(Name="crm-agent-eval-nat")},
                {"AllocationId": "eipalloc-other", "Tags": OTHER_TAGS},
            ]
        },
    )

    assert _names(found) == [("elastic-ip", "eipalloc-tag"), ("elastic-ip", "eipalloc-name")]


def test_ec2_calls_only_the_six_stubbed_operations(clients: StubbedClients) -> None:
    """Stubber raises on any unstubbed call, so describe_vpcs/subnets/route_tables never run."""
    assert _run_ec2(clients, {}) == []
    assert not {op for op, _ in EC2_OPS} & {
        "describe_vpcs",
        "describe_subnets",
        "describe_route_tables",
    }


def test_agentcore_runtime_with_underscored_name_carries_its_arn(clients: StubbedClients) -> None:
    runtime = next(p for p in PAGED_OPS if p.operation == "list_agent_runtimes").project_item
    stubber = clients.stub("bedrock-agentcore-control")
    stubber.add_response("list_agent_runtimes", {"agentRuntimes": [runtime]}, {})
    _stub_empty(
        clients,
        (
            ("bedrock-agentcore-control", "list_gateways"),
            ("bedrock-agentcore-control", "list_workload_identities"),
        ),
    )

    found = compute_network.scan_agentcore(clients, CONFIG)

    assert _names(found) == [("agent-runtime", "crm_agent_eval_runtime")]
    assert found[0].arn == runtime["agentRuntimeArn"]


def test_apigateway_rest_api_arn_is_account_free(clients: StubbedClients) -> None:
    clients.stub("apigateway").add_response(
        "get_rest_apis", {"items": [{"id": "a1", "name": "crm-agent-eval-api"}]}, {}
    )
    _stub_empty(clients, (("apigatewayv2", "get_apis"),))

    found = compute_network.scan_apigateway(clients, CONFIG)

    assert [r.arn for r in found] == [f"arn:aws:apigateway:{REGION}::/restapis/a1"]


SCANNER_FUNCTIONS: tuple[Callable[..., list[Resource]], ...] = tuple(
    compute_network.SCANNERS.values()
)


def test_every_scanner_requests_clients_in_the_scan_region(clients: StubbedClients) -> None:
    other = ScanConfig(region="eu-west-1")
    requested: list[tuple[str, str]] = []

    def recording_factory(service: str, region: str) -> Any:
        requested.append((service, region))
        raise Unmeasurable("x", "list_x", "Stop", "stop after the factory call")

    for scanner in SCANNER_FUNCTIONS:
        with pytest.raises(Unmeasurable):
            scanner(recording_factory, other)

    assert requested and {region for _, region in requested} == {"eu-west-1"}

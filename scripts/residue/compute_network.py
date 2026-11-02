# SPDX-License-Identifier: MIT
"""Compute and network residue scanners for the residue scan.

`SCANNERS` maps the family keys `lambda`, `codebuild`, `apigateway`, `elbv2`, `ec2`, `cognito`,
`stepfunctions` and `agentcore` to a `core.FamilyScanner`. Each scanner lists one family's
resources in `config.region` through `core.call`/`core.paginate` only and returns the
project-owned ones; a failed list call propagates as `core.Unmeasurable`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from scripts.residue.core import (
    VPC_ORIGINS_SERVICE_SG,
    ClientFactory,
    FamilyName,
    FamilyScanner,
    Resource,
    ScanConfig,
    call,
    has_project_tag,
    matches_prefix,
    paginate,
)

DEFAULT_SECURITY_GROUP = "default"
DELETED_STATE = "deleted"


def _tag_map(tags: Iterable[Mapping[str, Any]] | None) -> dict[str, str]:
    """Convert an EC2/ELB-style `[{"Key": k, "Value": v}]` list into a dict."""
    return {str(t["Key"]): str(t.get("Value", "")) for t in tags or ()}


def _tagged_or_named(tags: Mapping[str, str], config: ScanConfig) -> bool:
    """True when `tags` carry the project tag or a `Name` tag with a project prefix."""
    return has_project_tag(tags, config) or matches_prefix(tags.get("Name", ""), config)


def _contains_prefix(text: str, config: ScanConfig) -> bool:
    """True when any project name prefix appears anywhere in `text`."""
    return any(prefix in text for prefix in config.name_prefixes)


def _ec2_arn(region: str, owner: str | None, kind: str, resource_id: str) -> str | None:
    if not owner:
        return None
    return f"arn:aws:ec2:{region}:{owner}:{kind}/{resource_id}"


def scan_lambda(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """Lambda functions whose name carries a project prefix, eniprobe functions included."""
    client = factory("lambda", config.region)
    return [
        Resource("lambda", "function", fn["FunctionName"], fn.get("FunctionArn"), config.region)
        for fn in paginate(client, "lambda", "list_functions", "Functions")
        if matches_prefix(fn["FunctionName"], config)
    ]


def scan_codebuild(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """CodeBuild projects whose name carries a project prefix."""
    client = factory("codebuild", config.region)
    names = [str(name) for name in paginate(client, "codebuild", "list_projects", "projects")]
    return [
        Resource("codebuild", "project", name, None, config.region)
        for name in names
        if matches_prefix(name, config)
    ]


def scan_apigateway(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """REST (v1) and HTTP/WebSocket (v2) APIs matched by name prefix or project tag."""
    region = config.region
    v1 = factory("apigateway", region)
    found = [
        Resource(
            "apigateway",
            "rest-api",
            api["name"],
            f"arn:aws:apigateway:{region}::/restapis/{api['id']}",
            region,
            {"api_id": api["id"]},
        )
        for api in paginate(v1, "apigateway", "get_rest_apis", "items")
        if matches_prefix(api["name"], config) or has_project_tag(api.get("tags", {}), config)
    ]
    v2 = factory("apigatewayv2", region)
    found.extend(
        Resource(
            "apigateway",
            "api-v2",
            api["Name"],
            f"arn:aws:apigateway:{region}::/apis/{api['ApiId']}",
            region,
            {"api_id": api["ApiId"], "protocol": str(api.get("ProtocolType", ""))},
        )
        for api in paginate(v2, "apigateway", "get_apis", "Items")
        if matches_prefix(api["Name"], config) or has_project_tag(api.get("Tags", {}), config)
    )
    return found


def scan_elbv2(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """Load balancers and target groups whose name carries a project prefix."""
    client = factory("elbv2", config.region)
    found = [
        Resource(
            "elbv2",
            "load-balancer",
            lb["LoadBalancerName"],
            lb["LoadBalancerArn"],
            config.region,
            {"type": str(lb.get("Type", ""))},
        )
        for lb in paginate(client, "elbv2", "describe_load_balancers", "LoadBalancers")
        if matches_prefix(lb["LoadBalancerName"], config)
    ]
    found.extend(
        Resource(
            "elbv2", "target-group", tg["TargetGroupName"], tg["TargetGroupArn"], config.region
        )
        for tg in paginate(client, "elbv2", "describe_target_groups", "TargetGroups")
        if matches_prefix(tg["TargetGroupName"], config)
    )
    return found


def _scan_security_groups(client: Any, config: ScanConfig) -> tuple[list[Resource], set[str]]:
    """Project SGs plus the VPC-origins service SG; also the ids of the project SGs alone."""
    found: list[Resource] = []
    project_ids: set[str] = set()
    for sg in paginate(client, "ec2", "describe_security_groups", "SecurityGroups"):
        name = sg["GroupName"]
        if name == DEFAULT_SECURITY_GROUP:
            continue
        project = matches_prefix(name, config) or has_project_tag(_tag_map(sg.get("Tags")), config)
        if not project and name != VPC_ORIGINS_SERVICE_SG:
            continue
        if project:
            project_ids.add(sg["GroupId"])
        found.append(
            Resource(
                "ec2",
                "security-group",
                name,
                _ec2_arn(config.region, sg.get("OwnerId"), "security-group", sg["GroupId"]),
                config.region,
                {"group_id": sg["GroupId"], "vpc_id": str(sg.get("VpcId", ""))},
            )
        )
    return found, project_ids


def _with_eni_count(sg: Resource, enis: list[Any]) -> Resource:
    group_id = sg.detail.get("group_id")
    count = sum(1 for eni in enis if group_id in {g["GroupId"] for g in eni.get("Groups", ())})
    return replace(sg, detail={**sg.detail, "eni_count": str(count)})


def _project_network_interfaces(
    enis: list[Any], config: ScanConfig, project_sg_ids: set[str]
) -> list[Resource]:
    found: list[Resource] = []
    for eni in enis:
        group_ids = {g["GroupId"] for g in eni.get("Groups", ())}
        description = str(eni.get("Description", ""))
        if not (
            group_ids & project_sg_ids
            or _contains_prefix(description, config)
            or _tagged_or_named(_tag_map(eni.get("TagSet")), config)
        ):
            continue
        eni_id = eni["NetworkInterfaceId"]
        found.append(
            Resource(
                "ec2",
                "network-interface",
                eni_id,
                _ec2_arn(config.region, eni.get("OwnerId"), "network-interface", eni_id),
                config.region,
                {
                    "description": description,
                    "interface_type": str(eni.get("InterfaceType", "")),
                    "status": str(eni.get("Status", "")),
                },
            )
        )
    return found


def _scan_tagged_network(client: Any, config: ScanConfig) -> list[Resource]:
    """VPC endpoints, NAT gateways, Elastic IPs and flow logs owned by tag or `Name` prefix.

    Endpoints and NAT gateways in state `deleted` are skipped; every other state is returned.
    """
    region = config.region
    found: list[Resource] = []
    for ep in paginate(client, "ec2", "describe_vpc_endpoints", "VpcEndpoints"):
        state = str(ep.get("State", "")).lower()
        if state == DELETED_STATE or not _tagged_or_named(_tag_map(ep.get("Tags")), config):
            continue
        ep_id = ep["VpcEndpointId"]
        found.append(
            Resource(
                "ec2",
                "vpc-endpoint",
                ep_id,
                _ec2_arn(region, ep.get("OwnerId"), "vpc-endpoint", ep_id),
                region,
                {"service_name": str(ep.get("ServiceName", "")), "state": state},
            )
        )
    for nat in paginate(client, "ec2", "describe_nat_gateways", "NatGateways"):
        state = str(nat.get("State", "")).lower()
        if state == DELETED_STATE or not _tagged_or_named(_tag_map(nat.get("Tags")), config):
            continue
        found.append(
            Resource("ec2", "nat-gateway", nat["NatGatewayId"], None, region, {"state": state})
        )
    addresses = call(client, "ec2", "describe_addresses")
    for eip in addresses.get("Addresses", []):
        if not _tagged_or_named(_tag_map(eip.get("Tags")), config):
            continue
        found.append(
            Resource(
                "ec2",
                "elastic-ip",
                eip["AllocationId"],
                None,
                region,
                {"association_id": str(eip.get("AssociationId", ""))},
            )
        )
    for flow in paginate(client, "ec2", "describe_flow_logs", "FlowLogs"):
        log_group = str(flow.get("LogGroupName", ""))
        if not (
            _tagged_or_named(_tag_map(flow.get("Tags")), config)
            or _contains_prefix(log_group, config)
        ):
            continue
        found.append(
            Resource(
                "ec2",
                "flow-log",
                flow["FlowLogId"],
                None,
                region,
                {"log_group": log_group, "resource_id": str(flow.get("ResourceId", ""))},
            )
        )
    return found


def scan_ec2(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """Project SGs, the VPC-origins service SG, project ENIs, and tagged network resources.

    VPCs, subnets, route tables and `default` security groups are never returned. The VPC-origins
    service SG carries `detail["eni_count"]`, the number of ENIs using it. An ENI is
    project-owned when it uses a project SG (the VPC-origins service SG does not count), its
    description contains a project prefix, or it carries the project tag.
    """
    client = factory("ec2", config.region)
    groups, project_sg_ids = _scan_security_groups(client, config)
    enis = paginate(client, "ec2", "describe_network_interfaces", "NetworkInterfaces")
    found = [
        _with_eni_count(sg, enis) if sg.name == VPC_ORIGINS_SERVICE_SG else sg for sg in groups
    ]
    found.extend(_project_network_interfaces(enis, config, project_sg_ids))
    found.extend(_scan_tagged_network(client, config))
    return found


def scan_cognito(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """Cognito user pools whose name carries a project prefix, with the ARN from `describe_user_pool`."""
    client = factory("cognito-idp", config.region)
    pools = [
        pool
        for pool in paginate(client, "cognito", "list_user_pools", "UserPools", MaxResults=60)
        if matches_prefix(pool["Name"], config)
    ]
    found: list[Resource] = []
    for pool in pools:
        described = call(client, "cognito", "describe_user_pool", UserPoolId=pool["Id"])
        found.append(
            Resource(
                "cognito",
                "user-pool",
                pool["Name"],
                described.get("UserPool", {}).get("Arn"),
                config.region,
                {"pool_id": pool["Id"]},
            )
        )
    return found


def scan_stepfunctions(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """Step Functions state machines whose name carries a project prefix."""
    client = factory("stepfunctions", config.region)
    return [
        Resource("stepfunctions", "state-machine", sm["name"], sm["stateMachineArn"], config.region)
        for sm in paginate(client, "stepfunctions", "list_state_machines", "stateMachines")
        if matches_prefix(sm["name"], config)
    ]


def scan_agentcore(factory: ClientFactory, config: ScanConfig) -> list[Resource]:
    """AgentCore agent runtimes, gateways (ARN from `get_gateway`) and workload identities by name prefix."""
    client = factory("bedrock-agentcore-control", config.region)
    region = config.region
    found = [
        Resource(
            "agentcore",
            "agent-runtime",
            rt["agentRuntimeName"],
            rt.get("agentRuntimeArn"),
            region,
            {"runtime_id": str(rt.get("agentRuntimeId", ""))},
        )
        for rt in paginate(client, "agentcore", "list_agent_runtimes", "agentRuntimes")
        if matches_prefix(rt["agentRuntimeName"], config)
    ]
    for gw in paginate(client, "agentcore", "list_gateways", "items"):
        if not matches_prefix(gw["name"], config):
            continue
        described = call(client, "agentcore", "get_gateway", gatewayIdentifier=gw["gatewayId"])
        found.append(
            Resource(
                "agentcore",
                "gateway",
                gw["name"],
                described.get("gatewayArn"),
                region,
                {"gateway_id": gw["gatewayId"]},
            )
        )
    found.extend(
        Resource(
            "agentcore", "workload-identity", wi["name"], wi.get("workloadIdentityArn"), region
        )
        for wi in paginate(client, "agentcore", "list_workload_identities", "workloadIdentities")
        if matches_prefix(wi["name"], config)
    )
    return found


SCANNERS: dict[FamilyName, FamilyScanner] = {
    "lambda": scan_lambda,
    "codebuild": scan_codebuild,
    "apigateway": scan_apigateway,
    "elbv2": scan_elbv2,
    "ec2": scan_ec2,
    "cognito": scan_cognito,
    "stepfunctions": scan_stepfunctions,
    "agentcore": scan_agentcore,
}

ENUMERATED_ARN_TYPES: dict[FamilyName, frozenset[str]] = {
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
}
"""Per family, the `core.arn_resource_type` values of the resources its scanner returns."""

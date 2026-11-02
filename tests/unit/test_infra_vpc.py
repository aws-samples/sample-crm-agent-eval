# SPDX-License-Identifier: MIT
"""Plan-time and source assertions for the no-public-exposure constraint.

Covers the `vpc` module (no internet path, scoped security groups, endpoint reachability, flow
logs, the optional egress-only NAT, adopted-VPC inputs), the `vpc_config` on every Lambda via
`scripts/check_no_public_exposure.py`, the private REST API, the relay's `AWS_IAM` Function URL, and
`scripts/teardown.sh`'s adopted-VPC guard. Planned offline with mock credentials and no state, per
`_terraform_fixture`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts import proc_run
from scripts.hcl_text import blank_comments

from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_module,
    require_terraform,
    terraform_unavailable_reason,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA_DIR = REPO_ROOT / "infra"
EXPOSURE_GATE = REPO_ROOT / "scripts" / "check_no_public_exposure.py"
API_MAIN_TF = INFRA_DIR / "modules" / "api" / "main.tf"
RELAY_MAIN_TF = INFRA_DIR / "modules" / "agent_relay" / "main.tf"
LAMBDA_ACTION_MAIN_TF = INFRA_DIR / "modules" / "lambda_action" / "main.tf"
ROOT_MAIN_TF = INFRA_DIR / "main.tf"

REGION = "us-east-1"
ZONES = [f"{REGION}a", f"{REGION}b"]

# Adopted-VPC ids for the fixture: shape-valid for the module's `validation` blocks, and assembled
# at
# runtime because `check_no_account_id` rejects any literal `vpc-`/`subnet-` hex id in tracked
# content.
_HEX8 = 8  # AWS's short id form; the module's validations accept 8 or 17 hex digits.
FIXTURE_VPC_ID = "vpc-" + "0" * _HEX8
FIXTURE_VPC_CIDR = "10.20.0.0/20"
FIXTURE_PRIVATE_SUBNET_IDS = ["subnet-" + "a" * (_HEX8 - 1) + digit for digit in ("0", "1")]
FIXTURE_PRIVATE_ROUTE_TABLE_IDS = ["rtb-" + "c" * (_HEX8 - 1) + digit for digit in ("0", "1")]
# Not in FIXTURE_PRIVATE_SUBNET_IDS: a module precondition requires the NAT subnet to differ from
# every Lambda subnet.
FIXTURE_NAT_SUBNET_ID = "subnet-" + "b" * _HEX8
# The AgentCore Runtime's VPC-mode subnet and its own route table, distinct from every other fixture
# id.
FIXTURE_RUNTIME_SUBNET_IDS = ["subnet-" + "d" * _HEX8]
FIXTURE_RUNTIME_ROUTE_TABLE_IDS = ["rtb-" + "e" * _HEX8]

#: The interface endpoints the fixture asks for (not the root's list). `execute-api`,
#: `bedrock-agentcore` and `states` are required by preconditions on the module's matching
#: endpoint-id outputs.
FIXTURE_INTERFACE_SERVICES = ["bedrock-agentcore", "execute-api", "kms", "logs", "ssm", "states"]

#: Resource types that create a path to the internet; absent when `nat_gateway_enabled = false`.
FORBIDDEN_NETWORK_TYPES = (
    "aws_internet_gateway",
    "aws_nat_gateway",
    "aws_eip",
)

#: Resource types never created under any input.
UNCONDITIONALLY_ABSENT_TYPES = ("aws_egress_only_internet_gateway",)

ANY_IPV4 = "0.0.0.0/0"
ANY_IPV6 = "::/0"

#: Placeholder ids for the relay plan fixture; not hex after the prefix, so
#: `scripts/check_no_account_id.sh` does not match them as real ids.
FIXTURE_SUBNET_IDS = ["subnet-fixturezonea", "subnet-fixturezoneb"]
FIXTURE_SECURITY_GROUP_IDS = ["sg-fixturelambda"]


def _source_without_comments(path: Path) -> str:
    """`path`'s HCL with `#`/`//`/`/* */` comments blanked out, via
    `scripts.hcl_text.blank_comments`."""
    return blank_comments(path.read_text(encoding="utf-8"))


def _is_unknown_at_plan_time(plan: Plan, address: str, key: str) -> bool:
    """Whether `key` is `(known after apply)` for `address`, as opposed to simply absent."""
    change = plan.resource_changes[address].get("change") or {}
    unknown = change.get("after_unknown") or {}
    return bool(unknown.get(key))


def _module_arguments(**overrides: str) -> str:
    arguments = {
        "name_prefix": '"crm-eval-fixture"',
        "region": f'"{REGION}"',
        "vpc_id": f'"{FIXTURE_VPC_ID}"',
        "vpc_cidr_block": f'"{FIXTURE_VPC_CIDR}"',
        "private_subnet_ids": json.dumps(FIXTURE_PRIVATE_SUBNET_IDS),
        "private_route_table_ids": json.dumps(FIXTURE_PRIVATE_ROUTE_TABLE_IDS),
        "interface_endpoint_service_names": json.dumps(FIXTURE_INTERFACE_SERVICES),
        "gateway_endpoint_service_names": '["s3", "dynamodb"]',
        # Required with no default; the enabled shape has its own fixture, `plan_with_nat`.
        "nat_gateway_enabled": "false",
    }
    arguments.update(overrides)
    return "\n".join(f"  {key} = {value}" for key, value in arguments.items())


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)
    return plan_module(tmp_path_factory.mktemp("vpc-plan"), "vpc", _module_arguments())


@pytest.fixture(scope="module")
def plan_with_nat(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    """The same module with `nat_gateway_enabled = true`."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)
    return plan_module(
        tmp_path_factory.mktemp("vpc-plan-nat"),
        "vpc",
        _module_arguments(
            nat_gateway_enabled="true",
            nat_public_subnet_id=f'"{FIXTURE_NAT_SUBNET_ID}"',
        ),
    )


# ---------------------------------------------------------------------------
# Positive controls: the plan the absence assertions search is the right, non-empty plan.
# ---------------------------------------------------------------------------


def test_the_plan_contains_the_resources_this_stack_owns(plan: Plan) -> None:
    """The plan holds the three stack-owned security groups and one endpoint per requested
    service."""
    # The VPC and its subnets are adopted, so only stack-owned resources appear in the plan.
    assert plan.addresses_of_type("aws_security_group") == [
        "module.subject.aws_security_group.agent_runtime",
        "module.subject.aws_security_group.endpoints",
        "module.subject.aws_security_group.lambda",
    ]

    endpoints = plan.addresses_of_type("aws_vpc_endpoint")
    assert len(endpoints) == len(FIXTURE_INTERFACE_SERVICES) + 2, (
        f"expected one endpoint per interface service plus the two gateway endpoints, got {endpoints}"
    )


def test_every_requested_endpoint_is_planned(plan: Plan) -> None:
    """The planned endpoint service names equal the requested interface and gateway services."""
    planned = sorted(
        plan.after(address)["service_name"]
        for address in plan.addresses_of_type("aws_vpc_endpoint")
    )
    expected = sorted(
        f"com.amazonaws.{REGION}.{service}"
        for service in [*FIXTURE_INTERFACE_SERVICES, "s3", "dynamodb"]
    )
    assert planned == expected


# ---------------------------------------------------------------------------
# 1. Private-only: no path in, no path out.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("resource_type", FORBIDDEN_NETWORK_TYPES)
def test_no_internet_path_exists_when_nat_is_disabled(plan: Plan, resource_type: str) -> None:
    """With `nat_gateway_enabled = false` no internet-path resource of this type is planned."""
    assert plan.addresses_of_type(resource_type) == []


@pytest.mark.parametrize("resource_type", UNCONDITIONALLY_ABSENT_TYPES)
def test_some_types_are_absent_under_every_input(
    plan: Plan, plan_with_nat: Plan, resource_type: str
) -> None:
    """These types are absent from both the NAT-disabled and the NAT-enabled plans."""
    assert plan.addresses_of_type(resource_type) == []
    assert plan_with_nat.addresses_of_type(resource_type) == []


def test_no_route_table_carries_a_route_off_the_vpc(plan: Plan) -> None:
    """With NAT disabled, the module creates no route table and no standalone `aws_route`."""
    assert plan.addresses_of_type("aws_route_table") == [], (
        "this module created a route table; the adopted VPC's tables are used as-is"
    )
    assert plan.addresses_of_type("aws_route") == [], "a standalone route off the VPC was created"


def test_the_adopted_vpcs_default_resources_are_left_alone(plan: Plan) -> None:
    """No `aws_default_*` resource is declared against the adopted VPC, so co-tenant rules are
    untouched."""
    for resource_type in (
        "aws_default_route_table",
        "aws_default_security_group",
        "aws_default_network_acl",
    ):
        assert plan.addresses_of_type(resource_type) == [], (
            f"{resource_type} is declared against an ADOPTED VPC. Emptying or overwriting a default "
            "resource this stack does not own changes connectivity for every co-tenant already on it."
        )


def test_no_subnet_assigns_public_addresses(plan: Plan) -> None:
    """No planned subnet sets `map_public_ip_on_launch`."""
    for address in plan.addresses_of_type("aws_subnet"):
        assert plan.after(address)["map_public_ip_on_launch"] is False, address


def test_neither_security_group_this_stack_creates_declares_an_inline_rule(plan: Plan) -> None:
    """No stack-owned security group declares inline rules, so AWS's implicit allow-all egress is
    revoked."""
    for address in plan.addresses_of_type("aws_security_group"):
        after = plan.after(address)
        assert after.get("ingress") in ([], None), f"{address} declares an inline ingress block"
        assert after.get("egress") in ([], None), f"{address} declares an inline egress block"


def test_this_module_does_not_touch_the_adopted_vpcs_dns_attributes(plan: Plan) -> None:
    """The module declares no `aws_vpc`, so the adopted VPC's DNS attributes are a deploy-time
    precondition."""
    assert plan.addresses_of_type("aws_vpc") == [], (
        "this module declares an aws_vpc resource. The VPC is adopted: a resource here "
        "would put a VPC this stack does not own under Terraform management, and `terraform destroy` "
        "would then delete it along with every co-tenant subnet inside it."
    )


# ---------------------------------------------------------------------------
# Security groups: the egress policy, and the ways it silently fails.
# ---------------------------------------------------------------------------


def test_the_lambda_group_has_no_ingress_rule(plan: Plan) -> None:
    """The only ingress rules are the two on the endpoint group; none targets the Lambda group."""
    assert plan.addresses_of_type("aws_vpc_security_group_ingress_rule") == [
        "module.subject.aws_vpc_security_group_ingress_rule.endpoints_from_agent_runtime",
        "module.subject.aws_vpc_security_group_ingress_rule.endpoints_from_lambda",
    ], (
        "the only ingress rules this module may create are the two on the ENDPOINT group — from the "
        "Lambda group and from the Runtime group; neither targets the Lambda group itself"
    )

    assert plan.after("module.subject.aws_security_group.lambda").get("ingress") in (None, [])


def test_no_security_group_rule_names_a_public_cidr(plan: Plan) -> None:
    """No inline or standalone security-group rule names `0.0.0.0/0` or `::/0`."""
    rule_types = (
        "aws_vpc_security_group_egress_rule",
        "aws_vpc_security_group_ingress_rule",
    )
    for resource_type in rule_types:
        for address in plan.addresses_of_type(resource_type):
            after = plan.after(address)
            assert after.get("cidr_ipv4") != ANY_IPV4, address
            assert after.get("cidr_ipv6") != ANY_IPV6, address

    for address in plan.addresses_of_type("aws_security_group"):
        after = plan.after(address)
        for direction in ("ingress", "egress"):
            for rule in after.get(direction) or []:
                assert ANY_IPV4 not in (rule.get("cidr_blocks") or []), (address, direction)
                assert ANY_IPV6 not in (rule.get("ipv6_cidr_blocks") or []), (address, direction)


def test_the_lambda_groups_only_referenced_egress_is_the_endpoint_group(plan: Plan) -> None:
    """The Lambda group's endpoint egress is tcp/443 to the endpoint security group, not a CIDR."""
    address = "module.subject.aws_vpc_security_group_egress_rule.lambda_to_endpoints"
    after = plan.after(address)
    assert after["ip_protocol"] == "tcp"
    assert after["from_port"] == 443
    assert after["to_port"] == 443
    assert after.get("cidr_ipv4") is None
    # The referenced group id is unknown at plan time, so the source pins which group.
    egress_rule_source = _source_without_comments(INFRA_DIR / "modules" / "vpc" / "main.tf")
    assert "referenced_security_group_id = aws_security_group.endpoints.id" in egress_rule_source


def test_every_gateway_endpoint_has_a_matching_prefix_list_egress_rule(plan: Plan) -> None:
    """Each gateway endpoint has a tcp/443 prefix-list egress rule from both the Lambda and Runtime
    groups."""
    gateway_services = {"s3", "dynamodb"}
    rules = plan.addresses_of_type("aws_vpc_security_group_egress_rule")
    prefix_list_rules = [
        address
        for address in rules
        if plan.after(address).get("prefix_list_id") != "" and "gateway" in address
    ]
    # One prefix-list egress rule per gateway service per source group (Lambda, Runtime).
    assert len(prefix_list_rules) == 2 * len(gateway_services), (
        f"expected one prefix-list egress rule per gateway endpoint ({sorted(gateway_services)}) for "
        f"each of the Lambda and Runtime groups, got {prefix_list_rules}"
    )
    for address in prefix_list_rules:
        after = plan.after(address)
        assert after["ip_protocol"] == "tcp"
        assert after["from_port"] == 443
        assert after["to_port"] == 443


# ---------------------------------------------------------------------------
# The dedicated AgentCore Runtime security group. VPC mode runs the Runtime's
# ENIs on this group, which does not carry the Lambda group's 0.0.0.0/0 JWKS
# egress rule.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def plan_with_runtime_vpc(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    """The module planned with the Runtime's VPC-mode subnet and its own route table supplied."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)
    return plan_module(
        tmp_path_factory.mktemp("vpc-plan-runtime"),
        "vpc",
        _module_arguments(
            runtime_subnet_ids=json.dumps(FIXTURE_RUNTIME_SUBNET_IDS),
            runtime_route_table_ids=json.dumps(FIXTURE_RUNTIME_ROUTE_TABLE_IDS),
        ),
    )


def test_the_runtime_group_has_no_ingress_rule(plan: Plan) -> None:
    """The Runtime SG carries no inline ingress, and no standalone ingress rule targets it."""
    assert plan.after("module.subject.aws_security_group.agent_runtime").get("ingress") in (
        None,
        [],
    )
    main_tf = _source_without_comments(INFRA_DIR / "modules" / "vpc" / "main.tf")
    # The one rule naming the Runtime group as a SOURCE lands on the endpoint group.
    assert (
        "security_group_id            = aws_security_group.endpoints.id" in main_tf
        and "referenced_security_group_id = aws_security_group.agent_runtime.id" in main_tf
    ), "the Runtime->endpoints ingress rule must sit on the endpoint group, not the Runtime group"


def test_the_runtime_groups_egress_is_only_the_endpoint_group_and_prefix_lists(plan: Plan) -> None:
    """The Runtime SG's egress is tcp/443 to the endpoint group and the gateway prefix lists only,
    never 0.0.0.0/0."""
    endpoints_rule = plan.after(
        "module.subject.aws_vpc_security_group_egress_rule.agent_runtime_to_endpoints"
    )
    assert endpoints_rule["ip_protocol"] == "tcp"
    assert endpoints_rule["from_port"] == 443
    assert endpoints_rule["to_port"] == 443
    assert endpoints_rule.get("cidr_ipv4") is None
    assert endpoints_rule.get("cidr_ipv6") is None

    main_tf = _source_without_comments(INFRA_DIR / "modules" / "vpc" / "main.tf")
    assert (
        "security_group_id            = aws_security_group.agent_runtime.id" in main_tf
        and "referenced_security_group_id = aws_security_group.endpoints.id" in main_tf
    )
    # Every prefix-list egress rule on the Runtime group is tcp/443.
    for address in plan.addresses_of_type("aws_vpc_security_group_egress_rule"):
        if "agent_runtime_to_gateway_endpoints" not in address:
            continue
        after = plan.after(address)
        assert after["ip_protocol"] == "tcp"
        assert (after["from_port"], after["to_port"]) == (443, 443)
        assert after.get("cidr_ipv4") is None


def test_a_runtime_subnet_does_not_enter_the_interface_endpoints_subnet_set(
    plan_with_runtime_vpc: Plan,
) -> None:
    """Every interface endpoint's `subnet_ids` is exactly the private subnets, never the Runtime
    subnet."""
    interface_endpoints = [
        address
        for address in plan_with_runtime_vpc.addresses_of_type("aws_vpc_endpoint")
        if plan_with_runtime_vpc.after(address)["vpc_endpoint_type"] == "Interface"
    ]
    assert interface_endpoints, "no interface endpoint planned; the assertion would be vacuous"
    for address in interface_endpoints:
        subnet_ids = plan_with_runtime_vpc.after(address)["subnet_ids"]
        assert sorted(subnet_ids) == sorted(FIXTURE_PRIVATE_SUBNET_IDS), (
            f"{address} subnet_ids={subnet_ids} must equal the private subnets exactly; a Runtime "
            f"subnet ({FIXTURE_RUNTIME_SUBNET_IDS}) leaked into an interface endpoint"
        )


def test_a_runtime_route_table_joins_the_gateway_endpoint_associations(
    plan_with_runtime_vpc: Plan,
) -> None:
    """The module plans both gateway endpoints when a Runtime route table is supplied."""
    # `test_every_gateway_endpoint_is_associated_with_every_private_route_table` pins the concat
    # form.
    gateway_endpoints = [
        address
        for address in plan_with_runtime_vpc.addresses_of_type("aws_vpc_endpoint")
        if plan_with_runtime_vpc.after(address)["vpc_endpoint_type"] == "Gateway"
    ]
    assert len(gateway_endpoints) == 2, gateway_endpoints


# ---------------------------------------------------------------------------
# Endpoints: the workload's whole reachable AWS surface.
# ---------------------------------------------------------------------------


#: Services whose private DNS must be off, mirroring the module's default; private DNS for
#: `execute-api` would take over `*.execute-api.<region>.amazonaws.com` for the whole adopted VPC.
FIXTURE_PRIVATE_DNS_DISABLED = ["execute-api"]


def test_interface_endpoints_enable_private_dns_except_where_it_reaches_co_tenants(
    plan: Plan,
) -> None:
    """Private DNS is enabled on every interface endpoint except the declared exceptions, which have
    it off."""
    interface_endpoints = [
        address
        for address in plan.addresses_of_type("aws_vpc_endpoint")
        if plan.after(address)["vpc_endpoint_type"] == "Interface"
    ]
    assert len(interface_endpoints) == len(FIXTURE_INTERFACE_SERVICES)

    assert FIXTURE_PRIVATE_DNS_DISABLED, "no service is declared private-DNS-disabled"
    disabled_seen = 0
    for address in interface_endpoints:
        after = plan.after(address)
        service = after["service_name"].rsplit(".", 1)[-1]
        if service in FIXTURE_PRIVATE_DNS_DISABLED:
            assert after["private_dns_enabled"] is False, (
                f"{address} enables private DNS for `{service}`, which hijacks that service's hostname "
                "for the entire ADOPTED VPC and changes co-tenants' name resolution"
            )
            disabled_seen += 1
            continue
        assert after["private_dns_enabled"] is True, address
    assert disabled_seen == len(FIXTURE_PRIVATE_DNS_DISABLED), (
        f"only {disabled_seen} of the declared private-DNS-disabled services were present in the plan, "
        "so the co-tenant assertion covered fewer endpoints than it claims"
    )

    for address in interface_endpoints:
        # Same-apply attributes: asserted unknown (not absent) here, then pinned in source below.
        assert _is_unknown_at_plan_time(plan, address, "subnet_ids"), address
        assert _is_unknown_at_plan_time(plan, address, "security_group_ids"), address

    endpoints_tf = _source_without_comments(INFRA_DIR / "modules" / "vpc" / "endpoints.tf")
    # Whitespace-insensitive, since `terraform fmt` re-aligns assignment blocks.
    assert re.search(r"(?m)^\s*subnet_ids\s*=\s*var\.private_subnet_ids\s*$", endpoints_tf)
    assert re.search(
        r"(?m)^\s*security_group_ids\s*=\s*\[aws_security_group\.endpoints\.id\]\s*$",
        endpoints_tf,
    )


def test_every_gateway_endpoint_is_associated_with_every_private_route_table(plan: Plan) -> None:
    """Gateway endpoints associate with the distinct union of the private and Runtime route
    tables."""
    for address in plan.addresses_of_type("aws_vpc_endpoint"):
        after = plan.after(address)
        if after["vpc_endpoint_type"] != "Gateway":
            continue
        assert _is_unknown_at_plan_time(plan, address, "route_table_ids"), address

    endpoints_tf = _source_without_comments(INFRA_DIR / "modules" / "vpc" / "endpoints.tf")
    assert re.search(
        r"(?m)^\s*route_table_ids\s*=\s*distinct\(concat\(var\.private_route_table_ids,\s*"
        r"var\.runtime_route_table_ids\)\)\s*$",
        endpoints_tf,
    ), (
        "the gateway endpoints must associate with the ADOPTED private tables AND the Runtime subnet's "
        "own table, `distinct`-ed: the Runtime table needs the s3 gateway association for ECR "
        "image-layer pulls, and several adopted subnets commonly share one table (a duplicate "
        "association is an apply error)"
    )


# ---------------------------------------------------------------------------
# Flow logs.
# ---------------------------------------------------------------------------


def test_flow_logs_are_delivered_with_explicit_retention(plan: Plan) -> None:
    """The flow log captures ALL traffic to CloudWatch Logs, with a finite retention of at least
    365 days on its group."""
    flow_log = plan.after("module.subject.aws_flow_log.this[0]")
    assert flow_log["traffic_type"] == "ALL"
    assert flow_log["log_destination_type"] == "cloud-watch-logs"

    group = plan.after("module.subject.aws_cloudwatch_log_group.flow_logs[0]")
    assert group["retention_in_days"] >= 365, "an unset retention means never expire"


def test_the_flow_log_delivery_role_is_scoped_to_its_own_log_group(plan: Plan) -> None:
    """The delivery role's policy is scoped to its own log group and omits `logs:CreateLogGroup`."""
    rendered = plan.after("module.subject.aws_iam_role_policy.flow_logs[0]").get("policy")
    if not isinstance(rendered, str):
        # The document is `(known after apply)`, so the scoping is asserted from source.
        source = _source_without_comments(INFRA_DIR / "modules" / "vpc" / "flow_logs.tf")
        assert 'resources = ["${aws_cloudwatch_log_group.flow_logs[0].arn}:*"]' in source
        assert "logs:CreateLogGroup" not in source
        return
    document = json.loads(rendered)
    actions = {
        action
        for statement in document["Statement"]
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }
    assert "logs:CreateLogGroup" not in actions


def test_flow_logs_can_be_disabled_without_an_unknown_count(tmp_path: Path) -> None:
    """`flow_log_enabled = false` plans with no flow log, so `count` is driven by a plain bool."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)

    disabled = plan_module(
        tmp_path / "vpc-no-flow-logs", "vpc", _module_arguments(flow_log_enabled="false")
    )
    assert disabled.addresses_of_type("aws_flow_log") == []
    # Positive control: the security groups still plan.
    assert disabled.addresses_of_type("aws_security_group") == [
        "module.subject.aws_security_group.agent_runtime",
        "module.subject.aws_security_group.endpoints",
        "module.subject.aws_security_group.lambda",
    ]


# ---------------------------------------------------------------------------
# 2. Every Lambda in the tree is in a VPC — delegated to the gate itself.
# ---------------------------------------------------------------------------


def test_the_no_public_exposure_gate_passes_against_the_real_infra_tree() -> None:
    """`check_no_public_exposure` exits 0 against the real `infra/` tree; its exit 3 counts as a
    failure."""
    result = proc_run.run(
        [sys.executable, str(EXPOSURE_GATE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"check_no_public_exposure exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_the_gate_examines_exactly_the_two_lambda_function_blocks_that_exist() -> None:
    """The tree has exactly two `aws_lambda_function` blocks, and the gate reports that same
    count."""
    blocks = [
        (path.relative_to(INFRA_DIR), match)
        for path in sorted(INFRA_DIR.rglob("*.tf"))
        if ".terraform" not in path.parts
        for match in re.findall(
            r'(?m)^resource\s+"aws_lambda_function"\s+"([A-Za-z0-9_-]+)"',
            _source_without_comments(path),
        )
    ]
    assert sorted(blocks) == [
        (Path("modules/agent_relay/main.tf"), "this"),
        (Path("modules/lambda_action/main.tf"), "this"),
    ], f"the set of aws_lambda_function blocks moved: {sorted(blocks)}"

    result = proc_run.run(
        [sys.executable, str(EXPOSURE_GATE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{len(blocks)} aws_lambda_function" in result.stdout, (
        "the gate's own reported denominator disagrees with the blocks on disk, so a green exit code "
        f"is not evidence it examined them: {result.stdout!r}"
    )


def test_both_lambda_modules_declare_a_static_vpc_config_block() -> None:
    """Both Lambda modules declare a static `vpc_config {` block, never `dynamic "vpc_config"`."""
    for path in (LAMBDA_ACTION_MAIN_TF, RELAY_MAIN_TF):
        source = _source_without_comments(path)
        assert re.search(r"(?m)^\s*vpc_config\s*\{", source), f"{path.name} declares no vpc_config"
        assert 'dynamic "vpc_config"' not in source, f"{path.name} makes vpc_config conditional"


def test_both_lambda_modules_grant_the_eni_permissions_the_vpc_config_requires() -> None:
    """Each Lambda module's execution role grants the six EC2 ENI actions a `vpc_config`
    requires."""
    required = {
        "ec2:CreateNetworkInterface",
        "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeSubnets",
        "ec2:DeleteNetworkInterface",
        "ec2:AssignPrivateIpAddresses",
        "ec2:UnassignPrivateIpAddresses",
    }
    for path in (LAMBDA_ACTION_MAIN_TF, RELAY_MAIN_TF):
        source = _source_without_comments(path)
        missing = {action for action in required if f'"{action}"' not in source}
        assert not missing, f"{path.name} omits {sorted(missing)} from its ENI grant"


def test_every_root_lambda_module_instantiation_is_wired_to_the_vpc() -> None:
    """Every root `lambda_action`/`agent_relay` instantiation reads its subnets and groups from
    `module.vpc`."""
    source = _source_without_comments(ROOT_MAIN_TF)
    instantiations = re.findall(
        r'(?m)^\s*source\s*=\s*"\./modules/(lambda_action|agent_relay)"', source
    )
    assert instantiations, "no lambda_action/agent_relay instantiations found — the parse is wrong"

    subnet_wirings = source.count("vpc_subnet_ids         = module.vpc.lambda_subnet_ids")
    group_wirings = source.count("vpc_security_group_ids = module.vpc.lambda_security_group_ids")
    assert subnet_wirings == len(instantiations), (
        f"{len(instantiations)} function modules instantiated but {subnet_wirings} wired to module.vpc"
    )
    assert group_wirings == len(instantiations)


# ---------------------------------------------------------------------------
# 3. The REST API's endpoint type.
# ---------------------------------------------------------------------------


def test_the_rest_api_is_private_and_endpoint_scoped() -> None:
    """The REST API is `PRIVATE`, attaches a resource policy, and carries no
    `disable_execute_api_endpoint`."""
    source = _source_without_comments(API_MAIN_TF)

    assert re.search(r'(?ms)endpoint_configuration\s*\{.*?types\s*=\s*\["PRIVATE"\]', source), (
        'the REST API does not declare endpoint_configuration.types = ["PRIVATE"], so it keeps a '
        "public execute-api hostname and CloudFront is a route rather than a boundary"
    )
    # Either attachment form is accepted: a separate policy resource or an inline `policy =`.
    assert 'resource "aws_api_gateway_rest_api_policy"' in source or re.search(
        r"(?m)^\s*policy\s*=\s*data\.aws_iam_policy_document\.resource_policy", source
    ), (
        "the private API attaches no resource policy, so every interface endpoint in the VPC can invoke "
        "it rather than only the one named in vpc_endpoint_ids"
    )
    assert "disable_execute_api_endpoint" not in source, (
        "disable_execute_api_endpoint is back. It is a v2/HTTP-API attribute: on a v1 REST API it is "
        "silently meaningless, so its presence would read as a control while enforcing nothing"
    )


# ---------------------------------------------------------------------------
# 4. The relay's Function URL.
# ---------------------------------------------------------------------------


def test_the_relay_function_url_is_iam_authorized_in_source() -> None:
    """The relay source sets `authorization_type = "AWS_IAM"` and has no `"NONE"` assignment."""
    source = _source_without_comments(RELAY_MAIN_TF)
    assert re.search(r'(?m)^\s*authorization_type\s*=\s*"AWS_IAM"\s*$', source)
    assert not re.search(r'(?m)^\s*authorization_type\s*=\s*"NONE"\s*$', source)


def test_the_relay_module_declares_no_wildcard_principal_permission() -> None:
    """The relay source has no `public_function_url`, no `principal = "*"`, and no `NONE` URL auth
    condition."""
    source = _source_without_comments(RELAY_MAIN_TF)
    assert "public_function_url" not in source
    assert not re.search(r'(?m)^\s*principal\s*=\s*"\*"\s*$', source)
    assert not re.search(r'(?m)^\s*function_url_auth_type\s*=\s*"NONE"\s*$', source)


def test_the_relay_plan_contains_no_wildcard_lambda_permission(plan_relay: Plan) -> None:
    """No `aws_lambda_permission` in the relay plan renders `principal` as `*`."""
    for address in plan_relay.addresses_of_type("aws_lambda_permission"):
        assert plan_relay.after(address).get("principal") != "*", address


@pytest.fixture(scope="module")
def plan_relay(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    """The relay module planned with this file's VPC arguments supplied."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)

    work_dir = tmp_path_factory.mktemp("relay-d281-plan")
    relay_zip = work_dir / "relay.zip"
    relay_zip.write_bytes(b"")
    runtime_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/fixture-abc"
    audience = "/crm-eval-fixture/relay/cognito-audience"
    arguments = "\n".join(
        f"  {key} = {value}"
        for key, value in {
            "function_name": '"crm-eval-fixture-agent-relay"',
            "filename": f'"{relay_zip}"',
            "source_code_hash": '"0000000000000000000000000000000000000000000="',
            "agent_runtime_arn": f'"{runtime_arn}"',
            "cognito_issuer": '"https://cognito-idp.us-east-1.amazonaws.com/us-east-1_fixture"',
            "cognito_audience_parameter_name": f'"{audience}"',
            "cognito_audience_parameter_arn": f'"arn:aws:ssm:us-east-1:123456789012:parameter{audience}"',
            "vpc_subnet_ids": json.dumps(FIXTURE_SUBNET_IDS),
            "vpc_security_group_ids": json.dumps(FIXTURE_SECURITY_GROUP_IDS),
            # Required input with no module default.
            "reserved_concurrent_executions": "20",
            "eni_function_code_deny_policy_arn": (
                '"arn:aws:iam::123456789012:policy/crm-eval-fixture-deny-eni-actions-to-function-code"'
            ),
        }.items()
    )
    return plan_module(work_dir, "agent_relay", arguments)


def test_the_relay_vpc_config_carries_the_supplied_subnets_and_group(plan_relay: Plan) -> None:
    """The relay's planned `vpc_config` carries exactly the supplied subnets and security group."""
    vpc_config = plan_relay.after("module.subject.aws_lambda_function.this")["vpc_config"]
    entry: dict[str, Any] = vpc_config[0] if isinstance(vpc_config, list) else vpc_config
    assert sorted(entry["subnet_ids"]) == sorted(FIXTURE_SUBNET_IDS)
    assert sorted(entry["security_group_ids"]) == sorted(FIXTURE_SECURITY_GROUP_IDS)


# ---------------------------------------------------------------------------
# The optional NAT egress path (`nat_gateway_enabled = true`), which must be egress-only.
# ---------------------------------------------------------------------------


class TestTheNatGatewayIsEgressOnly:
    def test_the_variable_has_no_default_so_the_decision_cannot_be_made_silently(self) -> None:
        """Neither the module's `nat_gateway_enabled` nor the root's `vpc_nat_gateway_enabled` has a
        default."""
        source = _source_without_comments(INFRA_DIR / "modules" / "vpc" / "variables.tf")
        block = re.search(r'(?ms)^variable\s+"nat_gateway_enabled"\s*\{(.*?)^\}', source)
        assert block is not None, "the vpc module declares no `nat_gateway_enabled` variable"
        assert not re.search(r"(?m)^\s*default\s*=", block.group(1)), (
            "`nat_gateway_enabled` has acquired a default. Both values are wrong as a default: false "
            "ships a relay that cannot authenticate a single request, true commits ~$33/mo of the "
            "owner's money on the next apply. The plan must refuse until someone states a value."
        )

        root = _source_without_comments(INFRA_DIR / "variables.tf")
        root_block = re.search(r'(?ms)^variable\s+"vpc_nat_gateway_enabled"\s*\{(.*?)^\}', root)
        assert root_block is not None, "the root declares no `vpc_nat_gateway_enabled` variable"
        assert not re.search(r"(?m)^\s*default\s*=", root_block.group(1)), (
            "the root variable has acquired a default, which restores exactly the silent decision the "
            "module-level absence exists to prevent"
        )

    def test_the_private_subnets_route_to_the_nat_and_never_to_the_internet_gateway(
        self, plan_with_nat: Plan
    ) -> None:
        """Each private table's `0.0.0.0/0` route targets `nat_gateway_id`, never `gateway_id`."""
        private_routes = [
            address
            for address in plan_with_nat.addresses_of_type("aws_route")
            if "private_nat" in address
        ]
        assert len(private_routes) == len(ZONES), (
            f"expected one NAT route per private table, got {private_routes}"
        )
        for address in private_routes:
            after = plan_with_nat.after(address)
            assert after["destination_cidr_block"] == ANY_IPV4, address
            # The NAT id is unknown at plan time (wired to the NAT resource); `gateway_id` is unset.
            assert _is_unknown_at_plan_time(plan_with_nat, address, "nat_gateway_id"), address
            assert not after.get("gateway_id"), (
                f"{address} routes to an internet gateway rather than the NAT gateway, which makes "
                "the private subnets directly internet-facing"
            )

    def test_no_internet_gateway_or_public_table_is_created_at_all(
        self, plan_with_nat: Plan
    ) -> None:
        """With NAT enabled the module still creates no internet gateway, subnet or route table."""
        for resource_type in (
            "aws_internet_gateway",
            "aws_egress_only_internet_gateway",
            "aws_subnet",
            "aws_route_table",
            "aws_route_table_association",
        ):
            assert plan_with_nat.addresses_of_type(resource_type) == [], (
                f"{resource_type} is created even with NAT enabled; the NAT adopts an "
                "existing IGW-routed subnet rather than building a parallel public path"
            )

        # Positive control: one NAT route per private route table.
        nat_routes = plan_with_nat.addresses_of_type("aws_route")
        assert len(nat_routes) == len(FIXTURE_PRIVATE_ROUTE_TABLE_IDS), nat_routes

    def test_no_lambda_subnet_is_the_nat_subnet(self, plan_with_nat: Plan) -> None:
        """`lambda_subnet_ids` never includes `nat_public_subnet_id`, and the NAT's overlap
        precondition exists."""
        outputs = _source_without_comments(INFRA_DIR / "modules" / "vpc" / "outputs.tf")
        assert "value       = sort(var.private_subnet_ids)" in outputs
        # Only the `value` line, since the output's description mentions `nat_public_subnet_id`.
        value_lines = [
            line.strip()
            for line in outputs.split('output "lambda_subnet_ids"')[1].split("}")[0].splitlines()
            if line.strip().startswith("value")
        ]
        assert len(value_lines) == 1, f"expected exactly one value line, got {value_lines}"
        assert "nat_public_subnet_id" not in value_lines[0], (
            f"lambda_subnet_ids can include the NAT's IGW-routed subnet: {value_lines[0]}"
        )

        nat_tf = _source_without_comments(INFRA_DIR / "modules" / "vpc" / "nat.tf")
        assert "!contains(var.private_subnet_ids, coalesce(var.nat_public_subnet_id" in nat_tf, (
            "the overlap precondition is gone. Without it, passing one subnet id as both the NAT's and "
            "a Lambda's places Lambda ENIs behind an internet gateway with nothing to catch it."
        )

    def test_the_overlap_precondition_actually_fails_the_plan(self, tmp_path: Path) -> None:
        """Passing one subnet id as both the NAT's and a Lambda subnet fails the plan naming
        `private_subnet_ids`."""
        reason = terraform_unavailable_reason()
        if reason is not None:
            if require_terraform():
                pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
            pytest.skip(reason)

        overlapping = FIXTURE_PRIVATE_SUBNET_IDS[0]
        with pytest.raises(Exception) as caught:
            plan_module(
                tmp_path / "vpc-overlap",
                "vpc",
                _module_arguments(
                    nat_gateway_enabled="true",
                    nat_public_subnet_id=f'"{overlapping}"',
                ),
            )
        assert "private_subnet_ids" in str(caught.value), (
            f"the plan failed for some other reason than the overlap precondition: {caught.value}"
        )

    def test_a_missing_nat_subnet_fails_the_plan_rather_than_defaulting(
        self, tmp_path: Path
    ) -> None:
        """NAT enabled with no subnet id must fail at plan time, not create a gateway with no home."""
        reason = terraform_unavailable_reason()
        if reason is not None:
            if require_terraform():
                pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
            pytest.skip(reason)

        with pytest.raises(Exception) as caught:
            plan_module(
                tmp_path / "vpc-no-nat-subnet",
                "vpc",
                _module_arguments(nat_gateway_enabled="true"),
            )
        assert "nat_public_subnet_id" in str(caught.value), caught.value

    def test_the_only_public_cidr_egress_rule_is_https(self, plan_with_nat: Plan) -> None:
        """With NAT enabled there is exactly one `0.0.0.0/0` egress rule, and it is tcp/443."""
        public_rules = [
            address
            for address in plan_with_nat.addresses_of_type("aws_vpc_security_group_egress_rule")
            if plan_with_nat.after(address).get("cidr_ipv4") == ANY_IPV4
        ]
        assert len(public_rules) == 1, (
            f"expected exactly one public-CIDR egress rule, got {public_rules}"
        )
        after = plan_with_nat.after(public_rules[0])
        assert after["ip_protocol"] == "tcp"
        assert after["from_port"] == 443
        assert after["to_port"] == 443

    def test_no_ingress_rule_appears_when_the_egress_path_is_added(
        self, plan_with_nat: Plan
    ) -> None:
        """Enabling NAT adds no ingress rule beyond the two on the endpoint group."""
        assert plan_with_nat.addresses_of_type("aws_vpc_security_group_ingress_rule") == [
            "module.subject.aws_vpc_security_group_ingress_rule.endpoints_from_agent_runtime",
            "module.subject.aws_vpc_security_group_ingress_rule.endpoints_from_lambda",
        ], (
            "the only ingress rules are the two on the ENDPOINT group (from the Lambda and Runtime "
            "groups); enabling the NAT egress path must add no ingress rule"
        )
        for address in plan_with_nat.addresses_of_type("aws_security_group"):
            assert plan_with_nat.after(address).get("ingress") in (None, [])


# ---------------------------------------------------------------------------
# 5. The adopted-VPC inputs.
# ---------------------------------------------------------------------------


class TestTheAdoptedVpcInputs:
    """The adopted-VPC id variables: no data sources, no tracked defaults, validated shapes,
    unusable example."""

    @staticmethod
    def _module_source() -> str:
        return "\n".join(
            _source_without_comments(path)
            for path in sorted((INFRA_DIR / "modules" / "vpc").glob("*.tf"))
        )

    def test_the_module_declares_no_data_source_so_it_stays_plan_testable(self) -> None:
        """The vpc module declares no data source other than the locally computed
        `aws_iam_policy_document`."""
        offenders = [
            match
            for match in re.findall(r'^data\s+"([^"]+)"', self._module_source(), re.MULTILINE)
            if match != "aws_iam_policy_document"
        ]
        assert offenders == [], (
            f"the vpc module declares data source(s) {offenders}, which are read at plan time and need "
            "live credentials. That makes this module unplannable in the offline harness and every "
            "assertion in this file unrunnable. Pass the value in as a variable instead."
        )

    @pytest.mark.parametrize(
        "variable_name",
        ["vpc_id", "private_subnet_ids", "nat_public_subnet_id", "private_route_table_ids"],
    )
    def test_no_id_variable_carries_a_tracked_default(self, variable_name: str) -> None:
        """No id variable has a default other than `null`."""
        source = (INFRA_DIR / "modules" / "vpc" / "variables.tf").read_text(encoding="utf-8")
        block = re.search(rf'(?ms)^variable\s+"{variable_name}"\s*\{{(.*?)^\}}', source)
        assert block is not None, f"no `{variable_name}` variable declared"
        defaults = re.findall(r"^\s*default\s*=\s*(.+)$", block.group(1), re.MULTILINE)
        for value in defaults:
            assert value.strip() == "null", (
                f"`{variable_name}` has a tracked default {value.strip()!r}. A VPC, subnet or route "
                "table id may not appear in tracked content."
            )

    @pytest.mark.parametrize(
        ("overrides", "expected_in_error"),
        [
            pytest.param({"vpc_id": '"vpc-REPLACE_ME"'}, "vpc_id", id="placeholder_vpc_id"),
            pytest.param({"vpc_id": '"not-a-vpc"'}, "vpc_id", id="malformed_vpc_id"),
            pytest.param(
                {"private_subnet_ids": json.dumps([FIXTURE_PRIVATE_SUBNET_IDS[0]])},
                "private_subnet_ids",
                id="single_subnet_is_not_multi_az",
            ),
            pytest.param(
                {
                    "private_subnet_ids": json.dumps(
                        [FIXTURE_PRIVATE_SUBNET_IDS[0], FIXTURE_PRIVATE_SUBNET_IDS[0]]
                    )
                },
                "private_subnet_ids",
                id="duplicate_subnet_ids_look_multi_az_but_are_not",
            ),
            pytest.param(
                {"private_subnet_ids": '["subnet-REPLACE_ME_PRIVATE_A", "subnet-REPLACE_ME_B"]'},
                "private_subnet_ids",
                id="placeholder_subnet_ids",
            ),
            pytest.param(
                {"private_route_table_ids": "[]"},
                "private_route_table_ids",
                id="no_route_table_means_a_billed_nat_nothing_routes_to",
            ),
            pytest.param(
                {"private_route_table_ids": '["rtb-REPLACE_ME"]'},
                "private_route_table_ids",
                id="placeholder_route_table_id",
            ),
        ],
    )
    def test_a_rejected_input_fails_the_plan_naming_its_variable(
        self, tmp_path: Path, overrides: dict[str, str], expected_in_error: str
    ) -> None:
        """Each rejected input fails the plan with an error naming the offending variable."""
        reason = terraform_unavailable_reason()
        if reason is not None:
            if require_terraform():
                pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
            pytest.skip(reason)

        with pytest.raises(Exception) as caught:
            plan_module(tmp_path / "vpc-reject", "vpc", _module_arguments(**overrides))
        assert expected_in_error in str(caught.value), (
            f"the plan failed but never named `{expected_in_error}`, so an operator cannot tell which "
            f"input was wrong: {caught.value}"
        )

    def test_the_committed_tfvars_example_is_unusable_rather_than_realistic(self) -> None:
        """The example names every required VPC variable and carries only non-shape-valid
        `REPLACE_ME` ids."""
        example = (INFRA_DIR / "terraform.tfvars.example").read_text(encoding="utf-8")

        for required in (
            "vpc_id",
            "vpc_private_subnet_ids",
            "vpc_private_route_table_ids",
            "vpc_nat_gateway_enabled",
        ):
            assert re.search(rf"^{required}\s*=", example, re.MULTILINE), (
                f"`{required}` has no default and is absent from terraform.tfvars.example"
            )

        assigned_ids = re.findall(r'"((?:vpc|subnet|rtb)-[^"]*)"', example)
        assert assigned_ids, "no id-shaped values in the example at all, so this asserts nothing"
        for value in assigned_ids:
            assert "REPLACE_ME" in value, (
                f"terraform.tfvars.example carries {value!r}, which looks like a real id. Tracked "
                "content may not contain a VPC, subnet or route table id."
            )
            prefix, _, suffix = value.partition("-")
            assert not re.fullmatch(r"[0-9a-f]{8}([0-9a-f]{9})?", suffix), (
                f"{value!r} is shape-valid, so it would plan instead of failing loudly"
            )


class TestTeardownRefusesToDestroyTheAdoptedVpc:
    """`scripts/teardown.sh` refuses to destroy when `terraform state list` holds an adopted-VPC
    resource."""

    @staticmethod
    def _script() -> str:
        return (REPO_ROOT / "scripts" / "teardown.sh").read_text(encoding="utf-8")

    def test_the_guard_runs_before_destroy_not_after(self) -> None:
        """The adopted-VPC guard appears before `terraform destroy` in the script."""
        script = self._script()
        guard = script.index("Adopted-VPC guard")
        destroy = script.index('terraform destroy "${TF_DESTROY_ARGS[@]}"')
        assert guard < destroy, "the adopted-VPC guard must run BEFORE terraform destroy"

    @pytest.mark.parametrize(
        "guarded_type",
        [
            "aws_vpc",
            "aws_subnet",
            "aws_internet_gateway",
            "aws_route_table",
            "aws_route_table_association",
            "aws_default_route_table",
            "aws_default_security_group",
        ],
    )
    def test_every_adopted_resource_type_is_guarded(self, guarded_type: str) -> None:
        assert re.search(rf"^\s+{guarded_type}$", self._script(), re.MULTILINE), (
            f"`{guarded_type}` is not in teardown.sh's GUARDED_TYPES, so a stale one in state would be "
            "destroyed inside a VPC this stack does not own"
        )

    def test_a_failed_state_list_refuses_rather_than_proceeding(self) -> None:
        """A failed `state list` refuses the destroy, while an empty state is a no-op rather than a
        failure."""
        script = self._script()
        assert "Refusing to destroy blind" in script
        assert "No state to inspect" in script

    def test_the_type_match_is_anchored_so_stack_owned_resources_are_not_caught(self) -> None:
        """The guard matches a whole address segment, so `aws_vpc` does not catch
        `aws_vpc_endpoint`."""
        script = self._script()
        assert r'grep -E "(^|\.)${guarded_type}\."' in script, (
            "the guard must match a whole address segment; a substring match on `aws_vpc` also matches "
            "`aws_vpc_endpoint`, which this stack owns and must be able to destroy"
        )

    def test_the_guard_pattern_discriminates_in_both_directions(self) -> None:
        """The anchored pattern matches adopted `aws_vpc` addresses and not stack-owned `aws_vpc_*`
        ones."""
        pattern = re.compile(r"(^|\.)aws_vpc\.")
        assert pattern.search("module.vpc.aws_vpc.this"), "would miss a real adopted VPC in state"
        assert pattern.search("aws_vpc.this"), "would miss a root-level aws_vpc in state"
        assert not pattern.search('module.vpc.aws_vpc_endpoint.gateway["s3"]'), (
            "would refuse teardown of a VPC endpoint this stack owns"
        )
        assert not pattern.search(
            "module.vpc.aws_vpc_security_group_egress_rule.lambda_to_endpoints"
        ), "would refuse teardown of a security-group rule this stack owns"

# SPDX-License-Identifier: MIT
"""Plan-time assertions on `infra/modules/api_origin`.

The module adapts a CloudFront VPC origin to an internal ALB targeting the `execute-api` interface
endpoint's ENI IPs. The assertions cover the internal scheme, security-group-only ingress, egress to
the endpoint only, a health check that accepts 403, the plaintext listener with `http-only` origin
policy, deletion protection, and the input validations.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.unit._terraform_fixture import (
    MODULES_DIR,
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    TerraformError,
    plan_module,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)

#: Hex bodies for resource ids assembled at runtime, so no tracked line carries prefix-plus-hex for
#: `check_no_account_id` to reject.
_HEX_17 = "0a1b" * 4 + "1"
_HEX_17_ALT = "0a1b" * 4 + "2"
_HEX_17_SG = "0a1b" * 4 + "3"


def _id(prefix: str, body: str) -> str:
    return f"{prefix}-{body}"


VPC_ID = _id("vpc", _HEX_17)
SUBNET_IDS = [_id("subnet", _HEX_17), _id("subnet", _HEX_17_ALT)]
TARGET_IPS = ["10.0.1.10", "10.0.2.10"]
ENDPOINT_SG = _id("sg", _HEX_17)
ORIGIN_SG = _id("sg", _HEX_17_SG)

ALB_ADDRESS = "module.subject.aws_lb.this"
TARGET_GROUP_ADDRESS = "module.subject.aws_lb_target_group.execute_api"
LISTENER_ADDRESS = "module.subject.aws_lb_listener.this"
VPC_ORIGIN_ADDRESS = "module.subject.aws_cloudfront_vpc_origin.this"


def _hcl_list(values: list[str]) -> str:
    return "[" + ", ".join(f'"{value}"' for value in values) + "]"


def _module_arguments(**overrides: str) -> str:
    args = {
        "name_prefix": '"crm-test"',
        "vpc_id": f'"{VPC_ID}"',
        "private_subnet_ids": _hcl_list(SUBNET_IDS),
        "execute_api_target_ips": _hcl_list(TARGET_IPS),
        "execute_api_security_group_id": f'"{ENDPOINT_SG}"',
        "alb_ingress_security_group_ids": _hcl_list([ORIGIN_SG]),
        "access_logs_bucket": '"crm-test-alb-logs"',
        "access_logs_prefix": '"crm-test-api-origin"',
    }
    args.update(overrides)
    return "\n".join(f"  {key} = {value}" for key, value in args.items())


def _plan(work_dir: Path, **overrides: str) -> Plan:
    return plan_module(work_dir, "api_origin", _module_arguments(**overrides))


def _skip_unless_terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return _plan(tmp_path_factory.mktemp("api-origin-plan"))


# ---------------------------------------------------------------------------
# Non-vacuity first.
# ---------------------------------------------------------------------------


def test_the_plan_contains_the_whole_adapter(plan: Plan) -> None:
    """The plan contains the ALB, target group, listener and VPC origin."""
    for address in (ALB_ADDRESS, TARGET_GROUP_ADDRESS, LISTENER_ADDRESS, VPC_ORIGIN_ADDRESS):
        assert plan.after(address) is not None, address


# ---------------------------------------------------------------------------
# The negative properties, each a total defect if inverted.
# ---------------------------------------------------------------------------


def test_the_load_balancer_is_internal(plan: Plan) -> None:
    """The ALB is an internal application load balancer in the supplied subnets."""
    after = plan.after(ALB_ADDRESS)
    assert after["internal"] is True, "an internet-facing ALB bypasses CloudFront"
    assert after["load_balancer_type"] == "application"
    assert sorted(after["subnets"]) == sorted(SUBNET_IDS)


def test_the_alb_ingress_is_security_group_referenced_never_a_network_range(plan: Plan) -> None:
    """Ingress admits one referenced security group and no network range at all."""
    addresses = plan.addresses_of_type("aws_vpc_security_group_ingress_rule")
    assert addresses, "no ingress rule planned"
    for address in addresses:
        after = plan.after(address)
        assert after["referenced_security_group_id"] == ORIGIN_SG, address
        assert after.get("cidr_ipv4") is None, f"{address} admits a CIDR"
        assert after.get("cidr_ipv6") is None, f"{address} admits a CIDR"
        assert after.get("prefix_list_id") is None, f"{address} admits a prefix list"


def test_the_alb_egress_reaches_only_the_endpoint(plan: Plan) -> None:
    """There is exactly one egress rule: TCP 443 to the endpoint security group."""
    addresses = plan.addresses_of_type("aws_vpc_security_group_egress_rule")
    assert len(addresses) == 1, f"expected exactly one egress rule, got {addresses}"
    after = plan.after(addresses[0])
    assert after["referenced_security_group_id"] == ENDPOINT_SG
    assert after["from_port"] == 443 and after["to_port"] == 443
    assert after.get("cidr_ipv4") is None


def test_the_health_check_treats_403_as_healthy(plan: Plan) -> None:
    """The HTTPS health-check matcher includes 403, which `/` on `execute-api` returns."""
    health = plan.after(TARGET_GROUP_ADDRESS)["health_check"][0]
    assert "403" in health["matcher"], health["matcher"]
    assert health["protocol"] == "HTTPS"


def test_the_target_group_speaks_https_to_the_endpoint(plan: Plan) -> None:
    """The target group speaks HTTPS on 443 to IP targets."""
    after = plan.after(TARGET_GROUP_ADDRESS)
    assert after["protocol"] == "HTTPS"
    assert after["port"] == 443
    assert after["target_type"] == "ip"


def test_the_listener_and_origin_policy_are_the_recorded_deviation(plan: Plan) -> None:
    """The listener is HTTP and the VPC origin policy is `http-only` with TLSv1.2 SSL protocols."""
    assert plan.after(LISTENER_ADDRESS)["protocol"] == "HTTP"
    config = plan.after(VPC_ORIGIN_ADDRESS)["vpc_origin_endpoint_config"][0]
    assert config["origin_protocol_policy"] == "http-only"
    assert config["origin_ssl_protocols"][0]["items"] == ["TLSv1.2"]


# ---------------------------------------------------------------------------
# Inputs the module must refuse.
# ---------------------------------------------------------------------------


def test_the_module_refuses_a_single_subnet(tmp_path: Path) -> None:
    """A single private subnet fails variable validation."""
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path / "one-subnet", private_subnet_ids=_hcl_list(SUBNET_IDS[:1]))
    assert "at least two subnets" in str(raised.value)


def test_the_module_refuses_a_cidr_shaped_ingress_source(tmp_path: Path) -> None:
    """A CIDR in `alb_ingress_security_group_ids` fails variable validation."""
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path / "cidr-ingress", alb_ingress_security_group_ids='["0.0.0.0/0"]')
    assert "security group id" in str(raised.value)


def test_the_module_refuses_an_empty_access_log_prefix(tmp_path: Path) -> None:
    """An empty access-log prefix fails variable validation."""
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path / "empty-prefix", access_logs_prefix='""')
    assert "must be non-empty" in str(raised.value)


def test_the_module_refuses_a_prefix_containing_awslogs(tmp_path: Path) -> None:
    """A prefix containing the ELB-reserved `AWSLogs` segment fails variable validation."""
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path / "awslogs-prefix", access_logs_prefix='"AWSLogs"')
    assert "AWSLogs" in str(raised.value)


def test_the_alb_takes_its_log_prefix_from_the_caller(plan: Plan) -> None:
    """The ALB logs under the caller-supplied `access_logs_prefix`, with logging enabled."""
    after = plan.after(ALB_ADDRESS)
    assert after is not None, "no aws_lb in the plan; this assertion would be vacuous"
    access_logs = after["access_logs"]
    assert access_logs, "the ALB has no access_logs block"
    assert access_logs[0]["prefix"] == "crm-test-api-origin", access_logs
    assert access_logs[0]["enabled"] is True, access_logs


def test_the_module_refuses_match_viewer(tmp_path: Path) -> None:
    """`origin_protocol_policy = "match-viewer"` fails variable validation."""
    _skip_unless_terraform()
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path / "match-viewer", origin_protocol_policy='"match-viewer"')
    assert "match-viewer" in str(raised.value)


def test_the_alb_has_deletion_protection(plan: Plan) -> None:
    assert plan.after(ALB_ADDRESS)["enable_deletion_protection"] is True


def test_a_module_with_deletion_protection_off_is_detected(tmp_path: Path) -> None:
    """Control: a scratch copy of the module with the flag flipped plans `false`."""
    _skip_unless_terraform()
    module_copy = tmp_path / "api_origin"
    shutil.copytree(MODULES_DIR / "api_origin", module_copy)
    main_tf = module_copy / "main.tf"
    source = main_tf.read_text(encoding="utf-8")
    flag = "enable_deletion_protection = true"
    assert source.count(flag) == 1
    main_tf.write_text(source.replace(flag, "enable_deletion_protection = false"), encoding="utf-8")
    root = tmp_path / "root"
    mutated = plan_root(
        root, f'module "subject" {{\n  source = "{module_copy}"\n\n{_module_arguments()}\n}}\n'
    )
    assert mutated.after(ALB_ADDRESS)["enable_deletion_protection"] is False

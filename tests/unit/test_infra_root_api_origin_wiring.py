# SPDX-License-Identifier: MIT
"""The root composition connects the private `/api/*` path.

Asserts three things: the endpoint-group ingress rule from `infra/main.tf`, planned with its two module
references replaced by known ids; which module outputs that rule references, read textually; and that
the `api_origin` call site wires every required variable of `modules/api_origin`.
"""

from __future__ import annotations

import re

import pytest
from scripts.hcl_text import blank_comments, blank_heredoc_bodies

from tests.unit._terraform_fixture import (
    MODULES_DIR,
    REPO_ROOT,
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_module,
    require_terraform,
    terraform_unavailable_reason,
)

ROOT_MAIN = REPO_ROOT / "infra" / "main.tf"

#: The rule under test, and the two module outputs it must join.
RULE_TYPE = "aws_vpc_security_group_ingress_rule"
RULE_NAME = "endpoints_from_api_origin_alb"
ENDPOINT_SG_REFERENCE = "module.vpc.execute_api_security_group_id"
ALB_SG_REFERENCE = "module.api_origin.alb_security_group_id"

#: Synthetic ids are assembled at runtime, since `check_no_account_id` rejects literal `sg-`/`vpc-`/
#: `subnet-` ids in tracked content.
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
ALB_SG = _id("sg", _HEX_17_ALT)

RULE_ADDRESS = f"{RULE_TYPE}.{RULE_NAME}"


def _skip_unless_terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)


# ---------------------------------------------------------------------------
# Block extraction from the committed root, brace-matched rather than regexed.
# ---------------------------------------------------------------------------


def _extract_block(text: str, header: str) -> str:
    """The verbatim source of the one block whose header line contains `header`.

    Braces are matched over a copy with heredoc bodies and comments blanked; the slice is cut from the
    original text.
    """
    scannable = blank_comments(blank_heredoc_bodies(text))
    original_lines = text.splitlines(keepends=True)
    scan_lines = scannable.splitlines(keepends=True)
    assert len(original_lines) == len(scan_lines), "blanking changed the line count"

    starts = [index for index, line in enumerate(scan_lines) if header in line]
    assert len(starts) == 1, (
        f"expected exactly one block header containing {header!r}, got {starts}"
    )

    start = starts[0]
    depth = 0
    for offset, line in enumerate(scan_lines[start:], start=start):
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return "".join(original_lines[start : offset + 1])
    raise AssertionError(f"unterminated block for {header!r}")


@pytest.fixture(scope="module")
def root_text() -> str:
    return ROOT_MAIN.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rule_source(root_text: str) -> str:
    return _extract_block(root_text, f'resource "{RULE_TYPE}" "{RULE_NAME}"')


@pytest.fixture(scope="module")
def module_source(root_text: str) -> str:
    return _extract_block(root_text, 'module "api_origin"')


def test_the_extractor_is_not_vacuous(root_text: str) -> None:
    """The extractor finds a known block and raises on an absent one."""
    assert _extract_block(root_text, 'module "vpc"').startswith('module "vpc"')
    with pytest.raises(AssertionError):
        _extract_block(root_text, 'module "no_such_module_exists_anywhere"')


# ---------------------------------------------------------------------------
# 1. The ingress rule, planned from its own committed text.
# ---------------------------------------------------------------------------


def _api_origin_arguments() -> str:
    args = {
        "name_prefix": '"crm-test"',
        "vpc_id": f'"{VPC_ID}"',
        "private_subnet_ids": "[" + ", ".join(f'"{s}"' for s in SUBNET_IDS) + "]",
        "execute_api_target_ips": "[" + ", ".join(f'"{ip}"' for ip in TARGET_IPS) + "]",
        "execute_api_security_group_id": f'"{ENDPOINT_SG}"',
        "alb_ingress_security_group_ids": f'["{ORIGIN_SG}"]',
        "access_logs_bucket": '"crm-test-alb-logs"',
        # `test_infra_api_origin.py` builds the same `api_origin` arguments.
        "access_logs_prefix": '"crm-test-api-origin"',
    }
    return "\n".join(f"  {key} = {value}" for key, value in args.items())


def _plannable_rule(rule_source: str) -> str:
    """The committed rule with its two module references replaced by known ids.

    Asserts that both references were present and substituted.
    """
    substituted = rule_source.replace(ENDPOINT_SG_REFERENCE, f'"{ENDPOINT_SG}"').replace(
        ALB_SG_REFERENCE, f'"{ALB_SG}"'
    )
    assert ENDPOINT_SG_REFERENCE not in substituted, "endpoint-group reference was not substituted"
    assert ALB_SG_REFERENCE not in substituted, "ALB-group reference was not substituted"
    assert substituted != rule_source, "nothing was substituted, so the plan tests a rule verbatim"
    return substituted


@pytest.fixture(scope="module")
def plan(rule_source: str, tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return plan_module(
        tmp_path_factory.mktemp("root-api-origin"),
        "api_origin",
        _api_origin_arguments(),
        extra_hcl=_plannable_rule(rule_source),
    )


def test_the_endpoint_group_admits_the_alb_on_443(plan: Plan) -> None:
    """The rule admits the ALB security group to the endpoint security group on TCP 443."""
    after = plan.after(RULE_ADDRESS)
    assert after["security_group_id"] == ENDPOINT_SG, "the rule is not on the endpoint group"
    assert after["referenced_security_group_id"] == ALB_SG, "the rule does not admit the ALB group"
    assert after["ip_protocol"] == "tcp"
    assert after["from_port"] == 443, "the endpoint speaks only 443"
    assert after["to_port"] == 443


def test_the_rule_admits_a_security_group_and_never_a_network_range(plan: Plan) -> None:
    """The rule's source is a security group, never an IPv4/IPv6 range or a prefix list."""
    after = plan.after(RULE_ADDRESS)
    assert after.get("cidr_ipv4") is None, "the rule admits an IPv4 range"
    assert after.get("cidr_ipv6") is None, "the rule admits an IPv6 range"
    assert after.get("prefix_list_id") is None, "the rule admits a prefix list"


def test_the_adapter_plans_alongside_the_rule(plan: Plan) -> None:
    """The plan also contains the module's internal ALB."""
    assert plan.after("module.subject.aws_lb.this")["internal"] is True


# ---------------------------------------------------------------------------
# 2. The two endpoints the plan cannot see.
# ---------------------------------------------------------------------------


def test_the_rule_joins_the_endpoint_group_to_the_alb_group(rule_source: str) -> None:
    """The rule references the VPC module's endpoint group and the `api_origin` module's ALB group."""
    assert f"security_group_id            = {ENDPOINT_SG_REFERENCE}" in rule_source or (
        re.search(
            rf"^\s*security_group_id\s*=\s*{re.escape(ENDPOINT_SG_REFERENCE)}\s*$",
            rule_source,
            re.MULTILINE,
        )
    ), rule_source
    assert re.search(
        rf"^\s*referenced_security_group_id\s*=\s*{re.escape(ALB_SG_REFERENCE)}\s*$",
        rule_source,
        re.MULTILINE,
    ), rule_source


def test_the_rule_is_declared_at_the_root_and_not_inside_either_module() -> None:
    """Neither `modules/vpc` nor `modules/api_origin` declares the root's ingress rule."""
    for module_name in ("vpc", "api_origin"):
        for path in sorted((MODULES_DIR / module_name).glob("*.tf")):
            text = path.read_text(encoding="utf-8")
            assert f'"{RULE_NAME}"' not in text, f"{path} declares the root's rule"


# ---------------------------------------------------------------------------
# 3. The instantiation, complete against the module's own required variables.
# ---------------------------------------------------------------------------


def _required_variable_names(module_name: str) -> set[str]:
    """Names of every variable in `modules/<module_name>` that has no default."""
    required: set[str] = set()
    for path in sorted((MODULES_DIR / module_name).glob("*.tf")):
        text = path.read_text(encoding="utf-8")
        for header in re.finditer(r'^variable\s+"([a-z0-9_]+)"\s*\{', text, re.MULTILINE):
            block = _extract_block(text, header.group(0).rstrip())
            if not re.search(r"^\s*default\s*=", block, re.MULTILINE):
                required.add(header.group(1))
    assert required, f"no required variables found in modules/{module_name}"
    return required


def test_every_required_module_variable_is_wired_at_the_call_site(module_source: str) -> None:
    argument_names = set(
        re.findall(r"^\s{2}([a-z0-9_]+)\s*=", module_source, re.MULTILINE),
    )
    missing = sorted(_required_variable_names("api_origin") - argument_names)
    assert missing == [], f'module "api_origin" does not wire: {missing}'


def test_the_call_site_wires_the_real_module_outputs_rather_than_literals(
    module_source: str,
) -> None:
    """The `api_origin` call site reads its VPC, subnet, security-group and bucket values from module outputs and variables."""
    for expected in (
        "module.vpc.vpc_id",
        "module.vpc.lambda_subnet_ids",
        "module.vpc.execute_api_security_group_id",
        "module.frontend_cdn.logs_bucket_name",
        "var.api_origin_ingress_security_group_ids",
    ):
        assert expected in module_source, expected


def test_the_target_ips_come_from_the_endpoints_own_enis(
    module_source: str, root_text: str
) -> None:
    """The target IPs come from a root `data "aws_network_interface"` over the endpoint's ENI ids, using `count`."""
    data_source = _extract_block(root_text, 'data "aws_network_interface" "execute_api"')
    assert "module.vpc.execute_api_network_interface_ids" in data_source
    assert re.search(r"^\s*count\s*=", data_source, re.MULTILINE), (
        "for_each over unknown ENI ids is a hard plan error on a first apply"
    )
    assert "data.aws_network_interface.execute_api" in module_source

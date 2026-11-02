# SPDX-License-Identifier: MIT
"""Plan-time properties of the `infra/bootstrap_account/` root, planned offline.

The root's own `.tf` files (minus the partial backend) are copied to a tmp dir beside a provider
override that supplies mock credentials, so the planned provider block, `default_tags` included, is
the root's own. The region map is copied to the same relative path, `../regions.json`.
`CRM_REQUIRE_TERRAFORM=1` turns an unavailable terraform into a failure.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from scripts import proc_run, region_map

from tests.unit._terraform_fixture import (
    REPO_ROOT,
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    TerraformError,
    plan_directory,
    require_terraform,
    terraform_unavailable_reason,
)

ROOT_DIR = REPO_ROOT / "infra" / "bootstrap_account"

#: Not copied: a `backend "s3" {}` block makes `terraform plan` demand a backend init.
EXCLUDED_FILES = frozenset({"backend.tf"})

_MOCK_PROVIDER_OVERRIDE = """
provider "aws" {
  profile                     = null
  access_key                  = "mock"
  secret_key                  = "mock" # pragma: allowlist secret
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
}
"""

REGION_MAP = region_map.load()

ALLOWED_AZ_IDS = frozenset(REGION_MAP.entry("us-east-1").agentcore_az_ids)

#: Resource types whose schema carries `tags_all`; every planned instance must carry the tags.
TAGGABLE_TYPES = frozenset(
    {
        "aws_vpc",
        "aws_default_security_group",
        "aws_internet_gateway",
        "aws_subnet",
        "aws_route_table",
        "aws_iam_role",
    }
)

#: Every resource type the root may declare; anything else (NAT, EIP, endpoint, flow log) fails.
EXPECTED_TYPES = TAGGABLE_TYPES | {
    "aws_route",
    "aws_route_table_association",
    "aws_iam_role_policy_attachment",
    "aws_api_gateway_account",
}

REQUIRED_TAG_KEYS = (
    "service",
    "environment",
    "owner",
    "cost-center",
    "data-classification",
    "component",
    "AppName",
)


def _skip_unless_terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)


def _isolated_aws_env(monkeypatch: pytest.MonkeyPatch, scratch: Path) -> None:
    """No ambient profile or shared config can reach the mock-credentialed plan."""
    for name in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    empty = scratch / "empty-aws-config"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(empty))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(empty))


def _plan(scratch: Path, tfvars: str = "", *, map_json: str | None = None) -> Plan:
    """Plan the root copied to `scratch/bootstrap_account`, with the map (or `map_json`) beside it."""
    work_dir = scratch / "bootstrap_account"
    work_dir.mkdir(parents=True, exist_ok=True)
    (scratch / "regions.json").write_text(
        region_map.MAP_PATH.read_text(encoding="utf-8") if map_json is None else map_json,
        encoding="utf-8",
    )
    copied = 0
    for source in sorted(ROOT_DIR.glob("*.tf")):
        if source.name in EXCLUDED_FILES:
            continue
        shutil.copyfile(source, work_dir / source.name)
        copied += 1
    assert copied >= 5, f"only {copied} .tf files copied from {ROOT_DIR}"
    (work_dir / "mock_provider_override.tf").write_text(_MOCK_PROVIDER_OVERRIDE, encoding="utf-8")
    (work_dir / "terraform.tfvars").write_text(
        f'aws_profile = "unused-mock"\n{tfvars}', encoding="utf-8"
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        _isolated_aws_env(monkeypatch, work_dir)
        return plan_directory(work_dir)


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return _plan(tmp_path_factory.mktemp("bootstrap-account-plan"))


@pytest.fixture(scope="module")
def plan_without_apigw(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return _plan(
        tmp_path_factory.mktemp("bootstrap-account-plan-no-apigw"),
        "manage_apigw_account_logging = false\n",
    )


def _types(plan: Plan) -> dict[str, list[str]]:
    by_type: dict[str, list[str]] = {}
    for address, change in plan.resource_changes.items():
        by_type.setdefault(str(change.get("type")), []).append(address)
    return by_type


def _config_resources(plan: Plan) -> dict[str, dict[str, Any]]:
    root = plan.configuration.get("root_module", {})
    resources = root.get("resources", []) if isinstance(root, dict) else []
    return {r["address"]: r for r in resources if isinstance(r, dict) and "address" in r}


def _references(expressions: dict[str, Any], key: str) -> list[str]:
    value = expressions.get(key)
    if not isinstance(value, dict):
        return []
    refs = value.get("references", [])
    return [str(r) for r in refs] if isinstance(refs, list) else []


# ---------------------------------------------------------------------------
# Non-vacuity.
# ---------------------------------------------------------------------------


def test_the_plan_holds_every_expected_resource_type(plan: Plan) -> None:
    """Every declared type is planned, and nothing outside the expected set is."""
    assert set(_types(plan)) == EXPECTED_TYPES


def test_no_nat_eip_endpoint_or_flow_log_is_planned(plan: Plan) -> None:
    """The root creates no NAT gateway, EIP, VPC endpoint or flow log; the stack owns those."""
    for forbidden in ("aws_nat_gateway", "aws_eip", "aws_vpc_endpoint", "aws_flow_log"):
        assert plan.addresses_of_type(forbidden) == [], forbidden


# ---------------------------------------------------------------------------
# Network shape.
# ---------------------------------------------------------------------------


def test_five_subnets_are_placed_by_the_default_az_ids(plan: Plan) -> None:
    """Public in use1-az1; private and Runtime pairs in use1-az1 then use1-az2."""
    placed = {
        address: plan.after(address)["availability_zone_id"]
        for address in plan.addresses_of_type("aws_subnet")
    }
    assert placed == {
        "aws_subnet.public": "use1-az1",
        "aws_subnet.private[0]": "use1-az1",
        "aws_subnet.private[1]": "use1-az2",
        "aws_subnet.runtime[0]": "use1-az1",
        "aws_subnet.runtime[1]": "use1-az2",
    }
    assert set(placed.values()) <= ALLOWED_AZ_IDS
    for address in placed:
        assert plan.after(address).get("availability_zone") is None, (
            f"{address} pins an AZ name; placement must be by AZ id only"
        )


def test_no_subnet_maps_public_ips(plan: Plan) -> None:
    subnets = plan.addresses_of_type("aws_subnet")
    assert len(subnets) == 5
    for address in subnets:
        assert plan.after(address)["map_public_ip_on_launch"] is False, address


def test_subnet_cidrs_are_distinct_slash_24s_inside_the_vpc(plan: Plan) -> None:
    vpc_cidr = plan.after("aws_vpc.this")["cidr_block"]
    assert vpc_cidr == "10.42.0.0/16"
    cidrs = [plan.after(a)["cidr_block"] for a in plan.addresses_of_type("aws_subnet")]
    assert len(set(cidrs)) == 5
    for cidr in cidrs:
        assert cidr.startswith("10.42.") and cidr.endswith("/24"), cidr


def test_vpc_enables_dns_support_and_hostnames(plan: Plan) -> None:
    vpc = plan.after("aws_vpc.this")
    assert vpc["enable_dns_support"] is True
    assert vpc["enable_dns_hostnames"] is True


def test_default_security_group_declares_no_rule(plan: Plan) -> None:
    """The default SG is adopted with zero ingress and zero egress blocks, which empties it."""
    resources = _config_resources(plan)
    expressions = resources["aws_default_security_group.this"].get("expressions", {})
    assert "ingress" not in expressions, expressions.get("ingress")
    assert "egress" not in expressions, expressions.get("egress")
    assert _references(expressions, "vpc_id")[:1] == ["aws_vpc.this.id"]


def test_no_route_table_declares_an_inline_route(plan: Plan) -> None:
    resources = _config_resources(plan)
    tables = [a for a, r in resources.items() if r.get("type") == "aws_route_table"]
    assert sorted(tables) == [
        "aws_route_table.private",
        "aws_route_table.public",
        "aws_route_table.runtime",
    ]
    for address in tables:
        assert "route" not in resources[address].get("expressions", {}), address


def test_the_only_route_is_public_default_to_the_internet_gateway(plan: Plan) -> None:
    """Exactly one route exists: 0.0.0.0/0 on the public table, targeting the IGW only."""
    assert plan.addresses_of_type("aws_route") == ["aws_route.public_default"]
    route = plan.after("aws_route.public_default")
    assert route["destination_cidr_block"] == "0.0.0.0/0"
    for target in ("nat_gateway_id", "transit_gateway_id", "vpc_peering_connection_id"):
        assert route.get(target) is None, target
    expressions = _config_resources(plan)["aws_route.public_default"]["expressions"]
    assert "aws_internet_gateway.this.id" in _references(expressions, "gateway_id")
    assert "aws_route_table.public.id" in _references(expressions, "route_table_id")
    assert set(expressions) == {"route_table_id", "destination_cidr_block", "gateway_id"}


def _resource_reference(expressions: dict[str, Any], key: str) -> tuple[str, bool]:
    """The `aws_*` resource `key` references, and whether it is indexed by `count.index`."""
    refs = _references(expressions, key)
    resources = {r.removesuffix(".id") for r in refs if r.startswith("aws_")}
    assert len(resources) == 1, f"{key} references {refs}"
    return resources.pop(), "count.index" in refs


def test_every_subnet_has_an_explicit_association_with_its_own_table(plan: Plan) -> None:
    resources = _config_resources(plan)
    pairs = {
        address: (
            _resource_reference(resource["expressions"], "subnet_id"),
            _resource_reference(resource["expressions"], "route_table_id"),
        )
        for address, resource in resources.items()
        if resource.get("type") == "aws_route_table_association"
    }
    assert pairs == {
        "aws_route_table_association.public": (
            ("aws_subnet.public", False),
            ("aws_route_table.public", False),
        ),
        "aws_route_table_association.private": (
            ("aws_subnet.private", True),
            ("aws_route_table.private", True),
        ),
        "aws_route_table_association.runtime": (
            ("aws_subnet.runtime", True),
            ("aws_route_table.runtime", False),
        ),
    }
    assert len(plan.addresses_of_type("aws_route_table_association")) == 5
    assert len(plan.addresses_of_type("aws_route_table")) == 4


def test_no_main_route_table_association_is_declared(plan: Plan) -> None:
    for forbidden in ("aws_main_route_table_association", "aws_default_route_table"):
        assert plan.addresses_of_type(forbidden) == [], forbidden


# ---------------------------------------------------------------------------
# API Gateway CloudWatch Logs role and account setting.
# ---------------------------------------------------------------------------


def test_apigw_role_trusts_only_the_api_gateway_service(plan: Plan) -> None:
    role = plan.after("aws_iam_role.apigw_cloudwatch[0]")
    assert role["name"] == "crm-agent-eval-apigw-cloudwatch-logs"
    trust = json.loads(role["assume_role_policy"])
    statements = trust["Statement"]
    assert len(statements) == 1
    statement = statements[0]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "sts:AssumeRole"
    assert statement["Principal"] == {"Service": "apigateway.amazonaws.com"}


def test_apigw_role_carries_only_the_managed_push_to_logs_policy(plan: Plan) -> None:
    assert plan.addresses_of_type("aws_iam_role_policy_attachment") == [
        "aws_iam_role_policy_attachment.apigw_cloudwatch[0]"
    ]
    attachment = plan.after("aws_iam_role_policy_attachment.apigw_cloudwatch[0]")
    assert attachment["policy_arn"] == (
        "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
    )
    assert attachment["role"] == "crm-agent-eval-apigw-cloudwatch-logs"
    for inline in ("aws_iam_role_policy", "aws_iam_policy"):
        assert plan.addresses_of_type(inline) == [], inline


def test_the_account_setting_points_at_the_role(plan: Plan) -> None:
    assert plan.addresses_of_type("aws_api_gateway_account") == ["aws_api_gateway_account.this[0]"]
    expressions = _config_resources(plan)["aws_api_gateway_account.this"]["expressions"]
    assert "aws_iam_role.apigw_cloudwatch[0].arn" in _references(expressions, "cloudwatch_role_arn")


def test_flag_false_plans_no_role_attachment_or_account_setting(plan_without_apigw: Plan) -> None:
    for resource_type in (
        "aws_api_gateway_account",
        "aws_iam_role",
        "aws_iam_role_policy_attachment",
    ):
        assert plan_without_apigw.addresses_of_type(resource_type) == [], resource_type
    assert len(plan_without_apigw.addresses_of_type("aws_subnet")) == 5


# ---------------------------------------------------------------------------
# Tags.
# ---------------------------------------------------------------------------


def test_every_taggable_resource_carries_the_mandatory_tags(plan: Plan) -> None:
    tagged = [a for a, c in plan.resource_changes.items() if c.get("type") in TAGGABLE_TYPES]
    assert len(tagged) == 13, sorted(tagged)
    for address in tagged:
        tags = plan.after(address).get("tags_all")
        assert isinstance(tags, dict), f"{address} has no planned tags_all"
        for key in REQUIRED_TAG_KEYS:
            assert tags.get(key), f"{address} lacks tag {key!r}: {tags}"
        assert tags["service"] == "crm-agent-eval"
        assert tags["data-classification"] == "internal"
        assert tags["component"] == "bootstrap-account"
        assert tags["AppName"] == "crm-agent-eval"


# ---------------------------------------------------------------------------
# Variable validations.
# ---------------------------------------------------------------------------


@pytest.fixture
def _terraform() -> Iterator[None]:
    _skip_unless_terraform()
    yield


@pytest.mark.parametrize(
    ("tfvars", "message"),
    [
        ('aws_region = "eu-west-1"\n', "aws_region must be a US commercial region"),
        ('aws_region = "us-gov-west-1"\n', "aws_region must be a US commercial region"),
        ('runtime_az_ids = ["use1-az1", "use1-az1"]\n', "runtime_az_ids must hold"),
        ('private_az_ids = ["use1-az1"]\n', "private_az_ids must hold"),
        ('vpc_cidr = "10.42.0.0/20"\n', "vpc_cidr must be an IPv4 network CIDR of exactly /16"),
        ('vpc_cidr = "10.42.0.0/24"\n', "vpc_cidr must be an IPv4 network CIDR of exactly /16"),
        ('vpc_cidr = "10.0.0.0/8"\n', "vpc_cidr must be an IPv4 network CIDR of exactly /16"),
        ('vpc_cidr = "10.42.1.0/16"\n', "vpc_cidr must be an IPv4 network CIDR of exactly /16"),
        ('vpc_cidr = "fd00:42::/16"\n', "vpc_cidr must be an IPv4 network CIDR of exactly /16"),
    ],
    ids=[
        "region-eu-west-1",
        "region-us-gov-west-1",
        "runtime-duplicate",
        "private-single",
        "cidr-slash-20",
        "cidr-slash-24",
        "cidr-slash-8",
        "cidr-host-bits",
        "cidr-ipv6",
    ],
)
def test_validation_rejects(_terraform: None, tmp_path: Path, tfvars: str, message: str) -> None:
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path, tfvars)
    assert "Invalid value for variable" in str(raised.value)
    assert message in str(raised.value).replace("\n", " ")


ZONE_PRECONDITION = "must be one of the agentcore_az_ids infra/regions.json lists for"


@pytest.mark.parametrize(
    "tfvars",
    [
        'private_az_ids = ["use1-az1", "use1-az3"]\n',
        'runtime_az_ids = ["use1-az3", "use1-az2"]\n',
        'nat_az_id = "use1-az3"\n',
        'private_az_ids = ["usw2-az1", "use1-az2"]\n',
        'nat_az_id = "usw2-az1"\n',
    ],
    ids=["private-az3", "runtime-az3", "nat-az3", "private-usw2", "nat-usw2"],
)
def test_a_zone_outside_the_regions_map_set_fails_the_precondition(
    _terraform: None, tmp_path: Path, tfvars: str
) -> None:
    """In us-east-1, an id outside the map's us-east-1 set, a use1-az3 or a usw2 id, is refused."""
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path, tfvars)
    text = str(raised.value).replace("\n", " ")
    assert "Resource precondition failed" in text
    assert ZONE_PRECONDITION in text


def _map_with(region: str, agentcore_az_ids: list[str] | None) -> str:
    """The real map with `region`'s zone set replaced, or the region dropped when None."""
    document = json.loads(region_map.MAP_PATH.read_text(encoding="utf-8"))
    if agentcore_az_ids is None:
        del document["regions"][region]
    else:
        document["regions"][region]["agentcore_az_ids"] = agentcore_az_ids
    return json.dumps(document)


def test_a_region_absent_from_the_map_fails_the_precondition(
    _terraform: None, tmp_path: Path
) -> None:
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path, 'aws_region = "us-west-2"\n', map_json=_map_with("us-west-2", None))
    text = str(raised.value).replace("\n", " ")
    assert "Resource precondition failed" in text
    assert 'aws_region "us-west-2" is not in infra/regions.json' in text


def test_a_one_zone_region_in_the_map_fails_the_precondition(
    _terraform: None, tmp_path: Path
) -> None:
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path, 'aws_region = "us-west-2"\n', map_json=_map_with("us-west-2", ["usw2-az1"]))
    text = str(raised.value).replace("\n", " ")
    assert "infra/regions.json lists fewer than 2 agentcore_az_ids for us-west-2" in text


def test_us_west_2_defaults_place_subnets_in_usw2_az1_and_az2(
    _terraform: None, tmp_path: Path
) -> None:
    planned = _plan(tmp_path, 'aws_region = "us-west-2"\n')
    placed = {
        address: planned.after(address)["availability_zone_id"]
        for address in planned.addresses_of_type("aws_subnet")
    }
    assert placed == {
        "aws_subnet.public": "usw2-az1",
        "aws_subnet.private[0]": "usw2-az1",
        "aws_subnet.private[1]": "usw2-az2",
        "aws_subnet.runtime[0]": "usw2-az1",
        "aws_subnet.runtime[1]": "usw2-az2",
    }


@pytest.mark.parametrize("region", sorted(REGION_MAP.regions))
def test_planned_zones_equal_the_region_map_entry(
    _terraform: None, tmp_path: Path, region: str
) -> None:
    """Contract: with no zone variable set, the plan places exactly the map's bootstrap ids."""
    entry = REGION_MAP.entry(region)
    planned = _plan(tmp_path, f'aws_region = "{region}"\n')
    zone = {
        a: planned.after(a)["availability_zone_id"] for a in planned.addresses_of_type("aws_subnet")
    }
    assert zone.pop("aws_subnet.public") == entry.nat_az_id
    assert [zone[f"aws_subnet.private[{i}]"] for i in range(len(entry.private_az_ids))] == list(
        entry.private_az_ids
    )
    assert [zone[f"aws_subnet.runtime[{i}]"] for i in range(len(entry.runtime_az_ids))] == list(
        entry.runtime_az_ids
    )
    assert len(zone) == len(entry.private_az_ids) + len(entry.runtime_az_ids)


def test_another_slash_16_is_accepted_and_carves_slash_24s(
    _terraform: None, tmp_path: Path
) -> None:
    """A non-default IPv4 /16 plans, and every subnet is a /24 inside it."""
    planned = _plan(tmp_path, 'vpc_cidr = "10.50.0.0/16"\n')
    assert planned.after("aws_vpc.this")["cidr_block"] == "10.50.0.0/16"
    cidrs = sorted(planned.after(a)["cidr_block"] for a in planned.addresses_of_type("aws_subnet"))
    assert cidrs == [
        "10.50.0.0/24",
        "10.50.10.0/24",
        "10.50.11.0/24",
        "10.50.20.0/24",
        "10.50.21.0/24",
    ]


AZ_ID_LITERAL = re.compile(r"\bus[ew][0-9]-az[0-9]+\b")


@pytest.mark.parametrize("name", sorted(p.name for p in ROOT_DIR.glob("*.tf")))
def test_no_tf_file_hand_copies_an_az_id(name: str) -> None:
    """Zone ids live in `infra/regions.json` only; a `.tf` literal would be a second copy."""
    text = (ROOT_DIR / name).read_text(encoding="utf-8")
    assert AZ_ID_LITERAL.findall(text) == []


@pytest.mark.parametrize("name", ["terraform.tfvars", "operator.tfvars"])
def test_bootstrap_tfvars_files_are_ignored_by_the_tracked_gitignore(name: str) -> None:
    git = shutil.which("git")
    assert git is not None
    path = f"infra/bootstrap_account/{name}"
    result = proc_run.run(
        [git, "-c", "core.excludesFile=/dev/null", "check-ignore", "-v", "--no-index", path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    source = result.stdout.split(":", 1)[0]
    assert source == ".gitignore", result.stdout


def test_a_bootstrap_tf_file_is_not_ignored() -> None:
    git = shutil.which("git")
    assert git is not None
    result = proc_run.run(
        [git, "-c", "core.excludesFile=/dev/null", "check-ignore", "--no-index",
         "infra/bootstrap_account/vpc.tf"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    assert result.returncode == 1, result.stdout + result.stderr

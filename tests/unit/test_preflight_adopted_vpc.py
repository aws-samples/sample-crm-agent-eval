# SPDX-License-Identifier: MIT
"""Driven tests for `scripts/preflight_adopted_vpc.sh`, run against a stub `aws` on PATH.

The stub dispatches on the `aws ec2 describe-*` subcommand and answers from per-test canned JSON.
Each check is exercised in both directions: a compliant fixture passes, a violating one refuses
naming the resource.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "preflight_adopted_vpc.sh"

# Ids are assembled at runtime because `check_no_account_id` refuses literal `vpc-`/`subnet-`/`sg-`
# hex ids in tracked content.
VPC = "vpc-" + "0" * 8
OURS = ["subnet-" + "a" * 7 + d for d in ("0", "1")]
THEIRS = "subnet-" + "f" * 8
NAT_SUBNET = "subnet-" + "b" * 8
OUR_TABLES = ["rtb-" + "c" * 7 + d for d in ("0", "1")]
MAIN_TABLE = "rtb-" + "9" * 8
IGW = "igw-" + "e" * 8


def _write_stub(bin_dir: Path, responses: dict[str, object]) -> None:
    """A fake `aws` that answers from `responses`, keyed by the query it is asked."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "responses.json").write_text(json.dumps(responses), encoding="utf-8")
    (bin_dir / "aws").write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, pathlib\n"
        "args = sys.argv[1:]\n"
        "r = json.loads((pathlib.Path(__file__).parent / 'responses.json').read_text())\n"
        "def arg(name):\n"
        "    return args[args.index(name) + 1] if name in args else None\n"
        "sub = args[1] if len(args) > 1 else ''\n"
        "query = arg('--query') or ''\n"
        "key = None\n"
        "if sub == 'describe-subnets' and 'AvailableIpAddressCount' in query:\n"
        "    key = 'capacity:' + (arg('--subnet-ids') or '')\n"
        "elif sub == 'describe-subnets' and 'AvailabilityZone' in query:\n"
        "    key = 'zone:' + (arg('--subnet-ids') or '')\n"
        "elif sub == 'describe-subnets':\n"
        "    key = 'subnet_vpc:' + (arg('--subnet-ids') or '')\n"
        "elif sub == 'describe-route-tables' and 'Main==' in query:\n"
        "    key = 'main_table'\n"
        "elif sub == 'describe-route-tables' and 'Associations[?SubnetId' in query:\n"
        "    key = 'assoc:' + (arg('--route-table-ids') or '')\n"
        "elif sub == 'describe-route-tables' and 'association.subnet-id' in str(args):\n"
        "    key = 'nat_table'\n"
        "elif sub == 'describe-route-tables' and 'GatewayId' in query:\n"
        "    key = 'igw_route:' + (arg('--route-table-ids') or '')\n"
        "elif sub == 'describe-security-groups':\n"
        "    key = 'default_sg'\n"
        "elif sub == 'describe-vpc-endpoints':\n"
        "    key = 'gateway_endpoints'\n"
        # Keyed by attribute name, so a fixture can set each DNS attribute independently.
        "elif sub == 'describe-vpc-attribute':\n"
        "    key = 'vpc_attribute:' + (arg('--attribute') or '')\n"
        "if key is None or key not in r:\n"
        "    print('STUB-MISS:' + str(key), file=sys.stderr); sys.exit(254)\n"
        "value = r[key]\n"
        "print(value if isinstance(value, str) else json.dumps(value))\n",
        encoding="utf-8",
    )
    (bin_dir / "aws").chmod(0o755)


def _compliant() -> dict[str, object]:
    """Responses under which every check passes; the positive control for each refusal."""
    return {
        **{f"subnet_vpc:{s}": VPC for s in OURS},
        f"subnet_vpc:{NAT_SUBNET}": VPC,
        **{f"capacity:{s}": "10.20.1.0/24\t200" for s in OURS},
        # Distinct zones, one per adopted subnet: an ALB cannot be created in fewer than two.
        **{f"zone:{s}": f"us-east-1{chr(ord('a') + i)}" for i, s in enumerate(OURS)},
        "main_table": MAIN_TABLE,
        **{f"assoc:{OUR_TABLES[i]}": [OURS[i]] for i in (0, 1)},
        "nat_table": "rtb-" + "7" * 8,
        "igw_route:rtb-" + "7" * 8: IGW,
        "default_sg": [{"IpProtocol": "-1", "IpRanges": []}],
        "gateway_endpoints": [],
        # Private DNS on an interface endpoint needs both DNS attributes true.
        "vpc_attribute:enableDnsSupport": "True",
        "vpc_attribute:enableDnsHostnames": "True",
    }


def _run(
    tmp_path: Path,
    responses: dict[str, object],
    *extra: str,
    region: str = "us-east-1",
    script: Path = SCRIPT,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    _write_stub(bin_dir, responses)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    env.pop("AWS_REGION", None)
    env.pop("AWS_PROFILE", None)
    return proc_run.run(
        [
            "bash",
            str(script),
            "--region",
            region,
            "--vpc-id",
            VPC,
            "--private-subnet-ids",
            ",".join(OURS),
            "--route-table-ids",
            ",".join(OUR_TABLES),
            *extra,
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not on PATH")
class TestPreflightRefusesInBothDirections:
    def test_the_compliant_world_passes(self, tmp_path: Path) -> None:
        """The compliant fixture exits 0 and reports every check passed."""
        result = _run(tmp_path, _compliant(), "--nat-subnet-id", NAT_SUBNET)
        assert result.returncode == 0, (
            f"the compliant fixture was refused:\n{result.stdout}\n{result.stderr}"
        )
        assert "every adopted-VPC pre-apply check passed" in result.stdout

    def test_it_refuses_the_vpcs_main_route_table(self, tmp_path: Path) -> None:
        """A route table that is the VPC's main table is refused."""
        responses = _compliant()
        responses["main_table"] = OUR_TABLES[0]
        result = _run(tmp_path, responses)
        assert result.returncode != 0, "the main route table was accepted"
        assert "MAIN table" in result.stderr, result.stderr
        assert OUR_TABLES[0] in result.stderr

    def test_it_refuses_a_table_that_also_serves_someone_elses_subnet(self, tmp_path: Path) -> None:
        """A route table also associated with a foreign subnet is refused, naming that subnet."""
        responses = _compliant()
        responses[f"assoc:{OUR_TABLES[1]}"] = [OURS[1], THEIRS]
        result = _run(tmp_path, responses)
        assert result.returncode != 0, "a table serving a foreign subnet was accepted"
        assert THEIRS in result.stderr, result.stderr
        assert "not ours" in result.stderr

    def test_it_refuses_a_table_with_no_explicit_associations(self, tmp_path: Path) -> None:
        """A route table with no explicit subnet associations is refused."""
        responses = _compliant()
        responses[f"assoc:{OUR_TABLES[0]}"] = []
        result = _run(tmp_path, responses)
        assert result.returncode != 0
        assert "no explicit subnet associations" in result.stderr, result.stderr

    def test_it_refuses_a_default_security_group_open_to_the_world(self, tmp_path: Path) -> None:
        """A world-open default security group is refused, and the message says not to modify it."""
        responses = _compliant()
        responses["default_sg"] = [{"IpProtocol": "tcp", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
        result = _run(tmp_path, responses)
        assert result.returncode != 0, "a world-open default security group was accepted"
        assert "0.0.0.0/0" in result.stderr, result.stderr
        assert "must not modify that group" in result.stderr

    def test_it_refuses_a_nat_subnet_with_no_internet_gateway_route(self, tmp_path: Path) -> None:
        """A NAT subnet with no 0.0.0.0/0 route to an internet gateway is refused."""
        responses = _compliant()
        responses["igw_route:rtb-" + "7" * 8] = "None"
        result = _run(tmp_path, responses, "--nat-subnet-id", NAT_SUBNET)
        assert result.returncode != 0, "a NAT subnet with no IGW route was accepted"
        assert "no 0.0.0.0/0 route to an internet gateway" in result.stderr, result.stderr
        assert "TIMEOUT" in result.stderr

    def test_it_refuses_a_nat_subnet_that_is_also_a_lambda_subnet(self, tmp_path: Path) -> None:
        result = _run(tmp_path, _compliant(), "--nat-subnet-id", OURS[0])
        assert result.returncode != 0
        assert "directly internet-facing" in result.stderr, result.stderr

    def test_it_refuses_a_gateway_endpoint_that_already_serves_our_table(
        self, tmp_path: Path
    ) -> None:
        """An untagged gateway endpoint already on our route table is refused as a duplicate."""
        responses = _compliant()
        responses["gateway_endpoints"] = [
            {"Id": "vpce-" + "c" * 8, "RouteTableIds": [OUR_TABLES[0]], "Managed": None}
        ]
        result = _run(tmp_path, responses, "--nat-subnet-id", NAT_SUBNET)
        assert result.returncode != 0, "a duplicate gateway association was accepted"
        assert "duplicate" in result.stderr, result.stderr

    def test_it_ignores_a_gateway_endpoint_this_stack_already_manages(self, tmp_path: Path) -> None:
        """A gateway endpoint tagged as this stack's is ignored, and the exclusion is printed."""
        responses = _compliant()
        responses["gateway_endpoints"] = [
            {
                "Id": "vpce-" + "d" * 8,
                "RouteTableIds": [OUR_TABLES[0]],
                "Managed": "crm-agent-eval",
            }
        ]
        result = _run(tmp_path, responses, "--nat-subnet-id", NAT_SUBNET)
        assert result.returncode == 0, f"our own endpoint was refused: {result.stderr}"
        assert "ignoring" in result.stdout, result.stdout
        assert "vpce-" + "d" * 8 in result.stdout, result.stdout

    def test_a_managed_tag_that_does_not_match_is_still_foreign(self, tmp_path: Path) -> None:
        """The exclusion keys on the tag VALUE, not merely on a tag being present."""
        responses = _compliant()
        responses["gateway_endpoints"] = [
            {
                "Id": "vpce-" + "f" * 8,
                "RouteTableIds": [OUR_TABLES[0]],
                "Managed": "some-other-stack",
            }
        ]
        result = _run(tmp_path, responses, "--nat-subnet-id", NAT_SUBNET)
        assert result.returncode != 0, "another stack's endpoint was treated as ours"
        assert "duplicate" in result.stderr, result.stderr

    @pytest.mark.parametrize("attribute", ["enableDnsSupport", "enableDnsHostnames"])
    def test_it_refuses_when_either_vpc_dns_attribute_is_false(
        self, tmp_path: Path, attribute: str
    ) -> None:
        """Either VPC DNS attribute false is refused, naming the attribute and its CLI flag."""
        responses = _compliant()
        responses[f"vpc_attribute:{attribute}"] = "False"
        result = _run(tmp_path, responses, "--nat-subnet-id", NAT_SUBNET)
        assert result.returncode != 0, f"{attribute}=False was accepted"
        assert attribute in result.stderr, result.stderr
        assert "private DNS" in result.stderr, result.stderr
        expected_flag = (
            "--enable-dns-support" if attribute == "enableDnsSupport" else "--enable-dns-hostnames"
        )
        assert expected_flag in result.stderr, result.stderr

    def test_an_unreadable_dns_attribute_refuses_rather_than_skipping(self, tmp_path: Path) -> None:
        """An unreadable DNS attribute is refused rather than treated as satisfied."""
        responses = _compliant()
        responses["vpc_attribute:enableDnsHostnames"] = "None"
        result = _run(tmp_path, responses, "--nat-subnet-id", NAT_SUBNET)
        assert result.returncode != 0, "an unreadable attribute was treated as satisfied"
        assert "refusing rather than treating" in result.stderr, result.stderr

    def test_it_refuses_a_subnet_from_a_different_vpc(self, tmp_path: Path) -> None:
        responses = _compliant()
        responses[f"subnet_vpc:{OURS[0]}"] = "vpc-" + "5" * 8
        result = _run(tmp_path, responses)
        assert result.returncode != 0
        assert "not the adopted VPC" in result.stderr, result.stderr

    def test_a_failed_aws_call_refuses_rather_than_skipping(self, tmp_path: Path) -> None:
        """A failed `aws` call makes the preflight exit non-zero."""
        responses = _compliant()
        del responses["main_table"]
        result = _run(tmp_path, responses)
        assert result.returncode != 0, "an unanswerable main-table query was treated as a pass"

    def test_it_refuses_a_subnet_with_too_few_free_addresses(self, tmp_path: Path) -> None:
        """A subnet with fewer free addresses than the floor is refused, naming the subnet."""
        responses = _compliant()
        responses[f"capacity:{OURS[1]}"] = "10.20.9.240/28\t3"
        result = _run(tmp_path, responses)
        assert result.returncode != 0, "a subnet with 3 free addresses was accepted"
        assert OURS[1] in result.stderr, result.stderr
        assert "below the floor" in result.stderr
        assert "intermittent 5xx" in result.stderr

    def test_the_capacity_floor_is_configurable_upward(self, tmp_path: Path) -> None:
        """A subnet that passes the default floor must still be refused by a higher one."""
        responses = _compliant()
        responses[f"capacity:{OURS[0]}"] = "10.20.9.0/28\t9"
        assert _run(tmp_path, responses).returncode == 0, (
            "9 free should pass the default floor of 8"
        )
        raised = _run(tmp_path, responses, "--min-free-ips", "16")
        assert raised.returncode != 0, "--min-free-ips 16 did not tighten the floor"
        assert "floor of 16" in raised.stderr

    def test_an_unreadable_capacity_refuses_rather_than_skipping(self, tmp_path: Path) -> None:
        responses = _compliant()
        responses[f"capacity:{OURS[0]}"] = "10.20.1.0/24\tNone"
        result = _run(tmp_path, responses)
        assert result.returncode != 0
        assert "refusing rather than skipping the capacity check" in result.stderr, result.stderr

    def test_a_non_numeric_floor_is_rejected(self, tmp_path: Path) -> None:
        result = _run(tmp_path, _compliant(), "--min-free-ips", "eight")
        assert result.returncode != 0
        assert "non-negative integer" in result.stderr

    def test_the_undesignated_default_floor_declares_itself(self, tmp_path: Path) -> None:
        """A run using the default floor prints the `UNDERIVED DEFAULT` marker."""
        result = _run(tmp_path, _compliant())
        assert result.returncode == 0, result.stderr
        assert "UNDERIVED DEFAULT" in result.stdout, result.stdout

    def test_an_explicitly_derived_floor_does_not_declare_itself(self, tmp_path: Path) -> None:
        """A run with an explicit `--min-free-ips` prints `DERIVED` and not `UNDERIVED DEFAULT`."""
        result = _run(tmp_path, _compliant(), "--min-free-ips", "16")
        assert result.returncode == 0, result.stderr
        assert "UNDERIVED DEFAULT" not in result.stdout, result.stdout
        assert "DERIVED" in result.stdout, result.stdout

    def test_declaring_the_default_does_not_change_pass_fail_semantics(
        self, tmp_path: Path
    ) -> None:
        """Declaring the default floor leaves the pass/fail outcome unchanged in both directions."""
        below = _compliant()
        below[f"capacity:{OURS[1]}"] = "10.20.9.240/28\t3"
        refused = _run(tmp_path, below)
        assert refused.returncode != 0, "declaring the default softened the refusal"
        assert "below the floor" in refused.stderr

        assert _run(tmp_path, _compliant()).returncode == 0, (
            "declaring the default broke the passing case"
        )

    def test_it_refuses_subnets_that_share_one_availability_zone(self, tmp_path: Path) -> None:
        """Private subnets that share one Availability Zone are refused."""
        responses = _compliant()
        responses[f"zone:{OURS[1]}"] = responses[f"zone:{OURS[0]}"]
        result = _run(tmp_path, responses)
        assert result.returncode != 0, (
            "two subnets in one AZ were accepted; the ALB cannot be created"
        )
        assert "span only 1 Availability Zone" in result.stderr, result.stderr
        assert "two different Availability Zones" in result.stderr

    def test_distinct_zones_are_what_passes_not_merely_two_ids(self, tmp_path: Path) -> None:
        """The same subnet ids in distinct zones pass."""
        assert _run(tmp_path, _compliant()).returncode == 0

    def test_an_unreadable_zone_refuses_rather_than_skipping(self, tmp_path: Path) -> None:
        """An unreadable zone is refused rather than skipped."""
        responses = _compliant()
        responses[f"zone:{OURS[0]}"] = "None"
        result = _run(tmp_path, responses)
        assert result.returncode != 0
        assert "refusing rather than skipping the distinct-zone check" in result.stderr, (
            result.stderr
        )


RUNTIME_SUBNET = "subnet-" + "d" * 8
RUNTIME_TABLE = "rtb-" + "6" * 8


def _runtime_world(az_id: str) -> dict[str, object]:
    """The compliant fixture plus one Runtime subnet in `az_id` on its own IGW-free table."""
    responses = _compliant()
    responses[f"zone:{RUNTIME_SUBNET}"] = f"{VPC}\t{az_id}"
    responses["nat_table"] = RUNTIME_TABLE
    responses[f"igw_route:{RUNTIME_TABLE}"] = "None"
    return responses


def _scratch_preflight(tmp_path: Path, map_text: str | None) -> Path:
    """A two-script repo copy whose `infra/regions.json` is `map_text` (absent when None)."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "infra").mkdir()
    for name in ("preflight_adopted_vpc.sh", "region_map.py"):
        shutil.copy2(REPO_ROOT / "scripts" / name, root / "scripts" / name)
    if map_text is not None:
        (root / "infra" / "regions.json").write_text(map_text, encoding="utf-8")
    return root / "scripts" / "preflight_adopted_vpc.sh"


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not on PATH")
class TestRuntimeSubnetZonesComeFromTheRegionMap:
    @pytest.mark.parametrize(
        ("region", "az_id"), [("us-east-1", "use1-az4"), ("us-west-2", "usw2-az1")]
    )
    def test_a_runtime_subnet_in_a_mapped_zone_passes(
        self, tmp_path: Path, region: str, az_id: str
    ) -> None:
        """A Runtime subnet in one of the map's `agentcore_az_ids` for the region passes."""
        result = _run(
            tmp_path,
            _runtime_world(az_id),
            "--runtime-subnet-ids",
            RUNTIME_SUBNET,
            region=region,
        )
        assert result.returncode == 0, result.stderr
        assert f"AZ {az_id} supported" in result.stdout, result.stdout

    def test_the_us_east_1_set_is_read_from_the_map(self, tmp_path: Path) -> None:
        """The us-east-1 supported set printed is the map's, in the map's order."""
        result = _run(tmp_path, _runtime_world("use1-az1"), "--runtime-subnet-ids", RUNTIME_SUBNET)
        assert result.returncode == 0, result.stderr
        assert "against supported AZs {use1-az1 use1-az2 use1-az4}" in result.stdout

    @pytest.mark.parametrize(
        ("region", "az_id"), [("us-east-1", "use1-az3"), ("us-west-2", "usw2-az4")]
    )
    def test_a_runtime_subnet_outside_the_mapped_zones_refuses(
        self, tmp_path: Path, region: str, az_id: str
    ) -> None:
        """A Runtime subnet in a zone the map does not list for the region is refused."""
        result = _run(
            tmp_path,
            _runtime_world(az_id),
            "--runtime-subnet-ids",
            RUNTIME_SUBNET,
            region=region,
        )
        assert result.returncode != 0, f"{az_id} was accepted in {region}"
        assert f"is in AZ {az_id}, which AgentCore VPC mode does not support" in result.stderr

    def test_a_region_missing_from_the_map_refuses_naming_it(self, tmp_path: Path) -> None:
        """eu-west-1 is not in the map, so VPC-mode preflight refuses and names the map."""
        result = _run(
            tmp_path,
            _runtime_world("euw1-az1"),
            "--runtime-subnet-ids",
            RUNTIME_SUBNET,
            region="eu-west-1",
        )
        assert result.returncode != 0
        assert "region eu-west-1 is not in infra/regions.json" in result.stderr, result.stderr
        assert "us-east-1, us-east-2, us-west-1, us-west-2" in result.stderr

    @pytest.mark.parametrize("map_text", ["{", None], ids=["malformed", "absent"])
    def test_an_unreadable_map_refuses_rather_than_falling_back(
        self, tmp_path: Path, map_text: str | None
    ) -> None:
        """An invalid or missing map refuses even for us-east-1; there is no built-in set."""
        script = _scratch_preflight(tmp_path, map_text)
        result = _run(
            tmp_path,
            _runtime_world("use1-az1"),
            "--runtime-subnet-ids",
            RUNTIME_SUBNET,
            script=script,
        )
        assert result.returncode != 0, "an unreadable map was treated as a supported set"
        assert "from infra/regions.json (region_map.py exit 1)" in result.stderr, result.stderr

    def test_the_scratch_copy_with_a_valid_map_passes(self, tmp_path: Path) -> None:
        """Control for the unreadable-map cells: the same scratch copy with the real map passes."""
        real_map = (REPO_ROOT / "infra" / "regions.json").read_text(encoding="utf-8")
        script = _scratch_preflight(tmp_path, real_map)
        result = _run(
            tmp_path,
            _runtime_world("use1-az1"),
            "--runtime-subnet-ids",
            RUNTIME_SUBNET,
            script=script,
        )
        assert result.returncode == 0, result.stderr


class TestPreflightIsNonInteractiveAndFailsLoudly:
    def test_it_refuses_a_missing_region_rather_than_inheriting_an_ambient_one(self) -> None:
        """Without `--region` the script refuses rather than inheriting an ambient region."""
        env = {k: v for k, v in os.environ.items() if k not in ("AWS_REGION", "AWS_PROFILE")}
        result = proc_run.run(
            [
                "bash",
                str(SCRIPT),
                "--vpc-id",
                VPC,
                "--private-subnet-ids",
                OURS[0],
                "--route-table-ids",
                OUR_TABLES[0],
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        assert result.returncode != 0
        assert "Refusing rather than inheriting an ambient region" in result.stderr

    @pytest.mark.parametrize("omitted", ["--vpc-id", "--private-subnet-ids", "--route-table-ids"])
    def test_every_required_argument_is_required(self, omitted: str) -> None:
        args = [
            "--region",
            "us-east-1",
            "--vpc-id",
            VPC,
            "--private-subnet-ids",
            OURS[0],
            "--route-table-ids",
            OUR_TABLES[0],
        ]
        index = args.index(omitted)
        del args[index : index + 2]
        result = proc_run.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        assert result.returncode != 0, f"{omitted} was not required"
        assert omitted in result.stderr

    def test_it_never_blocks_on_stdin(self) -> None:
        """Closed stdin, and it must still terminate — no `read -p`, no confirmation prompt."""
        result = proc_run.run(
            ["bash", str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
        assert result.returncode == 0

# SPDX-License-Identifier: MIT
"""Tests for `scripts.scrub_plan_capture`: the mappings, the provenance, and each self-check refusal."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts import plan_fixtures, proc_run
from scripts import scrub_plan_capture as scrub

REPO_ROOT = Path(__file__).resolve().parents[2]
DIGEST = "ab" * 32
HEX = "0a1b2c3d4e5f6a7b8"


def _account(digit: str) -> str:
    return digit * 12


def _id(kind: str, n: int) -> str:
    """A real-shaped 17-hex id, distinct per `n`, assembled so no literal id appears in source."""
    return f"{kind}-{HEX[:-1]}{n:x}"


def _unique_id() -> str:
    return "AROA" + "Q" * 16


ACCOUNT = _account("4")
ROLE = "Synthetic-OperatorRole-Example1"
SESSION = "synthetic-session-1"


def _raw_plan(**variables: Any) -> dict[str, Any]:
    """A minimal raw `terraform show -json` document carrying every field the scrubber reads."""
    vpc, nat, p1, p2, r1, rt1, rt2, rrt, sg = (
        _id("vpc", 1),
        _id("subnet", 1),
        _id("subnet", 2),
        _id("subnet", 3),
        _id("subnet", 4),
        _id("rtb", 1),
        _id("rtb", 2),
        _id("rtb", 3),
        _id("sg", 1),
    )
    values: dict[str, Any] = {
        "aws_profile": "crm-synthetic01",
        "vpc_id": vpc,
        "vpc_nat_public_subnet_id": nat,
        "vpc_private_subnet_ids": [p1, p2],
        "vpc_runtime_subnet_ids": [r1, p2],
        "vpc_private_route_table_ids": [rt1, rt2],
        "vpc_runtime_route_table_ids": [rrt],
        "api_origin_ingress_security_group_ids": [sg],
        "crm_operator_principal_arns": [
            f"arn:aws:iam::{ACCOUNT}:role/{ROLE}",
            f"arn:aws:iam::{ACCOUNT}:role/Second-Operator",
        ],
        "vpc_flow_log_enabled": True,
    } | variables
    caller_arn = f"arn:aws:sts::{ACCOUNT}:assumed-role/{ROLE}/{SESSION}"
    return {
        "format_version": "1.2",
        "terraform_version": "1.5.7",
        "timestamp": "2026-01-02T03:04:05Z",
        "variables": {k: {"value": v} for k, v in values.items()},
        "configuration": {"provider_config": {}, "root_module": {"variables": {}}},
        "resource_changes": [
            {
                "address": f'module.vpc.aws_route.private_nat["{rt1}"]',
                "mode": "managed",
                "type": "aws_route",
                "name": "private_nat",
                "change": {
                    "actions": ["create"],
                    "after": {"route_table_id": rt1, "vpc_id": vpc, "subnet_ids": [p2, r1]},
                },
            },
            {
                "address": "aws_s3_bucket.logs",
                "mode": "managed",
                "type": "aws_s3_bucket",
                "name": "logs",
                "change": {
                    "actions": ["create"],
                    "after": {
                        "bucket": f"crm-logs-{ACCOUNT}",
                        "owner": _unique_id(),
                        "caller": caller_arn,
                    },
                },
            },
            {
                "address": "data.aws_caller_identity.current",
                "mode": "data",
                "type": "aws_caller_identity",
                "name": "current",
                "change": {"actions": ["read"], "after": {}},
            },
        ],
        "planned_values": {"outputs": {"leak": {"value": ROLE}}},
        "prior_state": {
            "values": {
                "root_module": {
                    "resources": [
                        {
                            "type": "aws_iam_session_context",
                            "values": {"issuer_name": ROLE, "session_name": SESSION},
                        }
                    ],
                    "child_modules": [
                        {
                            "resources": [
                                {
                                    "type": "aws_caller_identity",
                                    "values": {"account_id": ACCOUNT, "arn": caller_arn},
                                }
                            ]
                        }
                    ],
                }
            }
        },
    }


def _scrubbed(plan: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [json.loads(line) for line in scrub.scrub(plan or _raw_plan(), DIGEST)]


def _run(tmp_path: Path, plan: dict[str, Any], *extra: str) -> subprocess.CompletedProcess[str]:
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(plan), encoding="utf-8")
    return proc_run.run(
        [
            sys.executable,
            "scripts/scrub_plan_capture.py",
            "--plan",
            str(raw),
            "--out",
            str(tmp_path / "out.ndjson"),
            *(extra or ("--deny-token", "absent-token")),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_output_is_the_plan_fixtures_layout_one_sorted_object_per_line() -> None:
    lines = scrub.scrub(_raw_plan(), DIGEST)
    header = json.loads(lines[0])
    assert set(header) == plan_fixtures.HEADER_KEYS
    assert len(lines) == 1 + len(_raw_plan()["resource_changes"])
    assert all(line == json.dumps(json.loads(line), sort_keys=True) for line in lines)
    assert not any("planned_values" in line or "prior_state" in line for line in lines)


def test_the_account_id_maps_to_the_placeholder() -> None:
    text = "\n".join(scrub.scrub(_raw_plan(), DIGEST))
    assert ACCOUNT not in text
    assert f"crm-logs-{scrub.PLACEHOLDER_ACCOUNT_ID}" in text


def test_adopted_network_ids_map_to_their_variable_placeholders_stably() -> None:
    header, route, _bucket, _data = _scrubbed()
    variables = {k: v["value"] for k, v in header["variables"].items()}
    assert variables["vpc_id"] == "vpc-adoptedplaceholder"
    assert variables["vpc_private_subnet_ids"] == [
        "subnet-privateoneplaceholder",
        "subnet-privatetwoplaceholder",
    ]
    assert variables["vpc_runtime_subnet_ids"] == [
        "subnet-runtimeoneplaceholder",
        "subnet-privatetwoplaceholder",
    ]
    assert route["address"] == 'module.vpc.aws_route.private_nat["rtb-privateoneplaceholder"]'
    assert route["change"]["after"] == {
        "route_table_id": "rtb-privateoneplaceholder",
        "vpc_id": "vpc-adoptedplaceholder",
        "subnet_ids": ["subnet-privatetwoplaceholder", "subnet-runtimeoneplaceholder"],
    }


def test_caller_operator_session_unique_id_and_profile_map_to_example_names() -> None:
    header, _route, bucket, _data = _scrubbed()
    variables = {k: v["value"] for k, v in header["variables"].items()}
    assert variables["aws_profile"] == scrub.PLACEHOLDER_PROFILE
    assert variables["crm_operator_principal_arns"] == [
        "arn:aws:iam::123456789012:role/example-deployer",
        "arn:aws:iam::123456789012:role/example-operator-1",
    ]
    after = bucket["change"]["after"]
    assert after["owner"] == scrub.PLACEHOLDER_UNIQUE_ID
    assert after["caller"] == (
        "arn:aws:sts::123456789012:assumed-role/example-deployer/example-session"
    )


def test_the_provenance_records_the_infra_digest_and_carries_no_mutation() -> None:
    provenance = _scrubbed()[0]["_fixture_provenance"]
    assert provenance["captured_infra_digest"] == DIGEST
    assert "captured_from_commit" not in provenance
    assert "commit" not in json.dumps(provenance)
    assert provenance["captured_at"] == "2026-01-02"
    assert "mutation" not in provenance
    assert {"how", "currency", "derived_from", "variables_at_capture"} <= set(provenance)
    assert "2 to add, 0 to change, 0 to destroy; 1 data reads" in provenance["how"]
    assert "vpc_flow_log_enabled = true" in provenance["variables_at_capture"]


def test_a_clean_capture_is_written_and_exits_0(tmp_path: Path) -> None:
    result = _run(tmp_path, _raw_plan(), "--deny-token", ROLE, "--deny-token", "synthetic01")
    assert result.returncode == 0, result.stderr
    written = (tmp_path / "out.ndjson").read_text(encoding="utf-8").splitlines()
    assert written == scrub.scrub(_raw_plan(), plan_fixtures.infra_digest(REPO_ROOT))


def test_a_residual_twelve_digit_number_is_refused(tmp_path: Path) -> None:
    plan = _raw_plan(cost_center=f"cc-{_account('7')}")
    result = _run(tmp_path, plan)
    assert result.returncode == 2
    assert "residual twelve_digit_number" in result.stderr
    assert _account("7") not in result.stderr
    assert not (tmp_path / "out.ndjson").exists()


def test_an_unmapped_real_shaped_resource_id_is_refused(tmp_path: Path) -> None:
    plan = _raw_plan(extra_endpoint=_id("vpce", 9))
    result = _run(tmp_path, plan)
    assert result.returncode == 2
    assert "residual resource_id" in result.stderr
    assert _id("vpce", 9) not in result.stderr
    assert not (tmp_path / "out.ndjson").exists()


def test_a_deny_token_is_refused_case_insensitively_and_never_echoed(tmp_path: Path) -> None:
    plan = _raw_plan(owner="team-Pepper-Example")
    tokens = tmp_path / "tokens"
    tokens.write_text("unused-token\npepper-example\n", encoding="utf-8")
    result = _run(tmp_path, plan, "--deny-tokens-file", str(tokens))
    assert result.returncode == 2
    assert "residual deny_token[1]" in result.stderr
    assert "deny_token[0]" not in result.stderr
    assert "pepper" not in result.stderr.lower()
    assert not (tmp_path / "out.ndjson").exists()


def test_a_deny_token_the_profile_carried_is_scrubbed_not_refused(tmp_path: Path) -> None:
    result = _run(tmp_path, _raw_plan(), "--deny-token", "synthetic01")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        (json.dumps({"principal": _unique_id()}), "iam_unique_id"),
        (json.dumps({"origin": "vo_" + "Ab1C2dEf"}), "vpc_origin_id"),
        (json.dumps({"arn": f"arn:aws:iam::{_account('9')}:root"}), "twelve_digit_number"),
        (json.dumps({"subnet": _id("subnet", 5)}), "resource_id"),
    ],
    ids=["unique-id", "vpc-origin-id", "account-id", "subnet-id"],
)
def test_self_check_names_each_residual_class(line: str, kind: str) -> None:
    findings = scrub.self_check(["{}", line], [])
    assert findings == [scrub.Finding(kind, 2)]


def test_self_check_passes_the_placeholders() -> None:
    lines = [
        json.dumps({"a": "arn:aws:iam::123456789012:root", "b": "subnet-privateoneplaceholder"}),
        json.dumps({"c": scrub.PLACEHOLDER_UNIQUE_ID, "d": "sg-cloudfrontvpcoriginplaceholder"}),
    ]
    assert scrub.self_check(lines, ["absent-token"]) == []


def test_the_committed_base_passes_the_self_check() -> None:
    lines = plan_fixtures.BASE_PATH.read_text(encoding="utf-8").splitlines()
    assert scrub.self_check(lines, []) == []


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda p: p.pop("prior_state"), "one caller account id"),
        (
            lambda p: p["prior_state"]["values"]["root_module"]["child_modules"].append(
                {
                    "resources": [
                        {"type": "aws_caller_identity", "values": {"account_id": _account("5")}}
                    ]
                }
            ),
            "one caller account id",
        ),
        (
            lambda p: p["variables"]["vpc_runtime_route_table_ids"]["value"].append(_id("rtb", 7)),
            "vpc_runtime_route_table_ids holds 2 ids",
        ),
        (lambda p: p["resource_changes"][0]["change"].update(actions=["update"]), "neither create"),
        (lambda p: p.pop("configuration"), "missing a required field"),
    ],
    ids=["no-caller", "two-accounts", "unmapped-list-entry", "non-create", "missing-field"],
)
def test_an_unusable_plan_is_refused(edit: Any, message: str) -> None:
    plan = _raw_plan()
    edit(plan)
    with pytest.raises(scrub.ScrubError, match=message):
        scrub.scrub(plan, DIGEST)


@pytest.mark.parametrize(
    "digest", [DIGEST[:12], DIGEST.upper(), "c0ffee" + "0" * 34, DIGEST + "0"], ids=str
)
def test_a_value_that_is_not_a_sha256_digest_is_refused(digest: str) -> None:
    with pytest.raises(scrub.ScrubError, match="64-character"):
        scrub.scrub(_raw_plan(), digest)


def test_an_infra_root_that_is_not_a_work_tree_is_refused(tmp_path: Path) -> None:
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(_raw_plan()), encoding="utf-8")
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    argv = [
        "--plan",
        str(raw),
        "--infra-root",
        str(not_a_repo),
        "--deny-token",
        "x",
        "--out",
        str(tmp_path / "o"),
    ]
    assert scrub.main(argv) == 2
    assert not (tmp_path / "o").exists()


def test_a_pathed_role_arn_and_a_unique_id_session_suffix_are_scrubbed() -> None:
    plan = _raw_plan(
        crm_operator_principal_arns=[f"arn:aws:iam::{ACCOUNT}:role/team/path/{ROLE}"],
        notes=f"{_unique_id()}:{SESSION} uses role/{ROLE}",
    )
    header = _scrubbed(plan)[0]
    variables = {k: v["value"] for k, v in header["variables"].items()}
    assert variables["crm_operator_principal_arns"] == [
        "arn:aws:iam::123456789012:role/team/path/example-deployer"
    ]
    assert variables["notes"] == f"{scrub.PLACEHOLDER_UNIQUE_ID}:example-session uses role/{ROLE}"


def test_a_real_value_no_mapping_reached_is_refused_without_a_deny_token(tmp_path: Path) -> None:
    """The bare role name in `notes` is outside every ARN context, so only the derived check sees it."""
    result = _run(tmp_path, _raw_plan(notes=f"owned by {ROLE}"))
    assert result.returncode == 2
    assert "residual derived_real_value" in result.stderr
    assert ROLE not in result.stderr
    assert not (tmp_path / "out.ndjson").exists()


def test_the_profile_is_scrubbed_wherever_it_appears() -> None:
    header = _scrubbed(_raw_plan(command="aws --profile crm-synthetic01 sts"))[0]
    assert header["variables"]["command"]["value"] == "aws --profile example-deployer sts"


def test_a_derived_value_surviving_scrub_is_refused() -> None:
    lines = [json.dumps({"note": "run as crm-synthetic01"})]
    assert scrub.self_check(lines, [], ["crm-synthetic01"]) == [
        scrub.Finding("derived_real_value", 1)
    ]


def test_an_empty_deny_list_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "tokens"
    empty.write_text("\n", encoding="utf-8")
    result = _run(tmp_path, _raw_plan(), "--deny-tokens-file", str(empty))
    assert result.returncode == 2
    assert "no deny-token given" in result.stderr
    assert not (tmp_path / "out.ndjson").exists()


def test_a_generic_profile_is_not_derived() -> None:
    values = scrub.derived_real_values(_raw_plan(aws_profile="default"))
    assert "default" not in values
    assert {ACCOUNT, ROLE, SESSION, "Second-Operator", "crm-synthetic01"} <= set(
        scrub.derived_real_values(_raw_plan())
    )


def test_a_deny_token_json_would_escape_is_matched_on_the_decoded_text() -> None:
    line = json.dumps({"owner": 'Zoë "quoted"'})
    assert scrub.self_check([line], ['zoë "quoted"']) == [scrub.Finding("deny_token[0]", 1)]


def test_a_plan_without_a_timestamp_is_refused() -> None:
    plan = _raw_plan()
    plan.pop("timestamp")
    with pytest.raises(scrub.ScrubError, match="timestamp"):
        scrub.scrub(plan, DIGEST)


@pytest.mark.parametrize("trailer", [":", "*", "]", "}", ")"])
def test_a_role_name_followed_by_any_non_name_character_is_scrubbed(trailer: str) -> None:
    plan = _raw_plan(notes=f"arn:aws:iam::{ACCOUNT}:role/{ROLE}{trailer}")
    variables = _scrubbed(plan)[0]["variables"]
    assert (
        variables["notes"]["value"] == f"arn:aws:iam::123456789012:role/example-deployer{trailer}"
    )


def test_a_longer_name_sharing_the_role_prefix_is_left_for_the_self_check() -> None:
    plan = _raw_plan(notes=f"arn:aws:iam::{ACCOUNT}:role/{ROLE}-extra")
    variables = _scrubbed(plan)[0]["variables"]
    assert variables["notes"]["value"] == f"arn:aws:iam::123456789012:role/{ROLE}-extra"

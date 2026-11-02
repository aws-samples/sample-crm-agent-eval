# SPDX-License-Identifier: MIT
"""Tests for `scripts/render_vpc_tfvars.py`.

The renderer writes the six adopted-VPC keys of `infra/terraform.tfvars` from the bootstrap root's
`terraform output -json`, leaves every other line byte-identical, refuses bad input with exit 2, and
writes atomically at mode 0600.
"""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path

import pytest
from scripts.render_vpc_tfvars import (
    OUTPUT_TO_TFVARS,
    RenderError,
    main,
    refuse_placeholders,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_EXAMPLE = REPO_ROOT / "infra" / "terraform.tfvars.example"

VPC = "vpc-0123456789abcdef0"
PRIVATE_SUBNETS = ["subnet-00000000aaaaaaaa1", "subnet-00000000aaaaaaaa2"]
PRIVATE_TABLES = ["rtb-00000000bbbbbbbb1", "rtb-00000000bbbbbbbb2"]
NAT_SUBNET = "subnet-00000000cccccccc1"
RUNTIME_SUBNETS = ["subnet-00000000dddddddd1", "subnet-00000000dddddddd2"]
RUNTIME_TABLES = ["rtb-00000000eeeeeeee1"]

SG_LINE = 'api_origin_ingress_security_group_ids = ["sg-REPLACE_ME_CLOUDFRONT_VPC_ORIGIN"]'

EXAMPLE = f"""\
# Header comment.
aws_profile = "default"
aws_region  = "us-east-1"

vpc_id = "vpc-REPLACE_ME"

# Private subnets.
vpc_private_subnet_ids = [
  "subnet-REPLACE_ME_PRIVATE_A",
  "subnet-REPLACE_ME_PRIVATE_B",
]

vpc_nat_public_subnet_id = "subnet-REPLACE_ME_PUBLIC"

vpc_private_route_table_ids = [
  "rtb-REPLACE_ME_PRIVATE_A",
  "rtb-REPLACE_ME_PRIVATE_B",
]

vpc_runtime_subnet_ids = [
  "subnet-REPLACE_ME_RUNTIME_A",
]

vpc_runtime_route_table_ids = [
  "rtb-REPLACE_ME_RUNTIME_A",
]

vpc_nat_gateway_enabled = true

{SG_LINE}
"""

FILLED = f"""\
# Header comment.
aws_profile = "default"
aws_region  = "us-east-1"

vpc_id = "{VPC}"

# Private subnets.
vpc_private_subnet_ids = [
  "{PRIVATE_SUBNETS[0]}",
  "{PRIVATE_SUBNETS[1]}",
]

vpc_nat_public_subnet_id = "{NAT_SUBNET}"

vpc_private_route_table_ids = [
  "{PRIVATE_TABLES[0]}",
  "{PRIVATE_TABLES[1]}",
]

vpc_runtime_subnet_ids = [
  "{RUNTIME_SUBNETS[0]}",
  "{RUNTIME_SUBNETS[1]}",
]

vpc_runtime_route_table_ids = [
  "{RUNTIME_TABLES[0]}",
]

vpc_nat_gateway_enabled = true

{SG_LINE}
"""


def _outputs(**overrides: object) -> dict[str, object]:
    """A bootstrap-root `terraform output -json` document; overrides replace whole entries."""
    outputs: dict[str, object] = {
        "vpc_id": {"value": VPC, "type": "string", "sensitive": False},
        "private_subnet_ids": {
            "value": PRIVATE_SUBNETS,
            "type": ["list", "string"],
            "sensitive": False,
        },
        "private_route_table_ids": {
            "value": PRIVATE_TABLES,
            "type": ["list", "string"],
            "sensitive": False,
        },
        "nat_public_subnet_id": {"value": NAT_SUBNET, "type": "string", "sensitive": False},
        "runtime_subnet_ids": {
            "value": RUNTIME_SUBNETS,
            "type": ["list", "string"],
            "sensitive": False,
        },
        "runtime_route_table_ids": {
            "value": RUNTIME_TABLES,
            "type": ["list", "string"],
            "sensitive": False,
        },
        "apigw_cloudwatch_role_arn": {
            "value": "arn:aws:iam::123456789012:role/crm-agent-eval-apigw-cloudwatch-logs",
            "type": "string",
            "sensitive": False,
        },
        "az_name_by_id": {
            "value": {"use1-az1": "us-east-1a"},
            "type": ["map", "string"],
            "sensitive": False,
        },
    }
    outputs.update(overrides)
    return outputs


def _write_outputs(tmp_path: Path, outputs: object) -> Path:
    path = tmp_path / "outputs.json"
    path.write_text(json.dumps(outputs), encoding="utf-8")
    return path


def _run(tmp_path: Path, tfvars: Path, outputs: object = None, *extra: str) -> int:
    outputs_path = _write_outputs(tmp_path, _outputs() if outputs is None else outputs)
    return main(["--outputs-json", str(outputs_path), "--tfvars", str(tfvars), *extra])


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _strip_vpc_blocks(text: str) -> list[str]:
    """Drop the six mapped assignments (single-line or bracketed multi-line) by line scanning."""
    keys = set(OUTPUT_TO_TFVARS.values())
    kept: list[str] = []
    in_list = False
    for line in text.splitlines(keepends=True):
        if in_list:
            in_list = not line.lstrip().startswith("]")
            continue
        name = line.split("=", 1)[0].strip()
        if "=" in line and name in keys:
            value = line.split("=", 1)[1].strip()
            in_list = value.startswith("[") and "]" not in value
            continue
        kept.append(line)
    return kept


class TestFillFromExample:
    def test_absent_tfvars_is_filled_from_the_example_byte_for_byte(self, tmp_path: Path) -> None:
        example = tmp_path / "example"
        example.write_text(EXAMPLE, encoding="utf-8")
        tfvars = tmp_path / "terraform.tfvars"

        assert _run(tmp_path, tfvars, None, "--example", str(example)) == 0

        assert tfvars.read_bytes() == FILLED.encode("utf-8")
        assert example.read_text(encoding="utf-8") == EXAMPLE

    def test_the_real_example_keeps_every_other_line_in_order(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"

        assert _run(tmp_path, tfvars, None, "--example", str(REAL_EXAMPLE)) == 0

        original = REAL_EXAMPLE.read_text(encoding="utf-8")
        rendered = tfvars.read_text(encoding="utf-8")
        assert _strip_vpc_blocks(rendered) == _strip_vpc_blocks(original)
        assert "REPLACE_ME_CLOUDFRONT_VPC_ORIGIN" in rendered
        for value in [VPC, NAT_SUBNET, *PRIVATE_SUBNETS, *PRIVATE_TABLES, *RUNTIME_SUBNETS]:
            assert f'"{value}"' in rendered

    def test_the_line_stripper_is_not_vacuous(self) -> None:
        original = REAL_EXAMPLE.read_text(encoding="utf-8")
        stripped = "".join(_strip_vpc_blocks(original))
        for key in OUTPUT_TO_TFVARS.values():
            assert f"\n{key} =" in original
            assert f"\n{key} =" not in stripped
        assert SG_LINE in stripped

    def test_the_sg_placeholder_line_survives_byte_identical(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(EXAMPLE, encoding="utf-8")

        assert _run(tmp_path, tfvars) == 0

        assert f"\n{SG_LINE}\n" in tfvars.read_text(encoding="utf-8")


class TestIdempotence:
    def test_second_run_is_byte_identical_and_check_passes(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(EXAMPLE, encoding="utf-8")
        assert _run(tmp_path, tfvars) == 0
        first = tfvars.read_bytes()

        assert _run(tmp_path, tfvars) == 0
        assert tfvars.read_bytes() == first
        assert _run(tmp_path, tfvars, None, "--check") == 0

    def test_check_reports_a_stale_file_without_writing(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(EXAMPLE, encoding="utf-8")

        assert _run(tmp_path, tfvars, None, "--check") == 1
        assert tfvars.read_text(encoding="utf-8") == EXAMPLE

    def test_check_with_an_absent_tfvars_creates_nothing(self, tmp_path: Path) -> None:
        example = tmp_path / "example"
        example.write_text(EXAMPLE, encoding="utf-8")
        tfvars = tmp_path / "terraform.tfvars"

        assert _run(tmp_path, tfvars, None, "--example", str(example), "--check") == 1
        assert not tfvars.exists()


class TestRewriteShapes:
    def test_multi_line_list_is_replaced_with_a_shorter_list(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(
            EXAMPLE.replace(
                '  "rtb-REPLACE_ME_RUNTIME_A",\n',
                '  "rtb-REPLACE_ME_RUNTIME_A",\n  "rtb-OLD_B", # trailing ] in a comment\n  "rtb-OLD_C",\n',
            ),
            encoding="utf-8",
        )

        assert _run(tmp_path, tfvars) == 0

        assert tfvars.read_text(encoding="utf-8") == FILLED

    def test_single_line_list_and_odd_spacing_are_replaced(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(
            FILLED.replace(
                f'vpc_runtime_route_table_ids = [\n  "{RUNTIME_TABLES[0]}",\n]',
                'vpc_runtime_route_table_ids   =   ["rtb-REPLACE_ME"]',
            ).replace(f'vpc_id = "{VPC}"', 'vpc_id="vpc-REPLACE_ME"'),
            encoding="utf-8",
        )

        assert _run(tmp_path, tfvars) == 0

        assert tfvars.read_text(encoding="utf-8") == FILLED

    def test_absent_keys_are_appended(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text('aws_profile = "default"', encoding="utf-8")

        assert _run(tmp_path, tfvars) == 0

        rendered = tfvars.read_text(encoding="utf-8")
        assert rendered.startswith('aws_profile = "default"\nvpc_id = "')
        assert rendered.endswith("\n")
        for key in OUTPUT_TO_TFVARS.values():
            assert rendered.count(f"\n{key} = ") == 1

    def test_a_similarly_named_key_is_not_touched(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(EXAMPLE + 'vpc_id_note = "keep"\n', encoding="utf-8")

        assert _run(tmp_path, tfvars) == 0

        assert tfvars.read_text(encoding="utf-8") == FILLED + 'vpc_id_note = "keep"\n'

    def test_a_duplicated_key_is_refused(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(EXAMPLE + 'vpc_id = "vpc-REPLACE_ME"\n', encoding="utf-8")
        before = tfvars.read_bytes()

        assert _run(tmp_path, tfvars) == 2
        assert tfvars.read_bytes() == before

    def test_an_indented_scalar_is_rewritten_in_place_keeping_its_indent(
        self, tmp_path: Path
    ) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(
            EXAMPLE.replace('\nvpc_id = "vpc-REPLACE_ME"', '\n  vpc_id = "vpc-REPLACE_ME"'),
            encoding="utf-8",
        )

        assert _run(tmp_path, tfvars) == 0

        assert tfvars.read_text(encoding="utf-8") == FILLED.replace(
            f'\nvpc_id = "{VPC}"', f'\n  vpc_id = "{VPC}"'
        )

    def test_an_indented_list_is_rewritten_in_place(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(
            EXAMPLE.replace("\nvpc_runtime_subnet_ids = [", "\n\tvpc_runtime_subnet_ids = ["),
            encoding="utf-8",
        )

        assert _run(tmp_path, tfvars) == 0

        assert tfvars.read_text(encoding="utf-8") == FILLED.replace(
            "\nvpc_runtime_subnet_ids = [", "\n\tvpc_runtime_subnet_ids = ["
        )

    def test_a_list_opening_on_the_next_line_is_rewritten_in_place(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(
            EXAMPLE.replace("vpc_private_subnet_ids = [\n", "vpc_private_subnet_ids =\n  [\n"),
            encoding="utf-8",
        )

        assert _run(tmp_path, tfvars) == 0

        assert tfvars.read_text(encoding="utf-8") == FILLED

    def test_an_indented_duplicate_of_a_top_level_key_is_refused(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(EXAMPLE + '  vpc_id = "vpc-REPLACE_ME"\n', encoding="utf-8")
        before = tfvars.read_bytes()

        assert _run(tmp_path, tfvars) == 2
        assert tfvars.read_bytes() == before

    def test_an_unclosed_list_is_refused(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text('vpc_runtime_route_table_ids = [\n  "rtb-x",\n', encoding="utf-8")

        assert _run(tmp_path, tfvars) == 2


class TestBadOutputs:
    @pytest.mark.parametrize(
        ("output", "entry"),
        [
            ("vpc_id", {"value": "subnet-0123456789abcdef0", "sensitive": False}),
            ("vpc_id", {"value": "vpc-00000000AAAAAAAA1", "sensitive": False}),
            ("vpc_id", {"value": "vpc-0000000000", "sensitive": False}),
            ("vpc_id", {"value": "vpc-REPLACE_ME", "sensitive": False}),
            ("vpc_id", {"value": 7, "sensitive": False}),
            ("vpc_id", {"value": [VPC], "sensitive": False}),
            ("nat_public_subnet_id", {"value": "rtb-0123456789abcdef0", "sensitive": False}),
            ("private_subnet_ids", {"value": [], "sensitive": False}),
            ("private_subnet_ids", {"value": PRIVATE_SUBNETS[0], "sensitive": False}),
            ("private_subnet_ids", {"value": [PRIVATE_SUBNETS[0]] * 2, "sensitive": False}),
            ("private_route_table_ids", {"value": PRIVATE_SUBNETS, "sensitive": False}),
            ("runtime_route_table_ids", {"value": ["rtb-REPLACE_ME"], "sensitive": False}),
            ("runtime_subnet_ids", {"value": RUNTIME_SUBNETS, "sensitive": True}),
            ("vpc_id", {"value": VPC, "sensitive": "false"}),
            ("vpc_id", VPC),
            ("vpc_id", {"sensitive": False}),
        ],
    )
    def test_a_bad_output_is_refused_without_writing(
        self, tmp_path: Path, output: str, entry: object
    ) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(EXAMPLE, encoding="utf-8")

        assert _run(tmp_path, tfvars, _outputs(**{output: entry})) == 2
        assert tfvars.read_text(encoding="utf-8") == EXAMPLE

    @pytest.mark.parametrize("output", sorted(OUTPUT_TO_TFVARS))
    def test_each_missing_output_is_refused(self, tmp_path: Path, output: str) -> None:
        outputs = _outputs()
        del outputs[output]
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(EXAMPLE, encoding="utf-8")

        assert _run(tmp_path, tfvars, outputs) == 2
        assert tfvars.read_text(encoding="utf-8") == EXAMPLE

    def test_the_evidence_only_outputs_are_not_required(self, tmp_path: Path) -> None:
        outputs = _outputs()
        del outputs["apigw_cloudwatch_role_arn"], outputs["az_name_by_id"]
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(EXAMPLE, encoding="utf-8")

        assert _run(tmp_path, tfvars, outputs) == 0

    def test_malformed_json_is_refused(self, tmp_path: Path) -> None:
        bad = tmp_path / "outputs.json"
        bad.write_text("{not json", encoding="utf-8")
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(EXAMPLE, encoding="utf-8")

        assert main(["--outputs-json", str(bad), "--tfvars", str(tfvars)]) == 2

    def test_a_non_object_document_is_refused(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(EXAMPLE, encoding="utf-8")

        assert _run(tmp_path, tfvars, [1, 2]) == 2

    def test_an_unreadable_outputs_path_is_refused(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(EXAMPLE, encoding="utf-8")

        assert main(["--outputs-json", str(tmp_path / "nope"), "--tfvars", str(tfvars)]) == 2

    def test_absent_tfvars_without_an_example_is_refused(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"

        assert _run(tmp_path, tfvars) == 2
        assert not tfvars.exists()


class TestPlaceholderRefusal:
    def test_a_leftover_placeholder_in_a_mapped_key_is_refused(self) -> None:
        with pytest.raises(RenderError, match="vpc_runtime_route_table_ids"):
            refuse_placeholders(
                FILLED.replace(f'"{RUNTIME_TABLES[0]}"', '"rtb-REPLACE_ME_RUNTIME_A"')
            )

    def test_an_unset_mapped_key_is_refused(self) -> None:
        with pytest.raises(RenderError, match="vpc_nat_public_subnet_id"):
            refuse_placeholders(FILLED.replace(f'vpc_nat_public_subnet_id = "{NAT_SUBNET}"', ""))

    def test_a_placeholder_outside_the_mapped_keys_is_allowed(self) -> None:
        refuse_placeholders(FILLED)
        assert "REPLACE_ME" in FILLED


class TestFileHandling:
    def test_a_new_file_is_written_at_mode_0600(self, tmp_path: Path) -> None:
        example = tmp_path / "example"
        example.write_text(EXAMPLE, encoding="utf-8")
        example.chmod(0o644)
        tfvars = tmp_path / "terraform.tfvars"

        assert _run(tmp_path, tfvars, None, "--example", str(example)) == 0
        assert _mode(tfvars) == 0o600

    def test_a_rewritten_file_ends_at_mode_0600(self, tmp_path: Path) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(EXAMPLE, encoding="utf-8")
        tfvars.chmod(0o644)

        assert _run(tmp_path, tfvars) == 0
        assert _mode(tfvars) == 0o600

    def test_no_temporary_file_is_left_behind(self, tmp_path: Path) -> None:
        workdir = tmp_path / "infra"
        workdir.mkdir()
        tfvars = workdir / "terraform.tfvars"
        tfvars.write_text(EXAMPLE, encoding="utf-8")

        assert _run(tmp_path, tfvars) == 0
        assert sorted(p.name for p in workdir.iterdir()) == ["terraform.tfvars"]

    def test_outputs_are_read_from_stdin_with_dash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tfvars = tmp_path / "terraform.tfvars"
        tfvars.write_text(EXAMPLE, encoding="utf-8")
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_outputs())))

        assert main(["--outputs-json", "-", "--tfvars", str(tfvars)]) == 0
        assert tfvars.read_text(encoding="utf-8") == FILLED


def test_the_mapping_is_the_design_table() -> None:
    assert OUTPUT_TO_TFVARS == {
        "vpc_id": "vpc_id",
        "private_subnet_ids": "vpc_private_subnet_ids",
        "private_route_table_ids": "vpc_private_route_table_ids",
        "nat_public_subnet_id": "vpc_nat_public_subnet_id",
        "runtime_subnet_ids": "vpc_runtime_subnet_ids",
        "runtime_route_table_ids": "vpc_runtime_route_table_ids",
    }

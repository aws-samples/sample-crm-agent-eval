# SPDX-License-Identifier: MIT
"""Contract between `infra/bootstrap_account/outputs.tf`, `scripts/render_vpc_tfvars.py` and
`infra/variables.tf`.

Every bootstrap output is either mapped by `OUTPUT_TO_TFVARS` or a named evidence-only output; every
mapped tfvars key is a root `variable`; and the output's list-ness, the renderer's expected shape and
the variable's `type` agree. Each check reads the files themselves, so renaming or retyping either
side reds the test.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Final

import pytest
from scripts.hcl_text import blank_comments, blank_heredoc_bodies, blank_string_literals
from scripts.render_vpc_tfvars import OUTPUT_SHAPE, OUTPUT_TO_TFVARS

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_OUTPUTS_TF = REPO_ROOT / "infra" / "bootstrap_account" / "outputs.tf"
ROOT_VARIABLES_TF = REPO_ROOT / "infra" / "variables.tf"

#: Bootstrap outputs that are recorded as evidence and deliberately not rendered into tfvars.
EVIDENCE_ONLY_OUTPUTS: Final[frozenset[str]] = frozenset(
    {"apigw_cloudwatch_role_arn", "az_name_by_id"}
)


def _blocks(path: Path, kind: str) -> dict[str, str]:
    """Map each top-level `kind "name" { ... }` block in `path` to its body text (comments blanked)."""
    original = path.read_text(encoding="utf-8")
    code = blank_comments(blank_heredoc_bodies(original))
    structure = blank_string_literals(code)
    blocks: dict[str, str] = {}
    for header in re.finditer(rf'^{kind}\s+"([^"]+)"\s*\{{', code, re.MULTILINE):
        depth = 0
        for index in range(header.end() - 1, len(structure)):
            if structure[index] == "{":
                depth += 1
            elif structure[index] == "}":
                depth -= 1
                if depth == 0:
                    break
        else:
            raise AssertionError(f"{path}: {kind} {header.group(1)!r} has no closing brace")
        name = header.group(1)
        assert name not in blocks, f"{path}: {kind} {name!r} is declared twice"
        blocks[name] = code[header.end() : index]
    assert blocks, f"{path}: no {kind} block parsed, so every check below would be vacuous"
    return blocks


def _output_is_list(name: str, body: str) -> bool:
    """Whether an output's `value` is a list: a `[...]` literal or a `[*]` splat. A bare `.id`
    reference is a scalar; any other expression is refused rather than guessed."""
    value = re.search(r"^\s*value\s*=\s*(.+)$", body, re.MULTILINE)
    assert value is not None, f"output {name!r} has no single-line value"
    expression = value.group(1).strip()
    if expression.startswith("[") or "[*]" in expression:
        return True
    if re.fullmatch(r"[a-z_]+\.[a-z_]+\.id", expression):
        return False
    raise AssertionError(f"output {name!r} value {expression!r} is neither a list nor an id")


def _variable_is_list(name: str, body: str) -> bool:
    declared = re.search(r"^\s*type\s*=\s*(\S+)", body, re.MULTILINE)
    assert declared is not None, f"variable {name!r} declares no type"
    return declared.group(1).startswith("list(")


def _contract_problems(outputs_tf: Path, variables_tf: Path) -> list[str]:
    """Every disagreement between the two files and the renderer's mapping; empty when they agree."""
    outputs = _blocks(outputs_tf, "output")
    variables = _blocks(variables_tf, "variable")
    problems: list[str] = []

    unmapped = set(outputs) - set(OUTPUT_TO_TFVARS) - EVIDENCE_ONLY_OUTPUTS
    missing = set(OUTPUT_TO_TFVARS) - set(outputs)
    if unmapped:
        problems.append(f"outputs neither mapped nor evidence-only: {sorted(unmapped)}")
    if missing:
        problems.append(f"mapped outputs absent from {outputs_tf.name}: {sorted(missing)}")

    for output, key in OUTPUT_TO_TFVARS.items():
        if key not in variables:
            problems.append(f"{output} -> {key}, which is not a variable in {variables_tf.name}")
            continue
        if output not in outputs:
            continue
        shapes = {
            "output": _output_is_list(output, outputs[output]),
            "renderer": OUTPUT_SHAPE[output][1],
            "variable": _variable_is_list(key, variables[key]),
        }
        if len(set(shapes.values())) != 1:
            problems.append(f"{output} -> {key} list-ness disagrees: {shapes}")
    return problems


def test_the_mapped_outputs_are_exactly_the_non_evidence_outputs() -> None:
    outputs = set(_blocks(BOOTSTRAP_OUTPUTS_TF, "output"))
    assert outputs - EVIDENCE_ONLY_OUTPUTS == set(OUTPUT_TO_TFVARS)
    assert outputs >= EVIDENCE_ONLY_OUTPUTS, "an evidence-only output was removed"


def test_the_bootstrap_outputs_tfvars_and_variables_agree() -> None:
    assert _contract_problems(BOOTSTRAP_OUTPUTS_TF, ROOT_VARIABLES_TF) == []


def test_the_renderer_shape_table_covers_exactly_the_mapping() -> None:
    assert set(OUTPUT_SHAPE) == set(OUTPUT_TO_TFVARS)


@pytest.fixture
def copies(tmp_path: Path) -> tuple[Path, Path]:
    """Scratch copies of the two files; the unmutated pair must pass for a mutation to mean anything."""
    outputs_tf = tmp_path / "outputs.tf"
    variables_tf = tmp_path / "variables.tf"
    shutil.copyfile(BOOTSTRAP_OUTPUTS_TF, outputs_tf)
    shutil.copyfile(ROOT_VARIABLES_TF, variables_tf)
    assert _contract_problems(outputs_tf, variables_tf) == []
    return outputs_tf, variables_tf


def _mutate(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} is not unique in {path.name}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_a_renamed_output_reds_the_contract(copies: tuple[Path, Path]) -> None:
    outputs_tf, variables_tf = copies
    _mutate(outputs_tf, 'output "runtime_subnet_ids"', 'output "agentcore_subnet_ids"')
    problems = _contract_problems(outputs_tf, variables_tf)
    assert any("agentcore_subnet_ids" in p for p in problems)
    assert any("runtime_subnet_ids" in p and "absent" in p for p in problems)


def test_a_renamed_variable_reds_the_contract(copies: tuple[Path, Path]) -> None:
    outputs_tf, variables_tf = copies
    _mutate(variables_tf, 'variable "vpc_nat_public_subnet_id"', 'variable "vpc_nat_subnet_id"')
    problems = _contract_problems(outputs_tf, variables_tf)
    assert problems == [
        "nat_public_subnet_id -> vpc_nat_public_subnet_id, which is not a variable in variables.tf"
    ]


def test_a_scalar_output_for_a_list_variable_reds_the_contract(copies: tuple[Path, Path]) -> None:
    outputs_tf, variables_tf = copies
    _mutate(
        outputs_tf,
        "value       = [aws_route_table.runtime.id]",
        "value = aws_route_table.runtime.id",
    )
    problems = _contract_problems(outputs_tf, variables_tf)
    assert len(problems) == 1 and "runtime_route_table_ids" in problems[0]
    assert "'output': False" in problems[0]

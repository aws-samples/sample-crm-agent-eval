# SPDX-License-Identifier: MIT
"""The `dynamodb_grants` validations in `infra/modules/lambda_action/variables.tf`.

Each validation is exercised by a `terraform plan` over the variable block extracted verbatim from the
shipped file; `terraform validate` does not evaluate variable validations, which one test pins.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

import pytest
from scripts import proc_run

from tests.unit._terraform_fixture import (
    _PROVIDER_BLOCK,
    PLUGIN_DIR,
    REPO_ROOT,
    REQUIRE_TERRAFORM_ENV_VAR,
    require_terraform,
    terraform_unavailable_reason,
)

_VARIABLES_TF: Final[Path] = REPO_ROOT / "infra" / "modules" / "lambda_action" / "variables.tf"

#: Number of validations the shipped block carries: two over `actions`, two over the resource ARNs and
#: one over `leading_key_condition.operator`.
_EXPECTED_VALIDATION_BLOCKS: Final[int] = 5

_GOOD_TABLE_ARN: Final[str] = "arn:aws:dynamodb:us-east-1:123456789012:table/crm-agent-evalreps"


def _extract_dynamodb_grants_variable() -> str:
    """Return the shipped `dynamodb_grants` variable block, verbatim.

    Raises if the slice does not carry exactly `_EXPECTED_VALIDATION_BLOCKS` validation blocks.
    """
    source = _VARIABLES_TF.read_text(encoding="utf-8")
    start = source.index('variable "dynamodb_grants"')
    end = source.index('variable "denied_table_arns"')
    block = source[start:end].rstrip() + "\n"
    found = block.count("validation {")
    if found != _EXPECTED_VALIDATION_BLOCKS:
        raise AssertionError(
            f"extracted {found} validation block(s) from {_VARIABLES_TF}, expected "
            f"{_EXPECTED_VALIDATION_BLOCKS}. Either the extraction boundaries moved or a validation "
            "was removed; both make every wildcard plant in this file pass vacuously."
        )
    return block


def _require_terraform_or_skip() -> None:
    reason = terraform_unavailable_reason()
    if reason is None:
        return
    message = f"terraform plan harness unavailable: {reason}"
    if require_terraform():
        pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
    pytest.skip(message)


def _grants_hcl(*, table_arn: str, index_arns: str = "[]", action: str = "dynamodb:Query") -> str:
    return (
        f'dynamodb_grants = [{{table_name = "reps", table_arn = "{table_arn}", '
        f'index_arns = {index_arns}, actions = ["{action}"]}}]\n'
    )


def _grants_hcl_with_operator(*, operator: str) -> str:
    """A reps grant carrying a leading_key_condition with the given operator."""
    return (
        f'dynamodb_grants = [{{table_name = "reps", table_arn = "{_GOOD_TABLE_ARN}", '
        f'index_arns = ["{_GOOD_TABLE_ARN}/index/gsi_tenant"], actions = ["dynamodb:Query"], '
        f'leading_key_condition = {{attribute = "dynamodb:LeadingKeys", operator = "{operator}", '
        f'values = ["eval-*"]}}}}]\n'
    )


def _plan(work_dir: Path, tfvars: str) -> subprocess.CompletedProcess[str]:
    """Init and plan a root module holding only the shipped variable block, with `tfvars` supplied.

    Returns the completed `plan` process without checking its exit status.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "variables.tf").write_text(_extract_dynamodb_grants_variable(), encoding="utf-8")
    (work_dir / "provider.tf").write_text(_PROVIDER_BLOCK, encoding="utf-8")
    (work_dir / "terraform.tfvars").write_text(tfvars, encoding="utf-8")
    init = proc_run.run(
        [
            "terraform",
            "init",
            "-input=false",
            "-backend=false",
            "-no-color",
            f"-plugin-dir={PLUGIN_DIR}",
        ],
        cwd=work_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert init.returncode == 0, f"fixture init failed:\n{init.stdout}\n{init.stderr}"
    return proc_run.run(
        ["terraform", "plan", "-input=false", "-no-color"],
        cwd=work_dir,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_extraction_carries_every_shipped_validation_block() -> None:
    """The extracted block carries exactly `_EXPECTED_VALIDATION_BLOCKS` validation blocks."""
    assert _extract_dynamodb_grants_variable().count("validation {") == _EXPECTED_VALIDATION_BLOCKS


def test_the_preexisting_action_validation_still_rejects(tmp_path: Path) -> None:
    """A wildcard action is rejected, which is the harness's positive control."""
    _require_terraform_or_skip()
    result = _plan(tmp_path, _grants_hcl(table_arn=_GOOD_TABLE_ARN, action="dynamodb:*"))
    assert result.returncode != 0
    assert "may not include the wildcard action" in result.stdout + result.stderr


def test_a_correctly_scoped_grant_is_accepted(tmp_path: Path) -> None:
    """A fully-qualified table ARN with a trailing `/index/*` index ARN is accepted."""
    _require_terraform_or_skip()
    result = _plan(
        tmp_path,
        _grants_hcl(table_arn=_GOOD_TABLE_ARN, index_arns=f'["{_GOOD_TABLE_ARN}/index/*"]'),
    )
    assert result.returncode == 0, (
        f"a correct grant was rejected:\n{result.stdout}\n{result.stderr}"
    )


def test_a_stringlike_leading_key_operator_is_accepted(tmp_path: Path) -> None:
    """A StringLike leading_key_condition operator is accepted."""
    _require_terraform_or_skip()
    result = _plan(tmp_path, _grants_hcl_with_operator(operator="StringLike"))
    assert result.returncode == 0, (
        f"a StringLike leading_key_condition was rejected:\n{result.stdout}\n{result.stderr}"
    )


def test_an_unknown_leading_key_operator_is_rejected(tmp_path: Path) -> None:
    """A leading_key_condition operator other than StringEquals or StringLike is rejected."""
    _require_terraform_or_skip()
    result = _plan(tmp_path, _grants_hcl_with_operator(operator="StringNotEquals"))
    assert result.returncode != 0, "an unknown leading_key_condition.operator was accepted"
    assert "operator must be exactly" in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("label", "table_arn"),
    [
        ("bare wildcard", "*"),
        ("wildcard table segment", "arn:aws:dynamodb:us-east-1:123456789012:table/*"),
        ("wildcard region and account", "arn:aws:dynamodb:*:*:table/reps"),
        ("not an ARN at all", "reps"),
    ],
)
def test_a_wildcard_table_arn_is_rejected(label: str, table_arn: str, tmp_path: Path) -> None:
    """Each of these `table_arn` forms is a plan-time failure."""
    _require_terraform_or_skip()
    result = _plan(tmp_path, _grants_hcl(table_arn=table_arn))
    assert result.returncode != 0, f"{label} was accepted: {table_arn}"
    assert "must be one fully-qualified DynamoDB table ARN" in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("label", "index_arn"),
    [
        ("bare wildcard", "*"),
        ("wildcard table segment", "arn:aws:dynamodb:us-east-1:123456789012:table/*/index/*"),
        ("wildcard account", "arn:aws:dynamodb:us-east-1:*:table/reps/index/gsi_tenant"),
    ],
)
def test_a_wildcard_outside_the_index_name_is_rejected(
    label: str, index_arn: str, tmp_path: Path
) -> None:
    """The wildcard is permitted as the index name and nowhere else in the ARN."""
    _require_terraform_or_skip()
    result = _plan(tmp_path, _grants_hcl(table_arn=_GOOD_TABLE_ARN, index_arns=f'["{index_arn}"]'))
    assert result.returncode != 0, f"{label} was accepted: {index_arn}"
    assert "must be one table's index ARN" in result.stdout + result.stderr


def test_terraform_validate_cannot_see_these_validations(tmp_path: Path) -> None:
    """`terraform validate` exits 0 on a wildcard `table_arn` that a plan rejects."""
    _require_terraform_or_skip()
    work_dir = tmp_path / "validate_blind"
    work_dir.mkdir()
    (work_dir / "variables.tf").write_text(_extract_dynamodb_grants_variable(), encoding="utf-8")
    (work_dir / "provider.tf").write_text(_PROVIDER_BLOCK, encoding="utf-8")
    (work_dir / "terraform.tfvars").write_text(_grants_hcl(table_arn="*"), encoding="utf-8")
    init = proc_run.run(
        [
            "terraform",
            "init",
            "-input=false",
            "-backend=false",
            "-no-color",
            f"-plugin-dir={PLUGIN_DIR}",
        ],
        cwd=work_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert init.returncode == 0, f"fixture init failed:\n{init.stdout}\n{init.stderr}"
    validated = proc_run.run(
        ["terraform", "validate", "-no-color"],
        cwd=work_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert validated.returncode == 0, (
        "terraform validate rejected a wildcard table_arn. If this ever starts failing, the "
        "validate-is-blind caveat in this module's docstring is obsolete and the deferred "
        "dynamodb_no_wildcard_action rows gain a genuinely offline compensating control."
    )

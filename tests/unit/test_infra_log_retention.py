# SPDX-License-Identifier: MIT
"""Every `aws_cloudwatch_log_group` in `infra/` retains logs for at least 365 days (CKV_AWS_338).

Three subjects:
- the source tree: each log group's `retention_in_days` is a literal >= 365, or a module variable
  whose default is >= 365, whose `validation` refuses < 365, and that every root call site sets to a
  literal >= 365 when it sets it at all; scratch copies with one value lowered to 30 red it;
- terraform itself: each such variable's validation rejects 30 and accepts 365 in a real plan;
- the plans: the agentcore and lambda_action module plans, and the root plan fixture
  `plan_fixtures.build("compliant")`.
`modules/lambda_action` sets the literal 365 and takes no retention variable (CKV_AWS_338).
Terraform-backed cells skip when terraform is absent, unless `CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from scripts import plan_fixtures, proc_run

from tests.unit import _eni_function_code_deny as eni_deny
from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_module,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)
from tests.unit.test_infra_agentcore import _module_arguments

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
INFRA: Final[Path] = REPO_ROOT / "infra"

MINIMUM_RETENTION_DAYS: Final[int] = 365
LAMBDA_ACTION: Final[Path] = INFRA / "modules" / "lambda_action"
LAMBDA_ACTION_LOG_GROUP: Final[str] = "module.subject.aws_cloudwatch_log_group.this"

_LOG_GROUP = re.compile(r'(?m)^resource\s+"aws_cloudwatch_log_group"\s+"([\w-]+)"\s*\{')
_MODULE_CALL = re.compile(r'(?m)^module\s+"([\w-]+)"\s*\{')


@dataclass(frozen=True)
class LogGroup:
    path: Path
    name: str
    retention: str | None


def _block_body(text: str, open_brace: int) -> str:
    depth = 0
    for index in range(open_brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : index]
    raise AssertionError(f"unbalanced block at offset {open_brace}")


def _attribute(body: str, key: str) -> str | None:
    found = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*(\S+)\s*$", body)
    return found.group(1) if found else None


def _tf_files(infra: Path) -> list[Path]:
    return sorted(p for p in infra.rglob("*.tf") if ".terraform" not in p.parts)


def log_groups(infra: Path) -> list[LogGroup]:
    groups = []
    for path in _tf_files(infra):
        text = path.read_text(encoding="utf-8")
        for match in _LOG_GROUP.finditer(text):
            body = _block_body(text, match.end() - 1)
            groups.append(LogGroup(path, match.group(1), _attribute(body, "retention_in_days")))
    return groups


def _variable_body(module_dir: Path, name: str) -> str | None:
    for path in sorted(module_dir.glob("*.tf")):
        text = path.read_text(encoding="utf-8")
        match = re.search(rf'(?m)^variable\s+"{re.escape(name)}"\s*\{{', text)
        if match:
            return _block_body(text, match.end() - 1)
    return None


def _module_call_values(infra: Path, module_dir: Path, name: str) -> list[tuple[str, str | None]]:
    """`(call, value)` for every root `module` block sourcing `module_dir`."""
    calls = []
    source = f'"./{module_dir.relative_to(infra).as_posix()}"'
    for path in sorted(infra.glob("*.tf")):
        text = path.read_text(encoding="utf-8")
        for match in _MODULE_CALL.finditer(text):
            body = _block_body(text, match.end() - 1)
            if _attribute(body, "source") == source:
                calls.append((f"{path.name}:{match.group(1)}", _attribute(body, name)))
    return calls


def _at_least_minimum(literal: str | None) -> bool:
    return literal is not None and literal.isdigit() and int(literal) >= MINIMUM_RETENTION_DAYS


def retention_failures(infra: Path) -> list[str]:
    """Why some log group in `infra` could resolve to a retention below the minimum."""
    failures = []
    groups = log_groups(infra)
    if not groups:
        failures.append("no aws_cloudwatch_log_group found")
    for group in groups:
        where = f"{group.path.relative_to(infra)}:{group.name}"
        value = group.retention
        if value is not None and value.isdigit():
            if not _at_least_minimum(value):
                failures.append(f"{where}: retention_in_days = {value}")
            continue
        variable = re.fullmatch(r"var\.(\w+)", value or "")
        if variable is None:
            failures.append(
                f"{where}: retention_in_days = {value!r} is not a literal or a variable"
            )
            continue
        name = variable.group(1)
        body = _variable_body(group.path.parent, name)
        if body is None:
            failures.append(f"{where}: no variable {name!r}")
            continue
        default = _attribute(body, "default")
        if default is not None and not _at_least_minimum(default):
            failures.append(f"{where}: variable {name} default = {default}")
        if not re.search(rf"condition\s*=\s*var\.{name}\s*>=\s*{MINIMUM_RETENTION_DAYS}\b", body):
            failures.append(
                f"{where}: variable {name} has no `>= {MINIMUM_RETENTION_DAYS}` validation"
            )
        if group.path.parent != infra:
            for call, passed in _module_call_values(infra, group.path.parent, name):
                if passed is not None and not _at_least_minimum(passed):
                    failures.append(f"{where}: module call {call} passes {name} = {passed}")
    return failures


def _scratch_infra(tmp_path: Path) -> Path:
    scratch = tmp_path / "infra"
    for path in _tf_files(INFRA):
        target = scratch / path.relative_to(INFRA)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return scratch


def _require_harness() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)


def _validated_variables() -> list[tuple[Path, str]]:
    found = set()
    for group in log_groups(INFRA):
        variable = re.fullmatch(r"var\.(\w+)", group.retention or "")
        if variable:
            found.add((group.path.parent, variable.group(1)))
    return sorted(found)


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


def test_every_log_group_in_the_tree_retains_at_least_365_days() -> None:
    assert retention_failures(INFRA) == []


def test_the_source_check_enumerates_every_log_group_block() -> None:
    declared = sum(
        len(_LOG_GROUP.findall(path.read_text(encoding="utf-8"))) for path in _tf_files(INFRA)
    )
    assert declared > 0
    assert len(log_groups(INFRA)) == declared


def test_no_checkov_retention_skip_remains() -> None:
    assert [str(p) for p in _tf_files(INFRA) if "CKV_AWS_338" in p.read_text("utf-8")] == []


@pytest.mark.parametrize(
    ("relative", "old", "new"),
    [
        ("eval_sfn.tf", "  retention_in_days = 365\n", "  retention_in_days = 30\n"),
        (
            "modules/lambda_action/main.tf",
            "  retention_in_days = 365\n",
            "  retention_in_days = 30\n",
        ),
        (
            "modules/vpc/variables.tf",
            "    condition     = var.flow_log_retention_days >= 365\n",
            "    condition     = var.flow_log_retention_days >= 30\n",
        ),
        ("modules/api/variables.tf", "  default     = 365\n", "  default     = 30\n"),
    ],
)
def test_a_scratch_lowering_to_30_reds_the_source_check(
    tmp_path: Path, relative: str, old: str, new: str
) -> None:
    scratch = _scratch_infra(tmp_path)
    target = scratch / relative
    text = target.read_text(encoding="utf-8")
    assert text.count(old) == 1, (relative, old)
    target.write_text(text.replace(old, new), encoding="utf-8")
    assert retention_failures(scratch) != []


def test_lambda_action_retains_its_log_group_365_days_as_a_literal() -> None:
    (group,) = [g for g in log_groups(INFRA) if g.path.parent == LAMBDA_ACTION]
    assert group.retention == str(MINIMUM_RETENTION_DAYS)
    assert _variable_body(LAMBDA_ACTION, "log_retention_days") is None
    assert [
        c for c in _module_call_values(INFRA, LAMBDA_ACTION, "log_retention_days") if c[1]
    ] == []
    assert _module_call_values(INFRA, LAMBDA_ACTION, "source") != []


# ---------------------------------------------------------------------------
# Terraform: each variable's validation is enforced in a real plan
# ---------------------------------------------------------------------------


def test_every_retention_variable_is_validated() -> None:
    assert _validated_variables() != []


@pytest.mark.parametrize(
    ("module_dir", "name"),
    _validated_variables(),
    ids=lambda v: v.name if isinstance(v, Path) else str(v),
)
def test_terraform_rejects_30_and_accepts_365(tmp_path: Path, module_dir: Path, name: str) -> None:
    _require_harness()
    body = _variable_body(module_dir, name)
    assert body is not None
    (tmp_path / "main.tf").write_text(f'variable "{name}" {{{body}}}\n', encoding="utf-8")

    def plan(value: int) -> subprocess.CompletedProcess[str]:
        return proc_run.run(
            ["terraform", "plan", "-input=false", "-no-color", f"-var={name}={value}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

    init = proc_run.run(
        ["terraform", "init", "-input=false", "-backend=false", "-no-color"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert init.returncode == 0, init.stderr
    rejected = plan(30)
    assert rejected.returncode != 0
    assert "Invalid value for variable" in rejected.stderr, rejected.stderr
    accepted = plan(MINIMUM_RETENTION_DAYS)
    assert accepted.returncode == 0, accepted.stderr


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def agentcore_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _require_harness()
    return plan_module(
        tmp_path_factory.mktemp("retention-agentcore"), "agentcore", _module_arguments()
    )


def test_the_agentcore_plan_retains_every_log_group_365_days(agentcore_plan: Plan) -> None:
    addresses = agentcore_plan.addresses_of_type("aws_cloudwatch_log_group")
    assert addresses
    for address in addresses:
        retention = agentcore_plan.after(address)["retention_in_days"]
        assert retention >= MINIMUM_RETENTION_DAYS, (address, retention)


def _plan_lambda_action(work_dir: Path, module_dir: Path) -> Plan:
    function_zip = work_dir / "function.zip"
    work_dir.mkdir(parents=True, exist_ok=True)
    function_zip.write_bytes(b"")
    arguments = {
        "source": f'"{module_dir}"',
        "function_name": '"crm-agent-eval-get-account"',
        "handler": '"crm.handlers.get_account.handler"',
        "filename": f'"{function_zip}"',
        "source_code_hash": '"0000000000000000000000000000000000000000000="',
        "reserved_concurrent_executions": "5",
        "dynamodb_grants": "[]",
        "vpc_subnet_ids": json.dumps(["subnet-fixturezonea", "subnet-fixturezoneb"]),
        "vpc_security_group_ids": json.dumps(["sg-fixturelambda"]),
        "eni_function_code_deny_policy_arn": f'"{eni_deny.POLICY_ARN}"',
    }
    body = "\n".join(f"  {key} = {value}" for key, value in arguments.items())
    return plan_root(work_dir, f'module "subject" {{\n{body}\n}}\n')


def test_the_lambda_action_plan_retains_its_log_group_365_days(tmp_path: Path) -> None:
    _require_harness()
    plan = _plan_lambda_action(tmp_path / "root", LAMBDA_ACTION)
    assert plan.after(LAMBDA_ACTION_LOG_GROUP)["retention_in_days"] == MINIMUM_RETENTION_DAYS


def test_a_scratch_lambda_action_at_30_days_reds_the_plan_check(tmp_path: Path) -> None:
    _require_harness()
    module_dir = tmp_path / "lambda_action"
    shutil.copytree(LAMBDA_ACTION, module_dir, ignore=shutil.ignore_patterns(".terraform"))
    main = module_dir / "main.tf"
    text = main.read_text(encoding="utf-8")
    assert text.count("  retention_in_days = 365\n") == 1
    main.write_text(text.replace("  retention_in_days = 365\n", "  retention_in_days = 30\n"))
    plan = _plan_lambda_action(tmp_path / "root", module_dir)
    assert plan.after(LAMBDA_ACTION_LOG_GROUP)["retention_in_days"] == 30


def test_every_lambda_action_log_group_in_the_root_plan_fixture_plans_365_days() -> None:
    changes = plan_fixtures.build("compliant")["resource_changes"]
    retention = {
        c["address"]: c["change"]["after"]["retention_in_days"]
        for c in changes
        if c["type"] == "aws_cloudwatch_log_group"
        and c["address"].startswith("module.lambda_action")
    }
    assert retention
    assert set(retention.values()) == {MINIMUM_RETENTION_DAYS}, retention


def compliant_plan_failures(document: dict[str, object]) -> list[str]:
    changes = document.get("resource_changes")
    assert isinstance(changes, list)
    failures = []
    groups = [c for c in changes if c.get("type") == "aws_cloudwatch_log_group"]
    if not groups:
        failures.append("no aws_cloudwatch_log_group in the plan")
    for change in groups:
        retention = (change.get("change", {}).get("after") or {}).get("retention_in_days")
        if not isinstance(retention, int) or retention < MINIMUM_RETENTION_DAYS:
            failures.append(f"{change.get('address')}: retention_in_days = {retention!r}")
    return failures


def test_every_log_group_in_the_root_plan_fixture_retains_365_days() -> None:
    document = plan_fixtures.build("compliant")
    assert compliant_plan_failures(document) == []


def test_a_30_day_group_reds_the_root_plan_check() -> None:
    document = plan_fixtures.build("compliant")
    for change in document["resource_changes"]:
        if change.get("type") == "aws_cloudwatch_log_group":
            change["change"]["after"]["retention_in_days"] = MINIMUM_RETENTION_DAYS
    assert compliant_plan_failures(document) == []
    first = next(
        c for c in document["resource_changes"] if c.get("type") == "aws_cloudwatch_log_group"
    )
    first["change"]["after"]["retention_in_days"] = 30
    assert compliant_plan_failures(document) == [f"{first['address']}: retention_in_days = 30"]

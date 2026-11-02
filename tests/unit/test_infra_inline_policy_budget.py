# SPDX-License-Identifier: MIT
"""Plan-time bound: every VPC Lambda role's IAM policy sizes stay under `iam_policy_budget`.

Plans the root's real `lambda_action`, `agent_relay`, `eval_artifacts` and `eni_function_code_deny`
blocks, its locals, and the root inline and managed policies attached to those roles, read verbatim
from `infra/main.tf` and every sibling `infra/*.tf`. The modules they depend on but that attach no
policy to these roles (dynamodb, vpc, observability, cognito, agentcore, frontend_cdn) are replaced
by stub modules whose outputs are literals shaped like the deployed values: DynamoDB table and index
ARNs from the plan fixture's table names, KMS key ARNs with a 36-character key id. Scratch copies of
the real modules read the log-group and function ARNs from the function name rather than the
resource, and attach inline policies by role name, so every policy document renders at plan time.
Sizes are computed with `iam_policy_budget`'s own counter and compared with its own FAIL threshold:
the inline aggregate per role against 10,240 chars, each managed policy against 6,144, and each
role's attachment count against the managed-policies-per-role quota.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Final

import pytest
from scripts import plan_fixtures, proc_run
from scripts.smoke.iam_policy_budget import (
    FAIL_USAGE_FRACTION,
    INLINE_POLICY_AGGREGATE_LIMIT_CHARS,
    MANAGED_POLICIES_PER_ROLE_QUOTA,
    MANAGED_POLICY_DOCUMENT_LIMIT_CHARS,
    _policy_document_chars,
)
from scripts.smoke.iam_roles import asserted_dynamodb_actions

from crm.toolspec.definitions import ALL_TOOLS
from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
INFRA: Final[Path] = REPO_ROOT / "infra"

ACCOUNT: Final[str] = "123456789012"
REGION: Final[str] = "us-east-1"
_KEY_ID: Final[str] = "11111111-2222-3333-4444-5555eeee5555"

#: Modules planned from their real source. Every other referenced module becomes a stub.
REAL_MODULES: Final[frozenset[str]] = frozenset(
    {
        "lambda_action",
        "agent_relay",
        "eval_artifacts",
        "eni_function_code_deny",
    }
)

#: Root expressions with no stub module behind them, replaced by literals of the deployed shape.
ROOT_SUBSTITUTIONS: Final[dict[str, str]] = {
    "data.aws_caller_identity.current.account_id": f'"{ACCOUNT}"',
    "data.aws_iam_session_context.current.issuer_arn": f'"arn:aws:iam::{ACCOUNT}:role/fixture-operator"',
    "var.crm_operator_principal_arns": f'["arn:aws:iam::{ACCOUNT}:role/fixture-operator"]',
    "module.eval_artifacts.bucket_arn": f'"arn:aws:s3:::crm-agent-eval-eval-artifacts-{ACCOUNT}"',
    "aws_kms_key.eval_artifacts.arn": f'"arn:aws:kms:{REGION}:{ACCOUNT}:key/{_KEY_ID}"',
    "aws_iam_role.agentcore_gateway.arn": (
        f'"arn:aws:iam::{ACCOUNT}:role/crm-agent-eval-agentcore-gateway-role"'
    ),
    "local.eval_sweep_state_machine_arn": (
        f'"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:crm-agent-eval-eval-sweep"'
    ),
    "local.eval_sweep_execution_arn_pattern": (
        f'"arn:aws:states:{REGION}:{ACCOUNT}:execution:crm-agent-eval-eval-sweep:*"'
    ),
}

#: Sibling-file expressions on these roles' inline policies that are unknown at plan time.
SIBLING_SUBSTITUTIONS: Final[dict[str, str]] = {
    "module.eval_artifacts.bucket_arn": f'"arn:aws:s3:::crm-agent-eval-eval-artifacts-{ACCOUNT}"',
}

#: Expressions inside the real modules that are unknown at plan time, and their deployed shape.
MODULE_SUBSTITUTIONS: Final[dict[str, tuple[str, str]]] = {
    "aws_cloudwatch_log_group.this.arn": (
        "aws_cloudwatch_log_group.this.arn",
        f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/${{var.function_name}}",
    ),
    "role   = aws_iam_role.this.id": (
        "role   = aws_iam_role.this.id",
        "role   = aws_iam_role.this.name",
    ),
}

#: Expressions inside the real modules' `outputs.tf` that are unknown at plan time.
OUTPUT_SUBSTITUTIONS: Final[dict[str, str]] = {
    "value       = aws_lambda_function.this.arn": (
        f'value       = "arn:aws:lambda:{REGION}:{ACCOUNT}:function:${{var.function_name}}"'
    ),
}

#: Literal outputs for stub-module references that land in an inline policy on these roles.
_RUNTIME_ARN: Final[str] = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/crm_agent_eval_runtime-AAAAAAAAAA"
)

_BLOCK_START = re.compile(r"^(locals|module|resource|data|variable|check)\b[^\n]*\{\s*(\}\s*)?$")


def _top_level_blocks(text: str) -> list[str]:
    """Every column-0 block of `text`, each as its full source."""
    blocks: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _BLOCK_START.match(line)
        if not match:
            index += 1
            continue
        if match.group(2):
            blocks.append(line)
            index += 1
            continue
        end = index + 1
        while lines[end] != "}":
            end += 1
        blocks.append("\n".join(lines[index : end + 1]))
        index = end + 1
    return blocks


def _module_source(block: str) -> str | None:
    match = re.search(r'^\s*source\s*=\s*"\./modules/([a-z_]+)"', block, re.MULTILINE)
    return match.group(1) if match else None


def _selected_root(text: str) -> str:
    """Locals, the real-module blocks, and the root inline and managed policies on their roles."""
    blocks = _top_level_blocks(text)
    selected = [b for b in blocks if b.startswith("locals ")]
    real_names: set[str] = set()
    for block in blocks:
        if block.startswith("module ") and _module_source(block) in REAL_MODULES:
            selected.append(block)
            real_names.add(block.split('"')[1])
    documents = {
        b.split('"')[3]: b for b in blocks if b.startswith('data "aws_iam_policy_document"')
    }
    managed = {b.split('"')[3]: b for b in blocks if b.startswith('resource "aws_iam_policy" ')}
    for block in blocks:
        if block.startswith('resource "aws_iam_role_policy_attachment" '):
            policy = re.search(r"aws_iam_policy\.([a-z_]+)\.arn", block)
            assert policy is not None, block
            policy_block = managed[policy.group(1)]
        elif block.startswith('resource "aws_iam_role_policy" '):
            policy_block = block
        else:
            continue
        role = re.search(r"^\s*role\s*=\s*module\.([a-z_]+)\.", block, re.MULTILINE)
        if role is None or role.group(1) not in real_names:
            continue
        selected.append(block)
        if policy_block is not block:
            selected.append(policy_block)
        document = re.search(r"data\.aws_iam_policy_document\.([a-z_]+)\.json", policy_block)
        assert document is not None, policy_block
        selected.append(documents[document.group(1)])
    root = "\n\n".join(selected)
    for expression, literal in ROOT_SUBSTITUTIONS.items():
        root = root.replace(expression, literal)
    return root


def _dynamodb_outputs() -> dict[str, Any]:
    fixture = plan_fixtures.build("compliant")
    table_arns: dict[str, str] = {}
    gsi_arns: dict[str, dict[str, str]] = {}
    names: dict[str, str] = {}
    for change in fixture["resource_changes"]:
        if change.get("type") != "aws_dynamodb_table":
            continue
        logical = change["name"]
        after = change["change"]["after"]
        arn = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{after['name']}"
        names[logical] = after["name"]
        table_arns[logical] = arn
        gsi_arns[logical] = {
            gsi["name"]: f"{arn}/index/{gsi['name']}"
            for gsi in after.get("global_secondary_index") or []
        }
    assert table_arns, "the plan fixture holds no aws_dynamodb_table"
    return {
        "table_arns": table_arns,
        "gsi_arns": gsi_arns,
        "table_names": names,
        "kms_key_arn": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{_KEY_ID}",
    }


def _stub_value(module: str, output: str) -> Any:
    if module == "dynamodb":
        return _dynamodb_outputs()[output]
    if output == "agent_runtime_arn":
        return _RUNTIME_ARN
    if output.endswith("_ids"):
        return [f"stub-{module}-{output}-a", f"stub-{module}-{output}-b"]
    if "kms" in output and output.endswith("_arn"):
        return f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{_KEY_ID}"
    if output.endswith("policy_arn"):
        return f"arn:aws:iam::{ACCOUNT}:policy/stub-{module}-{output}"
    if output.endswith("_arn"):
        return f"arn:aws:iam::{ACCOUNT}:role/stub-{module}-{output}"
    return f"stub-{module}-{output}"


def _write_stub(work_dir: Path, module: str, outputs: set[str]) -> None:
    directory = work_dir / "modules" / module
    directory.mkdir(parents=True)
    body = "\n".join(
        f'output "{name}" {{\n  value = jsondecode({json.dumps(json.dumps(_stub_value(module, name)))})\n}}'
        for name in sorted(outputs)
    )
    (directory / "outputs.tf").write_text(body + "\n", encoding="utf-8")


def _copy_real_module(work_dir: Path, module: str) -> None:
    target = work_dir / "modules" / module
    shutil.copytree(INFRA / "modules" / module, target, ignore=shutil.ignore_patterns(".terraform"))
    main = target / "main.tf"
    text = main.read_text(encoding="utf-8")
    for old, new in MODULE_SUBSTITUTIONS.values():
        if old.startswith("aws_cloudwatch_log_group"):
            text = text.replace("${" + old + "}", new).replace(old, f'"{new}"')
        else:
            text = text.replace(old, new)
    main.write_text(text, encoding="utf-8")
    outputs = target / "outputs.tf"
    if outputs.exists():
        text = outputs.read_text(encoding="utf-8")
        for old, new in OUTPUT_SUBSTITUTIONS.items():
            text = text.replace(old, new)
        outputs.write_text(text, encoding="utf-8")


_BUILD_FILE = re.compile(r"\$\{var\.lambda_build_dir\}/([^\"]+)")


def _tool_keys(root: str) -> list[str]:
    """The keys of `local.tools`, which the per-tool module block names its zips after."""
    match = re.search(r"^  tools = \{\n(?P<body>.*?)^  \}$", root, re.DOTALL | re.MULTILINE)
    assert match is not None, "no `tools = { ... }` local in the selected root"
    keys = re.findall(r"^    ([a-z_]+)\s*=\s*\{", match.group("body"), re.MULTILINE)
    assert keys, "`local.tools` declares no tool"
    return keys


def _write_placeholder_build(root: str, build_dir: Path) -> list[str]:
    """One valid empty zip per `${var.lambda_build_dir}/...` file the root reads; returns the names."""
    names: set[str] = set()
    for reference in _BUILD_FILE.findall(root):
        if reference == "${each.key}.zip":
            names.update(f"{key}.zip" for key in _tool_keys(root))
        else:
            assert "${" not in reference, f"unhandled build-file reference {reference!r}"
            names.add(reference)
    build_dir.mkdir(parents=True)
    for name in sorted(names):
        with zipfile.ZipFile(build_dir / name, "w"):
            pass
    return sorted(names)


def _variables(root: str, build_dir: Path) -> str:
    """Every `var.X` the selected root reads, with the root's own default where it declares one."""
    declared = {
        b.split('"')[1]: b
        for b in _top_level_blocks(
            (INFRA / "variables.tf").read_text(encoding="utf-8")
            + "\n"
            + (INFRA / "main.tf").read_text(encoding="utf-8")
        )
        if b.startswith("variable ")
    }
    blocks = []
    for name in sorted(set(re.findall(r"\bvar\.([a-z_]+)", root))):
        if name == "lambda_build_dir":
            blocks.append(f'variable "lambda_build_dir" {{\n  default = "{build_dir}"\n}}')
        elif "default" in declared.get(name, ""):
            blocks.append(declared[name])
        else:
            blocks.append(f'variable "{name}" {{\n  default = "stub-{name}"\n}}')
    return "\n\n".join(blocks)


def _sibling_root_files() -> list[Path]:
    return [path for path in sorted(INFRA.glob("*.tf")) if path.name != "main.tf"]


def _build(work_dir: Path, main_tf: str | None = None) -> str:
    """The fixture root for `work_dir`, from `main_tf` (default: `infra/main.tf` on disk) and every
    sibling `infra/*.tf`, with `SIBLING_SUBSTITUTIONS` applied to the siblings."""
    if main_tf is None:
        main_tf = (INFRA / "main.tf").read_text(encoding="utf-8")
    siblings = []
    for path in _sibling_root_files():
        text = path.read_text(encoding="utf-8")
        for expression, literal in SIBLING_SUBSTITUTIONS.items():
            text = text.replace(expression, literal)
        siblings.append(text)
    root = _selected_root("\n\n".join([main_tf, *siblings]))
    for module in REAL_MODULES:
        _copy_real_module(work_dir, module)
    references: dict[str, set[str]] = defaultdict(set)
    for module, output in re.findall(r"\bmodule\.([a-z_]+)\.([a-z_]+)", root):
        references[module].add(output)
    real_names = {
        b.split('"')[1]
        for b in _top_level_blocks(root)
        if b.startswith("module ") and _module_source(b) in REAL_MODULES
    }
    stubs = {module: outputs for module, outputs in references.items() if module not in real_names}
    for module, outputs in stubs.items():
        _write_stub(work_dir, module, outputs)
    stub_blocks = "\n\n".join(
        f'module "{module}" {{\n  source = "./modules/{module}"\n}}' for module in sorted(stubs)
    )
    build_dir = work_dir / "build"
    _write_placeholder_build(root, build_dir)
    return f"{_variables(root, build_dir)}\n\n{stub_blocks}\n\n{root}\n"


def _inline_aggregates(plan: Plan) -> dict[str, tuple[int, int]]:
    """`{role_name: (aggregate_chars, policy_count)}` over every planned `aws_iam_role_policy`."""
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    unknown: list[str] = []
    for address in plan.addresses_of_type("aws_iam_role_policy"):
        after = plan.after(address)
        role, policy = after.get("role"), after.get("policy")
        if not isinstance(role, str) or not isinstance(policy, str):
            unknown.append(address)
            continue
        totals[role][0] += _policy_document_chars(json.loads(policy))
        totals[role][1] += 1
    assert not unknown, f"not rendered at plan time, so the aggregate would be short: {unknown}"
    return {role: (chars, count) for role, (chars, count) in totals.items()}


def _managed_documents(plan: Plan) -> dict[str, int]:
    """`{address: chars}` over every planned `aws_iam_policy`."""
    sizes: dict[str, int] = {}
    unknown: list[str] = []
    for address in plan.addresses_of_type("aws_iam_policy"):
        policy = plan.after(address).get("policy")
        if not isinstance(policy, str):
            unknown.append(address)
            continue
        sizes[address] = _policy_document_chars(json.loads(policy))
    assert not unknown, f"not rendered at plan time, so a managed size is unmeasured: {unknown}"
    return sizes


def _attachment_counts(plan: Plan) -> dict[str, int]:
    """`{role_name: n}` over every planned `aws_iam_role_policy_attachment`."""
    per_role: dict[str, int] = defaultdict(int)
    for address in plan.addresses_of_type("aws_iam_role_policy_attachment"):
        role = plan.after(address).get("role")
        assert isinstance(role, str), f"{address} has no plan-known role"
        per_role[role] += 1
    return dict(per_role)


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)
    work_dir = tmp_path_factory.mktemp("inline-policy-budget")
    return plan_root(work_dir, _build(work_dir))


@pytest.fixture(scope="module")
def aggregates(plan: Plan) -> dict[str, tuple[int, int]]:
    return _inline_aggregates(plan)


EVAL_TRIGGER_ROLE: Final[str] = "crm-agent-eval-eval-trigger-role"

RELAY_ROLE: Final[str] = "crm-agent-eval-agent-relay-role"


def test_the_harness_measures_every_vpc_lambda_role(
    plan: Plan, aggregates: dict[str, tuple[int, int]]
) -> None:
    """One role per `lambda_action` instantiation in any `infra/*.tf`, plus the relay."""
    expected = set(_lambda_action_roles(plan).values()) | {RELAY_ROLE}
    assert len(_lambda_action_roles(plan)) == len(_expected_lambda_action_modules())
    assert set(aggregates) == expected, sorted(set(aggregates) ^ expected)
    # The module's four, plus the root's `eval_trigger_states` and `eval_trigger_artifacts`.
    assert aggregates[EVAL_TRIGGER_ROLE][1] == 6


def test_every_role_stays_under_the_budget_fail_threshold(
    aggregates: dict[str, tuple[int, int]],
) -> None:
    limit = FAIL_USAGE_FRACTION * INLINE_POLICY_AGGREGATE_LIMIT_CHARS
    over = {
        role: f"{chars} chars ({chars / INLINE_POLICY_AGGREGATE_LIMIT_CHARS:.1%})"
        for role, (chars, _) in aggregates.items()
        if chars >= limit
    }
    assert not over, (
        f"at or above iam_policy_budget's {FAIL_USAGE_FRACTION:.0%} FAIL threshold: {over}. Move "
        "statements into a customer-managed policy; do not raise the threshold."
    )


def test_every_managed_policy_stays_under_the_budget_fail_threshold(plan: Plan) -> None:
    """Every customer-managed document attached to these roles, against the 6,144-char cap."""
    sizes = _managed_documents(plan)
    assert "module.eni_function_code_deny.aws_iam_policy.this" in sizes, sorted(sizes)
    limit = FAIL_USAGE_FRACTION * MANAGED_POLICY_DOCUMENT_LIMIT_CHARS
    over = {
        address: f"{chars} chars ({chars / MANAGED_POLICY_DOCUMENT_LIMIT_CHARS:.1%})"
        for address, chars in sizes.items()
        if chars >= limit
    }
    assert not over, (
        f"at or above iam_policy_budget's {FAIL_USAGE_FRACTION:.0%} FAIL threshold: {over}. "
        "CreatePolicy refuses a document over 6,144 chars; split it, do not raise the threshold."
    )


def test_no_role_reaches_the_managed_policy_attachment_quota(plan: Plan) -> None:
    """Moving statements out of inline policies spends this quota, so it is bounded here too."""
    per_role = _attachment_counts(plan)
    expected = set(_lambda_action_roles(plan).values()) | {RELAY_ROLE}
    assert set(per_role) == expected, sorted(set(per_role) ^ expected)
    assert per_role[EVAL_TRIGGER_ROLE] == 1, "the ENI deny"
    limit = FAIL_USAGE_FRACTION * MANAGED_POLICIES_PER_ROLE_QUOTA
    over = {role: n for role, n in per_role.items() if n >= limit}
    assert not over, over


# ---------------------------------------------------------------------------
# Explicit DynamoDB Denies on every lambda_action role
# ---------------------------------------------------------------------------

#: Policies on a `lambda_action` role allowed to be unknown at plan time; cell (b) skips them by
#: address. Any other unknown policy fails the cell.
PLAN_UNKNOWN_POLICY_EXCLUSIONS: Final[frozenset[str]] = frozenset()

#: `(role, sid)` of every `NotAction` statement on a `lambda_action` role. Each must be a Deny, so it
#: grants nothing and cell (b) skips it as non-granting.
NOT_ACTION_DENIES: Final[frozenset[tuple[str, str]]] = frozenset(
    {("crm-agent-eval-chat-target-role", "DenyEverythingButInvokeOnThisRuntime")}
)

CAPABILITIES_MODULE: Final[str] = "module.lambda_action_capabilities"
CAPABILITIES_ROLE: Final[str] = "crm-agent-eval-capabilities-role"


def _statements(document: str) -> list[dict[str, Any]]:
    statements = json.loads(document)["Statement"]
    return [statements] if isinstance(statements, dict) else list(statements)


def _as_list(value: Any) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def _expected_lambda_action_modules() -> set[str]:
    """Every `./modules/lambda_action` instance address declared in a tracked `infra/*.tf`.

    Read from `git ls-files`, independently of the files `_build` plans. The `for_each =
    local.tools` block expands to one address per `local.tools` key; any other `for_each` or
    `count` on a `lambda_action` block is refused rather than guessed.
    """
    tracked = proc_run.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", "infra/*.tf"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    paths = [
        REPO_ROOT / name for name in tracked if name and "/" not in name.removeprefix("infra/")
    ]
    assert INFRA / "main.tf" in paths, paths
    main_tf = (INFRA / "main.tf").read_text(encoding="utf-8")
    addresses: set[str] = set()
    for path in paths:
        for block in _top_level_blocks(path.read_text(encoding="utf-8")):
            if not block.startswith("module ") or _module_source(block) != "lambda_action":
                continue
            name = block.split('"')[1]
            repetition = re.findall(r"^  (for_each|count)\s*=\s*(.+)$", block, re.MULTILINE)
            if not repetition:
                addresses.add(f"module.{name}")
                continue
            assert repetition == [("for_each", "local.tools")], (path.name, name, repetition)
            addresses.update(f'module.{name}["{key}"]' for key in _tool_keys(main_tf))
    return addresses


def _lambda_action_roles(plan: Plan) -> dict[str, str]:
    """`{module address: role name}` for every planned instantiation of `./modules/lambda_action`."""
    names = {
        address.split("[", 1)[0].removeprefix("module.")
        for address in _expected_lambda_action_modules()
    }
    roles: dict[str, str] = {}
    for address in plan.addresses_of_type("aws_iam_role"):
        module = address.removesuffix(".aws_iam_role.this")
        if module != address and module.split("[", 1)[0].removeprefix("module.") in names:
            roles[module] = str(plan.after(address)["name"])
    return roles


def _role_policies(plan: Plan, module: str, role: str) -> dict[str, Any]:
    """`{address: policy}` for every planned inline policy on `role` and managed one under `module`."""
    policies = {
        address: plan.after(address).get("policy")
        for address in plan.addresses_of_type("aws_iam_role_policy")
        if plan.after(address).get("role") == role
    }
    policies.update(
        {
            address: plan.after(address).get("policy")
            for address in plan.addresses_of_type("aws_iam_policy")
            if address.startswith(f"{module}.")
        }
    )
    return policies


def _role_statements(
    plan: Plan, module: str, role: str
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    """`role`'s `Action` statements, and the `(role, sid)` of the `NotAction` Denies set aside.

    A policy unknown at plan time is skipped only when named in `PLAN_UNKNOWN_POLICY_EXCLUSIONS`;
    a `NotAction` statement only when it is a Deny. Anything else fails here, naming the address.
    """
    statements: list[dict[str, Any]] = []
    not_action: set[tuple[str, str]] = set()
    for address, policy in sorted(_role_policies(plan, module, role).items()):
        if not isinstance(policy, str):
            assert address in PLAN_UNKNOWN_POLICY_EXCLUSIONS, (
                f"{address} on {role} is unknown at plan time and not a named exclusion"
            )
            continue
        for statement in _statements(policy):
            if "NotAction" in statement:
                assert statement["Effect"] == "Deny", (
                    f"{address} on {role}: NotAction {statement['Effect']} "
                    f"{statement.get('Sid')!r} grants every action it does not name"
                )
                not_action.add((role, str(statement.get("Sid"))))
                continue
            assert "Action" in statement, (
                f"{address} on {role}: statement with no Action {statement}"
            )
            statements.append(statement)
    return statements, not_action


def _role_dynamodb_statements(plan: Plan, module: str, role: str) -> list[dict[str, Any]]:
    """`role`'s planned inline and `module`'s managed statements that name a `dynamodb:` action."""
    statements, _ = _role_statements(plan, module, role)
    return [s for s in statements if any(a.startswith("dynamodb:") for a in _as_list(s["Action"]))]


def _covers(statement: dict[str, Any], action: str, resource: str) -> bool:
    return any(fnmatch.fnmatchcase(action, a) for a in _as_list(statement["Action"])) and any(
        fnmatch.fnmatchcase(resource, r) for r in _as_list(statement["Resource"])
    )


def _table_resources(table: str) -> list[str]:
    """A table's ARN, its `/index/*` and each of its GSI ARNs."""
    outputs = _dynamodb_outputs()
    arn = outputs["table_arns"][table]
    return [arn, f"{arn}/index/*", *outputs["gsi_arns"][table].values()]


def _uncovered_pairs(statements: list[dict[str, Any]], asserted: set[str]) -> list[str]:
    """`table action resource` for each non-granted (table, asserted action) no Deny covers.

    An action is granted on a table when an Allow names it on the table or any of its indexes; a
    covering Deny is unconditioned and names the action on the table ARN, on `/index/*` and on each
    GSI ARN.
    """
    allows = [s for s in statements if s["Effect"] == "Allow"]
    denies = [s for s in statements if s["Effect"] == "Deny" and not s.get("Condition")]
    uncovered: list[str] = []
    for table in sorted(_dynamodb_outputs()["table_arns"]):
        resources = _table_resources(table)
        for action in sorted(asserted):
            if any(_covers(s, action, r) for s in allows for r in resources):
                continue
            uncovered += [
                f"{table} {action} {r}"
                for r in resources
                if not any(_covers(s, action, r) for s in denies)
            ]
    return uncovered


def test_capabilities_has_one_deny_on_every_table_and_no_allow(plan: Plan) -> None:
    """(a) The capabilities role's only DynamoDB policy is `ExplicitDenyCrossFunctionIsolation`."""
    after = plan.after(f"{CAPABILITIES_MODULE}.aws_iam_role_policy.dynamodb[0]")
    assert after["role"] == CAPABILITIES_ROLE
    [statement] = _statements(after["policy"])
    assert statement["Sid"] == "ExplicitDenyCrossFunctionIsolation"
    assert statement["Effect"] == "Deny"
    assert _as_list(statement["Action"]) == ["dynamodb:*"]
    assert "Condition" not in statement
    expected = {
        resource
        for arn in _dynamodb_outputs()["table_arns"].values()
        for resource in (arn, f"{arn}/index/*")
    }
    assert len(expected) == 22
    assert set(_as_list(statement["Resource"])) == expected
    assert len(_as_list(statement["Resource"])) == len(expected)
    on_role = _role_dynamodb_statements(plan, CAPABILITIES_MODULE, CAPABILITIES_ROLE)
    assert [s for s in on_role if s["Effect"] == "Allow"] == []


def test_the_harness_plans_every_lambda_action_instantiation(plan: Plan) -> None:
    """The planned `lambda_action` roles are exactly the instances `infra/*.tf` declares."""
    expected = _expected_lambda_action_modules()
    assert len(expected) > len(ALL_TOOLS), sorted(expected)
    planned = set(_lambda_action_roles(plan))
    assert planned == expected, {
        "unplanned": sorted(expected - planned),
        "extra": sorted(planned - expected),
    }


def test_the_only_not_action_statements_are_the_named_denies(plan: Plan) -> None:
    """Every `NotAction` statement cell (b) sets aside is a named Deny, so none hides a grant."""
    seen: set[tuple[str, str]] = set()
    for module, role in _lambda_action_roles(plan).items():
        seen |= _role_statements(plan, module, role)[1]
    assert seen == NOT_ACTION_DENIES, sorted(seen ^ NOT_ACTION_DENIES)


def test_no_lambda_action_policy_is_unknown_at_plan_time(plan: Plan) -> None:
    """Each named exclusion is really unknown, and every other policy, `sweep_control_artifacts`
    included, renders, so cell (b) reads it."""
    unknown = {
        address
        for module, role in _lambda_action_roles(plan).items()
        for address, policy in _role_policies(plan, module, role).items()
        if not isinstance(policy, str)
    }
    assert unknown == PLAN_UNKNOWN_POLICY_EXCLUSIONS, sorted(
        unknown ^ PLAN_UNKNOWN_POLICY_EXCLUSIONS
    )
    rendered = plan.after("aws_iam_role_policy.sweep_control_artifacts")
    assert rendered["role"] == "crm-agent-eval-sweep-control-role"
    assert isinstance(rendered["policy"], str)


def test_every_lambda_action_role_denies_each_non_granted_table_action(plan: Plan) -> None:
    """(b) Completeness: on every `lambda_action` role in `infra/*.tf`, every non-granted
    (table, asserted action) pair hits an explicit Deny."""
    roles = _lambda_action_roles(plan)
    assert CAPABILITIES_MODULE in roles
    assert set(roles) == _expected_lambda_action_modules(), sorted(roles)
    asserted = set(asserted_dynamodb_actions())
    assert asserted and all(a.startswith("dynamodb:") for a in asserted), asserted
    gaps = {
        role: uncovered
        for module, role in sorted(roles.items())
        if (uncovered := _uncovered_pairs(_role_dynamodb_statements(plan, module, role), asserted))
    }
    assert gaps == {}, gaps


def test_capabilities_inline_aggregate_stays_under_the_fail_threshold(
    aggregates: dict[str, tuple[int, int]],
) -> None:
    """(c) The new Deny keeps the capabilities role well inside the inline budget."""
    chars, count = aggregates[CAPABILITIES_ROLE]
    assert count == 3, "logs, vpc-access and dynamodb"
    assert chars < FAIL_USAGE_FRACTION * INLINE_POLICY_AGGREGATE_LIMIT_CHARS, chars

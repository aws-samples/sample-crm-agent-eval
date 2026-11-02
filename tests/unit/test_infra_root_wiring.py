# SPDX-License-Identifier: MIT
"""Root composition wiring: the cross-file contracts in `infra/main.tf`.

Policy documents and guards are planned from their committed text, with every outside reference
replaced by a known literal (`_terraform_fixture`, mock credentials); the cognito module is planned
whole. Wiring the plans cannot see (env, concurrency, route and scope references) is read from source.
Skips when terraform or the cached providers are absent, unless `CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
from scripts.hcl_text import blank_comments, blank_heredoc_bodies

from tests.unit._terraform_fixture import (
    REPO_ROOT,
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    TerraformError,
    plan_module,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)

INFRA: Final = REPO_ROOT / "infra"
MAIN_TF: Final = INFRA / "main.tf"
COGNITO_MAIN: Final = INFRA / "modules" / "cognito" / "main.tf"

ACCOUNT_ID: Final = "123456789012"
RUNTIME_ARN: Final = (
    f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT_ID}:runtime/fixture_rt-AbCdEf0123"
)
ENDPOINT_ARN: Final = f"{RUNTIME_ARN}/runtime-endpoint/DEFAULT"
VPCE_ID: Final = "-".join(("vpce", "0a1b2c3d4e5f60718"))
INVOKER_ROLES: Final = (
    f"arn:aws:iam::{ACCOUNT_ID}:role/crm-agent-eval-agent-relay-role",
    f"arn:aws:iam::{ACCOUNT_ID}:role/crm-agent-eval-sweep-task-role",
    f"arn:aws:iam::{ACCOUNT_ID}:role/crm-agent-eval-chat-target-role",
)
STATE_MACHINE_ARN: Final = (
    f"arn:aws:states:us-east-1:{ACCOUNT_ID}:stateMachine:crm-agent-eval-eval-sweep"
)
EXECUTION_PATTERN: Final = (
    f"arn:aws:states:us-east-1:{ACCOUNT_ID}:execution:crm-agent-eval-eval-sweep:*"
)


def _skip_unless_terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)


def _code(path: Path) -> str:
    return blank_comments(blank_heredoc_bodies(path.read_text(encoding="utf-8")))


def _block(text: str, header: str) -> str:
    """The one column-0 block whose first line starts with `header`, brace-matched."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith(header)]
    assert len(starts) == 1, f"expected one {header!r} block, got {len(starts)}"
    depth = 0
    for offset, line in enumerate(lines[starts[0] :], start=starts[0]):
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return "".join(lines[starts[0] : offset + 1])
    raise AssertionError(f"unterminated {header!r}")


def _map_body(text: str, name: str) -> str:
    match = re.search(rf"^\s*{name}\s*=\s*\{{([^}}]*)\}}", text, re.MULTILINE)
    assert match is not None, f"no `{name} = {{...}}` map"
    return match.group(1)


def _substitute(text: str, substitutions: dict[str, str]) -> str:
    for old, new in substitutions.items():
        assert old in text, f"{old!r} no longer appears in the extracted source; update the fixture"
        text = text.replace(old, new)
    leftovers = sorted(
        set(
            re.findall(
                r"\b(?:module|local|var|data\.aws_caller_identity)\.[a-z_]+", blank_comments(text)
            )
        )
    )
    assert leftovers == [], f"unsubstituted references: {leftovers}"
    return text


def _statements(policy_json: str) -> list[dict[str, Any]]:
    statements = json.loads(policy_json)["Statement"]
    return statements if isinstance(statements, list) else [statements]


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else [value]


# ---------------------------------------------------------------------------
# 1. The Runtime resource policies admit exactly the three invoker roles.
# ---------------------------------------------------------------------------


def _plan_runtime_policies(work_dir: Path, code: str) -> Plan:
    """Plan both Runtime policy documents from `code` (a `main.tf` text), outside refs as literals."""
    documents = "\n".join(
        _block(code, f'data "aws_iam_policy_document" "{name}"')
        for name in ("agent_runtime_relay_only", "agent_runtime_endpoint_relay_only")
    )
    roles = "[" + ", ".join(f'"{r}"' for r in INVOKER_ROLES) + "]"
    root = _substitute(
        documents,
        {
            "module.agentcore.agent_runtime_arn": f'"{RUNTIME_ARN}"',
            "local.agent_runtime_endpoint_arn": f'"{ENDPOINT_ARN}"',
            "local.runtime_invoker_role_arns": roles,
            "local.runtime_source_vpce_id": f'"{VPCE_ID}"',
            "var.runtime_source_vpce_report_only_enabled": "true",
            "var.runtime_source_vpce_deny_enabled": "true",
        },
    )
    # A plan needs managed resources; each document is rendered into one.
    root += "".join(
        f'\nresource "terraform_data" "{name}" {{\n'
        f"  input = data.aws_iam_policy_document.{document}.json\n}}\n"
        for name, document in RUNTIME_DOCUMENTS.items()
    )
    return plan_root(work_dir, root)


@pytest.fixture(scope="module")
def runtime_policy_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return _plan_runtime_policies(
        tmp_path_factory.mktemp("runtime-policies"), MAIN_TF.read_text(encoding="utf-8")
    )


#: Each Runtime policy document and the one resource ARN it is attached to.
RUNTIME_DOCUMENTS: Final = {
    "runtime": "agent_runtime_relay_only",
    "endpoint": "agent_runtime_endpoint_relay_only",
}
RUNTIME_RESOURCES: Final = {"runtime": RUNTIME_ARN, "endpoint": ENDPOINT_ARN}


@pytest.mark.parametrize("document", sorted(RUNTIME_DOCUMENTS))
def test_runtime_policy_allows_exactly_the_three_invoker_roles(
    runtime_policy_plan: Plan, document: str
) -> None:
    """Both documents: an Allow and its `aws:SourceVpce` twin, each for exactly the three roles."""
    statements = _statements(runtime_policy_plan.after(f"terraform_data.{document}")["input"])
    allows = [s for s in statements if s["Effect"] == "Allow"]
    assert len(allows) == 2, "the unconditioned Allow and its aws:SourceVpce twin"
    for statement in allows:
        assert sorted(_as_list(statement["Principal"]["AWS"])) == sorted(INVOKER_ROLES)
        assert _as_list(statement["Action"]) == ["bedrock-agentcore:InvokeAgentRuntime"]
        assert _as_list(statement["Resource"]) == [RUNTIME_RESOURCES[document]]
    twin = [s for s in allows if "Condition" in s]
    assert len(twin) == 1
    assert twin[0]["Condition"]["StringEquals"]["aws:SourceVpce"] == VPCE_ID
    (deny,) = [s for s in statements if s.get("Sid") == "DenyInvokeUnlessViaOurVpcEndpoint"]
    assert deny["Principal"] == {"AWS": "*"}
    assert deny["Condition"]["StringNotEquals"]["aws:SourceVpce"] == VPCE_ID


#: AgentCore's documented Runtime data-plane actions (resource-based-policies.html, 2026-10-20).
RUNTIME_DATA_PLANE_ACTIONS: Final = (
    "bedrock-agentcore:GetAgentCard",
    "bedrock-agentcore:InvokeAgentRuntime",
    "bedrock-agentcore:InvokeAgentRuntimeCommand",
    "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
    "bedrock-agentcore:InvokeAgentRuntimeForUser",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStreamForUser",
    "bedrock-agentcore:StopRuntimeSession",
)
PRINCIPAL_DENY_SID: Final = "DenyRuntimeDataPlaneToAnyOtherPrincipal"


def _assert_principal_deny(statements: list[dict[str, Any]], document: str) -> None:
    """The document Denies every data-plane action to `*` unless the caller is an invoker role."""
    denies = [s for s in statements if s.get("Sid") == PRINCIPAL_DENY_SID]
    assert len(denies) == 1, f"{document}: {len(denies)} {PRINCIPAL_DENY_SID} statements"
    (deny,) = denies
    assert deny["Effect"] == "Deny"
    assert deny["Principal"] == {"AWS": "*"}
    assert sorted(_as_list(deny["Action"])) == list(RUNTIME_DATA_PLANE_ACTIONS)
    assert _as_list(deny["Resource"]) == [RUNTIME_RESOURCES[document]]
    condition = deny["Condition"]
    assert list(condition) == ["ArnNotEquals"], condition
    assert list(condition["ArnNotEquals"]) == ["aws:PrincipalArn"], condition
    excluded = _as_list(condition["ArnNotEquals"]["aws:PrincipalArn"])
    assert sorted(excluded) == sorted(INVOKER_ROLES), f"{document}: excluded {excluded}"


@pytest.mark.parametrize("document", sorted(RUNTIME_DOCUMENTS))
def test_runtime_policy_denies_every_other_principal(
    runtime_policy_plan: Plan, document: str
) -> None:
    """Both documents Deny the data plane to every principal outside the three invokers."""
    statements = _statements(runtime_policy_plan.after(f"terraform_data.{document}")["input"])
    _assert_principal_deny(statements, document)


def test_no_runtime_policy_denies_a_control_plane_action(runtime_policy_plan: Plan) -> None:
    """The Denies name only data-plane actions; the deployer's control-plane calls are untouched."""
    for document in RUNTIME_DOCUMENTS:
        statements = _statements(runtime_policy_plan.after(f"terraform_data.{document}")["input"])
        for statement in statements:
            if statement["Effect"] != "Deny":
                continue
            for action in _as_list(statement["Action"]):
                assert action in RUNTIME_DATA_PLANE_ACTIONS, (document, action)


def test_dropping_an_invoker_from_the_deny_exclusion_reds_the_deny_check(tmp_path: Path) -> None:
    """Negative control: a Deny excluding only two invokers is caught by `_assert_principal_deny`."""
    _skip_unless_terraform()
    code = MAIN_TF.read_text(encoding="utf-8")
    exclusion = (
        '      variable = "aws:PrincipalArn"\n      values   = local.runtime_invoker_role_arns'
    )
    assert code.count(exclusion) == 2, "the Deny's exclusion moved; update this control"
    narrowed = code.replace(
        exclusion,
        exclusion.replace(
            "local.runtime_invoker_role_arns",
            "[" + ", ".join(f'"{role}"' for role in INVOKER_ROLES[:2]) + "]",
        ),
    )
    plan = _plan_runtime_policies(tmp_path / "narrowed", narrowed)
    for document in RUNTIME_DOCUMENTS:
        statements = _statements(plan.after(f"terraform_data.{document}")["input"])
        with pytest.raises(AssertionError, match="excluded"):
            _assert_principal_deny(statements, document)


def test_every_runtime_policy_principal_block_reads_the_invoker_list() -> None:
    """Both documents name principals only through `local.runtime_invoker_role_arns`."""
    code = _code(MAIN_TF)
    for name in ("agent_runtime_relay_only", "agent_runtime_endpoint_relay_only"):
        document = _block(code, f'data "aws_iam_policy_document" "{name}"')
        identifiers = re.findall(r"identifiers\s*=\s*(.+)", document)
        allow_identifiers = [i.strip() for i in identifiers if i.strip() != '["*"]']
        assert len(allow_identifiers) == 2, (name, identifiers)
        assert set(allow_identifiers) == {"local.runtime_invoker_role_arns"}, (name, identifiers)


def test_the_invoker_list_is_exactly_relay_sweep_task_and_chat_target() -> None:
    match = re.search(r"runtime_invoker_role_arns\s*=\s*\[([^\]]*)\]", _code(MAIN_TF))
    assert match is not None
    entries = [e.strip() for e in match.group(1).split(",") if e.strip()]
    assert entries == [
        "module.agent_relay.role_arn",
        "module.lambda_action_sweep_task.role_arn",
        "module.lambda_action_chat_target.role_arn",
    ]


# ---------------------------------------------------------------------------
# 2. eval_trigger starts and reads the eval sweep, and never stops it.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def states_policy_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    document = _block(
        MAIN_TF.read_text("utf-8"), 'data "aws_iam_policy_document" "eval_trigger_states"'
    )
    root = _substitute(
        document,
        {
            "local.eval_sweep_state_machine_arn": f'"{STATE_MACHINE_ARN}"',
            "local.eval_sweep_execution_arn_pattern": f'"{EXECUTION_PATTERN}"',
        },
    )
    root += '\nresource "terraform_data" "anchor" {\n  input = data.aws_iam_policy_document.eval_trigger_states.json\n}\n'
    return plan_root(tmp_path_factory.mktemp("eval-trigger-states"), root)


def test_eval_trigger_may_start_and_describe_only(states_policy_plan: Plan) -> None:
    statements = _statements(states_policy_plan.after("terraform_data.anchor")["input"])
    granted = {
        (a, r) for s in statements for a in _as_list(s["Action"]) for r in _as_list(s["Resource"])
    }
    assert all(s["Effect"] == "Allow" for s in statements)
    assert granted == {
        ("states:StartExecution", STATE_MACHINE_ARN),
        ("states:DescribeExecution", EXECUTION_PATTERN),
    }


def test_the_states_policy_is_attached_to_eval_trigger_and_names_no_stop() -> None:
    code = _code(MAIN_TF)
    attachment = _block(code, 'resource "aws_iam_role_policy" "eval_trigger_states"')
    assert "module.lambda_action_eval_trigger.role_name" in attachment
    assert "data.aws_iam_policy_document.eval_trigger_states.json" in attachment
    assert "StopExecution" not in code
    assert '"states:*"' not in code


def test_eval_trigger_env_names_the_state_machine() -> None:
    module = _block(_code(MAIN_TF), 'module "lambda_action_eval_trigger"')
    assert re.search(r"EVAL_STATE_MACHINE_ARN\s*=\s*local\.eval_sweep_state_machine_arn", module)


def test_the_constructed_state_machine_arn_names_the_real_machine() -> None:
    """The env's constructed ARN and the machine share one name local."""
    sfn = _code(INFRA / "eval_sfn.tf")
    machine = _block(sfn, 'resource "aws_sfn_state_machine" "eval_sweep"')
    assert re.search(
        r"^\s*name\s*=\s*local\.eval_sweep_state_machine_name\s*$", machine, re.MULTILINE
    )
    arn = re.search(r"eval_sweep_state_machine_arn\s*=\s*\"([^\"]+)\"", sfn)
    assert arn is not None
    assert arn.group(1).endswith(":stateMachine:${local.eval_sweep_state_machine_name}")


def test_eval_trigger_eval_runs_grants_are_key_scoped() -> None:
    """Base-table rows only under `job#*`/`lock#sweep`/`livequota#*`; `gsi_status` under `job`."""
    code = _code(MAIN_TF)
    grants = re.search(r"eval_trigger_eval_runs_grants\s*=\s*\[(.*?)\n  \]", code, re.DOTALL)
    assert grants is not None
    body = grants.group(1)
    assert re.findall(r"values\s*=\s*\[([^\]]*)\]", body) == [
        '"job#*", "lock#sweep", "livequota#*"',
        '"lock#sweep"',
        '"job"',
    ]
    assert '"dynamodb:Scan"' not in body
    assert 'gsi_arns["eval_runs"]["gsi_status"]' in body
    module = _block(code, 'module "lambda_action_eval_trigger"')
    assert re.search(r"dynamodb_grants\s*=\s*local\.eval_trigger_eval_runs_grants", module)


EVAL_RUNS_TABLE_ARN: Final = (
    f"arn:aws:dynamodb:us-east-1:{ACCOUNT_ID}:table/crm-agent-eval-eval_runs"
)
EVAL_RUNS_STATUS_INDEX_ARN: Final = f"{EVAL_RUNS_TABLE_ARN}/index/gsi_status"


@pytest.fixture(scope="module")
def eval_trigger_dynamodb_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    """`lambda_action` planned with `local.eval_trigger_eval_runs_grants`' committed text."""
    _skip_unless_terraform()
    code = MAIN_TF.read_text(encoding="utf-8")
    grants = re.search(r"eval_trigger_eval_runs_grants\s*=\s*(\[.*?\n  \])", code, re.DOTALL)
    assert grants is not None
    grants_hcl = _substitute(
        grants.group(1),
        {
            'module.dynamodb.table_arns["eval_runs"]': f'"{EVAL_RUNS_TABLE_ARN}"',
            'module.dynamodb.gsi_arns["eval_runs"]["gsi_status"]': f'"{EVAL_RUNS_STATUS_INDEX_ARN}"',
        },
    )
    work_dir = tmp_path_factory.mktemp("eval-trigger-dynamodb")
    function_zip = work_dir / "function.zip"
    function_zip.write_bytes(b"")
    arguments = {
        "function_name": '"crm-agent-eval-eval-trigger"',
        "handler": '"crm.handlers.eval_trigger.handler"',
        "filename": f'"{function_zip}"',
        "source_code_hash": '"0000000000000000000000000000000000000000000="',
        "reserved_concurrent_executions": "5",
        "dynamodb_grants": grants_hcl,
        "vpc_subnet_ids": '["subnet-fixturezonea", "subnet-fixturezoneb"]',
        "vpc_security_group_ids": '["sg-fixturelambda"]',
        "eni_function_code_deny_policy_arn": f'"arn:aws:iam::{ACCOUNT_ID}:policy/fixture-eni-deny"',
    }
    body = "\n".join(f"  {key} = {value}" for key, value in arguments.items())
    return plan_module(work_dir, "lambda_action", body)


def test_eval_trigger_may_delete_only_the_sweep_lock(eval_trigger_dynamodb_plan: Plan) -> None:
    """The one statement granting `DeleteItem` is `StringEquals` `lock#sweep` alone."""
    policy = eval_trigger_dynamodb_plan.after("module.subject.aws_iam_role_policy.dynamodb[0]")[
        "policy"
    ]
    deleting = [
        s
        for s in _statements(policy)
        if s["Effect"] == "Allow" and "dynamodb:DeleteItem" in _as_list(s["Action"])
    ]
    assert len(deleting) == 1, deleting
    (statement,) = deleting
    assert _as_list(statement["Action"]) == ["dynamodb:DeleteItem"]
    assert _as_list(statement["Resource"]) == [EVAL_RUNS_TABLE_ARN]
    condition = statement["Condition"]
    assert list(condition) == ["ForAllValues:StringEquals"], condition
    assert _as_list(condition["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"]) == [
        "lock#sweep"
    ]
    keyed_rows = [
        s
        for s in _statements(policy)
        if s["Effect"] == "Allow" and "dynamodb:PutItem" in _as_list(s["Action"])
    ]
    assert len(keyed_rows) == 1
    assert keyed_rows[0]["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"] == [
        "job#*",
        "lock#sweep",
        "livequota#*",
    ]


# ---------------------------------------------------------------------------
# 3. Cognito: Gateway #2's own scope, the admin group, and the SPA unchanged.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cognito_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    arguments = f"""  pre_token_generation_lambda_arn           = "arn:aws:lambda:us-east-1:{ACCOUNT_ID}:function:fixture-pre-token"
  pre_token_generation_lambda_function_name = "fixture-pre-token"
  spa_callback_urls = ["https://fixture.example.com/callback"]
  spa_logout_urls   = ["https://fixture.example.com/logout"]
"""
    return plan_module(tmp_path_factory.mktemp("cognito"), "cognito", arguments)


def _scope_servers(plan: Plan) -> dict[str, list[str]]:
    servers: dict[str, list[str]] = {}
    for address in plan.addresses_of_type("aws_cognito_resource_server"):
        after = plan.after(address)
        servers[after["identifier"]] = sorted(s["scope_name"] for s in after["scope"])
    return servers


def test_two_resource_servers_each_with_one_scope(cognito_plan: Plan) -> None:
    assert _scope_servers(cognito_plan) == {"gateway": ["invoke"], "supervisor": ["chat"]}


def test_the_admin_group_exists(cognito_plan: Plan) -> None:
    (address,) = cognito_plan.addresses_of_type("aws_cognito_user_group")
    assert cognito_plan.after(address)["name"] == "admin"


def test_the_spa_client_is_never_granted_a_gateway_scope(cognito_plan: Plan) -> None:
    """User tokens get the chat scope only through the pre-token trigger, never by client."""
    spa = cognito_plan.after("module.subject.aws_cognito_user_pool_client.spa")
    assert sorted(spa["allowed_oauth_scopes"]) == ["email", "openid", "profile"]


def test_the_mcp_client_is_granted_both_scopes() -> None:
    client = _block(_code(COGNITO_MAIN), 'resource "aws_cognito_user_pool_client" "mcp"')
    scopes = re.search(r"allowed_oauth_scopes\s*=\s*\[\n(.*?)\n\s*\]", client, re.DOTALL)
    assert scopes is not None
    entries = {e.strip() for e in scopes.group(1).split(",") if e.strip()}
    assert entries == {
        "aws_cognito_resource_server.gateway.scope_identifiers[0]",
        "aws_cognito_resource_server.supervisor.scope_identifiers[0]",
    }


def test_the_root_chat_scope_literal_is_the_supervisor_scope() -> None:
    """`local.chat_scope_identifier` is `<identifier>/<scope_name>` of the supervisor server."""
    server = _block(_code(COGNITO_MAIN), 'resource "aws_cognito_resource_server" "supervisor"')
    identifier = re.search(r'identifier\s*=\s*"([^"]+)"', server)
    scope = re.search(r'scope_name\s*=\s*"([^"]+)"', server)
    assert identifier is not None and scope is not None
    local = re.search(r'chat_scope_identifier\s*=\s*"([^"]+)"', _code(MAIN_TF))
    assert local is not None
    assert local.group(1) == f"{identifier.group(1)}/{scope.group(1)}"
    guard = _block(_code(MAIN_TF), 'resource "terraform_data" "chat_scope_agreement"')
    assert "module.cognito.chat_scope_identifier == local.chat_scope_identifier" in guard


def test_chat_scope_reaches_the_trigger_and_not_gateway_1s_interceptor() -> None:
    """The trigger issues the chat scope; Gateway #1's interceptor keeps `gateway/invoke` only."""
    code = _code(MAIN_TF)
    trigger = _block(code, 'module "lambda_action_pre_token_generation"')
    assert re.search(r"CHAT_SCOPE\s*=\s*local\.chat_scope_identifier", trigger)
    interceptor = _block(code, 'module "lambda_action_interceptor"')
    assert "CHAT_SCOPE" not in interceptor
    assert re.search(
        r"COGNITO_ALLOWED_SCOPES\s*=\s*module\.cognito\.gateway_scope_identifier", interceptor
    )


def test_the_pool_supports_access_token_customisation(cognito_plan: Plan) -> None:
    """`scopesToAdd` needs the V2_0 trigger on an Essentials or Plus pool."""
    pool = cognito_plan.after("module.subject.aws_cognito_user_pool.this")
    assert pool["user_pool_tier"] in ("ESSENTIALS", "PLUS")
    (lambda_config,) = pool["lambda_config"]
    (pre_token,) = lambda_config["pre_token_generation_config"]
    assert pre_token["lambda_version"] == "V2_0"


# ---------------------------------------------------------------------------
# 4. Concurrency, endpoints, routes and the agent-only guard.
# ---------------------------------------------------------------------------


def test_new_functions_claim_concurrency_from_the_counted_map() -> None:
    """Every new non-tool function's value is in `non_tool_reserved_concurrency`."""
    entries = dict(
        re.findall(
            r"^\s*([a-z_]+)\s*=\s*([0-9]+)\s*$",
            _map_body(_code(MAIN_TF), "non_tool_reserved_concurrency"),
            re.MULTILINE,
        )
    )
    assert {
        k: entries.get(k)
        for k in ("sweep_control", "sweep_task", "chat_target", "chat_interceptor", "user_settings")
    } == {
        "sweep_control": "2",
        "sweep_task": "1",
        "chat_target": "5",
        "chat_interceptor": "5",
        "user_settings": "5",
    }


def test_every_concurrency_key_has_exactly_one_consumer_and_back() -> None:
    """Both directions: no map key without a consumer, no consumer reading an absent key."""
    keys = set(
        re.findall(
            r"^\s*([a-z_]+)\s*=",
            _map_body(_code(MAIN_TF), "non_tool_reserved_concurrency"),
            re.MULTILINE,
        )
    )
    consumed: list[str] = []
    for path in sorted(INFRA.glob("*.tf")):
        consumed += re.findall(r'local\.reserved_concurrency\["([a-z_]+)"\]', _code(path))
    assert sorted(consumed) == sorted(set(consumed)), "a key is consumed twice"
    assert set(consumed) == keys


def test_the_states_interface_endpoint_is_declared() -> None:
    endpoints = re.search(
        r"vpc_interface_endpoint_services\s*=\s*\[(.*?)\]", _code(MAIN_TF), re.DOTALL
    )
    assert endpoints is not None
    assert '"states"' in endpoints.group(1)


def test_user_settings_is_an_allowlisted_extra_route() -> None:
    allowlist = re.search(
        r"setsubtract\(keys\(var\.extra_routes\),\s*\[([^\]]*)\]\)",
        _code(INFRA / "modules" / "api" / "variables.tf"),
    )
    assert allowlist is not None
    assert set(re.findall(r'"(\w+)"', allowlist.group(1))) == {
        "eval_trigger",
        "capabilities",
        "user_settings",
    }


def _guard_root(agent_only: list[str]) -> str:
    guard = _block(MAIN_TF.read_text("utf-8"), 'resource "terraform_data" "agent_only_tools_guard"')
    names = re.search(r"^  tool_names = .*$", MAIN_TF.read_text("utf-8"), re.MULTILINE)
    assert names is not None and "sort(keys(local.tools))" in names.group(0)
    listed = "[" + ", ".join(f'"{n}"' for n in agent_only) + "]"
    return (
        "locals {\n"
        '  tool_names       = ["get_account", "get_page_context"]\n'
        f"  agent_only_tools = {listed}\n"
        "}\n" + guard
    )


def test_the_agent_only_guard_admits_a_real_tool(tmp_path: Path) -> None:
    _skip_unless_terraform()
    plan = plan_root(tmp_path, _guard_root(["get_page_context"]))
    assert "terraform_data.agent_only_tools_guard" in plan.resource_changes


def test_the_agent_only_guard_refuses_a_misspelt_tool(tmp_path: Path) -> None:
    """A typo in `agent_only_tools` fails the plan instead of routing every tool."""
    _skip_unless_terraform()
    with pytest.raises(TerraformError, match="get_page_contxt"):
        plan_root(tmp_path, _guard_root(["get_page_contxt"]))


# ---------------------------------------------------------------------------
# 5. The `states` endpoint carries only the eval sweep's two calls.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def states_endpoint_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    document = _block(
        MAIN_TF.read_text("utf-8"), 'data "aws_iam_policy_document" "states_endpoint"'
    )
    root = _substitute(
        document,
        {
            "local.eval_sweep_state_machine_arn": f'"{STATE_MACHINE_ARN}"',
            "local.eval_sweep_execution_arn_pattern": f'"{EXECUTION_PATTERN}"',
            "data.aws_caller_identity.current.account_id": f'"{ACCOUNT_ID}"',
        },
    )
    root += (
        '\nresource "terraform_data" "anchor" {\n'
        "  input = data.aws_iam_policy_document.states_endpoint.json\n}\n"
    )
    return plan_root(tmp_path_factory.mktemp("states-endpoint"), root)


def test_the_states_endpoint_admits_only_this_accounts_sweep_calls(
    states_endpoint_plan: Plan,
) -> None:
    statements = _statements(states_endpoint_plan.after("terraform_data.anchor")["input"])
    granted = {
        (a, r) for s in statements for a in _as_list(s["Action"]) for r in _as_list(s["Resource"])
    }
    assert granted == {
        ("states:StartExecution", STATE_MACHINE_ARN),
        ("states:DescribeExecution", EXECUTION_PATTERN),
    }
    for statement in statements:
        assert statement["Effect"] == "Allow"
        assert statement["Condition"] == {"StringEquals": {"aws:PrincipalAccount": ACCOUNT_ID}}


def test_the_endpoint_policy_attaches_to_the_states_endpoint() -> None:
    resource = _block(_code(MAIN_TF), 'resource "aws_vpc_endpoint_policy" "states"')
    assert re.search(r"vpc_endpoint_id\s*=\s*module\.vpc\.states_vpc_endpoint_id", resource)
    assert "data.aws_iam_policy_document.states_endpoint.json" in resource
    output = _block(
        _code(INFRA / "modules" / "vpc" / "outputs.tf"), 'output "states_vpc_endpoint_id"'
    )
    assert 'aws_vpc_endpoint.interface["states"].id' in output
    assert 'contains(var.interface_endpoint_service_names, "states")' in output


# ---------------------------------------------------------------------------
# Principal lists: the logs-key users, the operators and the administrators.
# ---------------------------------------------------------------------------

ROOT_ADMINISTRATORS: Final = (
    r"concat\(var\.crm_operator_principal_arns,\s*"
    r'\["arn:aws:iam::\$\{data\.aws_caller_identity\.current\.account_id\}:root"\]\)'
)
SESSION_CALLER: Final = (
    r"caller_principal_arn\s*=\s*data\.aws_iam_session_context\.current\.issuer_arn"
)
LOGS_KEY_REFERENCE: Final = (
    r"environment_kms_key_arn\s*=\s*module\.observability\.cloudwatch_logs_kms_key_arn"
)


def _root_module_bodies() -> dict[str, str]:
    """Every root `module` call's body, by call name, across `infra/*.tf`."""
    bodies: dict[str, str] = {}
    for path in sorted(INFRA.glob("*.tf")):
        text = _code(path)
        for match in re.finditer(r'^module "([a-z_]+)" \{', text, re.MULTILINE):
            bodies[match.group(1)] = _block(text[match.start() :], f'module "{match.group(1)}"')
    return bodies


def _logs_key_local(main_tf: str) -> tuple[list[str], list[str]]:
    """(static `module.lambda_action[...]` keys, other module names) in the logs-key local."""
    match = re.search(r"logs_key_user_role_arns\s*=\s*concat\((.*?)\n  \)", main_tf, re.DOTALL)
    assert match is not None, "no `logs_key_user_role_arns = concat(...)` local"
    body = match.group(1)
    keys = re.findall(r'\bmodule\.lambda_action\["([a-z_]+)"\]\.role_arn\b', body)
    named = re.findall(r"\bmodule\.([a-z_]+)\.role_arn\b", body)
    leftover = re.sub(
        r'module\.lambda_action\["[a-z_]+"\]\.role_arn|module\.[a-z_]+\.role_arn', "", body
    )
    assert re.fullmatch(r"[\s\[\],]*", leftover), f"unexpected entries in the local: {leftover!r}"
    return keys, named


def _tool_keys(main_tf: str) -> list[str]:
    match = re.search(r"\n  tools = \{(.*?)\n  \}", main_tf, re.DOTALL)
    assert match is not None, "no `local.tools` map"
    return re.findall(r"^\s{4}([a-z_]+)\s*=", match.group(1), re.MULTILINE)


def _logs_key_callers(bodies: dict[str, str]) -> set[str]:
    return {name for name, body in bodies.items() if re.search(LOGS_KEY_REFERENCE, body)}


def test_logs_key_local_lists_every_tool_once() -> None:
    """The static tool keys equal `local.tools` and the toolspec's `ALL_TOOLS`, with no repeat."""
    from crm.toolspec.definitions import ALL_TOOLS

    main_tf = _code(MAIN_TF)
    keys, _ = _logs_key_local(main_tf)
    assert len(keys) == len(set(keys)), sorted(k for k in keys if keys.count(k) > 1)
    assert set(keys) == set(_tool_keys(main_tf)) == {tool.name for tool in ALL_TOOLS}


def test_logs_key_local_names_exactly_the_other_logs_key_callers() -> None:
    """Two-sided: every other module call that encrypts its environment with the logs CMK is
    listed by name, and nothing else is."""
    bodies = _root_module_bodies()
    callers = _logs_key_callers(bodies)
    assert "lambda_action" in callers
    _, named = _logs_key_local(_code(MAIN_TF))
    assert len(named) == len(set(named))
    assert set(named) == callers - {"lambda_action"}
    assert len(named) == 10, sorted(named)


@pytest.mark.parametrize(
    "mutation",
    ["drop-tool", "extra-tool", "drop-named", "whole-module"],
)
def test_the_logs_key_checks_red_on_a_drifted_local(mutation: str) -> None:
    """Control: a dropped or extra tool key, a dropped named module, or a whole-module reference
    each makes one of the two checks above fail."""
    main_tf = _code(MAIN_TF)
    tools = _tool_keys(main_tf)
    if mutation == "drop-tool":
        mutated = main_tf.replace(f'      module.lambda_action["{tools[0]}"].role_arn,\n', "", 1)
    elif mutation == "extra-tool":
        mutated = main_tf.replace(
            f'      module.lambda_action["{tools[0]}"].role_arn,\n',
            f'      module.lambda_action["{tools[0]}"].role_arn,\n'
            '      module.lambda_action["not_a_tool"].role_arn,\n',
            1,
        )
    elif mutation == "drop-named":
        mutated = main_tf.replace(
            "      module.agent_relay.role_arn,\n    ],\n  )", "    ],\n  )", 1
        )
    else:
        mutated = main_tf.replace(
            f'      module.lambda_action["{tools[0]}"].role_arn,\n',
            "      [for tool in module.lambda_action : tool.role_arn],\n",
            1,
        )
    assert mutated != main_tf
    try:
        keys, named = _logs_key_local(mutated)
    except AssertionError:
        return
    callers = _logs_key_callers(_root_module_bodies()) - {"lambda_action"}
    assert set(keys) != set(tools) or set(named) != callers


def test_observability_receives_the_logs_users_operators_and_administrators() -> None:
    block = _block(_code(MAIN_TF), 'module "observability"')
    assert re.search(
        r"logs_key_user_principal_arns\s*=\s*concat\(local\.logs_key_user_role_arns,\s*"
        r"var\.crm_operator_principal_arns\)",
        block,
    ), block
    assert re.search(
        r"operator_principal_arns\s*=\s*var\.crm_operator_principal_arns\s*$", block, re.M
    )
    assert re.search(rf"administrator_principal_arns\s*=\s*{ROOT_ADMINISTRATORS}", block), block
    assert re.search(SESSION_CALLER, block), block


def test_frontend_cdn_receives_the_operators_and_administrators() -> None:
    block = _block(_code(MAIN_TF), 'module "frontend_cdn"')
    assert re.search(
        r"operator_principal_arns\s*=\s*var\.crm_operator_principal_arns\s*$", block, re.M
    )
    assert re.search(rf"administrator_principal_arns\s*=\s*{ROOT_ADMINISTRATORS}", block), block
    assert re.search(SESSION_CALLER, block), block

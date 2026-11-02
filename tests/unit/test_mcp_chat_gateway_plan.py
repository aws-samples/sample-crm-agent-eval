# SPDX-License-Identifier: MIT
"""Plan-time assertions on Gateway #2: `infra/modules/mcp_chat_gateway` and `infra/mcp_chat_gateway.tf`.

Two plans from `_terraform_fixture`: the module alone with literal inputs, and the committed root file
with every reference outside it replaced by a known literal. Source-level checks cover what neither
plan can see (Gateway #1's scope, the cognito outputs, Gateway #1's role). Skips when terraform or the
cached providers are absent, unless `CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any, Final

import pytest
from scripts.hcl_text import blank_comments, blank_heredoc_bodies

from tests.unit._terraform_fixture import (
    MODULES_DIR,
    REPO_ROOT,
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_module,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)

ROOT_FILE: Final = REPO_ROOT / "infra" / "mcp_chat_gateway.tf"
ROOT_MAIN: Final = REPO_ROOT / "infra" / "main.tf"
COGNITO_OUTPUTS: Final = MODULES_DIR / "cognito" / "outputs.tf"
GATEWAY_1_INTERCEPTOR_PERMISSION: Final = MODULES_DIR / "agentcore" / "interceptor_permission.tf"

ACCOUNT_ID: Final = "123456789012"
DISCOVERY_URL: Final = (
    "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_fixture/.well-known/openid-configuration"
)
CLIENT_IDS: Final = [
    "fixturespaclient000000000000",
    "fixturemcpclient000000000000",
    "fixturemcpoauthclient0000000",
]
CHAT_SCOPE: Final = "fixture-chat/chat"
INTERCEPTOR_ARN: Final = f"arn:aws:lambda:us-east-1:{ACCOUNT_ID}:function:fixture-interceptor"
INTERCEPTOR_NAME: Final = "fixture-interceptor"
CHAT_TARGET_ARN: Final = f"arn:aws:lambda:us-east-1:{ACCOUNT_ID}:function:fixture-chat-target"
RUNTIME_ARN: Final = (
    f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT_ID}:runtime/fixture_rt-AbCdEf0123"
)
RUNTIME_ENDPOINT_ARN: Final = f"{RUNTIME_ARN}/runtime-endpoint/DEFAULT"
LOGS_KMS_ARN: Final = f"arn:aws:kms:us-east-1:{ACCOUNT_ID}:key/fixture-logs"
DDB_KMS_ARN: Final = f"arn:aws:kms:us-east-1:{ACCOUNT_ID}:key/fixture-ddb"
ENI_DENY_ARN: Final = f"arn:aws:iam::{ACCOUNT_ID}:policy/fixture-eni-deny"

#: Built at runtime: `check_no_account_id` rejects literal `subnet-`/`sg-` ids in tracked content.
SUBNET_IDS: Final = ["-".join(("subnet", "0a1b" * 4 + "1")), "-".join(("subnet", "0a1b" * 4 + "2"))]
SECURITY_GROUP_IDS: Final = ["-".join(("sg", "0a1b" * 4 + "3"))]

TABLES: Final = ("reps", "accounts", "opportunities", "eval_runs")
ASSERTED_ACTIONS: Final = (
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
)

GATEWAY: Final = "module.subject.aws_bedrockagentcore_gateway.this"
TARGET: Final = "module.subject.aws_bedrockagentcore_gateway_target.chat"
ROLE: Final = "module.subject.aws_iam_role.gateway"
ROLE_POLICY: Final = "module.subject.aws_iam_role_policy.gateway"
INTERCEPTOR_PERMISSION: Final = "module.subject.aws_lambda_permission.gateway_invoke_interceptor"

CHAT_FN: Final = "module.lambda_action_chat_target.aws_lambda_function.this"
CHAT_DDB_POLICY: Final = "module.lambda_action_chat_target.aws_iam_role_policy.dynamodb[0]"
CHAT_GATEWAY_PERMISSION: Final = (
    "module.lambda_action_chat_target.aws_lambda_permission.gateway_target_role[0]"
)
CHAT_RUNTIME_POLICY: Final = "aws_iam_role_policy.chat_target_invoke_runtime"
CHAT_INTERCEPTOR_FN: Final = "module.lambda_action_chat_interceptor.aws_lambda_function.this"
CHAT_INTERCEPTOR_PERMISSION: Final = (
    "module.lambda_action_chat_interceptor.aws_lambda_permission.gateway_target_role[0]"
)


def _skip_unless_terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)


def _blocks(after: dict[str, Any], name: str) -> list[dict[str, Any]]:
    value = after.get(name)
    if not value:
        return []
    assert isinstance(value, list), f"{name} rendered as {type(value).__name__}, not a block list"
    return [entry for entry in value if isinstance(entry, dict)]


def _one(after: dict[str, Any], *path: str) -> dict[str, Any]:
    node = after
    for name in path:
        blocks = _blocks(node, name)
        assert len(blocks) == 1, f"{name}: expected one block, got {blocks!r}"
        node = blocks[0]
    return node


def _statements(policy_json: str) -> list[dict[str, Any]]:
    document = json.loads(policy_json)
    statements = document["Statement"]
    return statements if isinstance(statements, list) else [statements]


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else [value]


# ---------------------------------------------------------------------------
# 1. The module alone, with literal inputs.
# ---------------------------------------------------------------------------


def _module_arguments() -> str:
    clients = ", ".join(f'"{c}"' for c in CLIENT_IDS)
    return f"""  gateway_name = "fixture-chat-gateway"
  role_name    = "fixture-chat-gateway-role"
  aws_region   = "us-east-1"
  account_id   = "{ACCOUNT_ID}"

  cognito_discovery_url      = "{DISCOVERY_URL}"
  cognito_allowed_client_ids = [{clients}]
  cognito_allowed_scopes     = ["{CHAT_SCOPE}"]

  identity_interceptor_lambda_arn           = "{INTERCEPTOR_ARN}"
  identity_interceptor_lambda_function_name = "{INTERCEPTOR_NAME}"

  chat_target_lambda_arn = "{CHAT_TARGET_ARN}"

  tags = {{ "data-classification" = "internal" }}
"""


@pytest.fixture(scope="module")
def module_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return plan_module(
        tmp_path_factory.mktemp("mcp-chat-gateway"), "mcp_chat_gateway", _module_arguments()
    )


def test_gateway_uses_custom_jwt_with_its_own_scope(module_plan: Plan) -> None:
    """CUSTOM_JWT over the shared discovery URL and clients, allowing exactly the chat scope."""
    after = module_plan.after(GATEWAY)
    assert after["authorizer_type"] == "CUSTOM_JWT"
    assert after["protocol_type"] == "MCP"
    jwt = _one(after, "authorizer_configuration", "custom_jwt_authorizer")
    assert jwt["discovery_url"] == DISCOVERY_URL
    assert sorted(jwt["allowed_clients"]) == sorted(CLIENT_IDS)
    assert jwt["allowed_scopes"] == [CHAT_SCOPE]
    assert jwt.get("allowed_audience") in (None, [])


def test_gateway_routes_every_request_through_the_shared_interceptor(module_plan: Plan) -> None:
    """A REQUEST interceptor on the shared function, receiving the raw request headers."""
    config = _one(module_plan.after(GATEWAY), "interceptor_configuration")
    assert config["interception_points"] == ["REQUEST"]
    assert _one(config, "input_configuration")["pass_request_headers"] is True
    assert _one(config, "interceptor", "lambda")["arn"] == INTERCEPTOR_ARN


def test_exactly_one_target_exposes_chat_with_supervisor(module_plan: Plan) -> None:
    """One target, one tool: `message` required, `conversation_id` optional, nothing else."""
    assert module_plan.addresses_of_type("aws_bedrockagentcore_gateway_target") == [TARGET]
    assert module_plan.addresses_of_type("aws_bedrockagentcore_gateway") == [GATEWAY]
    after = module_plan.after(TARGET)
    _one(after, "credential_provider_configuration", "gateway_iam_role")
    lam = _one(after, "target_configuration", "mcp", "lambda")
    assert lam["lambda_arn"] == CHAT_TARGET_ARN
    payload = _one(lam, "tool_schema", "inline_payload")
    assert payload["name"] == "chat_with_supervisor"
    schema = _one(payload, "input_schema")
    assert schema["type"] == "object"
    properties = {p["name"]: p for p in _blocks(schema, "property")}
    assert set(properties) == {"message", "conversation_id"}
    assert properties["message"]["type"] == "string"
    assert properties["message"]["required"] is True
    assert properties["conversation_id"]["type"] == "string"
    assert properties["conversation_id"]["required"] in (False, None)


def test_gateway_role_may_invoke_only_the_chat_target_and_the_interceptor(
    module_plan: Plan,
) -> None:
    """The role's only Allow is `lambda:InvokeFunction` on exactly those two ARNs; no action Lambda."""
    statements = _statements(module_plan.after(ROLE_POLICY)["policy"])
    assert all(s["Effect"] == "Allow" for s in statements)
    actions = {a for s in statements for a in _as_list(s["Action"])}
    resources = {r for s in statements for r in _as_list(s["Resource"])}
    assert actions == {"lambda:InvokeFunction"}
    assert resources == {CHAT_TARGET_ARN, INTERCEPTOR_ARN}


def test_gateway_role_trust_is_conditioned_on_account_and_gateway_arn(module_plan: Plan) -> None:
    """Trust: the AgentCore service only, from this account, from a gateway ARN in this region."""
    (statement,) = _statements(module_plan.after(ROLE)["assume_role_policy"])
    assert statement["Effect"] == "Allow"
    assert statement["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
    condition = statement["Condition"]
    assert condition["StringEquals"]["aws:SourceAccount"] == ACCOUNT_ID
    assert condition["ArnLike"]["aws:SourceArn"] == (
        f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT_ID}:gateway/*"
    )


def test_interceptor_permission_is_scoped_to_this_gateway_and_account(module_plan: Plan) -> None:
    """A distinct statement id from Gateway #1's, the AgentCore principal, and `source_account` set."""
    after = module_plan.after(INTERCEPTOR_PERMISSION)
    assert after["function_name"] == INTERCEPTOR_NAME
    assert after["principal"] == "bedrock-agentcore.amazonaws.com"
    assert after["source_account"] == ACCOUNT_ID
    assert "source_arn" in module_plan.after_unknown(INTERCEPTOR_PERMISSION), (
        "source_arn must be the gateway's (computed) ARN"
    )
    gateway_1 = re.search(
        r'statement_id\s*=\s*"([^"]+)"', GATEWAY_1_INTERCEPTOR_PERMISSION.read_text("utf-8")
    )
    assert gateway_1 is not None
    assert after["statement_id"] != gateway_1.group(1)


def test_gateway_and_role_carry_the_data_classification_tag(module_plan: Plan) -> None:
    for address in (GATEWAY, ROLE):
        assert module_plan.after(address)["tags"]["data-classification"] == "internal", address


# ---------------------------------------------------------------------------
# 2. The committed root file, with every outside reference replaced by a literal.
# ---------------------------------------------------------------------------


def _hcl_list(values: list[str] | tuple[str, ...]) -> str:
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def _table_arn(name: str) -> str:
    return f"arn:aws:dynamodb:us-east-1:{ACCOUNT_ID}:table/crm-agent-eval-{name}"


def _substitutions(build_dir: Path) -> list[tuple[str, str]]:
    tables = "{ " + ", ".join(f'{t} = "{_table_arn(t)}"' for t in TABLES) + " }"
    return [
        ("${var.lambda_build_dir}", str(build_dir)),
        ("${local.name_prefix}", "crm-agent-eval"),
        ("${var.aws_region}", "us-east-1"),
        ("${module.cognito.user_pool_id}", "us-east-1_fixture"),
        ("local.name_prefix", '"crm-agent-eval"'),
        ("var.aws_region", '"us-east-1"'),
        ("var.data_classification", '"internal"'),
        ("data.aws_caller_identity.current.account_id", f'"{ACCOUNT_ID}"'),
        ("module.eni_function_code_deny.policy_arn", f'"{ENI_DENY_ARN}"'),
        ("module.vpc.lambda_subnet_ids", _hcl_list(SUBNET_IDS)),
        ("module.vpc.lambda_security_group_ids", _hcl_list(SECURITY_GROUP_IDS)),
        ("module.agentcore.agent_runtime_arn", f'"{RUNTIME_ARN}"'),
        ("local.agent_runtime_endpoint_arn", f'"{RUNTIME_ENDPOINT_ARN}"'),
        ("local.relay_runtime_qualifier", '"DEFAULT"'),
        ("module.observability.cloudwatch_logs_kms_key_arn", f'"{LOGS_KMS_ARN}"'),
        ('local.reserved_concurrency["chat_target"]', "5"),
        ("module.dynamodb.table_arns", tables),
        ("module.dynamodb.kms_key_arn", f'"{DDB_KMS_ARN}"'),
        ("local.dynamodb_asserted_actions", _hcl_list(ASSERTED_ACTIONS)),
        ("module.cognito.chat_gateway_allowed_client_ids", _hcl_list(CLIENT_IDS)),
        ("module.cognito.chat_scope_identifier", f'"{CHAT_SCOPE}"'),
        ("local.gateway_service_principal_rep_id", '"svc-mcp-gateway"'),
        ('local.reserved_concurrency["chat_interceptor"]', "5"),
        ('"./modules/', f'"{MODULES_DIR}/'),
    ]


#: References that stay: the file's own module calls and data source.
_INTERNAL_REFERENCES: Final = frozenset(
    {
        "module.lambda_action_chat_target",
        "module.lambda_action_chat_interceptor",
        "module.mcp_chat_gateway",
        "data.aws_iam_policy_document",
    }
)
_ANY_REFERENCE: Final = re.compile(r"\b(?:module|local|var|data)\.[a-z_]+")


def _plannable_root(build_dir: Path) -> str:
    text = ROOT_FILE.read_text(encoding="utf-8")
    for old, new in _substitutions(build_dir):
        assert old in text, f"{old!r} no longer appears in {ROOT_FILE.name}; update the fixture"
        text = text.replace(old, new)
    code = blank_comments(blank_heredoc_bodies(text))
    leftovers = sorted(set(_ANY_REFERENCE.findall(code)) - _INTERNAL_REFERENCES)
    assert leftovers == [], f"unsubstituted outside references: {leftovers}"
    return text


@pytest.fixture(scope="module")
def root_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    build_dir = tmp_path_factory.mktemp("chat-target-build")
    with zipfile.ZipFile(build_dir / "chat_target.zip", "w") as archive:
        archive.writestr("src/gateway/chat_target.py", "def lambda_handler(e, c):\n    return {}\n")
    with zipfile.ZipFile(build_dir / "identity_interceptor.zip", "w") as archive:
        archive.writestr(
            "gateway/identity_interceptor.py", "def lambda_handler(e, c):\n    return {}\n"
        )
    return plan_root(tmp_path_factory.mktemp("mcp-chat-root"), _plannable_root(build_dir))


def test_chat_target_function_shape(root_plan: Plan) -> None:
    """Handler, 660 s timeout, VPC placement, KMS-encrypted env, reserved concurrency."""
    after = root_plan.after(CHAT_FN)
    assert after["function_name"] == "crm-agent-eval-chat-target"
    assert after["handler"] == "gateway.chat_target.lambda_handler"
    assert after["timeout"] == 660
    assert after["reserved_concurrent_executions"] == 5
    assert after["kms_key_arn"] == LOGS_KMS_ARN
    vpc = _one(after, "vpc_config")
    assert sorted(vpc["subnet_ids"]) == sorted(SUBNET_IDS)
    assert sorted(vpc["security_group_ids"]) == sorted(SECURITY_GROUP_IDS)


def test_chat_target_environment_is_exactly_what_the_handler_reads(root_plan: Plan) -> None:
    variables = _one(root_plan.after(CHAT_FN), "environment")["variables"]
    assert variables == {
        "CRM_TABLE_PREFIX": "crm-agent-eval",
        "PYTHONPATH": "/var/task/src",
        "AGENT_RUNTIME_ARN": RUNTIME_ARN,
        "AGENT_RUNTIME_QUALIFIER": "DEFAULT",
    }


def test_chat_target_reaches_reps_only_by_key_prefix(root_plan: Plan) -> None:
    """GetItem on `usersettings#*`/`conversation#*`, PutItem on `conversation#*`; all else denied."""
    statements = _statements(root_plan.after(CHAT_DDB_POLICY)["policy"])
    allows = [s for s in statements if s["Effect"] == "Allow"]
    by_action = {tuple(sorted(_as_list(s["Action"]))): s for s in allows}
    assert set(by_action) == {("dynamodb:GetItem",), ("dynamodb:PutItem",)}
    for statement in allows:
        assert _as_list(statement["Resource"]) == [_table_arn("reps")]
    get_keys = by_action[("dynamodb:GetItem",)]["Condition"]["ForAllValues:StringLike"]
    put_keys = by_action[("dynamodb:PutItem",)]["Condition"]["ForAllValues:StringLike"]
    assert sorted(_as_list(get_keys["dynamodb:LeadingKeys"])) == [
        "conversation#*",
        "usersettings#*",
    ]
    assert _as_list(put_keys["dynamodb:LeadingKeys"]) == ["conversation#*"]

    denies = [s for s in statements if s["Effect"] == "Deny"]
    denied_resources = {r for s in denies for r in _as_list(s["Resource"])}
    for table in TABLES:
        if table != "reps":
            assert _table_arn(table) in denied_resources, table
    own_deny = {
        a
        for s in denies
        if _table_arn("reps") in _as_list(s["Resource"])
        for a in _as_list(s["Action"])
    }
    assert own_deny == set(ASSERTED_ACTIONS) - {"dynamodb:GetItem", "dynamodb:PutItem"}


def test_chat_target_may_invoke_only_this_runtime(root_plan: Plan) -> None:
    """InvokeAgentRuntime on the runtime and its DEFAULT endpoint; every other action there denied."""
    statements = _statements(root_plan.after(CHAT_RUNTIME_POLICY)["policy"])
    (allow,) = [s for s in statements if s["Effect"] == "Allow"]
    (deny,) = [s for s in statements if s["Effect"] == "Deny"]
    assert _as_list(allow["Action"]) == ["bedrock-agentcore:InvokeAgentRuntime"]
    assert sorted(_as_list(allow["Resource"])) == sorted([RUNTIME_ARN, RUNTIME_ENDPOINT_ARN])
    assert _as_list(deny["NotAction"]) == ["bedrock-agentcore:InvokeAgentRuntime"]
    assert sorted(_as_list(deny["Resource"])) == sorted([RUNTIME_ARN, RUNTIME_ENDPOINT_ARN])


def _module_call(plan: Plan, name: str) -> dict[str, Any]:
    calls = plan.configuration["root_module"]["module_calls"]
    call = calls[name]
    assert isinstance(call, dict)
    return call


def test_chat_target_is_invocable_by_gateway_2s_role_only(root_plan: Plan) -> None:
    """The target's resource policy names Gateway #2's role, never Gateway #1's."""
    assert CHAT_GATEWAY_PERMISSION in root_plan.resource_changes
    expression = _module_call(root_plan, "lambda_action_chat_target")["expressions"][
        "gateway_target_role_arn"
    ]
    assert "module.mcp_chat_gateway.role_arn" in expression["references"]
    source = ROOT_FILE.read_text(encoding="utf-8")
    assert "aws_iam_role.agentcore_gateway" not in blank_comments(source)


def test_gateway_2_has_its_own_interceptor_requiring_only_the_chat_scope(root_plan: Plan) -> None:
    """Same code as Gateway #1's interceptor, its own instance, configured with the chat scope."""
    after = root_plan.after(CHAT_INTERCEPTOR_FN)
    assert after["function_name"] == "crm-agent-eval-chat-identity-interceptor"
    assert after["handler"] == "gateway.identity_interceptor.lambda_handler"
    variables = _one(after, "environment")["variables"]
    assert variables["COGNITO_ALLOWED_SCOPES"] == CHAT_SCOPE
    assert variables["COGNITO_ALLOWED_CLIENT_IDS"] == ",".join(CLIENT_IDS)
    assert variables["COGNITO_DISCOVERY_URL"] == DISCOVERY_URL
    assert variables["COGNITO_JWKS_URL"] == DISCOVERY_URL.replace(
        "openid-configuration", "jwks.json"
    )
    assert set(variables) == {
        "COGNITO_DISCOVERY_URL",
        "COGNITO_ALLOWED_CLIENT_IDS",
        "COGNITO_JWKS_URL",
        "COGNITO_ALLOWED_SCOPES",
        "GATEWAY_SERVICE_PRINCIPAL_REP_ID",
        "CRM_TABLE_PREFIX",
    }
    _one(after, "vpc_config")
    assert CHAT_INTERCEPTOR_PERMISSION in root_plan.resource_changes
    calls = root_plan.configuration["root_module"]["module_calls"]
    interceptor = calls["lambda_action_chat_interceptor"]["expressions"]
    assert (
        "module.mcp_chat_gateway.role_arn" in interceptor["gateway_target_role_arn"]["references"]
    )
    gateway = calls["mcp_chat_gateway"]["expressions"]
    assert (
        "module.lambda_action_chat_interceptor.function_arn"
        in (gateway["identity_interceptor_lambda_arn"]["references"])
    )


def test_gateway_1_interceptor_keeps_only_its_own_scope() -> None:
    """Gateway #1's interceptor env is unchanged: `gateway/invoke` only, no chat scope."""
    block = _block(ROOT_MAIN.read_text("utf-8"), 'module "lambda_action_interceptor"')
    assert re.search(
        r"^\s*COGNITO_ALLOWED_SCOPES\s*=\s*module\.cognito\.gateway_scope_identifier\s*$",
        block,
        re.MULTILINE,
    )
    assert "chat_scope" not in block
    assert "CHAT_SCOPE" not in block


def test_root_wires_the_module_to_the_chat_target(root_plan: Plan) -> None:
    gateway = _module_call(root_plan, "mcp_chat_gateway")["expressions"]
    assert (
        "module.lambda_action_chat_target.function_arn"
        in (gateway["chat_target_lambda_arn"]["references"])
    )
    assert (
        root_plan.after(f"module.mcp_chat_gateway.{GATEWAY.split('.', 2)[2]}")["authorizer_type"]
        == "CUSTOM_JWT"
    )
    outputs = root_plan.configuration["root_module"]["outputs"]
    assert (
        "module.mcp_chat_gateway.gateway_url"
        in (outputs["mcp_chat_gateway_url"]["expression"]["references"])
    )


# ---------------------------------------------------------------------------
# 3. Source checks the plans cannot see: the two gateways' scopes and roles stay separate.
# ---------------------------------------------------------------------------


def _block(text: str, header: str) -> str:
    scan = blank_comments(blank_heredoc_bodies(text)).splitlines(keepends=True)
    starts = [i for i, line in enumerate(scan) if line.startswith(header)]
    assert len(starts) == 1, f"expected one {header!r} block, got {starts}"
    depth = 0
    for offset, line in enumerate(scan[starts[0] :], start=starts[0]):
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return "".join(scan[starts[0] : offset + 1])
    raise AssertionError(f"unterminated {header!r}")


def _scope_argument(block: str) -> str:
    match = re.search(r"^\s*cognito_allowed_scopes\s*=\s*(.+?)\s*$", block, re.MULTILINE)
    assert match is not None, block
    return match.group(1)


def test_gateway_1_scope_is_unchanged_and_gateway_2_scope_differs() -> None:
    """Gateway #1 keeps `gateway/invoke`; Gateway #2 allows only the chat scope."""
    gateway_1 = _scope_argument(_block(ROOT_MAIN.read_text("utf-8"), 'module "agentcore"'))
    gateway_2 = _scope_argument(_block(ROOT_FILE.read_text("utf-8"), 'module "mcp_chat_gateway"'))
    assert gateway_1 == "[module.cognito.gateway_scope_identifier]"
    assert gateway_2 == "[module.cognito.chat_scope_identifier]"


def test_the_two_scope_outputs_are_distinct_scopes() -> None:
    """The cognito module declares both outputs, and they resolve to different scope expressions."""
    outputs = COGNITO_OUTPUTS.read_text("utf-8")
    values = {}
    for name in ("gateway_scope_identifier", "chat_scope_identifier"):
        block = _block(outputs, f'output "{name}"')
        match = re.search(r"^\s*value\s*=\s*(.+?)\s*$", block, re.MULTILINE)
        assert match is not None, f"output {name} has no value"
        values[name] = match.group(1)
    assert values["gateway_scope_identifier"] != values["chat_scope_identifier"], values
    assert "gateway/invoke" not in values["chat_scope_identifier"]


def test_gateway_1_role_cannot_invoke_the_chat_target() -> None:
    """Gateway #1's permission document never names the chat target."""
    block = _block(
        ROOT_MAIN.read_text("utf-8"),
        'data "aws_iam_policy_document" "agentcore_gateway_permissions"',
    )
    assert "chat_target" not in block

# SPDX-License-Identifier: MIT
"""Plan-time assertions on the MCP OAuth Cognito client and its Gateway #2-only wiring.

Plans `infra/modules/cognito`, `infra/modules/agentcore` and `infra/modules/mcp_chat_gateway` with
`_terraform_fixture`, and evaluates the root `agentcore_gateway_mcp_url` expression against both
gateway URL shapes. Source-level checks pin which root call sites read which client list. Skips
when terraform or the cached providers are absent, unless `CRM_REQUIRE_TERRAFORM=1`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import pytest
from scripts.hcl_text import blank_comments

from tests.unit._terraform_fixture import (
    REPO_ROOT,
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_module,
    plan_root,
    require_terraform,
    terraform_unavailable_reason,
)
from tests.unit.test_infra_agentcore import _module_arguments as agentcore_arguments
from tests.unit.test_mcp_chat_gateway_plan import _module_arguments as chat_gateway_arguments

INFRA: Final = REPO_ROOT / "infra"
ROOT_MAIN: Final = INFRA / "main.tf"
ROOT_CHAT: Final = INFRA / "mcp_chat_gateway.tf"
ROOT_OUTPUTS: Final = INFRA / "outputs.tf"

OAUTH_CLIENT: Final = "module.subject.aws_cognito_user_pool_client.mcp_oauth"
GATEWAY: Final = "module.subject.aws_bedrockagentcore_gateway.this"

CALLBACKS: Final = {
    "http://localhost:6274/oauth/callback",
    "http://localhost:6274/oauth/callback/debug",
    "http://localhost:33418/callback",
}
MCP_VERSIONS: Final = ["2025-03-26", "2025-06-18", "2025-11-25"]
GATEWAY_HOST: Final = (
    "https://fixture-gateway-abc123.gateway.bedrock-agentcore.us-east-1.amazonaws.com"
)


def _skip_unless_terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is None:
        return
    message = f"terraform plan harness unavailable: {reason}"
    if require_terraform():
        pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
    pytest.skip(message)


def _code(path: Path) -> str:
    return blank_comments(path.read_text(encoding="utf-8"))


def _block(text: str, header: str) -> str:
    start = text.index(header)
    depth = 0
    for index in range(text.index("{", start), len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unbalanced block {header!r}")


def _one(after: dict[str, Any], name: str) -> dict[str, Any]:
    value = after.get(name)
    assert isinstance(value, list) and len(value) == 1, f"{name}: {value!r}"
    entry = value[0]
    assert isinstance(entry, dict)
    return entry


# ---------------------------------------------------------------------------
# 1. The client's shape, from a plan of the cognito module.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cognito_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    arguments = """  pre_token_generation_lambda_arn           = "arn:aws:lambda:us-east-1:123456789012:function:fixture-pre-token"
  pre_token_generation_lambda_function_name = "fixture-pre-token"
  spa_callback_urls = ["https://fixture.example.com/callback"]
  spa_logout_urls   = ["https://fixture.example.com/logout"]
"""
    return plan_module(tmp_path_factory.mktemp("cognito-mcp-oauth"), "cognito", arguments)


def test_the_oauth_client_is_public_with_the_code_grant_only(cognito_plan: Plan) -> None:
    after = cognito_plan.after(OAUTH_CLIENT)
    assert after["generate_secret"] is False
    assert after["allowed_oauth_flows_user_pool_client"] is True
    assert after["allowed_oauth_flows"] == ["code"]
    assert after["explicit_auth_flows"] == ["ALLOW_REFRESH_TOKEN_AUTH"]
    assert after["supported_identity_providers"] == ["COGNITO"]
    assert after["prevent_user_existence_errors"] == "ENABLED"
    assert after["enable_token_revocation"] is True


def test_the_oauth_client_scopes_are_openid_and_the_chat_scope_only(cognito_plan: Plan) -> None:
    """Never `gateway/invoke`: Gateway #1 stays machine-token only.

    The resource-server scope id is unknown until apply, so the plan pins `openid` plus one element
    and the configuration pins that element to the `supervisor` server, whose scope is `chat`.
    """
    scopes = cognito_plan.after(OAUTH_CLIENT)["allowed_oauth_scopes"]
    assert len(scopes) == 2 and "openid" in scopes, scopes
    resources = cognito_plan.configuration["root_module"]["module_calls"]["subject"]["module"][
        "resources"
    ]
    (client,) = [r for r in resources if r["address"] == "aws_cognito_user_pool_client.mcp_oauth"]
    references = set(client["expressions"]["allowed_oauth_scopes"].get("references", []))
    assert "aws_cognito_resource_server.supervisor.scope_identifiers[0]" in references
    assert not {r for r in references if "resource_server.gateway" in r}
    server = cognito_plan.after("module.subject.aws_cognito_resource_server.supervisor")
    assert server["identifier"] == "supervisor"
    assert [s["scope_name"] for s in server["scope"]] == ["chat"]


def test_the_oauth_client_redirects_only_to_the_loopback_callbacks(cognito_plan: Plan) -> None:
    after = cognito_plan.after(OAUTH_CLIENT)
    assert set(after["callback_urls"]) == CALLBACKS
    assert len(after["callback_urls"]) == len(CALLBACKS)
    assert not after.get("logout_urls")
    assert not after.get("default_redirect_uri")


def test_the_oauth_client_cannot_write_a_scope_bearing_attribute(cognito_plan: Plan) -> None:
    assert sorted(cognito_plan.after(OAUTH_CLIENT)["write_attributes"]) == ["email", "name"]


def _output_references(plan: Plan, name: str) -> set[str]:
    outputs = plan.configuration["root_module"]["module_calls"]["subject"]["module"]["outputs"]
    references = outputs[name]["expression"]["references"]
    return {r for r in references if r.endswith(".id")}


def test_the_shared_client_list_excludes_the_oauth_client(cognito_plan: Plan) -> None:
    """`allowed_client_ids` feeds Gateway #1's authorizer and interceptor."""
    assert _output_references(cognito_plan, "allowed_client_ids") == {
        "aws_cognito_user_pool_client.spa.id",
        "aws_cognito_user_pool_client.mcp.id",
    }


def test_the_chat_gateway_client_list_adds_only_the_oauth_client(cognito_plan: Plan) -> None:
    assert _output_references(cognito_plan, "chat_gateway_allowed_client_ids") == {
        "aws_cognito_user_pool_client.spa.id",
        "aws_cognito_user_pool_client.mcp.id",
        "aws_cognito_user_pool_client.mcp_oauth.id",
    }


def test_the_oauth_client_id_output_names_the_oauth_client(cognito_plan: Plan) -> None:
    assert _output_references(cognito_plan, "mcp_oauth_client_id") == {
        "aws_cognito_user_pool_client.mcp_oauth.id"
    }


# ---------------------------------------------------------------------------
# 2. Root call sites: Gateway #2 reads the chat list, Gateway #1 and its interceptor never do.
# ---------------------------------------------------------------------------


def test_gateway_2_and_its_interceptor_read_the_chat_gateway_client_list() -> None:
    chat = _code(ROOT_CHAT)
    interceptor = _block(chat, 'module "lambda_action_chat_interceptor"')
    assert re.search(
        r'COGNITO_ALLOWED_CLIENT_IDS\s*=\s*join\(",",\s*module\.cognito\.chat_gateway_allowed_client_ids\)',
        interceptor,
    )
    gateway = _block(chat, 'module "mcp_chat_gateway"')
    assert re.search(
        r"cognito_allowed_client_ids\s*=\s*module\.cognito\.chat_gateway_allowed_client_ids\s*$",
        gateway,
        re.MULTILINE,
    )
    assert "module.cognito.allowed_client_ids" not in chat


def test_gateway_1_and_its_interceptor_never_read_the_oauth_client() -> None:
    main = _code(ROOT_MAIN)
    for header in ('module "lambda_action_interceptor"', 'module "agentcore"'):
        block = _block(main, header)
        assert "module.cognito.allowed_client_ids" in block, header
        assert "chat_gateway_allowed_client_ids" not in block, header
        assert "mcp_oauth" not in block, header
    assert "chat_gateway_allowed_client_ids" not in main
    assert "mcp_oauth_client_id" not in main


def test_the_root_exports_the_oauth_client_id() -> None:
    block = _block(_code(ROOT_OUTPUTS), 'output "cognito_mcp_oauth_client_id"')
    assert re.search(r"value\s*=\s*module\.cognito\.mcp_oauth_client_id\s*$", block, re.MULTILINE)


# ---------------------------------------------------------------------------
# 3. `agentcore_gateway_mcp_url`, evaluated for both `gatewayUrl` shapes.
# ---------------------------------------------------------------------------


def _mcp_url_expression() -> str:
    block = _block(_code(ROOT_OUTPUTS), 'output "agentcore_gateway_mcp_url"')
    match = re.search(r"^\s*value\s*=\s*(.+?)\s*$", block, re.MULTILINE)
    assert match is not None, block
    expression = match.group(1)
    assert "module.agentcore.gateway_url" in expression
    return expression


@pytest.fixture(scope="module")
def mcp_url_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    expression = _mcp_url_expression()
    shapes = {"bare": GATEWAY_HOST, "with_mcp": f"{GATEWAY_HOST}/mcp"}
    root = "\n".join(
        f'resource "terraform_data" "{name}" {{\n'
        f"  input = {expression.replace('module.agentcore.gateway_url', f'"{url}"')}\n"
        "}\n"
        for name, url in shapes.items()
    )
    return plan_root(tmp_path_factory.mktemp("gateway-mcp-url"), root)


@pytest.mark.parametrize("shape", ["bare", "with_mcp"])
def test_the_gateway_mcp_url_ends_in_exactly_one_mcp_path(mcp_url_plan: Plan, shape: str) -> None:
    assert mcp_url_plan.after(f"terraform_data.{shape}")["input"] == f"{GATEWAY_HOST}/mcp"


# ---------------------------------------------------------------------------
# 4. Gateway #2 declares the supported protocol versions; Gateway #1 sets no protocol.
# ---------------------------------------------------------------------------

GATEWAY_1_SOURCE: Final = INFRA / "modules" / "agentcore" / "gateway.tf"


@pytest.fixture(scope="module")
def gateway_1_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return plan_module(tmp_path_factory.mktemp("g1-protocol"), "agentcore", agentcore_arguments())


@pytest.fixture(scope="module")
def gateway_2_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    _skip_unless_terraform()
    return plan_module(
        tmp_path_factory.mktemp("g2-protocol"), "mcp_chat_gateway", chat_gateway_arguments()
    )


def test_gateway_2_is_mcp_with_the_supported_versions(gateway_2_plan: Plan) -> None:
    after = gateway_2_plan.after(GATEWAY)
    assert after["protocol_type"] == "MCP"
    mcp = _one(_one(after, "protocol_configuration"), "mcp")
    assert sorted(mcp["supported_versions"]) == MCP_VERSIONS


def test_gateway_1_sets_neither_protocol_type_nor_protocol_configuration(
    gateway_1_plan: Plan,
) -> None:
    """Gateway #1 carries no protocol attribute, in plan and in source."""
    after = gateway_1_plan.after(GATEWAY)
    assert after.get("protocol_type") is None
    assert not after.get("protocol_configuration")
    block = _block(_code(GATEWAY_1_SOURCE), 'resource "aws_bedrockagentcore_gateway" "this"')
    assert "protocol_type" not in block
    assert "protocol_configuration" not in block

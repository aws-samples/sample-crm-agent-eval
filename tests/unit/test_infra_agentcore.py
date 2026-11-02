# SPDX-License-Identifier: MIT
"""Plan-time assertions on `infra/modules/agentcore`.

The Runtime's inbound auth is IAM/SigV4 (no JWT authorizer); the Gateway keeps its `CUSTOM_JWT`
authorizer for external MCP clients. Assertions run against a real `terraform show -json` plan from
`_terraform_fixture`. Skips when terraform or the cached providers are absent, unless
`CRM_REQUIRE_TERRAFORM=1`, which turns the skip into a failure.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

from crm.toolspec.definitions import ALL_TOOLS, BY_NAME
from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_module,
    require_terraform,
    terraform_unavailable_reason,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

RUNTIME_ADDRESS = "module.subject.aws_bedrockagentcore_agent_runtime.this"
GATEWAY_ADDRESS = "module.subject.aws_bedrockagentcore_gateway.this"

DISCOVERY_URL = (
    "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_fixture/.well-known/openid-configuration"
)

#: The module validates both tool maps at exactly one entry per toolspec tool.
TOOL_COUNT = len(ALL_TOOLS)


def _tool_maps_hcl(schema_count: int = TOOL_COUNT) -> str:
    arns = "\n".join(
        f'    tool_{i} = "arn:aws:lambda:us-east-1:123456789012:function:fixture-tool-{i}"'
        for i in range(TOOL_COUNT)
    )
    schemas = "\n".join(
        f"""    tool_{i} = {{
      description = "Fixture tool {i}"
      properties = [{{
        name        = "account_id"
        type        = "string"
        description = "Fixture property"
        required    = true
      }}]
    }}"""
        for i in range(schema_count)
    )
    return f"  tool_lambda_arns = {{\n{arns}\n  }}\n\n  tool_schemas = {{\n{schemas}\n  }}\n"


def _module_arguments() -> str:
    return f"""  kms_key_arn        = "arn:aws:kms:us-east-1:123456789012:key/fixture-logs"
  source_bucket_arn  = "arn:aws:s3:::fixture-source-bucket"
  source_kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/fixture-source"
  source_key         = "agent-source.zip"
  source_content_hash = "0000000000000000000000000000000000000000000000000000000000000000"
  buildspec           = "version: 0.2\\nphases:\\n  build:\\n    commands:\\n      - true\\n"

  agent_runtime_role_arn = "arn:aws:iam::123456789012:role/fixture-runtime"
  gateway_role_arn       = "arn:aws:iam::123456789012:role/fixture-gateway"

  cognito_discovery_url      = "{DISCOVERY_URL}"
  cognito_allowed_client_ids = ["fixtureclientid0000000000000"]
  cognito_allowed_audiences  = ["fixtureclientid0000000000000"]

  identity_interceptor_lambda_arn           = "arn:aws:lambda:us-east-1:123456789012:function:fixture-interceptor"
  identity_interceptor_lambda_function_name = "fixture-interceptor"

  data_classification = "internal"

{_tool_maps_hcl()}"""


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)
    work_dir: Path = tmp_path_factory.mktemp("agentcore-plan")
    return plan_module(work_dir, "agentcore", _module_arguments())


def _blocks(after: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """The rendered nested-block list for `name`; `[]` when the block is absent or `null`."""
    value = after.get(name)
    if not value:
        return []
    assert isinstance(value, list), f"{name} rendered as {type(value).__name__}, not a block list"
    return [entry for entry in value if isinstance(entry, dict)]


# ---------------------------------------------------------------------------
# Runtime vs Gateway inbound auth, asserted against the same plan.
# ---------------------------------------------------------------------------


def test_the_runtime_has_no_jwt_authorizer_so_inbound_auth_is_iam(plan: Plan) -> None:
    """The Runtime has no `authorizer_configuration`, the provider's form of IAM inbound auth."""
    after = plan.after(RUNTIME_ADDRESS)
    assert _blocks(after, "authorizer_configuration") == []


def test_the_gateway_keeps_its_custom_jwt_authorizer(plan: Plan) -> None:
    """The Gateway has a `CUSTOM_JWT` authorizer bound to the Cognito discovery URL and client."""
    after = plan.after(GATEWAY_ADDRESS)
    assert after["authorizer_type"] == "CUSTOM_JWT"
    authorizer = _blocks(after, "authorizer_configuration")
    assert len(authorizer) == 1, authorizer
    jwt = _blocks(authorizer[0], "custom_jwt_authorizer")
    assert len(jwt) == 1, jwt
    assert jwt[0]["discovery_url"] == DISCOVERY_URL
    assert jwt[0]["allowed_clients"] == ["fixtureclientid0000000000000"]


def test_the_runtime_and_the_gateway_are_not_conflated(plan: Plan) -> None:
    """Exactly one Runtime and one Gateway exist, and only the Gateway carries an authorizer."""
    assert plan.addresses_of_type("aws_bedrockagentcore_agent_runtime") == [RUNTIME_ADDRESS]
    assert plan.addresses_of_type("aws_bedrockagentcore_gateway") == [GATEWAY_ADDRESS]

    with_authorizer = {
        address
        for address in (RUNTIME_ADDRESS, GATEWAY_ADDRESS)
        if _blocks(plan.after(address), "authorizer_configuration")
    }
    assert with_authorizer == {GATEWAY_ADDRESS}


def test_no_unvalidated_authorization_header_reaches_the_container(plan: Plan) -> None:
    """No resource in the plan configures a request-header allowlist forwarding to the container."""
    after = plan.after(RUNTIME_ADDRESS)
    assert _blocks(after, "request_header_configuration") == []

    for address, change in plan.resource_changes.items():
        rendered = change.get("change", {}).get("after")
        if isinstance(rendered, dict):
            for block in _blocks(rendered, "request_header_configuration"):
                assert not block.get("request_header_allowlist"), (
                    f"{address} forwards headers to the container under IAM inbound auth: "
                    f"{block['request_header_allowlist']}"
                )


def test_the_runtime_still_carries_its_container_and_network_configuration(plan: Plan) -> None:
    """By default the Runtime has a container artifact, `PUBLIC` mode, and no capacity provider."""
    after = plan.after(RUNTIME_ADDRESS)
    artifact = _blocks(after, "agent_runtime_artifact")
    assert len(artifact) == 1, artifact
    assert _blocks(artifact[0], "container_configuration")
    network = _blocks(after, "network_configuration")
    assert len(network) == 1, network
    assert network[0]["network_mode"] == "PUBLIC"
    # PUBLIC carries no network_mode_config block.
    assert _blocks(network[0], "network_mode_config") == []
    assert "capacity_provider_configuration" not in after


# Composed at runtime so `check_no_account_id` does not read a fixture value as a real id.
_VPC_MODE_SUBNET_IDS = ["subnet-" + "d" * 8, "subnet-" + "e" * 8]
_VPC_MODE_SG_IDS = ["sg-" + "f" * 8]


@pytest.fixture(scope="module")
def vpc_mode_plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    """The agentcore module planned in VPC mode, with the Runtime subnets and SG supplied."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)
    import json as _json

    overrides = (
        f'\n  runtime_network_mode = "VPC"'
        f"\n  runtime_subnet_ids = {_json.dumps(_VPC_MODE_SUBNET_IDS)}"
        f"\n  runtime_security_group_ids = {_json.dumps(_VPC_MODE_SG_IDS)}\n"
    )
    work_dir: Path = tmp_path_factory.mktemp("agentcore-plan-vpc")
    return plan_module(work_dir, "agentcore", _module_arguments() + overrides)


def test_the_runtime_runs_in_vpc_mode_when_configured(vpc_mode_plan: Plan) -> None:
    """In VPC mode `network_mode_config` carries exactly the supplied subnets and SGs."""
    after = vpc_mode_plan.after(RUNTIME_ADDRESS)
    network = _blocks(after, "network_configuration")
    assert len(network) == 1, network
    assert network[0]["network_mode"] == "VPC"
    config = _blocks(network[0], "network_mode_config")
    assert len(config) == 1, config
    assert sorted(config[0]["subnets"]) == sorted(_VPC_MODE_SUBNET_IDS)
    assert sorted(config[0]["security_groups"]) == sorted(_VPC_MODE_SG_IDS)


def test_vpc_mode_without_subnets_fails_the_plan(tmp_path_factory: pytest.TempPathFactory) -> None:
    """VPC mode with no subnets/SGs fails the plan."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)
    work_dir: Path = tmp_path_factory.mktemp("agentcore-plan-vpc-nosubnets")
    with pytest.raises(Exception) as excinfo:
        plan_module(
            work_dir, "agentcore", _module_arguments() + '\n  runtime_network_mode = "VPC"\n'
        )
    assert "runtime_subnet_ids" in str(excinfo.value) or "VPC mode" in str(excinfo.value)


def test_the_module_pins_no_tool_count() -> None:
    """The tool-map validations carry no count literal; the key set comes from the toolspec."""
    variables = (REPO_ROOT / "infra" / "modules" / "agentcore" / "variables.tf").read_text(
        encoding="utf-8"
    )
    assert re.findall(r"length\(var\.tool_\w+\) == \d+", variables) == []
    assert re.findall(r"length\(var\.tool_\w+\) > 0", variables), (
        "the non-empty validations on the tool maps are gone"
    )


def test_tool_maps_with_different_key_sets_fail_the_plan(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A schema map missing one target's key fails the gateway target's key-parity precondition."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)
    arguments = _module_arguments()
    assert _tool_maps_hcl() in arguments
    mismatched = arguments.replace(_tool_maps_hcl(), _tool_maps_hcl(schema_count=TOOL_COUNT - 1))
    with pytest.raises(Exception) as excinfo:
        plan_module(tmp_path_factory.mktemp("agentcore-plan-keys"), "agentcore", mismatched)
    assert "identical key sets" in " ".join(str(excinfo.value).split())


@pytest.mark.parametrize("name", ["runtime", "codebuild"])
def test_every_log_group_is_encrypted_with_the_module_cmk(plan: Plan, name: str) -> None:
    """Each of the module's log groups sets `kms_key_id` to the module's `kms_key_arn`."""
    address = f"module.subject.aws_cloudwatch_log_group.{name}"
    assert address in plan.addresses_of_type("aws_cloudwatch_log_group")
    assert (
        plan.after(address)["kms_key_id"] == "arn:aws:kms:us-east-1:123456789012:key/fixture-logs"
    )


# ---------------------------------------------------------------------------
# Platform version.
# ---------------------------------------------------------------------------


def test_the_runtime_defaults_to_platform_v2_with_minutes_long_timeouts(plan: Plan) -> None:
    """With no override the Runtime plans `platform_version = "V2"` and 30m create/update bounds."""
    after = plan.after(RUNTIME_ADDRESS)
    assert after["platform_version"] == "V2"
    assert after["timeouts"] == {"create": "30m", "update": "30m", "delete": None}


def _skip_or_fail_without_terraform() -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)


def test_the_v1_rollback_value_plans(tmp_path_factory: pytest.TempPathFactory) -> None:
    """`platform_version = "V1"` is accepted and reaches the Runtime (the documented rollback)."""
    _skip_or_fail_without_terraform()
    rollback = plan_module(
        tmp_path_factory.mktemp("agentcore-plan-v1"),
        "agentcore",
        _module_arguments() + '\n  platform_version = "V1"\n',
    )
    assert rollback.after(RUNTIME_ADDRESS)["platform_version"] == "V1"


def test_an_unknown_platform_version_fails_the_plan(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _skip_or_fail_without_terraform()
    with pytest.raises(Exception) as excinfo:
        plan_module(
            tmp_path_factory.mktemp("agentcore-plan-v3"),
            "agentcore",
            _module_arguments() + '\n  platform_version = "V3"\n',
        )
    assert "platform_version must be" in " ".join(str(excinfo.value).split())


# ---------------------------------------------------------------------------
# Gateway target property descriptions: carried to the plan and held to ALL_TOOLS.
# ---------------------------------------------------------------------------

TARGET_ADDRESS = 'module.subject.aws_bedrockagentcore_gateway_target.tool["tool_0"]'
PARITY_GATE = REPO_ROOT / "scripts" / "check_toolspec_parity.py"
MAIN_TF = REPO_ROOT / "infra" / "main.tf"


def _hcl_string(text: str) -> str:
    """`text` as an HCL quoted-string body, escaped the way `locals.tool_schema_properties` is."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _parity_gate() -> Any:
    """Import `scripts/check_toolspec_parity.py` as a module without running it."""
    spec = importlib.util.spec_from_file_location(
        "check_toolspec_parity_for_agentcore", PARITY_GATE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_toolspec_property_description_reaches_the_planned_gateway_target(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A toolspec property description, quotes and backticks included, plans onto the target."""
    _skip_or_fail_without_terraform()
    description = BY_NAME["get_page_context"].input_schema["properties"]["page_context"][
        "description"
    ]
    assert '"' in description and "`" in description
    arguments = _module_arguments()
    fixture_line = 'description = "Fixture property"'
    assert arguments.count(fixture_line) == TOOL_COUNT
    arguments = arguments.replace(fixture_line, f'description = "{_hcl_string(description)}"', 1)
    plan = plan_module(
        tmp_path_factory.mktemp("agentcore-plan-descriptions"), "agentcore", arguments
    )
    target = plan.after(TARGET_ADDRESS)
    payload = target["target_configuration"][0]["mcp"][0]["lambda"][0]["tool_schema"][0]
    properties = payload["inline_payload"][0]["input_schema"][0]["property"]
    assert [entry["description"] for entry in properties] == [description]


def test_main_tf_transcribes_every_toolspec_property_description() -> None:
    """The real `main.tf` passes the gate's property parity with no description violation."""
    violations = _parity_gate()._check_tool_schema_property_parity(
        MAIN_TF.read_text(encoding="utf-8")
    )
    assert violations == []


def _first_transcribed_description(text: str) -> tuple[str, str]:
    """The first `description = "..."` value in `locals.tool_schema_properties`, and its tool."""
    block_start = text.index("tool_schema_properties = {")
    match = re.compile(r'\{ name = "(\w+)",[^\n]*description = "([^"\\]+)').search(
        text, block_start
    )
    assert match is not None, "no transcribed property description to mutate"
    return match.group(1), match.group(2)


def test_a_one_character_toolspec_description_drift_fails_the_parity_gate() -> None:
    """Changing one character of one transcribed property description is a parity violation."""
    text = MAIN_TF.read_text(encoding="utf-8")
    name, description = _first_transcribed_description(text)
    drifted = description[:-1] + ("X" if description[-1] != "X" else "Y")
    mutated = text.replace(f'description = "{description}', f'description = "{drifted}', 1)
    assert mutated != text
    violations = _parity_gate()._check_tool_schema_property_parity(mutated)
    assert len(violations) == 1, violations
    assert f"[{name!r}].description diverges" in violations[0]


def test_a_dropped_toolspec_property_description_fails_the_parity_gate() -> None:
    """A transcribed entry that omits a description ALL_TOOLS declares is a parity violation."""
    text = MAIN_TF.read_text(encoding="utf-8")
    name, description = _first_transcribed_description(text)
    mutated = text.replace(f', description = "{description}"', "", 1)
    assert mutated != text
    violations = _parity_gate()._check_tool_schema_property_parity(mutated)
    assert len(violations) == 1, violations
    assert f"[{name!r}].description diverges" in violations[0]

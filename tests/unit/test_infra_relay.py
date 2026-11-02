# SPDX-License-Identifier: MIT
"""Plan-time assertions on `infra/modules/agent_relay`.

Covers the Function URL's `RESPONSE_STREAM` invoke mode and `AWS_IAM` auth, the `nodejs*` runtime,
the environment surface, and the execution role's exact grant surface (`InvokeAgentRuntime` on one
runtime, its own log group, one SSM parameter). The live per-role allow/deny check is
`scripts/smoke/iam_roles.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.unit import _eni_function_code_deny as eni_deny
from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    plan_module,
    require_terraform,
    terraform_unavailable_reason,
)

FUNCTION_ADDRESS = "module.subject.aws_lambda_function.this"
FUNCTION_URL_ADDRESS = "module.subject.aws_lambda_function_url.this"
ROLE_ADDRESS = "module.subject.aws_iam_role.this"
INVOKE_POLICY_ADDRESS = "module.subject.aws_iam_role_policy.invoke_runtime"
LOGS_POLICY_ADDRESS = "module.subject.aws_iam_role_policy.logs"
PARAMETER_POLICY_ADDRESS = "module.subject.aws_iam_role_policy.read_audience_parameter"
VPC_ACCESS_POLICY_ADDRESS = "module.subject.aws_iam_role_policy.vpc_access"

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/crm_agent_eval_runtime-abc"
RUNTIME_NOT_ACTION_DENY_SID = "DenyEverythingButTheOnePermittedInvokeOnThisRuntime"
HARNESS_DENY_SID = "DenyEveryHarness"
# The harness resource ARN the DenyEveryHarness statement must cover: the runtime ARN's first five
# colon-fields (arn:partition:service:region:account) with the harness resource type.
HARNESS_ARN_PREFIX = "arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/*"
PARAMETER_NAME = "/crm-agent-eval/relay/cognito-audience"
PARAMETER_ARN = f"arn:aws:ssm:us-east-1:123456789012:parameter{PARAMETER_NAME}"

#: Placeholder network ids for the plan fixture; not hex after the prefix, so
#: `scripts/check_no_account_id.sh` does not match them as real ids.
SUBNET_IDS = ["subnet-fixturezonea", "subnet-fixturezoneb"]
SECURITY_GROUP_IDS = ["sg-fixturelambda"]

#: Every environment variable the module sets, as an exact set.
EXPECTED_ENVIRONMENT = {
    "AGENT_RUNTIME_ARN",
    "AGENT_RUNTIME_QUALIFIER",
    "COGNITO_ISSUER",
    "COGNITO_JWKS_URL",
    "COGNITO_AUDIENCE_PARAMETER",
}

#: Variables the relay reads that Lambda reserves and injects, so this module must not set them.
RESERVED_RUNTIME_VARIABLES = {"AWS_REGION"}

#: The relay's TypeScript sources, whose `ENV_*` constants name the variables it requires at
#: startup.
RELAY_SRC = Path(__file__).resolve().parents[2] / "src" / "relay" / "src"


def _module_arguments(relay_zip: Path, **overrides: str) -> str:
    arguments = {
        "function_name": '"crm-agent-eval-agent-relay"',
        "filename": f'"{relay_zip}"',
        "source_code_hash": '"0000000000000000000000000000000000000000000="',
        "agent_runtime_arn": f'"{RUNTIME_ARN}"',
        "cognito_issuer": '"https://cognito-idp.us-east-1.amazonaws.com/us-east-1_fixture"',
        "cognito_audience_parameter_name": f'"{PARAMETER_NAME}"',
        "cognito_audience_parameter_arn": f'"{PARAMETER_ARN}"',
        "vpc_subnet_ids": json.dumps(SUBNET_IDS),
        "vpc_security_group_ids": json.dumps(SECURITY_GROUP_IDS),
        # Required input with no module default; any value > 0 satisfies the validation.
        "reserved_concurrent_executions": "20",
        "eni_function_code_deny_policy_arn": f'"{eni_deny.POLICY_ARN}"',
    }
    arguments.update(overrides)
    return "\n".join(f"  {key} = {value}" for key, value in arguments.items())


def _plan(work_dir: Path, **overrides: str) -> Plan:
    relay_zip = work_dir / "relay.zip"
    work_dir.mkdir(parents=True, exist_ok=True)
    relay_zip.write_bytes(b"")
    return plan_module(work_dir, "agent_relay", _module_arguments(relay_zip, **overrides))


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)
    return _plan(tmp_path_factory.mktemp("agent-relay-plan"))


def _policy_statements(plan: Plan, address: str) -> list[dict[str, Any]]:
    """The statements of a rendered inline role policy at `address`.

    Raises if the document is unknown at plan time; use :func:`_policy_document_is_deferred` for
    that case.
    """
    rendered = plan.after(address).get("policy")
    if not isinstance(rendered, str):
        raise AssertionError(f"{address}'s policy document is not known at plan time")
    document = json.loads(rendered)
    statements = document["Statement"]
    return statements if isinstance(statements, list) else [statements]


def _policy_document_is_deferred(plan: Plan, address: str) -> bool:
    """True when a policy's JSON is `(known after apply)` — it embeds a same-apply resource ARN."""
    return not isinstance(plan.after(address).get("policy"), str)


def _as_list(value: Any) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


# ---------------------------------------------------------------------------
# The three properties that decide whether the deployed relay can stream at all.
# ---------------------------------------------------------------------------


def test_the_function_url_streams_rather_than_buffers(plan: Plan) -> None:
    """The Function URL's invoke mode is `RESPONSE_STREAM`, not the default `BUFFER`."""
    assert plan.after(FUNCTION_URL_ADDRESS)["invoke_mode"] == "RESPONSE_STREAM"


def test_the_function_url_is_not_reachable_without_an_iam_signature(plan: Plan) -> None:
    """The Function URL's authorization type is `AWS_IAM`; CloudFront OAC supplies the signature."""
    assert plan.after(FUNCTION_URL_ADDRESS)["authorization_type"] == "AWS_IAM"


def test_no_permission_in_this_module_admits_a_wildcard_principal(plan: Plan) -> None:
    """No `aws_lambda_permission` the module plans has `principal = "*"`."""
    permissions = plan.addresses_of_type("aws_lambda_permission")
    for address in permissions:
        assert plan.after(address).get("principal") != "*", address
    assert "module.subject.aws_lambda_permission.public_function_url" not in permissions


def test_the_relay_runs_on_node_with_a_timeout_above_the_turn_cap(plan: Plan) -> None:
    """The relay uses a `nodejs*` runtime, a timeout above 120s, and positive reserved
    concurrency."""
    after = plan.after(FUNCTION_ADDRESS)
    assert after["runtime"].startswith("nodejs")
    assert after["timeout"] > 120
    assert after["reserved_concurrent_executions"] > 0


def test_the_relay_environment_is_exactly_the_declared_surface(plan: Plan) -> None:
    """The relay's environment is exactly the declared set, with the audience passed as a parameter
    name."""
    environment = plan.after(FUNCTION_ADDRESS)["environment"]
    assert isinstance(environment, list) and len(environment) == 1, environment
    variables = environment[0]["variables"]
    assert set(variables) == EXPECTED_ENVIRONMENT
    assert variables["AGENT_RUNTIME_ARN"] == RUNTIME_ARN
    assert variables["AGENT_RUNTIME_QUALIFIER"] == "DEFAULT"
    assert variables["COGNITO_AUDIENCE_PARAMETER"] == PARAMETER_NAME
    assert not any("origin-verification-secret" in value for value in variables.values())
    assert variables["COGNITO_JWKS_URL"].endswith("/.well-known/jwks.json")
    assert variables["COGNITO_JWKS_URL"].startswith(variables["COGNITO_ISSUER"])
    # `AWS_REGION` is reserved by Lambda and injected by the runtime.
    assert "AWS_REGION" not in variables


# ---------------------------------------------------------------------------
# The execution role: the grant surface, and everything it must NOT hold.
# ---------------------------------------------------------------------------


def _endpoint_scope(resources: list[str]) -> str:
    """The one non-runtime entry in a scope pair, asserted to be a `/*` child path of the
    runtime."""
    others = [r for r in resources if r != RUNTIME_ARN]
    assert len(others) == 1, f"expected exactly one endpoint entry beside the runtime, got {others}"
    entry = others[0]
    assert entry.startswith(f"{RUNTIME_ARN}/"), (
        f"{entry!r} is not a child path of this runtime, so the scope names something else entirely"
    )
    assert entry.endswith("/*"), f"{entry!r} does not wildcard a path segment"
    # A bare trailing wildcard would also match any sibling runtime sharing this id's prefix.
    assert entry != f"{RUNTIME_ARN}*", "bare prefix wildcard: matches sibling runtimes too"
    return entry


def test_the_role_may_invoke_exactly_one_runtime(plan: Plan) -> None:
    """The one Allow grants `InvokeAgentRuntime` on the runtime ARN and its endpoint children
    only."""
    allows = [s for s in _policy_statements(plan, INVOKE_POLICY_ADDRESS) if s["Effect"] == "Allow"]
    assert len(allows) == 1, allows
    assert _as_list(allows[0]["Action"]) == ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = _as_list(allows[0]["Resource"])
    assert RUNTIME_ARN in resources, resources
    _endpoint_scope(resources)


def test_the_deny_is_shaped_as_not_action_rather_than_an_enumerated_list(plan: Plan) -> None:
    """The runtime Deny has `NotAction` holding exactly `InvokeAgentRuntime` and no `Action` key."""
    denies = [s for s in _policy_statements(plan, INVOKE_POLICY_ADDRESS) if s["Effect"] == "Deny"]
    not_action_denies = [s for s in denies if s.get("Sid") == RUNTIME_NOT_ACTION_DENY_SID]
    assert len(not_action_denies) == 1, denies
    runtime_deny = not_action_denies[0]
    assert "Action" not in runtime_deny, (
        "an Action key means the enumerated form is back, and the enumeration is the defect"
    )
    assert set(_as_list(runtime_deny["NotAction"])) == {"bedrock-agentcore:InvokeAgentRuntime"}


def test_the_not_action_deny_is_scoped_to_the_runtime_resources(plan: Plan) -> None:
    """The `NotAction` Deny is scoped to the runtime and the same endpoint expression as the Allow,
    never `"*"`."""
    denies = [s for s in _policy_statements(plan, INVOKE_POLICY_ADDRESS) if s["Effect"] == "Deny"]
    runtime_deny = next(s for s in denies if s.get("Sid") == RUNTIME_NOT_ACTION_DENY_SID)
    deny_resources = _as_list(runtime_deny["Resource"])
    assert RUNTIME_ARN in deny_resources, deny_resources
    assert "*" not in deny_resources
    allows = [s for s in _policy_statements(plan, INVOKE_POLICY_ADDRESS) if s["Effect"] == "Allow"]
    assert _endpoint_scope(deny_resources) == _endpoint_scope(_as_list(allows[0]["Resource"])), (
        "the Deny and the Allow scope to different endpoint expressions, so one of them reaches a "
        "resource the other does not -- the shape that made the Deny fail open on the real endpoint"
    )


def test_the_deny_does_not_cover_the_action_the_relay_must_hold(plan: Plan) -> None:
    """Every allowed action is excepted by the runtime Deny's `NotAction`."""
    statements = _policy_statements(plan, INVOKE_POLICY_ADDRESS)
    allowed = {
        action
        for statement in statements
        if statement["Effect"] == "Allow"
        for action in _as_list(statement["Action"])
    }
    denies = [s for s in statements if s["Effect"] == "Deny"]
    runtime_deny = next(s for s in denies if s.get("Sid") == RUNTIME_NOT_ACTION_DENY_SID)
    not_actions = set(_as_list(runtime_deny["NotAction"]))
    assert allowed, "the allow side is empty, so this test would pass vacuously"
    assert allowed <= not_actions, (
        f"actions allowed but not excepted from the Deny would be denied: {allowed - not_actions}"
    )


def test_a_harness_deny_explicitly_denies_every_bedrock_agentcore_action_on_harness(
    plan: Plan,
) -> None:
    """A separate Deny covers `bedrock-agentcore:*` on this account/region's `harness/*`, and no
    Allow
    names a harness resource."""
    statements = _policy_statements(plan, INVOKE_POLICY_ADDRESS)
    harness_denies = [
        s for s in statements if s["Effect"] == "Deny" and s.get("Sid") == HARNESS_DENY_SID
    ]
    assert len(harness_denies) == 1, statements
    harness_deny = harness_denies[0]
    assert "bedrock-agentcore:*" in _as_list(harness_deny["Action"]), harness_deny
    assert _as_list(harness_deny["Resource"]) == [HARNESS_ARN_PREFIX], harness_deny
    for statement in statements:
        if statement["Effect"] != "Allow":
            continue
        for resource in _as_list(statement.get("Resource", [])):
            assert ":harness/" not in resource, (
                f"an Allow names a harness resource, which the relay must never hold: {resource}"
            )


def test_the_role_carries_exactly_four_inline_policies(plan: Plan) -> None:
    """The role has exactly the invoke, logs, parameter and VPC-access inline policies."""
    assert plan.addresses_of_type("aws_iam_role_policy") == sorted(
        [
            INVOKE_POLICY_ADDRESS,
            LOGS_POLICY_ADDRESS,
            PARAMETER_POLICY_ADDRESS,
            VPC_ACCESS_POLICY_ADDRESS,
        ]
    )


def test_no_plan_assertable_grant_exceeds_the_relay_s_job(plan: Plan) -> None:
    """The invoke and parameter policies' Allows grant exactly `InvokeAgentRuntime` and
    `ssm:GetParameter`, and the logs policy is the only one whose document is deferred to apply
    time."""
    granted: set[str] = set()
    for address in (INVOKE_POLICY_ADDRESS, PARAMETER_POLICY_ADDRESS):
        for statement in _policy_statements(plan, address):
            if statement["Effect"] != "Allow":
                continue
            granted.update(_as_list(statement["Action"]))

    assert granted == {"bedrock-agentcore:InvokeAgentRuntime", "ssm:GetParameter"}
    assert not any(action.endswith("*") for action in granted)

    deferred = [
        address
        for address in plan.addresses_of_type("aws_iam_role_policy")
        if _policy_document_is_deferred(plan, address)
    ]
    assert deferred == [LOGS_POLICY_ADDRESS], deferred


#: The X-Ray actions Lambda's active tracing needs on the execution role.
XRAY_WRITE_ACTIONS = ("xray:PutTraceSegments", "xray:PutTelemetryRecords")


def test_the_relay_traces_actively(plan: Plan) -> None:
    """`tracing_config.mode` is `Active`, not the `PassThrough` default."""
    tracing = plan.after(FUNCTION_ADDRESS).get("tracing_config")
    assert tracing == [{"mode": "Active"}], tracing


def test_the_role_may_write_xray_segments(plan: Plan) -> None:
    """The VPC-access policy's second Allow grants exactly the two X-Ray write actions on `*`."""
    xray = [
        statement
        for statement in _policy_statements(plan, VPC_ACCESS_POLICY_ADDRESS)
        if statement["Effect"] == "Allow"
        and set(_as_list(statement["Action"])) & set(XRAY_WRITE_ACTIONS)
    ]
    assert len(xray) == 1, xray
    assert sorted(_as_list(xray[0]["Action"])) == sorted(XRAY_WRITE_ACTIONS)
    assert _as_list(xray[0]["Resource"]) == ["*"]


def test_no_deny_on_the_role_reaches_the_xray_grant(plan: Plan) -> None:
    """No Deny in a plan-known relay policy covers the X-Ray actions on `*`.

    The runtime `NotAction` Deny names every action but `InvokeAgentRuntime`, so it would deny X-Ray
    too if its resources ever widened to `*`.
    """
    checked = 0
    for address in plan.addresses_of_type("aws_iam_role_policy"):
        if _policy_document_is_deferred(plan, address):
            continue
        for statement in _policy_statements(plan, address):
            if statement["Effect"] != "Deny":
                continue
            checked += 1
            if "*" not in _as_list(statement["Resource"]):
                continue
            if "NotAction" in statement:
                excluded = _as_list(statement["NotAction"])
                covered = [action for action in XRAY_WRITE_ACTIONS if action not in excluded]
            else:
                covered = [
                    action
                    for action in XRAY_WRITE_ACTIONS
                    for pattern in _as_list(statement["Action"])
                    if re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), action)
                ]
            assert not covered, (address, statement.get("Sid"), covered)
    assert checked >= 2, f"only {checked} Deny statement(s) read; the runtime Denies were not seen"


def test_the_parameter_grant_names_exactly_the_one_parameter(plan: Plan) -> None:
    """The parameter grant's resource list is exactly the audience parameter's ARN, with no
    wildcard."""
    statements = _policy_statements(plan, PARAMETER_POLICY_ADDRESS)
    assert len(statements) == 1, statements
    assert _as_list(statements[0]["Resource"]) == [PARAMETER_ARN]
    assert not any("*" in resource for resource in _as_list(statements[0]["Resource"]))


def test_the_role_is_this_function_s_own(plan: Plan) -> None:
    """One role per function, never shared: the relay must not reuse an action Lambda's role."""
    assert plan.after(ROLE_ADDRESS)["name"] == "crm-agent-eval-agent-relay-role"
    assert plan.addresses_of_type("aws_iam_role") == [ROLE_ADDRESS]
    trust = json.loads(plan.after(ROLE_ADDRESS)["assume_role_policy"])
    principals = {
        principal
        for statement in trust["Statement"]
        for principal in _as_list(statement["Principal"]["Service"])
    }
    assert principals == {"lambda.amazonaws.com"}


# ---------------------------------------------------------------------------
# Inputs the module must refuse. Each of these would otherwise deploy cleanly and misbehave.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        pytest.param(
            {"runtime": '"python3.13"'},
            "only Node.js managed runtimes support response streaming",
            id="python-runtime-cannot-stream",
        ),
        pytest.param(
            {"timeout_seconds": "600"},
            "must exceed the 600s chat turn wall clock",
            id="timeout-at-the-turn-cap",
        ),
        pytest.param(
            {"reserved_concurrent_executions": "0"},
            "permanently uninvokable",
            id="zero-reserved-concurrency-bricks-the-relay",
        ),
        pytest.param(
            {"agent_runtime_arn": '"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/*"'},
            "must name one runtime",
            id="wildcard-runtime-widens-the-only-grant",
        ),
        pytest.param(
            {"cognito_audience_parameter_arn": '"arn:aws:ssm:us-east-1:123456789012:parameter/*"'},
            "must name one parameter",
            id="wildcard-parameter-grants-every-parameter",
        ),
    ],
)
def test_the_module_refuses_a_configuration_that_would_deploy_and_misbehave(
    tmp_path: Path, overrides: dict[str, str], expected_message: str
) -> None:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)

    from tests.unit._terraform_fixture import TerraformError

    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path / "refused", **overrides)
    # Terraform hard-wraps its error text, so compare against whitespace-normalised output rather
    # than dropping the assertion to a single word that could match by accident.
    assert expected_message in " ".join(str(raised.value).split())


# ---------------------------------------------------------------------------
# Root wiring. Source assertions, not plan assertions: `infra/` as a whole cannot be planned offline
# (its state backend and several data sources need credentials), so what is checkable here is that the
# root declares the resources and reads the exact output names this module publishes.
# ---------------------------------------------------------------------------

ROOT_TF = Path(__file__).resolve().parents[2] / "infra" / "main.tf"


#: The roles the Runtime resource policies admit: the relay, `sweep_task`, `chat_target`.
RUNTIME_INVOKER_ROLES = (
    "module.agent_relay.role_arn",
    "module.lambda_action_sweep_task.role_arn",
    "module.lambda_action_chat_target.role_arn",
)


def test_the_root_states_exactly_which_roles_may_invoke_the_runtime() -> None:
    """Both Runtime resource policies admit `local.runtime_invoker_role_arns` and nothing else, and
    that local is exactly the relay, `sweep_task` and `chat_target` roles."""
    root = ROOT_TF.read_text(encoding="utf-8")
    assert 'resource "aws_bedrockagentcore_resource_policy" "agent_runtime_relay_only"' in root
    assert 'actions = ["bedrock-agentcore:InvokeAgentRuntime"]' in root
    local = re.search(r"(?ms)^  runtime_invoker_role_arns = \[\n(.*?)^  \]$", root)
    assert local is not None, "no `runtime_invoker_role_arns` local in infra/main.tf"
    members = [line.strip().rstrip(",") for line in local.group(1).splitlines()]
    assert [m for m in members if m and not m.startswith("#")] == list(RUNTIME_INVOKER_ROLES)
    for name in ("agent_runtime_relay_only", "agent_runtime_endpoint_relay_only"):
        document = re.search(
            rf'(?ms)^data "aws_iam_policy_document" "{name}" \{{\n(.*?)^\}}$', root
        )
        assert document is not None, name
        identifiers = re.findall(r"identifiers = (.+)", document.group(1))
        allows = [i for i in identifiers if i.strip() != '["*"]']
        assert allows and set(allows) == {"local.runtime_invoker_role_arns"}, (name, identifiers)


def test_the_root_passes_the_audience_by_constructed_arn_not_by_resource_attribute() -> None:
    """The root builds the audience parameter ARN with `format(` and never reads the parameter
    resource's `.arn`."""
    root = ROOT_TF.read_text(encoding="utf-8")
    # Comments stripped: the forbidden expression may be named in a root comment.
    code = "\n".join(line.split("#", 1)[0] for line in root.splitlines())
    assert "cognito_audience_parameter_arn = format(" in code
    assert "aws_ssm_parameter.relay_cognito_audience.arn" not in code
    assert "value = module.cognito.spa_client_id" in code


def _relay_source_files() -> list[Path]:
    """Every `*.ts` under `src/relay/src`, excluding generated output."""
    return sorted(
        path
        for path in RELAY_SRC.rglob("*.ts")
        if "generated" not in path.relative_to(RELAY_SRC).parts
    )


def test_the_relay_source_file_set_is_discovered_rather_than_assumed() -> None:
    """The discovered relay source set is non-empty, matches the directory listing, and includes the
    modules the environment contract reads."""
    discovered = _relay_source_files()
    assert discovered, f"no .ts sources found under {RELAY_SRC}; the parse would verify nothing"

    on_disk = sorted(
        path
        for path in RELAY_SRC.rglob("*.ts")
        if "generated" not in path.relative_to(RELAY_SRC).parts
    )
    assert discovered == on_disk

    names = {path.name for path in discovered}
    assert {"jwt.ts", "runtime.ts", "parameters.ts"} <= names, (
        f"a module the environment contract reads is missing from {RELAY_SRC}: got {sorted(names)}"
    )


def test_the_environment_surface_matches_what_the_relay_source_reads() -> None:
    """The module sets exactly the `ENV_*` variables the relay source reads, minus Lambda-reserved
    names."""
    declared: dict[str, str] = {}
    for source in _relay_source_files():
        for name, value in re.findall(
            r'export const (ENV_[A-Z_]+) = "([A-Z_]+)"',
            source.read_text(encoding="utf-8"),
        ):
            declared[name] = value

    assert declared, "no ENV_* constants found in the relay source; the parse verified nothing"
    read_by_relay = set(declared.values()) - RESERVED_RUNTIME_VARIABLES
    assert set(declared.values()) & RESERVED_RUNTIME_VARIABLES == RESERVED_RUNTIME_VARIABLES, (
        "the relay no longer reads every reserved runtime variable this carve-out names; shrink "
        f"RESERVED_RUNTIME_VARIABLES rather than leaving a stale exclusion: {sorted(declared.values())}"
    )

    # Direction 1: nothing the relay reads is left unset.
    assert read_by_relay <= EXPECTED_ENVIRONMENT, (
        f"the relay reads variables this module does not set: {sorted(read_by_relay - EXPECTED_ENVIRONMENT)}"
    )

    # Direction 2: nothing set is unread.
    assert EXPECTED_ENVIRONMENT - read_by_relay == set(), (
        "this module sets variables the relay never reads: "
        f"{sorted(EXPECTED_ENVIRONMENT - read_by_relay)}. Either wire the read or drop the variable."
    )


def test_the_root_states_that_only_the_relay_role_may_invoke_the_endpoint_too() -> None:
    """The root declares relay-only resource policies on both the runtime and its `runtime-endpoint`
    ARN."""
    root = ROOT_TF.read_text(encoding="utf-8")
    for name in ("agent_runtime_relay_only", "agent_runtime_endpoint_relay_only"):
        assert f'resource "aws_bedrockagentcore_resource_policy" "{name}"' in root

    code = "\n".join(line.split("#", 1)[0] for line in root.splitlines())
    # The endpoint ARN is constructed; declaring an endpoint resource would create one rather than
    # address the auto-created DEFAULT endpoint.
    assert "/runtime-endpoint/${local.relay_runtime_qualifier}" in code
    assert "/endpoint/${local.relay_runtime_qualifier}" not in code
    assert "aws_bedrockagentcore_agent_runtime_endpoint" not in code
    # One qualifier is shared by the grant's endpoint ARN and the module's env var.
    assert "agent_runtime_qualifier = local.relay_runtime_qualifier" in code


def test_the_root_exports_the_relay_role_arn_for_the_smoke_to_discover() -> None:
    """The root exports `agent_relay_role_arn` from `module.agent_relay.role_arn`."""
    outputs = (Path(__file__).resolve().parents[2] / "infra" / "outputs.tf").read_text(
        encoding="utf-8"
    )
    assert 'output "agent_relay_role_arn"' in outputs
    assert "module.agent_relay.role_arn" in outputs


def test_the_relay_declares_no_lambda_permission_at_all(plan: Plan) -> None:
    """The module plans no `aws_lambda_permission`; the CloudFront grant lives in
    `modules/frontend_cdn`."""
    assert plan.addresses_of_type("aws_lambda_permission") == []


# ---------------------------------------------------------------------------
# The ENI function-code Deny is attached, not inline.
# ---------------------------------------------------------------------------


def test_the_eni_function_code_deny_policy_is_attached_to_the_role(plan: Plan) -> None:
    after = plan.after("module.subject.aws_iam_role_policy_attachment.eni_function_code_deny")
    assert after["policy_arn"] == eni_deny.POLICY_ARN
    assert after["role"] == "crm-agent-eval-agent-relay-role"


def test_vpc_access_grants_exactly_the_eni_actions_the_shared_deny_covers(plan: Plan) -> None:
    granted = eni_deny.allowed_eni_actions(_policy_statements(plan, VPC_ACCESS_POLICY_ADDRESS))
    assert granted == eni_deny.ALLOWED_ENI_ACTIONS, granted


def test_vpc_access_holds_no_deny(plan: Plan) -> None:
    """The Deny stays out of the inline document, which counts toward the role's inline quota."""
    denies = [
        s for s in _policy_statements(plan, VPC_ACCESS_POLICY_ADDRESS) if s.get("Effect") == "Deny"
    ]
    assert not denies, denies

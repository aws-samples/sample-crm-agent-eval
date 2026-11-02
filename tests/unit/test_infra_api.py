# SPDX-License-Identifier: MIT
"""Plan-time assertions on `infra/modules/api`, the private REST API.

Every planned method carries the single Cognito authorizer and comes from the one generated method
resource. The API is `PRIVATE`, bound to the named `execute-api` endpoint, and its resource policy
denies every other `aws:sourceVpce`. Two route inputs are asserted:

* `tool_lambda_invoke_arns` and `tool_lambda_function_names` must carry identical key sets; the root
  passes `crm.toolspec.definitions.ALL_TOOLS` less the agent-only `get_page_context`. No count is
  pinned.
* `extra_routes` is a closed allowlist of non-toolspec routes: `eval_trigger` (POST) and
  `capabilities` (GET). See `EXPECTED_EXTRA_ROUTE_METHODS`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from tests.unit._terraform_fixture import (
    REQUIRE_TERRAFORM_ENV_VAR,
    Plan,
    TerraformError,
    plan_module,
    require_terraform,
    terraform_unavailable_reason,
)

AUTHORIZER_ADDRESS = "module.subject.aws_api_gateway_authorizer.cognito"

#: The allowlisted non-tool routes, as literals.
EXTRA_ROUTE_NAME = "eval_trigger"

#: `GET /api/capabilities`, the only route whose client method is not POST.
CAPABILITIES_ROUTE_NAME = "capabilities"

#: The expected client method per non-tool route. Every tool route is POST.
EXPECTED_EXTRA_ROUTE_METHODS = {
    EXTRA_ROUTE_NAME: "POST",
    CAPABILITIES_ROUTE_NAME: "GET",
    "user_settings": "POST",
}

#: The user pool ARN the COGNITO_USER_POOLS authorizer is given.
USER_POOL_ARN = "arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_fixture"

#: The `execute-api` interface endpoint this private API is reachable through, and the sole entry its
#: resource policy allows.
VPC_ENDPOINT_ID = "vpce-0123456789abcdef0"

#: The CloudWatch Logs CMK the access log group is given.
ACCESS_LOG_KMS_KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/fixture-logs"


def _routed_tool_names() -> list[str]:
    """The routed tool names: `ALL_TOOLS` less the root's `local.agent_only_tools`."""
    src = Path(__file__).resolve().parents[2] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from crm.toolspec.definitions import ALL_TOOLS

    agent_only = {"get_page_context"}
    return sorted(tool.name for tool in ALL_TOOLS if tool.name not in agent_only)


def _map_hcl(entries: dict[str, str]) -> str:
    body = "\n".join(f'    "{key}" = "{value}"' for key, value in sorted(entries.items()))
    return "{\n" + body + "\n  }"


def _invoke_arn(name: str) -> str:
    return (
        "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/"
        f"arn:aws:lambda:us-east-1:123456789012:function:crm-agent-eval-{name}/invocations"
    )


def _module_arguments(
    *,
    tool_names: list[str] | None = None,
    function_names: list[str] | None = None,
    extra_routes: str | None = None,
) -> str:
    names = _routed_tool_names() if tool_names is None else tool_names
    named_functions = names if function_names is None else function_names
    # `eval_trigger` omits `method`, so the main fixture exercises the default-to-POST path.
    extras = (
        f"""{{
    {EXTRA_ROUTE_NAME} = {{
      invoke_arn    = "{_invoke_arn(EXTRA_ROUTE_NAME)}"
      function_name = "crm-agent-eval-{EXTRA_ROUTE_NAME}"
    }}
    {CAPABILITIES_ROUTE_NAME} = {{
      invoke_arn    = "{_invoke_arn(CAPABILITIES_ROUTE_NAME)}"
      function_name = "crm-agent-eval-{CAPABILITIES_ROUTE_NAME}"
      method        = "GET"
    }}
    user_settings = {{
      invoke_arn    = "{_invoke_arn("user_settings")}"
      function_name = "crm-agent-eval-user_settings"
    }}
  }}"""
        if extra_routes is None
        else extra_routes
    )
    lines = [
        f"  tool_lambda_invoke_arns = {_map_hcl({name: _invoke_arn(name) for name in names})}",
        "  tool_lambda_function_names = "
        f"{_map_hcl({name: f'crm-agent-eval-{name}' for name in named_functions})}",
        f"  extra_routes = {extras}",
        f'  cognito_user_pool_arn = "{USER_POOL_ARN}"',
        f'  vpc_endpoint_ids = ["{VPC_ENDPOINT_ID}"]',
        f'  access_log_kms_key_arn = "{ACCESS_LOG_KMS_KEY_ARN}"',
    ]
    return "\n".join(lines)


def _plan(work_dir: Path, **kwargs: object) -> Plan:
    return plan_module(work_dir, "api", _module_arguments(**kwargs))  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> Plan:
    reason = terraform_unavailable_reason()
    if reason is not None:
        message = f"terraform plan harness unavailable: {reason}"
        if require_terraform():
            pytest.fail(f"{message} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(message)
    return _plan(tmp_path_factory.mktemp("api-plan"))


def _route_addresses(plan: Plan) -> list[str]:
    """Planned method addresses; route names come from `_route_names`."""
    return plan.addresses_of_type("aws_api_gateway_method")


def _route_names(plan: Plan) -> set[str]:
    return {
        plan.after(address)["path_part"]
        for address in plan.addresses_of_type("aws_api_gateway_resource")
    }


# ---------------------------------------------------------------------------
# Non-vacuity: the planned route set.
# ---------------------------------------------------------------------------


def test_the_plan_contains_every_expected_route(plan: Plan) -> None:
    """The plan has one route per routed tool plus one per allowlisted non-tool route."""
    expected = set(_routed_tool_names()) | set(EXPECTED_EXTRA_ROUTE_METHODS)
    assert _route_names(plan) == expected
    assert len(expected) == len(_routed_tool_names()) + len(EXPECTED_EXTRA_ROUTE_METHODS)
    # One method per resource.
    assert len(_route_addresses(plan)) == len(expected)


# ---------------------------------------------------------------------------
# The authorizer property, over every route rather than a sampled one.
# ---------------------------------------------------------------------------


def test_every_route_carries_the_cognito_authorizer(plan: Plan) -> None:
    """Every planned method uses `COGNITO_USER_POOLS` and has its route's expected HTTP method."""
    addresses = _route_addresses(plan)
    assert addresses, "no methods planned"
    tool_names = set(_routed_tool_names())
    for address in addresses:
        after = plan.after(address)
        assert after["authorization"] == "COGNITO_USER_POOLS", f"{address} is not authorized"
        name = address.split('["', 1)[1].rstrip('"]')
        expected = "POST" if name in tool_names else EXPECTED_EXTRA_ROUTE_METHODS[name]
        assert after["http_method"] == expected, f"{address} is {after['http_method']}"


def test_there_is_exactly_one_authorizer_and_it_is_the_cognito_one(plan: Plan) -> None:
    """The plan has exactly one authorizer: Cognito, on the pool ARN, reading `Authorization`."""
    assert plan.addresses_of_type("aws_api_gateway_authorizer") == [AUTHORIZER_ADDRESS]
    after = plan.after(AUTHORIZER_ADDRESS)
    assert after["type"] == "COGNITO_USER_POOLS"
    assert after["provider_arns"] == [USER_POOL_ARN]
    # `/api/*` has no OAC, so `Authorization` reaches the authorizer unchanged.
    assert after["identity_source"] == "method.request.header.Authorization"


def test_routes_come_from_exactly_one_resource_block(plan: Plan) -> None:
    """Every planned method is an instance of the single `aws_api_gateway_method.tool` resource."""
    for address in _route_addresses(plan):
        assert address.startswith('module.subject.aws_api_gateway_method.tool["'), address


# ---------------------------------------------------------------------------
# The non-tool route: present, integrated, and invocable.
# ---------------------------------------------------------------------------


def test_the_eval_trigger_route_exists_and_is_authorized(plan: Plan) -> None:
    """The `eval_trigger` route exists as an authorized POST method."""
    resource = plan.after(f'module.subject.aws_api_gateway_resource.tool["{EXTRA_ROUTE_NAME}"]')
    assert resource["path_part"] == EXTRA_ROUTE_NAME
    method = plan.after(f'module.subject.aws_api_gateway_method.tool["{EXTRA_ROUTE_NAME}"]')
    assert method["http_method"] == "POST"
    assert method["authorization"] == "COGNITO_USER_POOLS"


def test_the_eval_trigger_route_has_its_own_integration_and_invoke_permission(plan: Plan) -> None:
    """`eval_trigger` has an `AWS_PROXY` integration and an API Gateway invoke permission."""
    integration = plan.after(
        f'module.subject.aws_api_gateway_integration.tool["{EXTRA_ROUTE_NAME}"]'
    )
    assert integration["type"] == "AWS_PROXY"
    assert integration["integration_http_method"] == "POST"
    assert EXTRA_ROUTE_NAME in integration["uri"]

    permission = plan.after(
        f'module.subject.aws_lambda_permission.apigw_invoke["{EXTRA_ROUTE_NAME}"]'
    )
    assert permission["action"] == "lambda:InvokeFunction"
    assert permission["principal"] == "apigateway.amazonaws.com"
    assert permission["function_name"] == f"crm-agent-eval-{EXTRA_ROUTE_NAME}"


def test_every_route_has_a_matching_invoke_permission(plan: Plan) -> None:
    """The set of route names equals the set of invoke-permission function names."""
    route_names = _route_names(plan)
    permission_names = {
        plan.after(address)["function_name"].removeprefix("crm-agent-eval-")
        for address in plan.addresses_of_type("aws_lambda_permission")
    }
    assert route_names == permission_names


# ---------------------------------------------------------------------------
# Inputs the module must refuse.
# ---------------------------------------------------------------------------


def test_the_module_refuses_a_non_allowlisted_extra_route(tmp_path: Path) -> None:
    """An `extra_routes` name outside the allowlist fails variable validation."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)
    smuggled = """{
    start_live_sweep = {
      invoke_arn    = "arn:aws:apigateway:us-east-1:lambda:path/x/invocations"
      function_name = "crm-agent-eval-start-live-sweep"
    }
  }"""
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path / "refused-extra", extra_routes=smuggled)
    assert "allowlisted non-tool route names" in str(raised.value)


def test_the_module_refuses_tool_maps_whose_key_sets_differ(tmp_path: Path) -> None:
    """A function-name map missing one routed tool fails the REST API key-parity precondition."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)
    with pytest.raises(TerraformError) as raised:
        _plan(tmp_path / "refused-keys", function_names=_routed_tool_names()[:-1])
    assert "identical key sets" in " ".join(str(raised.value).split())


def test_the_module_accepts_any_non_empty_routed_set_with_matching_keys(tmp_path: Path) -> None:
    """With no pinned count, a smaller routed set with matching maps plans exactly those routes."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)
    subset = _routed_tool_names()[:-1]
    planned = _plan(tmp_path / "subset", tool_names=subset)
    assert _route_names(planned) == set(subset) | set(EXPECTED_EXTRA_ROUTE_METHODS)


# ---------------------------------------------------------------------------
# Private endpoint, resource policy, and the authorizer's audience limitation.
# ---------------------------------------------------------------------------


def test_the_api_is_private_and_bound_to_the_named_endpoint(plan: Plan) -> None:
    """The REST API's endpoint type is `PRIVATE` and it is bound to the named VPC endpoint."""
    api = plan.after("module.subject.aws_api_gateway_rest_api.this")
    config = api["endpoint_configuration"][0]
    assert config["types"] == ["PRIVATE"], config["types"]
    assert config["vpc_endpoint_ids"] == [VPC_ENDPOINT_ID]


def test_the_resource_policy_denies_every_other_vpc_endpoint(plan: Plan) -> None:
    """The rendered resource policy denies `execute-api:Invoke` from any other `aws:sourceVpce`."""
    import json

    api = plan.after("module.subject.aws_api_gateway_rest_api.this")
    policy = json.loads(api["policy"])
    statements = policy["Statement"]
    effects = {statement["Effect"] for statement in statements}
    assert effects == {"Allow", "Deny"}, f"expected both an Allow and a Deny, got {effects}"

    deny = next(statement for statement in statements if statement["Effect"] == "Deny")
    assert deny["Action"] == "execute-api:Invoke"
    condition = deny["Condition"]
    assert "StringNotEquals" in condition, condition
    # A single-valued condition may render as a bare string or a one-element list.
    source_vpce = condition["StringNotEquals"]["aws:sourceVpce"]
    if isinstance(source_vpce, str):
        source_vpce = [source_vpce]
    assert source_vpce == [VPC_ENDPOINT_ID]


def test_the_audience_regression_is_visible_in_source_not_silently_absent() -> None:
    """No `cognito_user_pool_client_ids` variable exists, and `main.tf` discloses the unchecked audience.

    A `COGNITO_USER_POOLS` authorizer does not validate the app-client audience.
    """
    module_dir = Path(__file__).resolve().parents[2] / "infra" / "modules" / "api"
    variables = (module_dir / "variables.tf").read_text(encoding="utf-8")
    assert 'variable "cognito_user_pool_client_ids"' not in variables, (
        "the audience variable is declared again; if the check was restored, update this test — if it "
        "was not, an unread input implies an audience check that does not happen"
    )
    main = (module_dir / "main.tf").read_text(encoding="utf-8")
    assert "does not" in main and "audience" in main.lower(), (
        "the module no longer discloses that the app-client audience is unchecked; a regression nobody "
        "can read is worse than the regression"
    )


# ---------------------------------------------------------------------------
# `GET /api/capabilities`.
# ---------------------------------------------------------------------------


def test_the_capabilities_route_exists_and_is_a_get(plan: Plan) -> None:
    """The `capabilities` route is an authorized GET with an authorizer attached."""
    resource = plan.after(
        f'module.subject.aws_api_gateway_resource.tool["{CAPABILITIES_ROUTE_NAME}"]'
    )
    assert resource["path_part"] == CAPABILITIES_ROUTE_NAME
    address = f'module.subject.aws_api_gateway_method.tool["{CAPABILITIES_ROUTE_NAME}"]'
    method = plan.after(address)
    assert method["http_method"] == "GET"
    assert method["authorization"] == "COGNITO_USER_POOLS"
    # `authorizer_id` is known only after apply, so it appears in `after_unknown`.
    unknown = plan.resource_changes[address]["change"]["after_unknown"]
    assert unknown.get("authorizer_id") is True


def test_the_capabilities_route_keeps_a_post_lambda_invocation(plan: Plan) -> None:
    """The GET `capabilities` integration still invokes the Lambda with POST (`AWS_PROXY`)."""
    integration = plan.after(
        f'module.subject.aws_api_gateway_integration.tool["{CAPABILITIES_ROUTE_NAME}"]'
    )
    assert integration["type"] == "AWS_PROXY"
    assert integration["integration_http_method"] == "POST"
    assert integration["http_method"] == "GET", "the client-facing half must follow the method"
    assert CAPABILITIES_ROUTE_NAME in integration["uri"]

    permission = plan.after(
        f'module.subject.aws_lambda_permission.apigw_invoke["{CAPABILITIES_ROUTE_NAME}"]'
    )
    assert permission["action"] == "lambda:InvokeFunction"


def test_the_capabilities_route_is_an_instance_of_the_single_method_resource(plan: Plan) -> None:
    """The `capabilities` method is an instance of the single method resource."""
    address = f'module.subject.aws_api_gateway_method.tool["{CAPABILITIES_ROUTE_NAME}"]'
    assert address in _route_addresses(plan)
    assert len({a.split('["', 1)[0] for a in _route_addresses(plan)}) == 1


def test_the_routed_tools_and_extra_routes_are_disjoint_and_together_planned(plan: Plan) -> None:
    """The toolspec-derived routes and the extra routes are disjoint and together planned."""
    tool_names = set(_routed_tool_names())
    assert not tool_names & set(EXPECTED_EXTRA_ROUTE_METHODS), (
        "a non-tool route leaked into the toolspec-derived set"
    )
    assert _route_names(plan) == tool_names | set(EXPECTED_EXTRA_ROUTE_METHODS)


def test_an_extra_route_that_omits_its_method_defaults_to_post(plan: Plan) -> None:
    """An extra route declared without `method` (`eval_trigger`) is planned as POST."""
    method = plan.after(f'module.subject.aws_api_gateway_method.tool["{EXTRA_ROUTE_NAME}"]')
    assert method["http_method"] == "POST"


# --- Mutation controls. ---


def test_mutation_a_capabilities_route_reverted_to_post_is_detectable(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """With `capabilities` declared POST, the planned method is POST and still authorized."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)
    reverted = f"""{{
    {CAPABILITIES_ROUTE_NAME} = {{
      invoke_arn    = "{_invoke_arn(CAPABILITIES_ROUTE_NAME)}"
      function_name = "crm-agent-eval-{CAPABILITIES_ROUTE_NAME}"
      method        = "POST"
    }}
  }}"""
    mutated = _plan(tmp_path_factory.mktemp("api-post"), extra_routes=reverted)
    method = mutated.after(
        f'module.subject.aws_api_gateway_method.tool["{CAPABILITIES_ROUTE_NAME}"]'
    )
    assert method["http_method"] == "POST", "the mutation did not take, so this control is vacuous"
    assert method["authorization"] == "COGNITO_USER_POOLS", (
        "even the reverted route must stay authorized -- the authorizer is not what varies here"
    )


def test_mutation_dropping_the_extra_routes_merge_removes_the_route(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """With `extra_routes = {}` the plan has only the routed tool routes."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)
    mutated = _plan(tmp_path_factory.mktemp("api-noextra"), extra_routes="{}")
    assert _route_names(mutated) == set(_routed_tool_names())
    assert len(_route_addresses(mutated)) == len(_routed_tool_names())
    # `Plan.after` raises `AssertionError` on an absent address.
    with pytest.raises(AssertionError):
        mutated.after(f'module.subject.aws_api_gateway_method.tool["{CAPABILITIES_ROUTE_NAME}"]')


def test_mutation_an_unlisted_extra_route_name_is_refused_by_the_allowlist(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """An arbitrary extra-route name is refused by the allowlist validation."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)
    smuggled = f"""{{
    admin_backdoor = {{
      invoke_arn    = "{_invoke_arn("admin_backdoor")}"
      function_name = "crm-agent-eval-admin_backdoor"
      method        = "GET"
    }}
  }}"""
    with pytest.raises(TerraformError) as excinfo:
        _plan(tmp_path_factory.mktemp("api-smuggle"), extra_routes=smuggled)
    assert "allowlisted non-tool route names" in str(excinfo.value)


def test_mutation_an_unsupported_method_is_refused(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """An extra-route `method` other than GET or POST fails variable validation."""
    reason = terraform_unavailable_reason()
    if reason is not None:
        if require_terraform():
            pytest.fail(f"{reason} (and {REQUIRE_TERRAFORM_ENV_VAR}=1 forbids skipping)")
        pytest.skip(reason)
    bad = f"""{{
    {CAPABILITIES_ROUTE_NAME} = {{
      invoke_arn    = "{_invoke_arn(CAPABILITIES_ROUTE_NAME)}"
      function_name = "crm-agent-eval-{CAPABILITIES_ROUTE_NAME}"
      method        = "PATCH"
    }}
  }}"""
    with pytest.raises(TerraformError) as excinfo:
        _plan(tmp_path_factory.mktemp("api-patch"), extra_routes=bad)
    assert "must be GET or POST" in str(excinfo.value)


def test_every_route_has_exactly_one_invoke_grant(plan: Plan) -> None:
    """Each route has exactly one invoke grant and each grant has a route."""
    routes = {address.split('["', 1)[1].rstrip('"]') for address in _route_addresses(plan)}
    granted = {
        address.split('["', 1)[1].rstrip('"]')
        for address in plan.addresses_of_type("aws_lambda_permission")
    }
    assert routes, "no methods planned"
    assert granted == routes, (
        f"routes without a grant: {sorted(routes - granted)}; "
        f"grants without a route: {sorted(granted - routes)}"
    )


def test_the_invoke_grant_derives_its_method_from_the_same_map_as_the_route(plan: Plan) -> None:
    """The invoke grant's `source_arn` takes its method from `local.route_methods[each.key]`.

    Read from source, since `source_arn` is unknown at plan time.
    """
    source = (Path(__file__).resolve().parents[2] / "infra/modules/api/main.tf").read_text()
    grant_block = source.split('resource "aws_lambda_permission" "apigw_invoke"', 1)
    assert len(grant_block) == 2, "the invoke-grant resource is not in this module any more"
    body = grant_block[1].split("\n}", 1)[0]
    arn_lines = [line for line in body.splitlines() if "source_arn" in line]
    assert len(arn_lines) == 1, f"expected one source_arn assignment, got {arn_lines}"
    arn = arn_lines[0]
    assert "local.route_methods[each.key]" in arn, (
        f"the invoke grant does not derive its method from local.route_methods: {arn.strip()!r}"
    )
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY"):
        assert f"/{method}/" not in arn, (
            f"the invoke grant hardcodes /{method}/, so it cannot follow its route: {arn.strip()!r}"
        )
    assert "/*/*" not in arn, f"the invoke grant is method-wildcarded: {arn.strip()!r}"


def test_the_invoke_grants_method_is_unknown_at_plan_time_which_is_why_source_is_read(
    plan: Plan,
) -> None:
    """The invoke grant's `source_arn` is unknown at plan time."""
    address = f'module.subject.aws_lambda_permission.apigw_invoke["{CAPABILITIES_ROUTE_NAME}"]'
    change = plan.resource_changes[address]["change"]
    assert change["after_unknown"].get("source_arn") is True
    assert "source_arn" not in (change.get("after") or {})


# ---------------------------------------------------------------------------
# Request validators: an edge envelope check; per-field validation stays in the Lambda.
# ---------------------------------------------------------------------------


def test_exactly_one_request_validator_with_both_flags_enabled(plan: Plan) -> None:
    """There is one request validator with body and parameter validation both enabled."""
    addresses = plan.addresses_of_type("aws_api_gateway_request_validator")
    assert len(addresses) == 1, addresses
    after = plan.after(addresses[0])
    assert after["validate_request_body"] is True, after
    assert after["validate_request_parameters"] is True, after


def test_every_method_references_the_request_validator(plan: Plan) -> None:
    """Every method has a `request_validator_id`, known or deferred to apply."""
    addresses = _route_addresses(plan)
    assert addresses, "no methods planned; the assertion would pass vacuously"
    for address in addresses:
        known = plan.after(address).get("request_validator_id")
        deferred = plan.after_unknown(address).get("request_validator_id") is True
        assert known or deferred, (
            f"{address} references no request validator (known={known!r}, deferred={deferred})"
        )


def test_post_methods_carry_the_object_model_and_the_get_does_not(plan: Plan) -> None:
    """POST methods carry the `JsonObject` model and the GET method carries none."""
    saw_post = saw_get = False
    for address in _route_addresses(plan):
        after = plan.after(address)
        models = after.get("request_models") or {}
        if after["http_method"] == "POST":
            saw_post = True
            assert models.get("application/json") == "JsonObject", (address, models)
        else:
            saw_get = True
            assert "application/json" not in models, (address, models)
    assert saw_post and saw_get, "expected both POST routes and the GET /capabilities route"


def test_the_object_model_declares_no_properties(plan: Plan) -> None:
    """The single API model is a `{"type": "object"}` schema with no `properties`."""
    import json

    addresses = plan.addresses_of_type("aws_api_gateway_model")
    assert len(addresses) == 1, addresses
    schema = json.loads(plan.after(addresses[0])["schema"])
    assert schema.get("type") == "object", schema
    assert "properties" not in schema, schema


def test_the_deployment_trigger_folds_in_the_validation_surface() -> None:
    """`deployment_trigger` folds in `request_validation_trigger` (both flags and the model).

    Read from source, since the trigger is a sha1 hash.
    """
    source = (Path(__file__).resolve().parents[2] / "infra/modules/api/main.tf").read_text()
    trigger_block = source.split("deployment_trigger = sha1(jsonencode([", 1)[1].split("]))", 1)[0]
    assert "local.request_validation_trigger" in trigger_block, (
        "deployment_trigger does not fold in request_validation_trigger; a validator/model change "
        "would not redeploy the stage"
    )
    validation_local = source.split("request_validation_trigger = {", 1)[1].split("}", 1)[0]
    assert "validate_request_body" in validation_local, validation_local
    assert "validate_request_parameters" in validation_local, validation_local
    assert "json_object.schema" in validation_local, validation_local


# ---------------------------------------------------------------------------
# Root wiring, read from `infra/main.tf`.
# ---------------------------------------------------------------------------


_ROOT_TF = Path(__file__).resolve().parents[2] / "infra" / "main.tf"


def _root_extra_routes() -> dict[str, str]:
    """`{route name: method}` from the root's `extra_routes`; an omitted method resolves to POST."""
    block = re.search(
        r"extra_routes\s*=\s*\{(.*?)\n  \}", _ROOT_TF.read_text(encoding="utf-8"), re.DOTALL
    )
    assert block is not None, (
        f"no `extra_routes` block in {_ROOT_TF}; the root's route wiring cannot be derived, so "
        "failing rather than passing over an unread composition"
    )
    routes: dict[str, str] = {}
    for entry in re.finditer(
        r"^\s{4}(?P<name>\w+)\s*=\s*\{(?P<inner>.*?)^\s{4}\}",
        block.group(1),
        re.DOTALL | re.MULTILINE,
    ):
        declared = re.search(r'method\s*=\s*"(?P<method>\w+)"', entry.group("inner"))
        routes[entry.group("name")] = declared.group("method") if declared else "POST"
    assert routes, "parsed no entries from the root's `extra_routes`"
    return routes


def test_the_root_wires_the_capabilities_route_as_a_get() -> None:
    """The root wires `capabilities` with `method = "GET"`."""
    routes = _root_extra_routes()
    assert routes.get(CAPABILITIES_ROUTE_NAME) == "GET", (
        f"the root wires {CAPABILITIES_ROUTE_NAME} as {routes.get(CAPABILITIES_ROUTE_NAME)!r}; the "
        "SPA fetches it with GET and a POST-only route answers that with 403"
    )


def test_the_root_wires_eval_trigger_without_a_method_so_it_defaults_to_post() -> None:
    """The root wires `eval_trigger` without a method, so it resolves to POST."""
    assert _root_extra_routes().get(EXTRA_ROUTE_NAME) == "POST"


def test_the_roots_extra_routes_match_the_modules_allowlist_exactly() -> None:
    """The root's `extra_routes` names equal the module's allowlist, in both directions."""
    allowlisted = re.search(
        r"setsubtract\(keys\(var\.extra_routes\),\s*\[(?P<names>[^\]]*)\]\)",
        (Path(__file__).resolve().parents[2] / "infra/modules/api/variables.tf").read_text(
            encoding="utf-8"
        ),
    )
    assert allowlisted is not None, "the extra_routes allowlist validation was not found"
    admitted = set(re.findall(r'"(\w+)"', allowlisted.group("names")))
    assert admitted == set(EXPECTED_EXTRA_ROUTE_METHODS), (
        f"the module admits {sorted(admitted)} but this file expects "
        f"{sorted(EXPECTED_EXTRA_ROUTE_METHODS)}"
    )
    assert set(_root_extra_routes()) == admitted, (
        f"the root wires {sorted(_root_extra_routes())} but the module admits {sorted(admitted)}"
    )


def test_the_access_log_group_is_encrypted_with_the_given_cmk(plan: Plan) -> None:
    """The access log group's `kms_key_id` is the `access_log_kms_key_arn` the module is given."""
    after = plan.after("module.subject.aws_cloudwatch_log_group.access_logs")
    assert after["kms_key_id"] == ACCESS_LOG_KMS_KEY_ARN


def test_the_stage_traces_with_xray(plan: Plan) -> None:
    """The stage samples requests into X-Ray."""
    assert plan.after("module.subject.aws_api_gateway_stage.this")["xray_tracing_enabled"] is True

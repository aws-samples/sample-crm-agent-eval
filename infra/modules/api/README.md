# `api` module

Private REST API (API Gateway v1) fronting the routed CRM tools plus an allowlisted set of
non-tool routes. Each route is served at `/<stage>/<route_name>`, sits behind a Cognito user pool
authorizer, and inherits the stage's throttle/burst limits, CloudWatch access logging and X-Ray
tracing. Execution logging and method-level metrics are off: execution logging would write an API
Gateway-owned log group outside the project CMK, retention and teardown.

See also: the [documentation map](../../../README.md#documentation), the
[runbook](../../../docs/runbook.md#post-deploy-checks-for-api) and the [security
baseline](../../../docs/security-baseline.md). Modules named here:
[`cognito`](../cognito/README.md), [`frontend_cdn`](../frontend_cdn/README.md),
[`lambda_action`](../lambda_action/README.md), [`observability`](../observability/README.md),
[`vpc`](../vpc/README.md).

## Routes and the authorizer-coverage guarantee

Every route resource, method, integration, Lambda permission and authorizer attachment is generated
by one `for_each` over the merge of `var.tool_lambda_invoke_arns` and `var.extra_routes`. The module
has exactly one `aws_api_gateway_method` resource and the authorizer reference lives inside it, so no
route can exist without the authorizer.

The routed tool set is every tool in `crm.toolspec.definitions.ALL_TOOLS` less the root's
`local.agent_only_tools` (today `get_page_context`), i.e. `local.routed_tool_names` in
`infra/main.tf`. List it from there rather than from a copy here.

Every tool route is `POST`. Only an `extra_routes` entry may set a different client method, via its
optional `method` (`GET` or `POST`, default `POST`); `capabilities` is the one `GET` route, called by
the SPA from `frontend/src/components/tuning/capabilityMatrix.ts`. To vary a method, set it on the
`extra_routes` entry; do not add a second method resource.

`aws_api_gateway_integration.integration_http_method` is always `POST`. That is the method API
Gateway uses to invoke the Lambda under `AWS_PROXY`, not the client method; setting it to `GET`
breaks the route at runtime.

Every method references one request validator, `aws_api_gateway_request_validator.body_and_params`
(`body-and-params`, validating the body and parameters). POST methods also carry the `JsonObject`
model for `application/json`, which checks only that the body is a JSON object. Field-level
validation happens inside each action Lambda against the tool's `input_schema`
(`additionalProperties: false`).

The plan-time authorizer-coverage assertion lives in `scripts/check_plan_security_baseline.py`.

## Network exposure

The API has no public hostname. Two controls apply, and both are required:

| Control | Refuses |
|---|---|
| `endpoint_configuration.types = ["PRIVATE"]` | every caller outside the VPC |
| Resource policy statement `DenyInvokeFromAnyOtherVpcEndpoint` (`aws:sourceVpce` not in `var.vpc_endpoint_ids`) | callers arriving through any other interface endpoint, including one in another account's VPC |

The API is addressable only as `https://<api-id>-<vpc-endpoint-id>.execute-api.<region>.amazonaws.com`
through the endpoints in `var.vpc_endpoint_ids`. CloudFront reaches it through a VPC origin to an
internal ALB (`modules/api_origin`, [`modules/frontend_cdn`](../frontend_cdn/README.md)).

The Cognito authorizer is a separate control that establishes who is calling. A
`COGNITO_USER_POOLS` authorizer checks that the token was issued by the configured pool; it does not
check the app-client audience.

`stage_invoke_url` is not publicly callable. A check that reaches it from outside the VPC has not
tested this API.

## Inputs

| Variable | Required | Notes |
|---|---|---|
| `tool_lambda_invoke_arns` | yes | `tool_name -> invoke ARN`, from `module.lambda_action[...]`. Must be non-empty. |
| `tool_lambda_function_names` | yes | `tool_name -> function name`. Must carry exactly the keys of `tool_lambda_invoke_arns` (REST API precondition). |
| `extra_routes` | no (default `{}`) | `name -> { invoke_arn, function_name, method = "POST" }`. Keys restricted to `eval_trigger`, `capabilities` and `user_settings`; `method` restricted to `GET`/`POST`. |
| `cognito_user_pool_arn` | yes | The `cognito` module's `user_pool_arn`. Validated as a `cognito-idp` ARN: a pool id creates the authorizer but refuses every request at runtime. |
| `vpc_endpoint_ids` | yes | `execute-api` interface endpoint ids from `module.vpc`. At least one; each must match `vpce-` plus 8 or 17 hex digits. Sets the endpoint association and the resource-policy allowlist. |
| `api_name` | no (default `crm-agent-eval-api`) | |
| `stage_name` | no (default `prod`) | |
| `throttle_burst_limit` | no (default `50`) | Stage-wide method setting. |
| `throttle_rate_limit` | no (default `25`) | Requests per second. |
| `log_retention_days` | no (default `365`) | Access log group retention; minimum 365 (validated). |
| `access_log_kms_key_arn` | yes | CMK encrypting the access log group; the root passes `module.observability.cloudwatch_logs_kms_key_arn`. |
| `tags` | no (default `{}`) | Added to the provider's `default_tags`. |

## Outputs

| Output | Description |
|---|---|
| `api_id` | Private REST API id. |
| `private_origin_domain_name` | `<api-id>-<vpc-endpoint-id>.execute-api.<region>.amazonaws.com`, using the first entry of `vpc_endpoint_ids`. The CloudFront `/api/*` origin's `domain_name`; needs no custom domain and no `x-apigw-api-id` header. |
| `stage_invoke_url` | Stage invoke URL, reachable only from inside the VPC through a named endpoint. |
| `stage_name` | Stage name; routes are served beneath it. |
| `execution_arn` | API execution ARN. |
| `authorizer_id` | The single Cognito authorizer every method references. |
| `route_tool_names` | Sorted tool names routes were generated for. |
| `access_log_group_name` | Access log group name. |
| `vpc_endpoint_ids` | Echo of the associated endpoint ids. |

There is no `api_endpoint` output: a private REST API has no such attribute.

## Verification

```bash
cd infra && terraform fmt -check -recursive && terraform validate
cd infra/modules/api && terraform init -backend=false && terraform validate
```

These are syntax and type checks over the repository. Against a deployed stack,
`scripts/smoke/public_exposure.py` (`rest_api_private_vpce_only`) checks the endpoint type and
resource policy, and `scripts/smoke/api_routes.py` checks that every method carries the Cognito
authorizer and that an unauthenticated call through CloudFront is refused.

`scripts/check_no_public_exposure.py`'s `api-default-endpoint` control inspects
`aws_apigatewayv2_api` blocks only, so it says nothing about this REST API's endpoint type.

## Out of scope

- Cognito user pool and clients: [`modules/cognito`](../cognito/README.md).
- The `execute-api` interface VPC endpoint: [`modules/vpc`](../vpc/README.md).
- The internal ALB and CloudFront VPC origin carrying `/api/*`: `modules/api_origin`,
  [`modules/frontend_cdn`](../frontend_cdn/README.md).

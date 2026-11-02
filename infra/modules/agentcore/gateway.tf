/**
 * `aws_bedrockagentcore_gateway` with `authorizer_type = "CUSTOM_JWT"` and a REQUEST interceptor
 * pointed at `identity_interceptor`. Arguments match the pinned 6.71.0 provider schema:
 * `authorizer_type`, `authorizer_configuration.custom_jwt_authorizer.{discovery_url,
 * allowed_clients, allowed_audience, allowed_scopes, custom_claim}`,
 * `interceptor_configuration.{interception_points, input_configuration.pass_request_headers,
 * interceptor.lambda.arn}`.
 *
 * `pass_request_headers = true` hands the interceptor the raw inbound `Authorization` bearer token,
 * which it needs to derive the caller's identity. The interceptor re-verifies the token against
 * Cognito's JWKS (`src/gateway/identity_interceptor.py`) rather than trusting it.
 */

resource "aws_bedrockagentcore_gateway" "this" {
  name            = var.gateway_name
  role_arn        = var.gateway_role_arn
  authorizer_type = "CUSTOM_JWT"
  region          = var.aws_region

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url   = var.cognito_discovery_url
      allowed_clients = var.cognito_allowed_client_ids
      # Not set: a Cognito `client_credentials` access token carries no `aud` claim, so any
      # `allowed_audience` value rejects every valid token. `allowed_clients` and `allowed_scopes`
      # still constrain the caller.
      allowed_audience = null
      allowed_scopes   = var.cognito_allowed_scopes

      dynamic "custom_claim" {
        for_each = var.cognito_required_custom_claims

        content {
          inbound_token_claim_name       = custom_claim.value.claim_name
          inbound_token_claim_value_type = "STRING"

          authorizing_claim_match_value {
            claim_match_operator = "EQUALS"

            claim_match_value {
              match_value_string = custom_claim.value.required_value
            }
          }
        }
      }
    }
  }

  interceptor_configuration {
    interception_points = ["REQUEST"]

    input_configuration {
      pass_request_headers = true
    }

    interceptor {
      lambda {
        arn = var.identity_interceptor_lambda_arn
      }
    }
  }

  tags = var.tags
}

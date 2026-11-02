/**
 * CloudFront distribution with three origins: the private S3 bucket holding the SPA build (OAC),
 * the relay's `AWS_IAM` Lambda Function URL (OAC), and the private REST API through
 * `modules/api_origin`'s VPC origin. The `/api/*` and `/agent/*` behaviours disable caching and
 * forward the viewer's headers.
 *
 * The relay's OAC signs with `signing_behavior = "always"`, which overwrites the viewer's
 * `Authorization` header, so the Cognito JWT travels in `X-Crm-Authorization`. Viewer TLS is
 * described in the README.
 */

resource "aws_cloudfront_origin_access_control" "origin" {
  name                              = "${var.name_prefix}frontend-cdn-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_origin_access_control" "agent_relay" {
  name                              = "${var.name_prefix}agent-relay-oac"
  description                       = "SigV4-signs origin requests to the relay's AWS_IAM Function URL"
  origin_access_control_origin_type = "lambda"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Lets this distribution, and only it, invoke the relay's Function URL.
resource "aws_lambda_permission" "agent_relay_function_url" {
  statement_id           = "AllowCloudFrontOacInvokeFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = var.agent_relay_function_name
  principal              = "cloudfront.amazonaws.com"
  source_arn             = aws_cloudfront_distribution.frontend.arn
  function_url_auth_type = "AWS_IAM"
}

# The function-invocation half of the OAC-to-Lambda grant: an `AWS_IAM` Function URL needs both
# `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` for `cloudfront.amazonaws.com`.
resource "aws_lambda_permission" "agent_relay_invoke_function" {
  statement_id  = "AllowCloudFrontOacInvokeFunction"
  action        = "lambda:InvokeFunction"
  function_name = var.agent_relay_function_name
  principal     = "cloudfront.amazonaws.com"
  source_arn    = aws_cloudfront_distribution.frontend.arn
}

# AWS managed cache policy "CachingDisabled" (4135ea2d-6df8-44a3-9df3-4b5a84be39ad), used on
# /api/* and /agent/* so no identity-bearing response is cached.
data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

# AWS managed origin-request policy "AllViewerExceptHostHeader"
# (b689b0a8-53d0-40ab-baf2-68738e2966ac): forwards every viewer header except Host, plus all query
# strings and cookies. "AllViewer" would forward Host too, which breaks custom-origin routing.
data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

# `/agent/*`'s origin-request policy: every viewer header except `Host`, all cookies, all query
# strings. Declared in source so tests can assert that `X-Crm-Authorization` and
# `x-amz-content-sha256` (the body hash OAC requires on a POST) are forwarded.
resource "aws_cloudfront_origin_request_policy" "agent_relay" {
  name    = "${var.name_prefix}agent-relay-origin-request"
  comment = "Forward every viewer header except Host to the relay origin"

  headers_config {
    header_behavior = "allExcept"

    headers {
      items = ["host"]
    }
  }

  cookies_config {
    cookie_behavior = "all"
  }

  query_strings_config {
    query_string_behavior = "all"
  }
}

# AWS managed response-headers policy "SecurityHeadersPolicy" (67f7725c-6f97-4210-82d7-5512b31e9d03):
# HSTS, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` and `X-XSS-Protection` on every
# behaviour. It adds headers only, so the `/agent/*` SSE stream is not buffered.
data "aws_cloudfront_response_headers_policy" "security_headers" {
  name = "Managed-SecurityHeadersPolicy"
}

# AWS managed cache policy "CachingOptimized" for the default (static asset) behaviour.
data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}

# The `/api/*` behaviour's viewer-request URI rewrite: replaces the leading `/api` segment with
# `/<stage>`, since the REST API serves its routes beneath its stage. Headers, method, query string
# and body are untouched.
resource "aws_cloudfront_function" "api_path_rewrite" {
  name    = "${var.name_prefix}api-path-rewrite"
  runtime = "cloudfront-js-2.0"
  comment = "Rewrite /api/* onto the REST API's stage path"
  publish = true
  code = templatefile("${path.module}/functions/api_path_rewrite.js", {
    api_gateway_stage_name = var.api_gateway_stage_name
  })
}

# The default (S3) behaviour's viewer-request rewrite, serving `/index.html` for client-side routes.
resource "aws_cloudfront_function" "spa_history_fallback" {
  name    = "${var.name_prefix}spa-history-fallback"
  runtime = "cloudfront-js-2.0"
  comment = "Serve /index.html for SPA client-side routes"
  publish = true
  code    = file("${path.module}/functions/spa_history_fallback.js")
}

resource "aws_cloudfront_distribution" "frontend" {
  # checkov:skip=CKV_AWS_68: no WAF on the distribution, an open known limitation
  # checkov:skip=CKV2_AWS_47: no WAF on the distribution, an open known limitation
  # checkov:skip=CKV_AWS_174: default *.cloudfront.net certificate; TLS 1.2 minimum is not enforced, an open known limitation
  # checkov:skip=CKV2_AWS_42: default *.cloudfront.net certificate; no custom domain
  # checkov:skip=CKV_AWS_374: no geographic restriction requirement
  # checkov:skip=CKV_AWS_310: single-region origins; no failover origin exists
  enabled             = true
  comment             = "${var.name_prefix}crm frontend"
  default_root_object = "index.html"
  price_class         = "PriceClass_100"

  origin {
    domain_name              = aws_s3_bucket.origin.bucket_regional_domain_name
    origin_id                = "s3-frontend-origin"
    origin_access_control_id = aws_cloudfront_origin_access_control.origin.id
  }

  # The private REST API, through `modules/api_origin`'s VPC origin, which routes to the internal ALB
  # by ARN. `domain_name` is only the `Host` header (`<api-id>-<vpce-id>.execute-api.<region>.amazonaws.com`).
  # Ports, protocol policy and origin SSL protocols are set on the VPC origin, not here.
  origin {
    domain_name = var.api_gateway_domain_name
    origin_id   = "api-gateway-origin"

    vpc_origin_config {
      vpc_origin_id = var.api_origin_vpc_origin_id
    }
  }

  # The relay's `AWS_IAM` Function URL, reached through this distribution's OAC. OAC establishes only
  # that a request came through this distribution; the caller's identity is the Cognito JWT in
  # `X-Crm-Authorization`.
  origin {
    domain_name              = var.agent_relay_function_url_domain
    origin_id                = "agent-relay-origin"
    origin_access_control_id = aws_cloudfront_origin_access_control.agent_relay.id

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      # Inter-packet; the Runtime's 20 s idle heartbeat keeps a 600 s turn inside it.
      origin_read_timeout = 60
    }
  }

  # Default behaviour: static frontend assets from the private S3 origin, cached normally.
  default_cache_behavior {
    target_origin_id       = "s3-frontend-origin"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = data.aws_cloudfront_cache_policy.caching_optimized.id

    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security_headers.id

    # Serves `/index.html` for client-side routes. A distribution-wide `custom_error_response` would
    # also rewrite `/api/*`'s 403s into 200s.
    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_history_fallback.arn
    }
  }

  # /api/* -> the private REST API. Caching disabled; `Authorization` forwarded.
  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = "api-gateway-origin"
    viewer_protocol_policy   = "https-only"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id

    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security_headers.id

    # See `aws_cloudfront_function.api_path_rewrite`.
    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.api_path_rewrite.arn
    }
  }

  # /agent/* -> the relay's Function URL. Caching disabled and viewer headers forwarded, plus
  # `compress = false` (CloudFront gzip buffers the response, holding every SSE frame) and no
  # viewer-request function (the relay does not route on path).
  ordered_cache_behavior {
    path_pattern             = "/agent/*"
    target_origin_id         = "agent-relay-origin"
    viewer_protocol_policy   = "https-only"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = false
    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = aws_cloudfront_origin_request_policy.agent_relay.id

    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security_headers.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # No custom certificate: CloudFront serves the default `*.cloudfront.net` certificate, for which
  # `minimum_protocol_version` is not honoured. See the README.
  viewer_certificate {
    cloudfront_default_certificate = true
  }

  logging_config {
    bucket          = aws_s3_bucket.logs.bucket_domain_name
    prefix          = "cloudfront-access-logs/"
    include_cookies = false
  }

  tags = {
    "data-classification" = var.data_classification
  }
}

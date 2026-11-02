/**
 * The private path from CloudFront to the PRIVATE REST API: CloudFront VPC origin -> internal ALB
 * -> target group of the `execute-api` interface endpoint's ENI IPs -> REST API. This module owns the
 * ALB, its security group, listener and target group, and the `aws_cloudfront_vpc_origin`; the
 * interface endpoint is in `modules/vpc`.
 *
 * An ALB because a CloudFront VPC origin must be an ALB, NLB or EC2 instance. The CloudFront->ALB
 * hop is plain HTTP inside the VPC; the ALB->endpoint hop is HTTPS on 443.
 */

# ---------------------------------------------------------------------------
# The endpoint's ENI addresses
# ---------------------------------------------------------------------------

# The target group needs the endpoint's ENI addresses. They arrive as `var.execute_api_target_ips`,
# looked up by the root, so this module plans without credentials.

# ---------------------------------------------------------------------------
# Security group
# ---------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-api-origin-alb"
  description = "Internal ALB fronting the private REST API for the CloudFront /api/* VPC origin"
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-api-origin-alb" })
}

# Ingress is from security groups only, never a prefix list or CIDR: a VPC origin reaches the ALB
# from ENIs inside the VPC, and `check_plan_security_baseline`'s `security_group_zero_inbound`
# rejects a network source. The source group is a required variable with no default.
resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  # checkov:skip=CKV_AWS_260: the source is a referenced security group, never a CIDR
  for_each = var.alb_ingress_security_group_ids

  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = var.listener_port
  to_port                      = var.listener_port
  description                  = "CloudFront VPC origin to internal ALB (${var.listener_port})"
}

# Egress on 443 to the shared interface-endpoint security group, so it reaches every endpoint on
# that group, not `execute-api` alone.
resource "aws_vpc_security_group_egress_rule" "alb_to_endpoint" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = var.execute_api_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "internal ALB to execute-api interface endpoint (443, TLS)"
}

# ---------------------------------------------------------------------------
# Internal ALB
# ---------------------------------------------------------------------------

# `internal = true`: an internet-facing ALB would be a public entry point bypassing CloudFront.
resource "aws_lb" "this" {
  # checkov:skip=CKV2_AWS_20: internal ALB reached only by the CloudFront VPC origin over HTTP inside the VPC; no HTTPS listener to redirect to
  name               = "${var.name_prefix}-api-origin"
  internal           = true
  load_balancer_type = "application"
  subnets            = var.private_subnet_ids
  security_groups    = [aws_security_group.alb.id]

  drop_invalid_header_fields = true
  enable_deletion_protection = true

  # The destination bucket's policy must grant `logdelivery.elasticloadbalancing.amazonaws.com`
  # `s3:PutObject` on `<bucket>/<prefix>/AWSLogs/<account>/*`, or `aws_lb` creation fails: ELB writes
  # a test object. The prefix is an input so it matches the grant.
  access_logs {
    bucket  = var.access_logs_bucket
    prefix  = var.access_logs_prefix
    enabled = true
  }

  tags = var.tags
}

resource "aws_lb_target_group" "execute_api" {
  name        = "${var.name_prefix}-execute-api"
  target_type = "ip"
  vpc_id      = var.vpc_id
  port        = 443
  protocol    = "HTTPS"

  # The endpoint's certificate is for `*.execute-api.<region>.amazonaws.com`, which an IP target
  # cannot match, so this hop is encrypted but the certificate is not verified.
  health_check {
    protocol = "HTTPS"
    path     = "/"
    # `/` on an `execute-api` endpoint answers 403 (no unauthenticated route), so 403 is healthy.
    matcher             = "200,403"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  tags = var.tags
}

# One attachment per endpoint ENI. `count` rather than `for_each`: the addresses are unknown at plan
# time, their number is not.
resource "aws_lb_target_group_attachment" "execute_api" {
  count = length(var.private_subnet_ids)

  target_group_arn = aws_lb_target_group.execute_api.arn
  target_id        = var.execute_api_target_ips[count.index]
  port             = 443

  # No `availability_zone`: the targets are in-VPC addresses, for which the API rejects one.
}

resource "aws_lb_listener" "this" {
  # checkov:skip=CKV_AWS_2: CloudFront VPC origin to internal ALB is plain HTTP inside the VPC, see this file's header
  # checkov:skip=CKV_AWS_103: HTTP listener, so no TLS policy applies; see this file's header
  load_balancer_arn = aws_lb.this.arn
  port              = var.listener_port
  protocol          = var.listener_protocol

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.execute_api.arn
  }

  tags = var.tags
}

# ---------------------------------------------------------------------------
# CloudFront VPC origin
# ---------------------------------------------------------------------------

# The distribution references this by id and reaches the ALB by ARN, with no DNS lookup on the
# origin's domain name, so the `/api/*` origin's `domain_name` serves only as the `Host` header.
resource "aws_cloudfront_vpc_origin" "this" {
  vpc_origin_endpoint_config {
    name                   = "${var.name_prefix}-api-origin"
    arn                    = aws_lb.this.arn
    http_port              = var.listener_port
    https_port             = 443
    origin_protocol_policy = var.origin_protocol_policy

    # Required by the provider even under `http-only`, where it is unused; set to the TLS 1.2 floor.
    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }

  tags = var.tags
}

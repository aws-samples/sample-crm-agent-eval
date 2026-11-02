/**
 * `aws_bedrockagentcore_agent_runtime`, running the CodeBuild-built image. Arguments match the
 * pinned `hashicorp/aws` 6.71.0 provider schema. Inbound auth is IAM/SigV4; the relay is the only
 * caller.
 *
 * No `capacity_provider_configuration` is set (the 6.71.0 schema has none), so the runtime uses
 * consumption-mode microVM billing.
 */

resource "aws_bedrockagentcore_agent_runtime" "this" {
  agent_runtime_name = var.agent_runtime_name
  role_arn           = var.agent_runtime_role_arn
  region             = var.aws_region
  platform_version   = var.platform_version

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.agent.repository_url}:${var.image_tag}"
    }
  }

  # VPC mode places the microVM ENIs in the Runtime subnets with the Runtime security group. It
  # governs egress only; inbound is controlled by the resource policies at the root.
  # `network_mode_config` is required when network_mode is VPC.
  network_configuration {
    network_mode = var.runtime_network_mode

    dynamic "network_mode_config" {
      for_each = var.runtime_network_mode == "VPC" ? [1] : []
      content {
        subnets         = var.runtime_subnet_ids
        security_groups = var.runtime_security_group_ids
      }
    }
  }

  # IAM/SigV4 inbound auth is selected by omitting `authorizer_configuration`.
  # `request_header_configuration` is also omitted: under IAM auth a forwarded `Authorization`
  # header would be unvalidated. Identity reaches the container in the signed payload.

  environment_variables = var.agent_runtime_environment_variables

  tags = var.tags

  depends_on = [null_resource.image_build]

  # The provider waits for READY (observed twice) on create and update, bounded by these.
  timeouts {
    create = "30m"
    update = "30m"
  }

  lifecycle {
    # A plan-time precondition over two variables.
    precondition {
      condition = var.runtime_network_mode != "VPC" || (
        length(var.runtime_subnet_ids) >= 1 && length(var.runtime_security_group_ids) >= 1
      )
      error_message = "runtime_network_mode is \"VPC\" but runtime_subnet_ids or runtime_security_group_ids is empty. VPC mode needs at least one owner-created Runtime subnet and the dedicated Runtime security group."
    }
  }
}

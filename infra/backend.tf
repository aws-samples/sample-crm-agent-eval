/**
 * Terraform root: provider requirements and a partial S3 state backend. The backend settings come
 * from the gitignored `backend.hcl` (written by `infra/bootstrap_state_backend.sh`) at
 * `terraform init -backend-config=backend.hcl`. The AWS provider is pinned to an exact version
 * because the `agentcore` module needs its `aws_bedrockagentcore_*` resources.
 */

terraform {
  required_version = ">= 1.17.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.71.0"
    }
  }

  backend "s3" {}
}

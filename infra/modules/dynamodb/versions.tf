/**
 * Module-local provider pin: the same exact `hashicorp/aws` version as `infra/backend.tf`, so
 * this module can be validated in isolation.
 */

terraform {
  required_version = ">= 1.17.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.71.0"
    }
  }
}

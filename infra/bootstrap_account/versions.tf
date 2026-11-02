/**
 * Account bootstrap root: the network the stack adopts and the account-wide API Gateway CloudWatch
 * Logs role. Applied once per account by `scripts/bootstrap_account.sh`, before the stack; outlives it.
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

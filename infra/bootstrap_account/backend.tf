# Partial backend: `scripts/bootstrap_account.sh` supplies `../backend.hcl` and the state key.
terraform {
  backend "s3" {}
}

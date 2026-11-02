provider "aws" {
  profile = var.aws_profile
  region  = var.aws_region

  default_tags {
    tags = {
      service               = var.service
      environment           = var.environment
      owner                 = var.owner
      "cost-center"         = var.cost_center
      "data-classification" = var.data_classification
      component             = "bootstrap-account"
      AppName               = "crm-agent-eval"
    }
  }
}

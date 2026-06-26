# OpenTofu / Terraform >= 1.6, AWS provider 5.x. Region comes from the AWS_REGION env var (the
# same .env the pipeline uses), so it is not set here. State is local for now — see README.md.
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  default_tags {
    tags = {
      Project   = "bii"
      ManagedBy = "opentofu"
    }
  }
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

# Terraform state lives in the versioned bucket that the bootstrap script creates
# (<project>-tfstate, prefix terraform/state). The bucket name is passed at init time:
#   terraform init -backend-config="bucket=<project>-tfstate"
# `terraform init -backend=false` still validates without it.
terraform {
  backend "gcs" {
    prefix = "terraform/state"
  }
}

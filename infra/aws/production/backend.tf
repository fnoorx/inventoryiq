# The state bucket and region come from an ignored backend.hcl:
#   terraform init -backend-config=backend.hcl
# Copy backend.hcl.example to backend.hcl and fill in your values.
terraform {
  backend "s3" {
    key          = "production/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}

variable "aws_region" {
  description = "AWS region for the deployment."
  type        = string
  default     = "ca-central-1"
}

variable "project" {
  description = "Project name used in resource names and tags."
  type        = string
  default     = "inventoryiq"
}

variable "owner" {
  description = "Person or team responsible for the deployment (resource tag)."
  type        = string
}

variable "aws_account_id" {
  description = "AWS account ID used in the state bucket name."
  type        = string
}

variable "state_bucket_prefix" {
  description = "Prefix for the Terraform state bucket; the account ID is appended."
  type        = string
  default     = "inventoryiq-terraform-state"
}

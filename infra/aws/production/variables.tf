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
  description = "AWS account hosting the production deployment."
  type        = string
}

variable "vpc_cidr" {
  description = "Private IPv4 address range for the deployment VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "availability_zones" {
  description = "Availability Zones used for public and database subnets."
  type        = list(string)
  default     = ["ca-central-1a", "ca-central-1b"]
}

variable "app_settings" {
  description = "Non-secret bot settings such as *_CHANNEL_ID and INVENTORY_DEFAULT_LOCATION. Credentials belong in Secrets Manager."
  type        = map(string)
  default     = {}
}

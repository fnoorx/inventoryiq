output "state_bucket_name" {
  description = "S3 bucket used to store Terraform state."
  value       = aws_s3_bucket.terraform_state.id
}

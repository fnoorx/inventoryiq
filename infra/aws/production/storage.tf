resource "aws_s3_bucket" "deployment" {
  bucket = "${var.project}-production-${var.aws_account_id}"
}

resource "aws_s3_bucket_public_access_block" "deployment" {
  bucket                  = aws_s3_bucket.deployment.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "deployment" {
  bucket = aws_s3_bucket.deployment.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_policy" "deployment" {
  bucket = aws_s3_bucket.deployment.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.deployment.arn, "${aws_s3_bucket.deployment.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_s3_bucket_lifecycle_configuration" "deployment" {
  bucket = aws_s3_bucket.deployment.id
  rule {
    id     = "expire-migration-staging"
    status = "Enabled"
    filter { prefix = "migration/" }
    expiration { days = 7 }
  }
  rule {
    id     = "expire-database-backups"
    status = "Enabled"
    filter { prefix = "database-backups/" }
    expiration { days = 90 }
  }
}

resource "aws_iam_role_policy" "task_storage" {
  name = "deployment-storage"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${aws_s3_bucket.deployment.arn}/migration/*"] },
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${aws_s3_bucket.deployment.arn}/database-backups/*"] },
    ]
  })
}

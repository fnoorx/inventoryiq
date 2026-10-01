output "vpc_id" {
  description = "VPC containing the production deployment."
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "Public subnets for Fargate tasks."
  value       = aws_subnet.public[*].id
}

output "database_subnet_ids" {
  description = "Isolated subnets for the RDS subnet group."
  value       = aws_subnet.database[*].id
}

output "ecs_security_group_id" {
  description = "Security group for the bot tasks."
  value       = aws_security_group.ecs.id
}

output "database_security_group_id" {
  description = "Security group for PostgreSQL."
  value       = aws_security_group.database.id
}

output "database_address" {
  description = "Private DNS hostname for PostgreSQL."
  value       = aws_db_instance.main.address
}

output "database_port" {
  description = "PostgreSQL connection port."
  value       = aws_db_instance.main.port
}

output "database_name" {
  description = "Initial PostgreSQL database name."
  value       = aws_db_instance.main.db_name
}

output "database_master_secret_arn" {
  description = "ARN of the RDS-managed master credentials secret, not its contents."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "application_secret_arn" {
  description = "Secret ARN to set as APPLICATION_SECRET_ARN in the bot task."
  value       = aws_secretsmanager_secret.application.arn
}

output "stockx_token_secret_arn" {
  description = "Secret ARN to set as STOCKX_TOKEN_SECRET_ARN in the bot task."
  value       = aws_secretsmanager_secret.stockx_tokens.arn
}

output "ecs_execution_role_arn" {
  description = "Role used by ECS for image pulls, logs, and startup secret injection."
  value       = aws_iam_role.ecs_execution.arn
}

output "ecs_task_role_arn" {
  description = "Role used by application code for secret reads and StockX token writes."
  value       = aws_iam_role.ecs_task.arn
}

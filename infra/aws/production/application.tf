variable "image_tag" {
  description = "Immutable image tag published to ECR."
  type        = string
  default     = "deployment-pending"
}

variable "bot_enabled" {
  description = "Enable only after the database transfer and verification."
  type        = bool
  default     = false
}

resource "aws_ecr_repository" "bot" {
  name                 = var.project
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_cloudwatch_log_group" "bot" {
  name              = "/ecs/${var.project}-production"
  retention_in_days = 30
}

resource "aws_ecs_cluster" "main" {
  name = "${var.project}-production"
}

resource "aws_ecs_task_definition" "bot" {
  family                   = "${var.project}-production"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name        = "bot"
    image       = "${aws_ecr_repository.bot.repository_url}:${var.image_tag}"
    essential   = true
    stopTimeout = 60
    environment = concat([
      { name = "ENABLE_LIVE_INTEGRATIONS", value = "true" },
      { name = "CREDENTIALS_BACKEND", value = "aws" },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "APPLICATION_SECRET_ARN", value = aws_secretsmanager_secret.application.arn },
      { name = "STOCKX_TOKEN_SECRET_ARN", value = aws_secretsmanager_secret.stockx_tokens.arn },
      { name = "DATABASE_SECRET_ARN", value = aws_db_instance.main.master_user_secret[0].secret_arn },
      { name = "DATABASE_HOST", value = aws_db_instance.main.address },
      { name = "DATABASE_NAME", value = aws_db_instance.main.db_name },
      { name = "DEPLOYMENT_BUCKET", value = aws_s3_bucket.deployment.id },
    ], [for name, value in var.app_settings : { name = name, value = value }])
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.bot.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }
  }])
}

resource "aws_ecs_service" "bot" {
  name                               = var.project
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.bot.arn
  desired_count                      = var.bot_enabled ? 1 : 0
  launch_type                        = "FARGATE"
  platform_version                   = "1.4.0"
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = true
  }
  depends_on = [aws_iam_role_policy_attachment.ecs_execution, aws_iam_role_policy.task_secrets]
}

output "ecr_repository_url" { value = aws_ecr_repository.bot.repository_url }
output "ecs_cluster_name" { value = aws_ecs_cluster.main.name }
output "ecs_service_name" { value = aws_ecs_service.bot.name }
output "ecs_task_definition_arn" { value = aws_ecs_task_definition.bot.arn }
output "deployment_bucket" { value = aws_s3_bucket.deployment.id }

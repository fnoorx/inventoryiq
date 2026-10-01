resource "aws_secretsmanager_secret" "application" {
  name        = "${var.project}/production/application"
  description = "Application credentials supplied separately from Terraform."
}

resource "aws_secretsmanager_secret" "stockx_tokens" {
  name        = "${var.project}/production/stockx-tokens"
  description = "Current StockX access and refresh tokens, updated by the bot."
}

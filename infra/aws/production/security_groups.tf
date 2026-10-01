resource "aws_security_group" "ecs" {
  name        = "${var.project}-production-ecs"
  description = "Outbound bot connections; no inbound connections."
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "database" {
  name        = "${var.project}-production-database"
  description = "PostgreSQL connections from the bot only."
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_egress_rule" "ecs_https" {
  security_group_id = aws_security_group.ecs.id
  description       = "HTTPS to Discord, StockX, Google, and AWS services."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "ecs_postgres" {
  security_group_id            = aws_security_group.ecs.id
  description                  = "PostgreSQL to the database security group."
  referenced_security_group_id = aws_security_group.database.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "database_postgres" {
  security_group_id            = aws_security_group.database.id
  description                  = "PostgreSQL from the bot security group."
  referenced_security_group_id = aws_security_group.ecs.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

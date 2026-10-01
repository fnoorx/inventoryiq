resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-production"
  subnet_ids = aws_subnet.database[*].id

  tags = { Name = "${var.project}-production-database" }
}

resource "aws_db_instance" "main" {
  identifier = "${var.project}-production"
  engine     = "postgres"

  engine_version              = "17"
  instance_class              = "db.t4g.micro"
  db_name                     = "inventoryiq"
  username                    = "inventory_admin"
  manage_master_user_password = true

  allocated_storage     = 20
  max_allocated_storage = 25
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false
  multi_az               = false
  port                   = 5432

  backup_retention_period    = 7
  backup_window              = "07:00-07:30"
  maintenance_window         = "sun:08:00-sun:08:30"
  auto_minor_version_upgrade = true
  engine_lifecycle_support   = "open-source-rds-extended-support-disabled"
  copy_tags_to_snapshot      = true

  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.project}-production-final"

  tags = { Name = "${var.project}-production" }
}

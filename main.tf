data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_subnet" "first" {
  id = data.aws_subnets.default.ids[0]
}

# Secrets
resource "random_password" "rcon" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "rcon_password" {
  name = "${var.server_name}-rcon-password"

  tags = {
    Project = "mc-server"
  }
}

resource "aws_secretsmanager_secret_version" "rcon_password" {
  secret_id     = aws_secretsmanager_secret.rcon_password.id
  secret_string = random_password.rcon.result
}

resource "aws_secretsmanager_secret" "discord_signing_key" {
  name = "${var.server_name}-discord-signing-key"

  tags = {
    Project = "mc-server"
  }
}

# NOTE: The Discord public key must be set manually after creation.
# Copy the public key hex from Discord Developer Portal → Application → General Information:
#   aws secretsmanager update-secret --secret-id <arn> --secret-string '{"public_key":"<hex>"}'
# See docs/runbook.md for instructions.

resource "aws_secretsmanager_secret" "enzy_api_key" {
  name = "${var.server_name}-enzy-api-key"

  tags = {
    Project = "mc-server"
  }
}

# NOTE: The Enzy X-Secret-Token must be set manually after creation (the value
# is never committed to this repo). The export Lambda reads it at runtime:
#   aws secretsmanager put-secret-value --secret-id <arn> --secret-string '<token>'
# See docs/stats-leaderboard.md §5.

# Network module
module "network" {
  source = "./modules/network"

  server_name = var.server_name
  vpc_id      = data.aws_vpc.default.id
}

# Compute module
module "compute" {
  source = "./modules/compute"

  server_name         = var.server_name
  instance_type       = var.instance_type
  minecraft_version   = var.minecraft_version
  minecraft_memory    = var.minecraft_memory
  minecraft_seed      = var.minecraft_seed
  security_group_id   = module.network.security_group_id
  eip_allocation_id   = module.network.eip_allocation_id
  subnet_id           = data.aws_subnet.first.id
  rcon_password       = random_password.rcon.result
  whitelist_seed      = var.whitelist_seed
  stats_bucket_name   = module.stats.bucket_name
  stats_bucket_arn    = module.stats.bucket_arn
  idle_stop_minutes   = var.idle_stop_minutes
  discord_webhook_url = var.discord_webhook_url
}

# Storage module
module "storage" {
  source = "./modules/storage"

  server_name = var.server_name
  volume_size = var.mc_volume_size
  volume_type = var.mc_volume_type
  # Source AZ from the subnet (static) rather than the instance (computed).
  # Otherwise, any user_data change becomes a data-destroying apply because the
  # instance is replaced with AZ "known after apply", forcing volume replacement.
  availability_zone = data.aws_subnet.first.availability_zone
  instance_id       = module.compute.instance_id
}

# Control module
module "control" {
  source = "./modules/control"

  server_name                    = var.server_name
  instance_id                    = module.compute.instance_id
  discord_signing_key_secret_arn = aws_secretsmanager_secret.discord_signing_key.arn
  rcon_password_secret_arn       = aws_secretsmanager_secret.rcon_password.arn
  discord_webhook_url            = var.discord_webhook_url
  idle_stop_alarm_name           = "${var.server_name}-idle-stop"
  admin_discord_user_ids         = var.admin_discord_user_ids
  world_profiles                 = var.world_profiles
  email_map_parameter_name       = module.stats.email_map_parameter_name
  email_map_parameter_arn        = module.stats.email_map_parameter_arn
  stats_bucket_name              = module.stats.bucket_name
  stats_bucket_arn               = module.stats.bucket_arn
}

# World-manager web UI module
module "webui" {
  source = "./modules/webui"

  server_name              = var.server_name
  instance_id              = module.compute.instance_id
  rcon_password_secret_arn = aws_secretsmanager_secret.rcon_password.arn
  idle_stop_alarm_name     = "${var.server_name}-idle-stop"
}

# DNS module
module "dns" {
  source = "./modules/dns"

  server_name = var.server_name
  domain_name = var.domain_name
  eip_address = module.network.eip_address
}

# Stats export module
module "stats" {
  source = "./modules/stats"

  server_name         = var.server_name
  enzy_secret_arn     = aws_secretsmanager_secret.enzy_api_key.arn
  enzy_base_url       = var.enzy_base_url
  schedule_expression = var.stats_export_schedule
  dry_run             = var.stats_export_dry_run
}

# Monitoring module
module "monitoring" {
  source = "./modules/monitoring"

  server_name               = var.server_name
  instance_id               = module.compute.instance_id
  idle_stop_minutes         = var.idle_stop_minutes
  stop_lambda_function_arn  = module.control.function_arn
  stop_lambda_function_name = module.control.function_name
}

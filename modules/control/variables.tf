variable "server_name" {
  description = "Server name used for naming prefix"
  type        = string
}

variable "instance_id" {
  description = "EC2 instance ID to control"
  type        = string
}

variable "discord_signing_key_secret_arn" {
  description = "ARN of the Secrets Manager secret containing Discord Ed25519 signing key"
  type        = string
}

variable "rcon_password_secret_arn" {
  description = "ARN of the Secrets Manager secret containing the RCON password"
  type        = string
}

variable "discord_webhook_url" {
  description = "Discord webhook URL for idle-stop notifications"
  type        = string
  default     = ""
  sensitive   = true
}

variable "idle_stop_alarm_name" {
  description = "Name of the CloudWatch idle-stop alarm that Lambda resets to OK on /mc start"
  type        = string
}

variable "admin_discord_user_ids" {
  description = "Discord user IDs allowed to run admin-gated subcommands (e.g. /mc whitelist remove). Empty list denies all."
  type        = list(string)
  default     = []
}

variable "world_profiles" {
  description = "Known Minecraft world profiles; seeds the world-list SSM param that /mc world reads. First entry is the boot default."
  type        = list(string)
  default     = ["survival"]
}

variable "email_map_parameter_name" {
  description = "Name of the SSM parameter holding the UUID→email map; /mc register reads and writes it."
  type        = string
}

variable "email_map_parameter_arn" {
  description = "ARN of the UUID→email map SSM parameter, used to scope the controller Lambda's SSM grant."
  type        = string
}

variable "stats_bucket_name" {
  description = "Name of the stats export bucket; /mc register and /mc whitelist add seed a zero baseline into its state/ object."
  type        = string
}

variable "stats_bucket_arn" {
  description = "ARN of the stats export bucket, used to scope the controller Lambda's state-object read/write grant."
  type        = string
}

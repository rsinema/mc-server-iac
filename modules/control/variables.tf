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

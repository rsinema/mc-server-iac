variable "server_name" {
  description = "Prefix for resources and the SSM world-param paths (matches the root server_name)."
  type        = string
}

variable "instance_id" {
  description = "EC2 instance ID the web UI starts/stops and reports on."
  type        = string
}

variable "rcon_password_secret_arn" {
  description = "ARN of the RCON password secret (read-only, for the player count in status)."
  type        = string
}

variable "idle_stop_alarm_name" {
  description = "Name of the idle-stop CloudWatch alarm, reset when the UI starts the server."
  type        = string
}

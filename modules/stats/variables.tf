variable "server_name" {
  description = "Server name used for naming prefix"
  type        = string
}

variable "enzy_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the Enzy X-Secret-Token (created in the root module, populated out-of-band)"
  type        = string
}

variable "enzy_base_url" {
  description = "Base URL of the Enzy API. Override to target staging."
  type        = string
  default     = "https://api.enzy.co"
}

variable "schedule_expression" {
  description = "EventBridge schedule for the daily export. Default 11:00 UTC ≈ early-morning Mountain time (after late-night sessions have ended and synced)."
  type        = string
  default     = "cron(0 11 * * ? *)"
}

variable "dry_run" {
  description = "Seed for the runtime push toggle ONLY (the push-enabled SSM parameter is created from this once, then ignore_changes leaves it to the toggle). true = seed dry-run/off, false = seed live/on. After the parameter exists, flip it at runtime with `aws ssm put-parameter` — changing this variable no longer has any effect. The first real POST permanently locks the Enzy column set."
  type        = bool
  default     = true
}

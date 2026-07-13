variable "aws_region" {
  description = "AWS region to deploy resources"
  type        = string
  default     = "us-west-2"
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "m7g.large"
}

variable "server_name" {
  description = "Value of the Name tag and prefix for resources"
  type        = string
  default     = "MCServerInstance"
}

variable "mc_volume_size" {
  description = "Size of the EBS data volume in GB. Sized for multiple world profiles under /opt/minecraft/worlds/ (see docs/multi-world.md)."
  type        = number
  default     = 25
}

variable "world_profiles" {
  description = "Known Minecraft world profiles (e.g. [\"survival\", \"skyblock\"]). Seeds the world-list SSM param that /mc world list reads and /mc world set validates against. The first entry is the default the server boots if the active-world param is unset or invalid."
  type        = list(string)
  default     = ["survival"]
}

variable "mc_volume_type" {
  description = "Type of EBS volume"
  type        = string
  default     = "gp3"
}

variable "minecraft_version" {
  description = "Minecraft server version to run"
  type        = string
  default     = "26.2"
}

variable "minecraft_memory" {
  description = "Memory allocation for Minecraft server in GB"
  type        = number
  default     = 6
}

variable "minecraft_seed" {
  description = "World seed passed to the Minecraft server. Only takes effect when the world is generated; once /opt/minecraft/world exists, the seed is baked into level.dat and this value is ignored. Leave empty for a random seed."
  type        = string
  default     = "enzy-minecraft"
}

variable "owner_tag" {
  description = "Value for the Owner tag on all resources"
  type        = string
  default     = "rsinema"
}

variable "domain_name" {
  description = "Root domain for DNS (e.g. rsinema.com)"
  type        = string
  default     = "rsinema.com"
}

variable "idle_stop_minutes" {
  description = "Minutes of idle time before auto-stop"
  type        = number
  default     = 15
}

variable "discord_webhook_url" {
  description = "Discord webhook URL for idle-stop notifications (optional)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "whitelist_seed" {
  description = "Mojang usernames seeded into the Minecraft whitelist on first boot. Set at least your own username before applying, since ENFORCE_WHITELIST=TRUE is on and no one is joinable without being listed."
  type        = list(string)
  default     = []
}

variable "admin_discord_user_ids" {
  description = "Discord user IDs (snowflakes, as strings) permitted to run admin-gated commands like /mc whitelist remove. Populate via terraform.tfvars; if empty, admin-gated commands are denied to everyone."
  type        = list(string)
  default     = []
}

variable "enzy_base_url" {
  description = "Base URL of the Enzy API for the stats export. Override to target staging."
  type        = string
  default     = "https://api.enzy.co"
}

variable "stats_export_schedule" {
  description = "EventBridge schedule expression for the daily stats export. Default 11:00 UTC ≈ early-morning Mountain time."
  type        = string
  default     = "cron(0 11 * * ? *)"
}

variable "stats_export_dry_run" {
  description = "When true (default), the stats export Lambda logs payloads but does not POST to Enzy. Set false in terraform.tfvars to go live — the first real POST permanently locks the Enzy column set."
  type        = bool
  default     = true
}

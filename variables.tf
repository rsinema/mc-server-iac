variable "aws_region" {
  description = "AWS region to deploy resources"
  type        = string
  default     = "us-west-2"
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t4g.large"
}

variable "server_name" {
  description = "Value of the Name tag and prefix for resources"
  type        = string
  default     = "MCServerInstance"
}

variable "mc_volume_size" {
  description = "Size of the EBS data volume in GB"
  type        = number
  default     = 10
}

variable "mc_volume_type" {
  description = "Type of EBS volume"
  type        = string
  default     = "gp3"
}

variable "minecraft_version" {
  description = "Minecraft server version to run"
  type        = string
  default     = "1.21.5"
}

variable "minecraft_memory" {
  description = "Memory allocation for Minecraft server in GB"
  type        = number
  default     = 6
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

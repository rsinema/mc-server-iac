variable "server_name" {
  description = "Server name used for naming prefix"
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t4g.large"
}

variable "minecraft_version" {
  description = "Minecraft server version"
  type        = string
  default     = "1.21.5"
}

variable "minecraft_memory" {
  description = "Memory allocation for Minecraft in GB"
  type        = number
  default     = 6
}

variable "security_group_id" {
  description = "Security group ID for the EC2 instance"
  type        = string
}

variable "eip_allocation_id" {
  description = "EIP allocation ID for association"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID to deploy into"
  type        = string
}

variable "rcon_password" {
  description = "RCON password for the Minecraft server"
  type        = string
  sensitive   = true
}

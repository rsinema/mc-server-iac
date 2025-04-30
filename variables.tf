variable "aws_region" {
  description = "AWS region to deploy resources"
  type        = string
  default     = "us-west-2"
}

variable "instance_type" {
  description = "Type of EC2 instance to launch"
  type        = string
  default     = "t4g.large" # arm64 compatible - 2 vCPUs, 8GB mem, 0.067 USD/hr
}

variable "server_name" {
  description = "Value of the Name tag for the EC2 instance"
  type        = string
  default     = "MCServerInstance"
}

variable "server_ami" {
  description = "Value of the AMI to use for the EC2 instance"
  type        = string
  default     = "ami-0d5dcd1555c7fb494" # amzn linux 2 arm64
}

variable "mc_volume_size" {
  description = "Size of the EBS volume to attach to the EC2 instance"
  type        = number
  default     = 10
}

variable "mc_volume_type" {
  description = "Type of EBS volume to attach to the EC2 instance"
  type        = string
  default     = "gp3"
}

variable "home_ip" {
  description = "Public IP of the home network"
  type        = string
  default     = "0.0.0.0" # placeholder ip
}

variable "ssh_key" {
  description = "Path to the SSH key to use for the EC2 instance"
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "api_key" {
  description = "API key for the MC server"
  type        = string
  default     = "c77354f0-313a-4114-8baa-6fbfc6891247"
}

variable "minecraft_version" {
  description = "Version of Minecraft server to install"
  type        = string
  default     = "1.21.5" # Current stable version as of March 2025
}

variable "minecraft_download_url" {
  description = "URL to download Minecraft server JAR"
  type        = string
  default     = "https://piston-data.mojang.com/v1/objects/8f3112a1049751cc472ec13e397eade5336ca7ae/server.jar"
}

variable "minecraft_memory" {
  description = "Memory allocation for Minecraft server (in GB)"
  type        = number
  default     = 6
}

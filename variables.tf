variable "aws_region" {
  description = "AWS region to deploy resources"
  type        = string
  default     = "us-west-2"
}

variable "instance_type" {
  description = "Type of EC2 instance to launch"
  type        = string
  default     = "t4g.small" # arm64 compatible - 2 vCPUs, 2GB mem, 0.0168 USD/hr
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
  default     = "38.70.245.100"
}

variable "ssh_key" {
  description = "Path to the SSH key to use for the EC2 instance"
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}
variable "server_name" {
  description = "Server name used for naming prefix"
  type        = string
}

variable "volume_size" {
  description = "Size of the EBS data volume in GB"
  type        = number
  default     = 10
}

variable "volume_type" {
  description = "EBS volume type"
  type        = string
  default     = "gp3"
}

variable "availability_zone" {
  description = "Availability zone for the EBS volume"
  type        = string
}

variable "instance_id" {
  description = "Instance ID to attach the volume to"
  type        = string
}

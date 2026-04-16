variable "server_name" {
  description = "Server name used for naming prefix"
  type        = string
}

variable "domain_name" {
  description = "Root domain for the DNS record (e.g. rsinema.com)"
  type        = string
}

variable "eip_address" {
  description = "EIP address to point the DNS record at"
  type        = string
}

variable "subdomain" {
  description = "Subdomain prefix (e.g. 'mc' for mc.rsinema.com)"
  type        = string
  default     = "mc"
}

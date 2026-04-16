output "hostname" {
  description = "Full hostname of the Minecraft server DNS record"
  value       = "${var.subdomain}.${var.domain_name}"
}

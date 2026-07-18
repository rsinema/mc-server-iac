output "function_url" {
  description = "Lambda Function URL for server control"
  value       = module.control.function_url
}

output "function_name" {
  description = "Name of the server controller Lambda function"
  value       = module.control.function_name
}

output "webui_url" {
  description = "World-manager web UI URL (open in a browser; enter the shared token)"
  value       = module.webui.function_url
}

output "webui_token_secret_arn" {
  description = "ARN of the web UI bearer-token secret; set its value out-of-band before first use"
  value       = module.webui.token_secret_arn
}

output "instance_id" {
  description = "ID of the EC2 Minecraft server instance"
  value       = module.compute.instance_id
}

output "eip_address" {
  description = "Elastic IP address assigned to the server"
  value       = module.network.eip_address
}

output "server_hostname" {
  description = "DNS hostname of the Minecraft server"
  value       = module.dns.hostname
}

output "security_group_id" {
  description = "Security group ID of the Minecraft server"
  value       = module.network.security_group_id
}

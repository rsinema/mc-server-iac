output "security_group_id" {
  description = "ID of the Minecraft server security group"
  value       = aws_security_group.mc_server_sg.id
}

output "eip_allocation_id" {
  description = "Allocation ID of the EIP"
  value       = aws_eip.mc_server.id
}

output "eip_address" {
  description = "Public IP address of the EIP"
  value       = aws_eip.mc_server.public_ip
}

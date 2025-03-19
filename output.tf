output "instance_id" {
  description = "ID of the EC2 instance"
  value       = aws_instance.mc_server.id
}

output "instance_public_ip" {
  description = "Public IP address of the EC2 instance"
  value       = aws_instance.mc_server.public_ip
}

output "instance_ami" {
  description = "AMI ID of the EC2 instance"
  value       = aws_instance.mc_server.ami
}
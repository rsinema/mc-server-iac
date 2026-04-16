output "instance_id" {
  description = "ID of the EC2 instance"
  value       = aws_instance.mc_server.id
}

output "availability_zone" {
  description = "Availability zone of the EC2 instance"
  value       = aws_instance.mc_server.availability_zone
}

output "public_ip" {
  description = "Public IP address of the EC2 instance"
  value       = aws_instance.mc_server.public_ip
}

output "instance_profile_name" {
  description = "Name of the IAM instance profile"
  value       = aws_iam_instance_profile.ec2_instance.name
}

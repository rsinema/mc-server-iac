output "volume_id" {
  description = "ID of the EBS data volume"
  value       = aws_ebs_volume.mc_data.id
}

output "bucket_name" {
  description = "Name of the stats staging/state bucket"
  value       = aws_s3_bucket.stats.bucket
}

output "bucket_arn" {
  description = "ARN of the stats bucket (used to scope the EC2 instance's PutObject grant)"
  value       = aws_s3_bucket.stats.arn
}

output "email_map_parameter_name" {
  description = "SSM parameter holding the UUID→email map; written by the control Lambda's /mc register"
  value       = aws_ssm_parameter.email_map.name
}

output "email_map_parameter_arn" {
  description = "ARN of the UUID→email map parameter (used to scope the control Lambda's SSM grant)"
  value       = aws_ssm_parameter.email_map.arn
}

output "push_enabled_parameter_name" {
  description = "SSM parameter that toggles the Enzy POST on/off at runtime (aws ssm put-parameter --value true|false, no redeploy)"
  value       = aws_ssm_parameter.push_enabled.name
}

output "export_function_name" {
  description = "Name of the stats export Lambda (for manual/dry-run invocation)"
  value       = aws_lambda_function.export.function_name
}

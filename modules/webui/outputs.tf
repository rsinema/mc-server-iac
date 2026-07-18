output "function_url" {
  description = "Web UI URL — open in a browser and enter the shared token."
  value       = aws_lambda_function_url.world_manager.function_url
}

output "function_name" {
  description = "Name of the world-manager Lambda function."
  value       = aws_lambda_function.world_manager.function_name
}

output "token_secret_arn" {
  description = "ARN of the webui bearer-token secret. Set its value with: aws secretsmanager put-secret-value --secret-id <arn> --secret-string \"$(openssl rand -hex 32)\""
  value       = aws_secretsmanager_secret.webui_token.arn
}

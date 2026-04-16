output "function_url" {
  description = "Lambda Function URL for the server controller"
  value       = aws_lambda_function_url.server_controller.function_url
}

output "function_name" {
  description = "Name of the server controller Lambda function"
  value       = aws_lambda_function.server_controller.function_name
}

output "function_arn" {
  description = "ARN of the server controller Lambda function"
  value       = aws_lambda_function.server_controller.arn
}

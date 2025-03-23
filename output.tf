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

output "lambda_function_name" {
  description = "Name of the Lambda function"
  value       = aws_lambda_function.server_controller.function_name
}

output "lambda_test_command" {
  description = "AWS CLI command to test the Lambda function"
  value       = <<-EOT
    # Test the Lambda function with AWS CLI

    # To test the Lambda function, create a payload.json file with the following content:
    # {
    #   "action": "<start|stop|status>"
    # }

    # Then run the following command to invoke the Lambda function:
    aws lambda invoke --function-name ${aws_lambda_function.server_controller.function_name} \
      --payload fileb://payload.json \
      response.json && cat response.json
  EOT
}

output "api_gateway_url" {
  description = "URL of the API Gateway endpoint"
  value       = "${aws_api_gateway_deployment.api_deployment.invoke_url}/server"
}

output "api_curl_commands" {
  description = "Curl commands to test the API Gateway endpoints"
  value       = <<-EOT
    # To get your API key, run:
    # terraform output -raw api_key
    
    # To check server status:
    curl -X GET ${aws_api_gateway_deployment.api_deployment.invoke_url}/server/status \\
      -H "x-api-key: API_KEY"
    
    # To start the server:
    curl -X POST ${aws_api_gateway_deployment.api_deployment.invoke_url}/server/start \\
      -H "Content-Type: application/json" \\
      -H "x-api-key: API_KEY" \\
      -d '{}'
    
    # To stop the server:
    curl -X POST ${aws_api_gateway_deployment.api_deployment.invoke_url}/server/stop \\
      -H "Content-Type: application/json" \\
      -H "x-api-key: API_KEY" \\
      -d '{}'
  EOT
}

output "status_curl_command" {
  description = "Ready-to-use curl command to check server status"
  sensitive   = true
  value       = "curl -X GET ${aws_api_gateway_deployment.api_deployment.invoke_url}/server/status -H \"x-api-key: ${aws_api_gateway_api_key.mc_server_api_key.value}\""
}

output "start_curl_command" {
  description = "Ready-to-use curl command to start the server"
  sensitive   = true
  value       = "curl -X POST ${aws_api_gateway_deployment.api_deployment.invoke_url}/server/start -H \"Content-Type: application/json\" -H \"x-api-key: ${aws_api_gateway_api_key.mc_server_api_key.value}\" -d '{}'"
}

output "stop_curl_command" {
  description = "Ready-to-use curl command to stop the server"
  sensitive   = true
  value       = "curl -X POST ${aws_api_gateway_deployment.api_deployment.invoke_url}/server/stop -H \"Content-Type: application/json\" -H \"x-api-key: ${aws_api_gateway_api_key.mc_server_api_key.value}\" -d '{}'"
}

output "api_key" {
  description = "API key for the API Gateway endpoint"
  value       = aws_api_gateway_api_key.mc_server_api_key.value
  sensitive   = true
}
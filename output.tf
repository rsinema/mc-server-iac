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
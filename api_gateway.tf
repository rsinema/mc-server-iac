resource "aws_api_gateway_rest_api" "mc_server_api" {
  name        = "${var.server_name}-api"
  description = "API Gateway for Minecraft server controller"
}

resource "aws_api_gateway_api_key" "mc_server_api_key" {
  name        = "${var.server_name}-api-key"
  description = "API key for Minecraft server controller"
}


resource "aws_api_gateway_resource" "mc_server_resource" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  parent_id   = aws_api_gateway_rest_api.mc_server_api.root_resource_id
  path_part   = "server"
}

resource "aws_api_gateway_method" "server_method" {
  rest_api_id   = aws_api_gateway_rest_api.mc_server_api.id
  resource_id   = aws_api_gateway_resource.mc_server_resource.id
  http_method   = "POST"
  authorization = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_integration" "lambda_integration" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  resource_id = aws_api_gateway_resource.mc_server_resource.id
  http_method = aws_api_gateway_method.server_method.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.server_controller.invoke_arn
}

resource "aws_api_gateway_deployment" "api_deployment" {
  depends_on  = [aws_api_gateway_integration.lambda_integration]
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  stage_name  = "prod"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_usage_plan" "mc_server_usage_plan" {
  name        = "${var.server_name}-usage-plan"
  description = "Usage plan for Minecraft server controller"
  api_stages {
    api_id = aws_api_gateway_rest_api.mc_server_api.id
    stage  = aws_api_gateway_deployment.api_deployment.stage_name
  }

  quota_settings {
    limit  = 100
    period = "DAY"
  }

  throttle_settings {
    burst_limit = 10
    rate_limit  = 5
  }
}

resource "aws_api_gateway_usage_plan_key" "mc_server_usage_plan_key" {
  key_id        = aws_api_gateway_api_key.mc_server_api_key.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.mc_server_usage_plan.id
}

resource "aws_lambda_permission" "api_gateway_lambda" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.server_controller.function_name
  principal     = "apigateway.amazonaws.com"

  source_arn = "${aws_api_gateway_rest_api.mc_server_api.execution_arn}/*/*"
}
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

# Create resources for the status, start, and stop endpoints
resource "aws_api_gateway_resource" "status_resource" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  parent_id   = aws_api_gateway_resource.mc_server_resource.id
  path_part   = "status"
}

resource "aws_api_gateway_resource" "start_resource" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  parent_id   = aws_api_gateway_resource.mc_server_resource.id
  path_part   = "start"
}

resource "aws_api_gateway_resource" "stop_resource" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  parent_id   = aws_api_gateway_resource.mc_server_resource.id
  path_part   = "stop"
}

# Methods for the status, start, and stop endpoints
## Create methods for the status endpoint
## Methods: GET, OPTIONS
resource "aws_api_gateway_method" "mc_server_status_get" {
  rest_api_id      = aws_api_gateway_rest_api.mc_server_api.id
  resource_id      = aws_api_gateway_resource.status_resource.id
  http_method      = "GET"
  authorization    = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_method" "mc_server_status_options" {
  rest_api_id   = aws_api_gateway_rest_api.mc_server_api.id
  resource_id   = aws_api_gateway_resource.status_resource.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

## Create methods for the start endpoint
## Methods: POST, OPTIONS

resource "aws_api_gateway_method" "mc_server_start_post" {
  rest_api_id      = aws_api_gateway_rest_api.mc_server_api.id
  resource_id      = aws_api_gateway_resource.start_resource.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_method" "mc_server_start_options" {
  rest_api_id   = aws_api_gateway_rest_api.mc_server_api.id
  resource_id   = aws_api_gateway_resource.start_resource.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

## Create methods for the stop endpoint
## Methods: POST, OPTIONS

resource "aws_api_gateway_method" "mc_server_stop_post" {
  rest_api_id      = aws_api_gateway_rest_api.mc_server_api.id
  resource_id      = aws_api_gateway_resource.stop_resource.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_method" "mc_server_stop_options" {
  rest_api_id   = aws_api_gateway_rest_api.mc_server_api.id
  resource_id   = aws_api_gateway_resource.stop_resource.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

# Integrations for the status, start, and stop endpoints
## Create integrations for the status endpoint
## Integrations: GET -AWS_PROXY-> Lambda, OPTIONS -> Mock

resource "aws_api_gateway_integration" "mc_server_status_get_integration" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  resource_id = aws_api_gateway_resource.status_resource.id
  http_method = aws_api_gateway_method.mc_server_status_get.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.server_controller.invoke_arn

  request_templates = {
    "application/json" = jsonencode({
      action = "status"
    })
  }
}

resource "aws_api_gateway_integration" "mc_server_status_options_integrations" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  resource_id = aws_api_gateway_resource.status_resource.id
  http_method = aws_api_gateway_method.mc_server_status_options.http_method

  type = "MOCK"
  request_templates = {
    "application/json" = jsonencode({
      statusCode = 200
    })
  }
}

## Integrations for the start endpoint
## Integrations: POST -AWS_PROXY-> Lambda, OPTIONS -> Mock

resource "aws_api_gateway_integration" "mc_server_start_post_integration" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  resource_id = aws_api_gateway_resource.start_resource.id
  http_method = aws_api_gateway_method.mc_server_start_post.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.server_controller.invoke_arn

  request_templates = {
    "application/json" = jsonencode({
      action = "start"
    })
  }
}

resource "aws_api_gateway_integration" "mc_server_start_options_integration" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  resource_id = aws_api_gateway_resource.start_resource.id
  http_method = aws_api_gateway_method.mc_server_start_options.http_method

  type = "MOCK"
  request_templates = {
    "application/json" = jsonencode({
      statusCode = 200
    })
  }
}

## Integrations for the stop endpoint
## Integrations: POST -AWS_PROXY-> Lambda, OPTIONS -> Mock
resource "aws_api_gateway_integration" "mc_server_stop_post_integration" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  resource_id = aws_api_gateway_resource.stop_resource.id
  http_method = aws_api_gateway_method.mc_server_stop_post.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.server_controller.invoke_arn

  request_templates = {
    "application/json" = jsonencode({
      action = "stop"
    })
  }
}

resource "aws_api_gateway_integration" "mc_server_stop_options_integration" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  resource_id = aws_api_gateway_resource.stop_resource.id
  http_method = aws_api_gateway_method.mc_server_stop_options.http_method

  type = "MOCK"
  request_templates = {
    "application/json" = jsonencode({
      statusCode = 200
    })
  }
}

# Method responses for the status, start, and stop endpoints
## Create method responses for the status endpoint
## Method responses: OPTIONS -> 200, GET gets handled by the Lambda function

resource "aws_api_gateway_method_response" "mc_server_status_options_200" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  resource_id = aws_api_gateway_resource.status_resource.id
  http_method = aws_api_gateway_method.mc_server_status_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }
}

## Method responses for the start endpoint
## Method responses: OPTIONS -> 200, POST gets handled by the Lambda function

resource "aws_api_gateway_method_response" "mc_server_start_options_200" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  resource_id = aws_api_gateway_resource.start_resource.id
  http_method = aws_api_gateway_method.mc_server_start_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }
}

## Method responses for the stop endpoint
## Method responses: OPTIONS -> 200, POST gets handled by the Lambda function

resource "aws_api_gateway_method_response" "mc_server_stop_options_200" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  resource_id = aws_api_gateway_resource.stop_resource.id
  http_method = aws_api_gateway_method.mc_server_stop_options.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }
}

# Integration responses for the status, start, and stop endpoints
## Create integration responses for the status endpoint
## Integration responses: OPTIONS -> CORS headers, GET is handled by the Lambda function

resource "aws_api_gateway_integration_response" "mc_server_status_options_integration_response" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  resource_id = aws_api_gateway_resource.status_resource.id
  http_method = aws_api_gateway_method.mc_server_status_options.http_method
  status_code = aws_api_gateway_method_response.mc_server_status_options_200.status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,OPTIONS'"
    "method.response.header.Access-Control-Allow-Origin"  = "'*'" # Allow any origin - you can restrict this to your specific domain
  }
}

## Integration responses for the start endpoint
## Integration responses: OPTIONS -> CORS headers, POST is handled by the Lambda function

resource "aws_api_gateway_integration_response" "mc_server_start_options_integration_response" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  resource_id = aws_api_gateway_resource.start_resource.id
  http_method = aws_api_gateway_method.mc_server_start_options.http_method
  status_code = aws_api_gateway_method_response.mc_server_start_options_200.status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token'"
    "method.response.header.Access-Control-Allow-Methods" = "'POST,OPTIONS'"
    "method.response.header.Access-Control-Allow-Origin"  = "'*'" # Allow any origin - you can restrict this to your specific domain
  }
}

## Integration responses for the stop endpoint
## Integration responses: OPTIONS -> CORS headers, POST is handled by the Lambda function

resource "aws_api_gateway_integration_response" "mc_server_stop_options_integration_response" {
  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  resource_id = aws_api_gateway_resource.stop_resource.id
  http_method = aws_api_gateway_method.mc_server_stop_options.http_method
  status_code = aws_api_gateway_method_response.mc_server_stop_options_200.status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token'"
    "method.response.header.Access-Control-Allow-Methods" = "'POST,OPTIONS'"
    "method.response.header.Access-Control-Allow-Origin"  = "'*'" # Allow any origin - you can restrict this to your specific domain
  }
}

# Deploy the API

resource "aws_api_gateway_deployment" "api_deployment" {
  depends_on = [
    aws_api_gateway_integration.mc_server_status_get_integration,
    aws_api_gateway_integration.mc_server_status_options_integrations,
    aws_api_gateway_integration.mc_server_start_post_integration,
    aws_api_gateway_integration.mc_server_stop_post_integration
  ]

  rest_api_id = aws_api_gateway_rest_api.mc_server_api.id
  stage_name  = "prod"

  # This is critical: it ensures a new deployment happens when the API configuration changes
  triggers = {
    # Include a hash of the API methods and integrations to force redeployment on changes
    redeployment = sha1(jsonencode([
      aws_api_gateway_method.mc_server_status_get,
      aws_api_gateway_method.mc_server_start_post,
      aws_api_gateway_method.mc_server_stop_post,
      aws_api_gateway_integration.mc_server_status_get_integration,
      aws_api_gateway_integration.mc_server_status_options_integrations,
      aws_api_gateway_integration.mc_server_start_post_integration,
      aws_api_gateway_integration.mc_server_start_options_integration,
      aws_api_gateway_integration.mc_server_stop_post_integration,
      aws_api_gateway_integration.mc_server_stop_options_integration,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_usage_plan" "mc_server_usage_plan" {
  name        = "${var.server_name}-usage-plan"
  description = "Usage plan for Minecraft server controller"
  depends_on  = [aws_api_gateway_deployment.api_deployment]
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
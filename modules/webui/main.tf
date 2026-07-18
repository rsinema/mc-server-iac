# World-manager web UI (see docs/webui.md).
#
# A browser-facing Lambda + Function URL, separate from the Discord control
# Lambda, that CRUDs Minecraft world profile documents in SSM and starts/stops
# the instance. The only gate is a shared bearer token stored in Secrets Manager
# and checked in the handler (constant-time); the Function URL auth type is NONE.
#
# The world params (active-world, world-list) are created by the control module.
# This module references them by their deterministic name/ARN rather than a
# cross-module output, so root composition stays unchanged. The per-world profile
# documents under /<server_name>/stats/worlds/* are created at runtime by this
# Lambda, not by Terraform.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  ssm_prefix         = "/${var.server_name}/stats"
  worlds_prefix      = "${local.ssm_prefix}/worlds/"
  active_world_param = "${local.ssm_prefix}/active-world"
  world_list_param   = "${local.ssm_prefix}/world-list"

  ssm_arn_base = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter"
  world_param_arns = [
    "${local.ssm_arn_base}${local.active_world_param}",
    "${local.ssm_arn_base}${local.world_list_param}",
    "${local.ssm_arn_base}${local.ssm_prefix}/worlds/*",
  ]
}

# Shared bearer token. Created empty; the value is set out-of-band (never in
# tfvars/git), matching the Discord signing key convention:
#   aws secretsmanager put-secret-value --secret-id <arn> --secret-string "$(openssl rand -hex 32)"
resource "aws_secretsmanager_secret" "webui_token" {
  name = "${var.server_name}-webui-token"

  tags = {
    Project = "mc-server"
  }
}

resource "aws_iam_role" "webui_lambda" {
  name = "${var.server_name}-webui-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action    = "sts:AssumeRole"
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
      }
    ]
  })
}

resource "aws_iam_policy" "webui_lambda" {
  name        = "${var.server_name}-webui-policy"
  description = "Allows the world-manager Lambda to CRUD world SSM params and start/stop the instance"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EC2Control"
        Effect   = "Allow"
        Action   = ["ec2:StartInstances", "ec2:StopInstances", "ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Sid      = "CloudWatchAlarmReset"
        Effect   = "Allow"
        Action   = ["cloudwatch:SetAlarmState"]
        Resource = "*"
      },
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Sid      = "SecretsRead"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.webui_token.arn, var.rcon_password_secret_arn]
      },
      {
        Sid      = "WorldParamsReadWrite"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter", "ssm:DeleteParameter"]
        Resource = local.world_param_arns
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "webui_lambda" {
  role       = aws_iam_role.webui_lambda.name
  policy_arn = aws_iam_policy.webui_lambda.arn
}

data "archive_file" "webui_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../../world_manager"
  output_path = "${path.module}/../../lambdas/world_manager.zip"
}

resource "aws_lambda_function" "world_manager" {
  function_name = "${var.server_name}-world-manager"
  description   = "Bearer-token web UI for managing Minecraft world profiles"

  filename         = data.archive_file.webui_zip.output_path
  source_code_hash = data.archive_file.webui_zip.output_base64sha256

  runtime       = "python3.11"
  handler       = "handler.lambda_handler"
  architectures = ["arm64"]
  # Long enough to cover the "switch & restart" path: stop, wait for stopped
  # (~30-60s), then start.
  timeout = 180

  role = aws_iam_role.webui_lambda.arn

  environment {
    variables = {
      INSTANCE_ID              = var.instance_id
      ACTIVE_WORLD_PARAM       = local.active_world_param
      WORLD_LIST_PARAM         = local.world_list_param
      WORLDS_PREFIX            = local.worlds_prefix
      WEBUI_TOKEN_SECRET_ARN   = aws_secretsmanager_secret.webui_token.arn
      RCON_PASSWORD_SECRET_ARN = var.rcon_password_secret_arn
      IDLE_STOP_ALARM_NAME     = var.idle_stop_alarm_name
    }
  }
}

# Auth is enforced in code (bearer token); the URL itself is unauthenticated.
# Same-origin SPA (served by GET /) + API (POST/PUT/DELETE under /api/*), so no
# CORS configuration is required.
resource "aws_lambda_function_url" "world_manager" {
  function_name      = aws_lambda_function.world_manager.function_name
  authorization_type = "NONE"
}

# Public invoke permission for the unauthenticated Function URL. Auth is the
# in-handler bearer token, not IAM, so the URL itself must be reachable by
# anyone (principal "*") before the token check runs.
resource "aws_lambda_permission" "function_url" {
  statement_id           = "AllowPublicFunctionUrlInvoke"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.world_manager.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

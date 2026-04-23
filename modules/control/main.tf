resource "aws_iam_role" "controller_lambda" {
  name = "${var.server_name}-controller-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_policy" "controller_lambda" {
  name        = "${var.server_name}-controller-policy"
  description = "Allows Lambda to control EC2, read secrets, write CloudWatch logs, and be invoked by EventBridge"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EC2Control"
        Effect = "Allow"
        Action = [
          "ec2:StartInstances",
          "ec2:StopInstances",
          "ec2:DescribeInstances"
        ]
        Resource = "*"
      },
      {
        Sid    = "SecretsManagerRead"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = "*"
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Sid    = "CloudWatchAlarmReset"
        Effect = "Allow"
        Action = [
          "cloudwatch:SetAlarmState"
        ]
        Resource = "*"
      },
      {
        Sid    = "InvokeSelfForDeferredDiscord"
        Effect = "Allow"
        Action = [
          "lambda:InvokeFunction"
        ]
        Resource = "arn:aws:lambda:*:*:function:${var.server_name}-server-controller"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "controller_lambda" {
  role       = aws_iam_role.controller_lambda.name
  policy_arn = aws_iam_policy.controller_lambda.arn
}

resource "aws_lambda_function" "server_controller" {
  function_name = "${var.server_name}-server-controller"
  description   = "Discord-controlled Minecraft server start/stop/status"

  filename         = data.archive_file.server_controller_zip.output_path
  source_code_hash = data.archive_file.server_controller_zip.output_base64sha256

  runtime       = "python3.11"
  handler       = "controller.lambda_handler"
  timeout       = 30
  architectures = ["arm64"]

  role = aws_iam_role.controller_lambda.arn

  environment {
    variables = {
      INSTANCE_ID                    = var.instance_id
      DISCORD_SIGNING_KEY_SECRET_ARN = var.discord_signing_key_secret_arn
      RCON_PASSWORD_SECRET_ARN       = var.rcon_password_secret_arn
      DISCORD_WEBHOOK_URL            = var.discord_webhook_url
      IDLE_STOP_ALARM_NAME           = var.idle_stop_alarm_name
      ADMIN_DISCORD_USER_IDS         = join(",", var.admin_discord_user_ids)
    }
  }
}

resource "aws_lambda_function_url" "server_controller" {
  function_name      = aws_lambda_function.server_controller.function_name
  authorization_type = "NONE"

  cors {
    allow_credentials = false
    allow_origins     = ["*"]
    allow_methods     = ["POST"]
    allow_headers     = ["x-signature-ed25519", "x-signature-timestamp", "content-type"]
    expose_headers    = []
    max_age           = 0
  }
}

data "archive_file" "server_controller_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../../server_controller"
  output_path = "${path.module}/../../lambdas/server_controller.zip"
}

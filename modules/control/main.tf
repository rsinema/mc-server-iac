# Shared waypoint (coordinate) notes written by /mc waypoint save and read by
# /mc waypoint list. Owned by the control plane (only this Lambda touches it),
# so it lives in this module rather than being wired through the root. Seeded
# empty; ignore_changes leaves the live contents to the Lambda after create.
#
# Named under the /stats/ prefix so it falls within the SSM path the deploy
# user is already granted (MCServer-stats-job-policy scopes PutParameter to
# /MCServerInstance/stats/*); it's a control-plane concern, not a stats one,
# but reusing that prefix avoids a separate IAM grant for the deploy principal.
resource "aws_ssm_parameter" "waypoints" {
  name        = "/${var.server_name}/stats/waypoints"
  description = "JSON {\"<label>\": {\"name\", \"x\", \"y\", \"z\", \"by\"}} of shared Minecraft coordinates; written by /mc waypoint"
  type        = "String"
  value       = jsonencode({})

  tags = {
    Project = "mc-server"
  }

  lifecycle {
    ignore_changes = [value]
  }
}

# Active world profile the instance boots. Read on the box at container-start
# time (modules/compute run.sh) and by /mc status; written by /mc world set.
# Seeded to the first known profile; ignore_changes leaves the live value to
# the Lambda / operators after create — same pattern as the waypoints param.
#
# Named under the /stats/ prefix (like waypoints) so it falls within the SSM
# path the deploy user is already granted (MCServer-stats-job-policy scopes
# PutParameter to /MCServerInstance/stats/*); avoids a separate IAM grant for
# the deploy principal. It's a control-plane concern, not a stats one.
resource "aws_ssm_parameter" "active_world" {
  name        = "/${var.server_name}/stats/active-world"
  description = "Name of the Minecraft world profile the server boots (subdir under /opt/minecraft/worlds/). Written by /mc world set."
  type        = "String"
  value       = length(var.world_profiles) > 0 ? var.world_profiles[0] : "survival"

  tags = {
    Project = "mc-server"
  }

  lifecycle {
    ignore_changes = [value]
  }
}

# Registry of known world profiles. The Lambda has no filesystem access, so the
# set of switchable worlds lives here: /mc world list renders it and /mc world
# set validates membership against it. Seeded from var.world_profiles; live
# edits (e.g. a future /mc world add) are preserved via ignore_changes.
#
# Under the /stats/ prefix for the same reason as active_world above: it keeps
# PutParameter within the path the deploy user is already granted.
resource "aws_ssm_parameter" "world_list" {
  name        = "/${var.server_name}/stats/world-list"
  description = "Comma-separated list of known Minecraft world profiles for /mc world."
  type        = "StringList"
  value       = join(",", length(var.world_profiles) > 0 ? var.world_profiles : ["survival"])

  tags = {
    Project = "mc-server"
  }

  lifecycle {
    ignore_changes = [value]
  }
}

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
      },
      {
        Sid    = "StatsEmailMapReadWrite"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:PutParameter"
        ]
        Resource = var.email_map_parameter_arn
      },
      {
        Sid    = "WaypointsReadWrite"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:PutParameter"
        ]
        Resource = aws_ssm_parameter.waypoints.arn
      },
      {
        Sid    = "WorldSelectReadWrite"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:PutParameter"
        ]
        Resource = [
          aws_ssm_parameter.active_world.arn,
          aws_ssm_parameter.world_list.arn
        ]
      },
      {
        Sid    = "StatsBaselineSeed"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]
        Resource = "${var.stats_bucket_arn}/state/*"
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
      PLAYER_EMAIL_MAP_PARAM         = var.email_map_parameter_name
      STATS_BUCKET                   = var.stats_bucket_name
      WAYPOINTS_PARAM                = aws_ssm_parameter.waypoints.name
      ACTIVE_WORLD_PARAM             = aws_ssm_parameter.active_world.name
      WORLD_LIST_PARAM               = aws_ssm_parameter.world_list.name
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

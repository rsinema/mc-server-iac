# ---------------------------------------------------------------------------
# Stats export module
#
# Owns the S3 bucket that the EC2 instance syncs vanilla stat files into (raw/),
# the delta-state object (state/), the daily export Lambda, its EventBridge
# schedule, and the UUID→email mapping (SSM parameter, hand-maintained).
#
# The EC2 instance writes to this bucket; that PutObject grant lives on the
# instance role in modules/compute (which receives this bucket's ARN), so this
# module has no dependency on compute.
# ---------------------------------------------------------------------------

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "stats" {
  bucket = "${lower(var.server_name)}-stats-${random_id.bucket_suffix.hex}"

  tags = {
    Project   = "mc-server"
    ManagedBy = "opentofu"
  }
}

resource "aws_s3_bucket_public_access_block" "stats" {
  bucket                  = aws_s3_bucket.stats.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "stats" {
  bucket = aws_s3_bucket.stats.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "stats" {
  bucket = aws_s3_bucket.stats.id

  # raw/ objects are overwritten on every sync; keep only a week of old versions.
  rule {
    id     = "expire-noncurrent-raw"
    status = "Enabled"
    filter {
      prefix = "raw/"
    }
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
  }

  rule {
    id     = "abort-incomplete-mpu"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# UUID → email mapping, maintained by hand (kept out of git). Seeded empty;
# ignore_changes means `aws ssm put-parameter` edits are never reverted by apply.
resource "aws_ssm_parameter" "email_map" {
  name        = "/${var.server_name}/stats/player-email-map"
  description = "JSON {\"<mc-uuid>\": \"<enzy-email>\"} consumed by the stats export Lambda"
  type        = "String"
  value       = jsonencode({})

  tags = {
    Project = "mc-server"
  }

  lifecycle {
    ignore_changes = [value]
  }
}

# ---------------------------------------------------------------------------
# Export Lambda — IAM
# ---------------------------------------------------------------------------

resource "aws_iam_role" "export_lambda" {
  name = "${var.server_name}-stats-export-role"

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

resource "aws_iam_policy" "export_lambda" {
  name        = "${var.server_name}-stats-export-policy"
  description = "Stats export Lambda: read/write the stats bucket, read the Enzy secret and email map, write logs"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "StatsBucketReadWrite"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.stats.arn,
          "${aws_s3_bucket.stats.arn}/*"
        ]
      },
      {
        Sid      = "ReadEnzySecret"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.enzy_secret_arn
      },
      {
        Sid      = "ReadEmailMap"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.email_map.arn
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
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "export_lambda" {
  role       = aws_iam_role.export_lambda.name
  policy_arn = aws_iam_policy.export_lambda.arn
}

# ---------------------------------------------------------------------------
# Export Lambda — function
#
# Pure stdlib + boto3 (provided by the runtime), so the source dir zips with no
# vendored dependencies. DRY_RUN defaults to "1": the first deploys compute and
# log the payload but do NOT POST, so the column set is not locked until you
# deliberately flip DRY_RUN to "0" after eyeballing a dry run.
# ---------------------------------------------------------------------------

data "archive_file" "export_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../../server_stats"
  output_path = "${path.module}/../../lambdas/server_stats.zip"
}

resource "aws_lambda_function" "export" {
  function_name = "${var.server_name}-stats-export"
  description   = "Daily export of vanilla Minecraft player stats to the Enzy leaderboard"

  filename         = data.archive_file.export_zip.output_path
  source_code_hash = data.archive_file.export_zip.output_base64sha256

  runtime       = "python3.11"
  handler       = "export.lambda_handler"
  timeout       = 120
  architectures = ["arm64"]

  role = aws_iam_role.export_lambda.arn

  environment {
    variables = {
      STATS_BUCKET           = aws_s3_bucket.stats.bucket
      ENZY_SECRET_ARN        = var.enzy_secret_arn
      ENZY_BASE_URL          = var.enzy_base_url
      PLAYER_EMAIL_MAP_PARAM = aws_ssm_parameter.email_map.name
      DRY_RUN                = var.dry_run ? "1" : "0"
    }
  }
}

# ---------------------------------------------------------------------------
# Daily schedule
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "daily_export" {
  name                = "${var.server_name}-stats-export"
  description         = "Daily trigger for the Minecraft stats export Lambda"
  schedule_expression = var.schedule_expression
}

resource "aws_cloudwatch_event_target" "daily_export" {
  rule      = aws_cloudwatch_event_rule.daily_export.name
  target_id = "StatsExport"
  arn       = aws_lambda_function.export.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.export.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily_export.arn
}

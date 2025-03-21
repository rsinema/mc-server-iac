resource "aws_iam_role" "lambda_role" {
  name = "${var.server_name}-lambda-role"
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

resource "aws_iam_policy" "lambda_ec2_policy" {
  name        = "${var.server_name}-lambda-ec2-policy"
  description = "Policy to allow Lambda to start and stop EC2 instances"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "ec2:StartInstances",
          "ec2:StopInstances",
          "ec2:DescribeInstances"
        ]
        Effect   = "Allow"
        Resource = "*"
      },
      {
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Effect   = "Allow"
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_ec2_attachment" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = aws_iam_policy.lambda_ec2_policy.arn
}

resource "aws_lambda_function" "server_controller" {
  function_name = "${var.server_name}-server-controller"
  description   = "Function to start/stop the server"

  filename         = data.archive_file.server_controller_zip.output_path
  source_code_hash = data.archive_file.server_controller_zip.output_base64sha256

  runtime = "python3.11"
  handler = "controller.lambda_handler"

  role    = aws_iam_role.lambda_role.arn
  timeout = 30

  environment {
    variables = {
      INSTANCE_ID = aws_instance.mc_server.id
    }
  }
}
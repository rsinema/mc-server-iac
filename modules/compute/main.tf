data "aws_ami" "al2023_arm64" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-arm64"]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

resource "aws_iam_role" "ec2_instance" {
  name = "${var.server_name}-ec2-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "ec2_permissions" {
  name = "${var.server_name}-ec2-policy"
  role = aws_iam_role.ec2_instance.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchPutMetric"
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData"
        ]
        Resource = "*"
      },
      {
        Sid    = "EC2SelfStop"
        Effect = "Allow"
        Action = [
          "ec2:StopInstances"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/Name" = var.server_name
          }
        }
      },
      {
        Sid    = "StatsBucketWrite"
        Effect = "Allow"
        Action = [
          "s3:PutObject"
        ]
        Resource = "${var.stats_bucket_arn}/raw/*"
      },
      {
        Sid    = "StatsBucketList"
        Effect = "Allow"
        Action = [
          "s3:ListBucket"
        ]
        Resource = var.stats_bucket_arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["raw/*"]
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.ec2_instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ec2_instance" {
  name = "${var.server_name}-instance-profile"
  role = aws_iam_role.ec2_instance.name
}

resource "aws_instance" "mc_server" {
  ami                    = data.aws_ami.al2023_arm64.id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [var.security_group_id]
  iam_instance_profile   = aws_iam_instance_profile.ec2_instance.name

  associate_public_ip_address = true

  # gzip-compressed: the rendered cloud-config exceeds EC2's 16 KB plaintext
  # user-data limit (the world-profile reconciler in run.sh added to it), so we
  # submit it gzipped — cloud-init on AL2023 decompresses it automatically. This
  # also leaves generous headroom for future user-data growth.
  user_data_base64 = base64gzip(templatefile("${path.module}/scripts/compute_setup.sh.tpl", {
    server_name       = var.server_name
    minecraft_version = var.minecraft_version
    minecraft_memory  = var.minecraft_memory
    rcon_password     = var.rcon_password
    stats_bucket      = var.stats_bucket_name
  }))
  user_data_replace_on_change = true

  tags = {
    Name    = var.server_name
    Project = "mc-server"
  }

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    tags = {
      Name    = "${var.server_name}-root"
      Project = "mc-server"
    }
  }
}

resource "aws_eip_association" "mc_server" {
  instance_id   = aws_instance.mc_server.id
  allocation_id = var.eip_allocation_id
}

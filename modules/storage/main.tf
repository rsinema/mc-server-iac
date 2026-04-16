resource "aws_ebs_volume" "mc_data" {
  availability_zone = var.availability_zone
  size              = var.volume_size
  type              = var.volume_type
  encrypted         = true

  tags = {
    Name    = "${var.server_name}-data"
    Project = "mc-server"
  }
}

resource "aws_volume_attachment" "mc_data" {
  device_name = "/dev/sdf"
  instance_id = var.instance_id
  volume_id   = aws_ebs_volume.mc_data.id
}

resource "aws_iam_role" "dlm" {
  name = "${var.server_name}-dlm-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRole"
      Principal = {
        Service = "dlm.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "dlm" {
  name = "${var.server_name}-dlm-policy"
  role = aws_iam_role.dlm.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ec2:CreateSnapshot",
        "ec2:CreateTags",
        "ec2:DeleteSnapshot",
        "ec2:DescribeInstances",
        "ec2:DescribeVolumes",
        "ec2:DescribeSnapshots"
      ]
      Resource = "*"
    }]
  })
}

resource "aws_dlm_lifecycle_policy" "mc_snapshots" {
  description        = "Daily EBS snapshots with 7-day retention"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]

    target_tags = {
      Project = "mc-server"
    }

    schedule {
      name = "daily-snapshot"

      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["05:00"]
      }

      retain_rule {
        count = 7
      }

      copy_tags = true
    }
  }
}

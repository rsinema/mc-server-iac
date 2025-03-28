resource "aws_s3_bucket" "mc_server_backup_bucket" {
bucket = lower("rsinema-${var.server_name}-backup")

  tags = {
    Name = "${var.server_name}-backup"
  }
}
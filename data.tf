data "aws_vpc" "default" {
  default = true
}

data "archive_file" "server_controller_zip" {
  type        = "zip"
  source_dir  = "${path.module}/server_controller"
  output_path = "${path.module}/lambdas/server_controller.zip"
}
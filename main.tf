provider "aws" {
  region = var.aws_region
}

resource "aws_instance" "mc_server" {
  ami           = var.server_ami
  instance_type = var.instance_type

  tags = {
    Name = var.server_name
  }
}


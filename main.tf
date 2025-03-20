provider "aws" {
  region = var.aws_region
}

resource "aws_key_pair" "mc_server_key" {
  key_name   = "${var.server_name}-key"
  public_key = file(var.ssh_key)
}

resource "aws_security_group" "mc_server_sg" {
  name        = "${var.server_name}-sg"
  description = "Allow inbound traffic on port 25565 and SSH"
  vpc_id      = data.aws_vpc.default.id
  tags = {
    Name = "${var.server_name}-sg"
  }
}

resource "aws_vpc_security_group_ingress_rule" "allow_ssh" {
  security_group_id = aws_security_group.mc_server_sg.id

  cidr_ipv4   = "${var.home_ip}/32"
  from_port   = 22
  to_port     = 22
  ip_protocol = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "allow_minecraft" {
  security_group_id = aws_security_group.mc_server_sg.id

  cidr_ipv4   = "${var.home_ip}/32"
  from_port   = 25565
  to_port     = 25565
  ip_protocol = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "default" {
  security_group_id = aws_security_group.mc_server_sg.id

  cidr_ipv4   = "0.0.0.0/0"
  from_port   = 0
  to_port     = 0
  ip_protocol = "-1"
}

resource "aws_instance" "mc_server" {
  ami                    = var.server_ami
  instance_type          = var.instance_type
  vpc_security_group_ids = [aws_security_group.mc_server_sg.id]

  key_name = aws_key_pair.mc_server_key.key_name
  tags = {
    Name = var.server_name
  }

  root_block_device {
    volume_size = 8
    volume_type = "gp3"
    tags = {
      Name = "${var.server_name}-root"
    }
  }
}

resource "aws_ebs_volume" "mc_volume" {
  availability_zone = aws_instance.mc_server.availability_zone
  size              = var.mc_volume_size
  type              = var.mc_volume_type
  tags = {
    Name = "${var.server_name}-volume"
  }
}

resource "aws_volume_attachment" "mc_volume_attachment" {
  device_name = "/dev/sdf"
  instance_id = aws_instance.mc_server.id
  volume_id   = aws_ebs_volume.mc_volume.id
}
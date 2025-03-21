resource "aws_key_pair" "mc_server_key" {
  key_name   = "${var.server_name}-key"
  public_key = file(var.ssh_key)
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
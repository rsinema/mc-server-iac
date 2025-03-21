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
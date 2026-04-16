resource "aws_security_group" "mc_server_sg" {
  name        = "${var.server_name}-sg"
  description = "Allow inbound Minecraft on 25565, all outbound"
  vpc_id      = var.vpc_id

  tags = {
    Name = "${var.server_name}-sg"
  }
}

resource "aws_vpc_security_group_ingress_rule" "allow_minecraft" {
  security_group_id = aws_security_group.mc_server_sg.id

  cidr_ipv4   = "0.0.0.0/0"
  from_port   = 25565
  to_port     = 25565
  ip_protocol = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "allow_rcon" {
  security_group_id = aws_security_group.mc_server_sg.id

  cidr_ipv4   = "0.0.0.0/0"
  from_port   = 25575
  to_port     = 25575
  ip_protocol = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "allow_all" {
  security_group_id = aws_security_group.mc_server_sg.id

  cidr_ipv4   = "0.0.0.0/0"
  ip_protocol = "-1"
}

resource "aws_eip" "mc_server" {
  domain = "vpc"

  tags = {
    Name = "${var.server_name}-eip"
  }
}

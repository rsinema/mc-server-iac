resource "aws_key_pair" "mc_server_key" {
  key_name   = "${var.server_name}-key"
  public_key = file(var.ssh_key)
}

resource "aws_instance" "mc_server" {
  ami                    = var.server_ami
  instance_type          = var.instance_type
  vpc_security_group_ids = [aws_security_group.mc_server_sg.id]

  key_name = aws_key_pair.mc_server_key.key_name

  user_data = <<-EOF
    #!/bin/bash
    
    # Install Java
    sudo yum update -y
    sudo amazon-linux-extras install java-openjdk11 -y
    
    # Set up the EBS volume
    sudo mkfs -t ext4 /dev/nvme1n1 || true
    sudo mkdir -p /opt/minecraft
    
    # Add entry to fstab to mount on boot
    if ! grep -q "/opt/minecraft" /etc/fstab; then
      echo "/dev/nvme1n1 /opt/minecraft ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
    fi
    
    # Mount the volume
    sudo mount -a
    sudo chown ec2-user:ec2-user /opt/minecraft
    
    # Download Minecraft server if it doesn't exist
    if [ ! -f /opt/minecraft/server.jar ]; then
      cd /opt/minecraft
      sudo -u ec2-user wget -O server.jar https://piston-data.mojang.com/v1/objects/8f3112a1049751cc472ec13e397eade5336ca7ae/server.jar
      
      # Create a start script
      echo '#!/bin/bash' | sudo -u ec2-user tee start_server.sh
      echo 'cd /opt/minecraft' | sudo -u ec2-user tee -a start_server.sh
      echo 'java -Xmx2G -Xms1G -jar server.jar nogui' | sudo -u ec2-user tee -a start_server.sh
      sudo chmod +x /opt/minecraft/start_server.sh
      
      # Accept EULA
      echo 'eula=true' | sudo -u ec2-user tee eula.txt
    fi
    
    # Add a systemd service for automatic start
    cat <<-SERVICE | sudo tee /etc/systemd/system/minecraft.service
    [Unit]
    Description=Minecraft Server
    After=network.target
    
    [Service]
    User=ec2-user
    WorkingDirectory=/opt/minecraft
    ExecStart=/opt/minecraft/start_server.sh
    Restart=on-failure
    
    [Install]
    WantedBy=multi-user.target
    SERVICE
    
    sudo systemctl daemon-reload
    sudo systemctl enable minecraft.service
    sudo systemctl start minecraft.service
  EOF

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
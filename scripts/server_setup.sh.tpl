#!/bin/bash

# Output all commands for better debugging
set -x

# Install Java 17 for arm64 (for Amazon Linux 2)
sudo rpm --import https://yum.corretto.aws/corretto.key
sudo curl -L -o /etc/yum.repos.d/corretto.repo https://yum.corretto.aws/corretto.repo
sudo yum install -y java-17-amazon-corretto-devel

# Verify Java installation
java -version

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
  sudo -u ec2-user wget -O server.jar ${minecraft_download_url}
  # Verify download was successful
  if [ ! -s server.jar ]; then
    echo "Failed to download server.jar"
    exit 1
  fi
  
  # Create a start script with proper error handling
  cat <<EOF | sudo -u ec2-user tee start_server.sh
#!/bin/bash
cd /opt/minecraft
# Log output to a file for debugging
java -Xmx1G -Xms1G -jar server.jar nogui 2>&1 | tee -a minecraft_console.log
exit \${PIPESTATUS[0]}
EOF
  
  sudo chmod +x /opt/minecraft/start_server.sh
  
  # Accept EULA
  echo 'eula=true' | sudo -u ec2-user tee eula.txt
fi

# Test run the server to see if it works and generate files
cd /opt/minecraft
sudo -u ec2-user java -Xmx1G -Xms512M -jar server.jar --initSettings nogui || true

# Add a systemd service with proper logging
cat <<SERVICE | sudo tee /etc/systemd/system/minecraft.service
[Unit]
Description=Minecraft Server
After=network.target

[Service]
User=ec2-user
WorkingDirectory=/opt/minecraft
ExecStart=/opt/minecraft/start_server.sh
StandardOutput=journal
StandardError=journal
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable minecraft
sudo systemctl start minecraft

# Log status for debugging
sudo systemctl status minecraft

# Create a log directory if it doesn't exist
sudo -u ec2-user mkdir -p /opt/minecraft/logs
#cloud-config
# AL2023 EC2 user-data: runs as root on first boot (after network is up)
runcmd:
  - |
    set -e

    # Wait for the EBS data volume to attach (Terraform attaches async)
    DEVICE="/dev/sdf"
    MAX_WAIT=120
    echo "Waiting up to $${MAX_WAIT}s for $${DEVICE} to attach..."
    for i in $(seq 1 $MAX_WAIT); do
        if [ -b "$DEVICE" ]; then
            echo "$${DEVICE} is ready after $${i}s"
            break
        fi
        sleep 1
    done

    if [ ! -b "$DEVICE" ]; then
        echo "ERROR: $${DEVICE} did not attach within $${MAX_WAIT}s"
        exit 1
    fi

    # Format only if no filesystem
    if ! blkid "$DEVICE" > /dev/null 2>&1; then
        echo "Formatting $${DEVICE} as ext4..."
        mkfs.ext4 -E nodiscard "$DEVICE"
    fi

    # Mount the data volume at /opt/minecraft
    if mountpoint -q /opt/minecraft; then
        echo "/opt/minecraft already mounted"
    elif [ -d /opt/minecraft ] && [ "$(ls -A /opt/minecraft)" ]; then
        echo "/opt/minecraft has content, migrating to data volume..."
        mkdir -p /mnt/migrate
        cp -a /opt/minecraft/. /mnt/migrate/
        mount "$DEVICE" /opt/minecraft
        cp -a /mnt/migrate/. /opt/minecraft/
        rm -rf /mnt/migrate
    else
        mkdir -p /opt/minecraft
        mount "$DEVICE" /opt/minecraft
    fi

    # Persist mount across reboots
    UUID=$(blkid -s UUID -o value "$DEVICE")
    if ! grep -q "$UUID" /etc/fstab 2>/dev/null; then
        echo "UUID=$${UUID} /opt/minecraft ext4 defaults,nofail 0 2" >> /etc/fstab
    fi

    # Install and enable Docker
    dnf install -y docker
    systemctl enable docker
    systemctl start docker

    # Relax ephemeral port range for Minecraft
    sysctl -w net.ipv4.ip_local_port_range="49152 65535"

    # Write the Minecraft systemd unit
    mkdir -p /etc/systemd/system
    cat > /etc/systemd/system/minecraft.service << 'MCSERVICE'
[Unit]
Description=Minecraft Server (PaperMC via itzg)
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Restart=always
RestartSec=10
ExecStartPre=-/usr/bin/docker stop minecraft
ExecStartPre=-/usr/bin/docker rm minecraft
ExecStart=/usr/bin/docker run \
    --name minecraft \
    -v /opt/minecraft:/data \
    -e TYPE=PAPER \
    -e VERSION=${minecraft_version} \
    -e MEMORY=${minecraft_memory}G \
    -e EULA=TRUE \
    -e ENABLE_RCON=true \
    -e RCON_PASSWORD=${rcon_password} \
    -e SERVER_PORT=25565 \
    -p 25565:25565 \
    -p 25575:25575 \
    itzg/minecraft-server
ExecStop=/usr/bin/docker stop minecraft
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
MCSERVICE

    systemctl daemon-reload
    systemctl enable minecraft
    systemctl start minecraft

    # Player count metric publisher: systemd timer runs every 60s
    mkdir -p /opt/mc-monitor
    cat > /opt/mc-monitor/check_players.sh << 'MCSCRIPT'
#!/bin/bash
# Get instance ID via IMDSv2
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)

# Parse player count from RCON list output: "There are N of a max of M players online:"
RAW=$(docker exec minecraft rcon-cli list 2>/dev/null || true)
PLAYERS=$(echo "$RAW" | head -1 | grep -oE 'There are [0-9]+' | grep -oE '[0-9]+' || echo 0)
PLAYERS=$${PLAYERS:-0}

aws cloudwatch put-metric-data \
    --namespace Minecraft \
    --metric-name PlayerCount \
    --value "$PLAYERS" \
    --unit Count \
    --dimensions InstanceId="$INSTANCE_ID"
MCSCRIPT
    chmod +x /opt/mc-monitor/check_players.sh

    cat > /etc/systemd/system/mc-monitor.timer << 'MCTIMER'
[Unit]
Description=Minecraft player count check every 60s

[Timer]
OnBootSec=60
OnUnitActiveSec=60

[Install]
WantedBy=timers.target
MCTIMER

    cat > /etc/systemd/system/mc-monitor.service << 'MCSVC'
[Unit]
Description=Minecraft player count metric publisher

[Service]
Type=oneshot
ExecStart=/opt/mc-monitor/check_players.sh
StandardOutput=journal
StandardError=journal
MCSVC

    systemctl daemon-reload
    systemctl enable mc-monitor.timer
    systemctl start mc-monitor.timer

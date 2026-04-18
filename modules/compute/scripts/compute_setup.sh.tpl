#cloud-config
# AL2023 EC2 user-data: runs as root on first boot (after network is up)
write_files:
  - path: /etc/systemd/system/minecraft.service
    permissions: '0644'
    content: |
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

  - path: /opt/mc-monitor/check_players.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
      INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
      RAW=$(docker exec minecraft rcon-cli list 2>/dev/null || true)
      PLAYERS=$(echo "$RAW" | head -1 | grep -oE 'There are [0-9]+' | grep -oE '[0-9]+' || echo 0)
      PLAYERS=$${PLAYERS:-0}
      aws cloudwatch put-metric-data \
          --namespace Minecraft \
          --metric-name PlayerCount \
          --value "$PLAYERS" \
          --unit Count \
          --dimensions InstanceId="$INSTANCE_ID"

  - path: /etc/systemd/system/mc-monitor.service
    permissions: '0644'
    content: |
      [Unit]
      Description=Minecraft player count metric publisher

      [Service]
      Type=oneshot
      ExecStart=/opt/mc-monitor/check_players.sh
      StandardOutput=journal
      StandardError=journal

  - path: /etc/systemd/system/mc-monitor.timer
    permissions: '0644'
    content: |
      [Unit]
      Description=Minecraft player count check every 60s

      [Timer]
      OnBootSec=60
      OnUnitActiveSec=60

      [Install]
      WantedBy=timers.target

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

    # Start Minecraft and the player-count timer (unit files written by cloud-init write_files)
    systemctl daemon-reload
    systemctl enable minecraft
    systemctl start minecraft
    systemctl enable mc-monitor.timer
    systemctl start mc-monitor.timer

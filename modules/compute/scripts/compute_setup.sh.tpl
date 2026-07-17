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
      # The world profile to boot is resolved from SSM at start time by run.sh
      # (see docs/multi-world.md), so the docker run lives there rather than
      # inline here — a unit ExecStart can't read SSM before it launches.
      ExecStart=/opt/mc-server/run.sh
      ExecStop=/usr/bin/docker stop minecraft
      # End-of-session flush: after the container stops (Paper saves stats on
      # SIGTERM), push the final state to S3. Runs before network teardown
      # since this unit is After=network-online.target. Best-effort — the
      # leading '-' and timeout keep a slow/failed sync from stalling shutdown.
      ExecStopPost=-/usr/bin/timeout 60 /opt/mc-stats/sync_stats.sh
      StandardOutput=journal
      StandardError=journal

      [Install]
      WantedBy=multi-user.target

  - path: /opt/mc-server/run.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      # Resolve the active world profile from SSM at container-start time and
      # mount /opt/minecraft/worlds/<name> as /data. Reading SSM here — rather
      # than in cloud-init runcmd, which only runs on an instance's first boot —
      # means every `/mc start` honors the latest `/mc world set`. Falls back to
      # the default profile if the param is unset, unreadable, or fails the
      # charset guard. An unknown-but-valid name is auto-created (itzg generates
      # a fresh world there); pre-provision plugin worlds like skyblock first.
      set -e
      DEFAULT=survival
      NAME=$(aws ssm get-parameter --name "/${server_name}/stats/active-world" \
              --query 'Parameter.Value' --output text 2>/dev/null || echo "$${DEFAULT}")
      case "$${NAME}" in
          *[!a-z0-9_-]*|"") NAME=$${DEFAULT} ;;
      esac
      DIR="/opt/minecraft/worlds/$${NAME}"
      mkdir -p "$${DIR}"
      echo "$${DIR}" > /run/mc-active-world

      # Share the whitelist and operator (admin) list across every world profile
      # by bind-mounting single canonical files into /data — whichever world is
      # live reads and writes the same files, so `/mc whitelist add` and `/mc op`
      # apply everywhere. Seed once from the survival profile's existing files
      # (or empty JSON). Docker creates a *directory* at a missing bind-mount
      # source, so these must exist as files before the run.
      SHARED=/opt/minecraft/shared
      mkdir -p "$${SHARED}"
      for f in whitelist.json ops.json; do
          if [ ! -f "$${SHARED}/$${f}" ]; then
              if [ -f "/opt/minecraft/worlds/survival/$${f}" ]; then
                  cp -p "/opt/minecraft/worlds/survival/$${f}" "$${SHARED}/$${f}"
              else
                  echo "[]" > "$${SHARED}/$${f}"
              fi
          fi
      done

      # Per-profile world definition. The server type, version, memory, plugins/
      # mods and extra server.properties for a world live in an SSM JSON document
      # at /${server_name}/stats/worlds/<name>, written by the world-manager web
      # UI (see docs/webui.md). run.sh reconciles that document into the world dir
      # on every cold start: it downloads declared artifacts to their exact paths
      # (things itzg can't express, e.g. a BentoBox addon under
      # plugins/BentoBox/addons/) and renders the rest into itzg env vars. The
      # document is the source of truth; when it is absent we fall back to the
      # legacy per-profile profile.env (MC_VERSION/PAPER_CHANNEL — see
      # docs/multi-world.md) and then to the server defaults, so worlds
      # provisioned before the UI keep working unchanged.
      #
      # Profile values become argv to `docker run` via a bash array (never eval'd
      # or interpolated into a command string), so a malformed value cannot inject
      # a shell command; values used in a URL or filesystem path are additionally
      # scheme/charset-guarded below.
      MC_VERSION="${minecraft_version}"
      PAPER_CHANNEL_VALUE="experimental"
      TYPE_VALUE="PAPER"
      MEMORY_VALUE="${minecraft_memory}G"
      ENV_ARGS=()
      # VERSION_EXPLICIT tracks whether a specific MC version was requested. It
      # stays 0 for a modpack with no explicit version, so we let the pack pick
      # its own MC version instead of forcing the server default (which would
      # filter out every real pack build). MODPACK holds the .mrpack reference.
      VERSION_EXPLICIT=0
      MODPACK=""

      OVERRIDE="$${DIR}/profile.env"
      if [ -f "$${OVERRIDE}" ]; then
          V=$(sed -n 's/^MC_VERSION=//p' "$${OVERRIDE}" | tail -1 | tr -d '\r')
          C=$(sed -n 's/^PAPER_CHANNEL=//p' "$${OVERRIDE}" | tail -1 | tr -d '\r')
          case "$${V}" in ""|*[!a-zA-Z0-9._-]*) : ;; *) MC_VERSION="$${V}"; VERSION_EXPLICIT=1 ;; esac
          case "$${C}" in default|experimental) PAPER_CHANNEL_VALUE="$${C}" ;; esac
      fi

      PROFILE_JSON=$(aws ssm get-parameter \
              --name "/${server_name}/stats/worlds/$${NAME}" \
              --query 'Parameter.Value' --output text 2>/dev/null || echo "")
      if echo "$${PROFILE_JSON}" | jq -e . >/dev/null 2>&1; then
          echo "Reconciling world profile document for '$${NAME}'"

          # Server type / version / memory (each optional; charset-guarded, so a
          # malformed value just falls back to the default computed above).
          T=$(echo "$${PROFILE_JSON}" | jq -r '.type // empty')
          case "$${T}" in ""|*[!A-Za-z]*) : ;; *) TYPE_VALUE="$${T}" ;; esac
          V=$(echo "$${PROFILE_JSON}" | jq -r '.version // empty')
          case "$${V}" in ""|*[!a-zA-Z0-9._-]*) : ;; *) MC_VERSION="$${V}"; VERSION_EXPLICIT=1 ;; esac
          M=$(echo "$${PROFILE_JSON}" | jq -r '.memory_gb // empty')
          case "$${M}" in ""|*[!0-9]*) : ;; *) MEMORY_VALUE="$${M}G" ;; esac

          # Plugin / mod auto-download lists — itzg fetches these itself on boot.
          SPIGET=$(echo "$${PROFILE_JSON}" | jq -r '.plugins.spiget // [] | join(",")')
          PLUGIN_MODRINTH=$(echo "$${PROFILE_JSON}" | jq -r '.plugins.modrinth // [] | join(",")')
          PLUGIN_URLS=$(echo "$${PROFILE_JSON}" | jq -r '.plugins.urls // [] | join(",")')
          MOD_MODRINTH=$(echo "$${PROFILE_JSON}" | jq -r '.mods.modrinth // [] | join(",")')
          MOD_CURSE=$(echo "$${PROFILE_JSON}" | jq -r '.mods.curseforge // [] | join(",")')
          MOD_URLS=$(echo "$${PROFILE_JSON}" | jq -r '.mods.urls // [] | join(",")')
          [ -n "$${SPIGET}" ] && ENV_ARGS+=( -e "SPIGET_RESOURCES=$${SPIGET}" )
          # MODRINTH_PROJECTS is the itzg env for both plugins (Paper) and mods
          # (Fabric/Forge); merge the two lists and strip stray commas.
          MODRINTH_ALL=$(printf '%s,%s' "$${PLUGIN_MODRINTH}" "$${MOD_MODRINTH}" | sed 's/^,//; s/,$//')
          [ -n "$${MODRINTH_ALL}" ] && ENV_ARGS+=( -e "MODRINTH_PROJECTS=$${MODRINTH_ALL}" )
          [ -n "$${PLUGIN_URLS}" ] && ENV_ARGS+=( -e "PLUGINS=$${PLUGIN_URLS}" )
          [ -n "$${MOD_CURSE}" ] && ENV_ARGS+=( -e "CURSEFORGE_FILES=$${MOD_CURSE}" )
          [ -n "$${MOD_URLS}" ] && ENV_ARGS+=( -e "MODS=$${MOD_URLS}" )

          # Modrinth modpack (.mrpack): the server runs the exact pack players
          # install, so client mod versions match automatically. Forces
          # TYPE=MODRINTH; loader + MC version come from the pack (so we skip the
          # forced VERSION below unless the profile pinned one explicitly).
          MODPACK=$(echo "$${PROFILE_JSON}" | jq -r '.mods.modpack // empty')
          if [ -n "$${MODPACK}" ]; then
              TYPE_VALUE="MODRINTH"
              ENV_ARGS+=( -e "MODRINTH_MODPACK=$${MODPACK}" )
          fi

          # Auto-install transitive dependencies (Fabric API, architectury, …)
          # for individually listed Modrinth mods — the usual "mod won't load"
          # cause. Harmless for modpacks, which bundle their own dependencies.
          if [ -n "$${MODRINTH_ALL}" ] || [ -n "$${MODPACK}" ]; then
              ENV_ARGS+=( -e "MODRINTH_DOWNLOAD_DEPENDENCIES=required" )
          fi

          # Optional CurseForge API key (needed for CURSEFORGE_FILES downloads).
          CF_KEY=$(echo "$${PROFILE_JSON}" | jq -r '.cf_api_key // empty')
          [ -n "$${CF_KEY}" ] && ENV_ARGS+=( -e "CF_API_KEY=$${CF_KEY}" )

          # Extra server.properties, mapped straight to itzg env vars (e.g.
          # DIFFICULTY=normal, LEVEL_TYPE=...). Passed as literal argv entries.
          while IFS= read -r kv; do
              [ -n "$${kv}" ] && ENV_ARGS+=( -e "$${kv}" )
          done < <(echo "$${PROFILE_JSON}" | jq -r '.properties // {} | to_entries[] | "\(.key)=\(.value)"')

          # Explicit artifact placement for files itzg cannot express (plugin
          # addons, loose config jars), declared as {url, dest}. Download only
          # when missing (idempotent, self-healing across reboots). Guardrails:
          # https only, and dest must be a clean relative path (no leading '/',
          # no '..'). chown each created path component to the container uid
          # (itzg runs the server as 1000) so the plugin can create its own
          # data/config inside those dirs at runtime.
          while IFS= read -r entry; do
              [ -z "$${entry}" ] && continue
              url=$(echo "$${entry}" | jq -r '.url // empty')
              dest=$(echo "$${entry}" | jq -r '.dest // empty')
              case "$${url}" in https://*) ;; *) echo "  skip file: bad url '$${url}'"; continue ;; esac
              case "$${dest}" in ""|/*|*..*) echo "  skip file: bad dest '$${dest}'"; continue ;; esac
              target="$${DIR}/$${dest}"
              [ -f "$${target}" ] && continue
              mkdir -p "$$(dirname "$${target}")"
              if curl -fsSL "$${url}" -o "$${target}.tmp"; then
                  mv "$${target}.tmp" "$${target}"
                  chown 1000:1000 "$${target}" 2>/dev/null || true
                  d=$$(dirname "$${target}")
                  while [ "$${d}" != "$${DIR}" ] && [ "$${d}" != "/" ]; do
                      chown 1000:1000 "$${d}" 2>/dev/null || true
                      d=$$(dirname "$${d}")
                  done
                  echo "  downloaded $${dest}"
              else
                  rm -f "$${target}.tmp"
                  echo "  download FAILED: $${url}"
              fi
          done < <(echo "$${PROFILE_JSON}" | jq -c '.files // [] | .[]')
      fi

      # Force an explicit MC version for everything except a modpack that should
      # pick its own (VERSION would otherwise filter out the pack's real builds).
      if [ -z "$${MODPACK}" ] || [ "$${VERSION_EXPLICIT}" = "1" ]; then
          ENV_ARGS+=( -e "VERSION=$${MC_VERSION}" )
      fi

      exec /usr/bin/docker run \
          --name minecraft \
          -v "$${DIR}:/data" \
          -v "$${SHARED}/whitelist.json:/data/whitelist.json" \
          -v "$${SHARED}/ops.json:/data/ops.json" \
          -e TYPE="$${TYPE_VALUE}" \
          -e PAPER_CHANNEL="$${PAPER_CHANNEL_VALUE}" \
          -e MEMORY="$${MEMORY_VALUE}" \
          -e EULA=TRUE \
          -e ALLOW_FLIGHT=TRUE \
          -e ENABLE_RCON=true \
          -e RCON_PASSWORD=${rcon_password} \
          -e SERVER_PORT=25565 \
          -e ENFORCE_WHITELIST=TRUE \
          "$${ENV_ARGS[@]}" \
          -p 25565:25565 \
          -p 25575:25575 \
          itzg/minecraft-server

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

  - path: /opt/mc-stats/sync_stats.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      # Flush the world to disk, then push the vanilla stat files to S3 so the
      # daily export Lambda can read them while the server is stopped. Each step
      # is best-effort (|| true) so it no-ops when the container is down.
      #
      # This Paper build stores player data under world/players/ (stats,
      # advancements) rather than the vanilla world/stats + world/advancements.
      # Prefer the players/ layout, fall back to vanilla, so we're robust to
      # either and to a future world reseed/config change.
      docker exec minecraft rcon-cli save-all flush >/dev/null 2>&1 || true

      # The leaderboard tracks the survival world only (see docs/multi-world.md),
      # so read from its profile dir regardless of which world is currently live.
      # When another world is active these files are unchanged, so `s3 sync`
      # uploads nothing — a harmless no-op.
      WORLD=/opt/minecraft/worlds/survival
      STATS_DIR=$WORLD/world/players/stats
      [ -d "$STATS_DIR" ] || STATS_DIR=$WORLD/world/stats
      ADV_DIR=$WORLD/world/players/advancements
      [ -d "$ADV_DIR" ] || ADV_DIR=$WORLD/world/advancements

      aws s3 sync "$STATS_DIR/" s3://${stats_bucket}/raw/stats/        --only-show-errors || true
      aws s3 sync "$ADV_DIR/"   s3://${stats_bucket}/raw/advancements/ --only-show-errors || true
      aws s3 cp "$WORLD/usercache.json" s3://${stats_bucket}/raw/usercache.json --only-show-errors || true

  - path: /etc/systemd/system/mc-stats-sync.service
    permissions: '0644'
    content: |
      [Unit]
      Description=Sync Minecraft stat files to S3 for the leaderboard export

      [Service]
      Type=oneshot
      ExecStart=/opt/mc-stats/sync_stats.sh
      StandardOutput=journal
      StandardError=journal

  - path: /etc/systemd/system/mc-stats-sync.timer
    permissions: '0644'
    content: |
      [Unit]
      Description=Sync Minecraft stats to S3 every 5 minutes

      [Timer]
      OnBootSec=300
      OnUnitActiveSec=300

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

    # Grow the filesystem to fill the volume in case mc_volume_size was bumped
    # (EBS grows online, but the ext4 fs must be resized to see the new space).
    # No-op when the fs already spans the whole device. ext4 is mkfs'd directly
    # on the device (no partition table), so no growpart step is needed.
    resize2fs "$DEVICE" || true

    # One-time migration to per-world profiles (see docs/multi-world.md): move a
    # legacy top-level /data (world/, plugins/, server.properties, ...) into
    # worlds/survival so run.sh can mount it as a profile. Idempotent — skipped
    # once worlds/survival exists. Instant, since it's a rename on the same
    # filesystem. Runs only on first boot (runcmd), which is exactly when a
    # replaced instance re-attaches the existing data volume.
    cd /opt/minecraft
    if [ ! -d worlds/survival ] && [ -d world ]; then
        echo "Migrating legacy top-level world into worlds/survival..."
        mkdir -p worlds/survival
        find . -maxdepth 1 -mindepth 1 ! -name worlds ! -name 'lost+found' \
            -exec mv -t /opt/minecraft/worlds/survival {} +
    fi
    mkdir -p /opt/minecraft/worlds/survival
    cd /

    # Install and enable Docker. jq parses the per-world profile document that
    # run.sh reconciles at container-start time (see docs/webui.md).
    dnf install -y docker jq
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
    systemctl enable mc-stats-sync.timer
    systemctl start mc-stats-sync.timer

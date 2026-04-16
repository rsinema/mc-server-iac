# Ops Runbook

Operational procedures for the mc-server-iac Minecraft server.

---

## How to Bump the Minecraft Version

1. Edit `variables.tf` (root) and update `minecraft_version`:
   ```hcl
   variable "minecraft_version" {
     default = "1.21.5"  # your desired version
   }
   ```

2. Run `tofu apply`. The compute module passes this to the Docker container via user-data environment variables (`VERSION=<var>`).

3. SSH to the instance via SSM and restart the container:
   ```bash
   aws ssm start-session --target <instance-id>
   sudo systemctl restart minecraft
   ```

   Or wait for the next `/mc stop` + `/mc start` cycle — the new version is picked up on container restart.

---

## How to Restore from an EBS Snapshot

> **⚠️  Always take a fresh snapshot of the current volume before restoring from an older one.**

1. **Take a backup snapshot of the current state:**
   ```bash
   INSTANCE_ID=$(aws ec2 describe-instances \
     --filters Name=tag:Name=MCServerInstance \
     --query 'Reservations[*].Instances[*].InstanceId' --output text)
   VOLUME_ID=$(aws ec2 describe-volumes \
     --filters Name=tag:Name=MCServerInstance-data \
     --query 'Volumes[*].VolumeId' --output text)
   aws ec2 create-snapshot \
     --volume-id "$VOLUME_ID" \
     --description "pre-restore-backup-$(date -u +%Y%m%dT%H%M%SZ)"
   ```

2. **Identify the snapshot to restore from:**
   ```bash
   aws ec2 describe-snapshots \
     --filters Name=tag:Project=mc-server \
     --query 'Snapshots[*].[SnapshotId,Description,StartTime]' \
     --output table
   ```

3. **Stop the server and wait for `stopped` state:**
   ```bash
   aws ec2 stop-instances --instance-ids "$INSTANCE_ID"
   aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID"
   ```

4. **Detach the current volume:**
   ```bash
   aws ec2 detach-volume --volume-id "$VOLUME_ID"
   aws ec2 wait volume-available --volume-ids "$VOLUME_ID"
   ```

5. **Create a new volume from the snapshot:**
   ```bash
   AZ=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
     --query 'Reservations[0].Instances[0].Placement.AvailabilityZone' --output text)
   aws ec2 create-volume \
     --snapshot-id <snapshot-id> \
     --availability-zone "$AZ" \
     --volume-type gp3 \
     --encrypted \
     --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=MCServerInstance-data},{Key=Project,Value=mc-server}]' \
     --query 'VolumeId' --output text
   ```

6. **Attach the new volume** (see device name note below):
   ```bash
   aws ec2 attach-volume \
     --volume-id <new-volume-id> \
     --instance-id "$INSTANCE_ID" \
     --device /dev/sdf
   ```

7. **Start the instance and verify the mount:**
   ```bash
   aws ec2 start-instances --instance-ids "$INSTANCE_ID"
   aws ssm start-session --target "$INSTANCE_ID"
   # inside the session:
   for i in $(seq 1 30); do
       mountpoint -q /opt/minecraft && echo "mounted" && break
       sleep 1
   done
   ls /opt/minecraft
   systemctl start minecraft
   ```

> **Device name note:** On AL2023 with Nitro-based instances (including `t4g`), EBS volumes appear as NVMe devices (`/dev/nvme1n1` etc.). Terraform's `aws_volume_attachment` handles this mapping — specify `/dev/sdf` when attaching and the kernel will assign the correct NVMe name. Verify with `lsblk | grep nvme`.

---

## How to Add a Friend to the Whitelist

1. SSH to the instance via SSM:
   ```bash
   aws ssm start-session --target <instance-id>
   ```

2. Use the RCON CLI to add the player:
   ```bash
   rcon-cli whitelist add <username>
   ```

   If RCON CLI isn't on your path, run:
   ```bash
   docker exec minecraft rcon-cli whitelist add <username>
   ```

3. **Verify** the player can connect:
   - Ask them to try connecting to `mc.rsinema.com:25565`.
   - Check the whitelist: `rcon-cli whitelist list`.

---

## How to Update the Discord Public Key

Discord owns the signing key pair. Your Lambda only needs the **public key** to verify
incoming interaction signatures. You cannot rotate this key yourself — Discord controls it.

1. **Copy the public key** from the Discord Developer Portal:
   - Go to your Application → General Information.
   - Copy the **Public Key** (hex-encoded string).

2. **Store it in AWS Secrets Manager:**
   ```bash
   aws secretsmanager update-secret \
     --secret-id MCServerInstance-discord-signing-key \
     --secret-string '{"public_key":"<hex-public-key>"}'
   ```

3. The Lambda reads the key from Secrets Manager on each invocation — no redeploy needed.

---

## How to Set Up the Cloudflare API Token

1. **Log in to Cloudflare** at dash.cloudflare.com.

2. Go to **My Profile** → **API Tokens** → **Create Token** → **Create Custom Token**.

3. Configure the token:
   | Field | Value |
   |---|---|
   | Token name | `mc-server-iac` |
   | Account permissions | None required |
   | Zone permissions | `Zone:DNS:Edit` on `rsinema.com` |
   | Specific zone | `rsinema.com` |

4. **Copy the token** — it will only be shown once.

5. **Store it locally** for `tofu apply`:
   ```bash
   export CLOUDFLARE_API_TOKEN=<your-token>
   ```

   Or add it to your shell profile (`~/.bashrc`, `~/.zshrc`):
   ```bash
   export CLOUDFLARE_API_TOKEN=<your-token>
   ```

6. **Verify** the token works:
   ```bash
   curl -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
        -H "Content-Type: application/json" \
        "https://api.cloudflare.com/client/v4/user/tokens/verify"
   ```
   Expected: `"status": "active"`.

> **Note:** Never commit this token to git. If you use a secrets manager (1Password, AWS Secrets Manager), you can point `tofu apply` at it there.

---

## How to Check Server Logs

```bash
aws ssm start-session --target <instance-id>
sudo journalctl -u minecraft -f
```

Or from your local machine without SSH:
```bash
aws logs stream lambda --log-group-name /aws/lambda/MCServerInstance-server-controller --follow
```

---

## How to Manually Start / Stop the Server

```bash
# Start
aws ec2 start-instances --instance-ids <instance-id>

# Stop
aws ec2 stop-instances --instance-ids <instance-id>
```

Or via Discord: `/mc start`, `/mc stop`

---

## Useful AWS Queries

```bash
# Get instance ID
aws ec2 describe-instances --filters Name=tag:Name=MCServerInstance --query 'Reservations[*].Instances[*].InstanceId' --output text

# Get public IP
aws ec2 describe-instances --filters Name=tag:Name=MCServerInstance --query 'Reservations[*].Instances[*].PublicIpAddress' --output text

# Check EBS snapshots
aws ec2 describe-snapshots --owner-ids self --query 'Snapshots[*].[SnapshotId,StartTime,Tags[?Key==`Name`].Value|[0]]' --output table

# Check Lambda logs
aws logs describe-log-streams --log-group-name /aws/lambda/MCServerInstance-server-controller
aws logs get-log-events --log-group-name /aws/lambda/MCServerInstance-server-controller --log-stream-name <stream-name>
```

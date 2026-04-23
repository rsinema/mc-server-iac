# Ops Runbook

Operational procedures for the mc-server-iac Minecraft server.

---

## How to Configure IAM for `tofu apply`

The IAM principal running `tofu apply` needs permissions to manage everything in this stack. Most of it is covered by common managed policies (`AmazonEC2FullAccess`, `IAMFullAccess`, `AWSLambda_FullAccess`, `SecretsManagerReadWrite`, `CloudWatchFullAccess`), but the one that's easy to miss is **DLM (Data Lifecycle Manager)** — specifically `dlm:TagResource`. No AWS-managed policy grants a user the ability to create and tag DLM lifecycle policies, so attach this inline or customer-managed policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "dlm:CreateLifecyclePolicy",
        "dlm:DeleteLifecyclePolicy",
        "dlm:GetLifecyclePolicy",
        "dlm:UpdateLifecyclePolicy",
        "dlm:TagResource",
        "dlm:UntagResource",
        "dlm:ListTagsForResource"
      ],
      "Resource": "*"
    }
  ]
}
```

Symptom if missing: `tofu apply` fails with `AccessDeniedException: ... is not authorized to perform: dlm:TagResource`.

---

## How to Build / Rebuild the Lambda Dependencies

The Lambda runtime is **Python 3.11 on arm64**. `PyNaCl` (Discord signature verification) depends on `cffi`, which ships compiled C extensions (`_cffi_backend*.so`, `_sodium.abi3.so`). These must match the Lambda runtime — installing with your Mac's `pip` will produce macOS wheels that fail at import time in AWS with `Runtime.ImportModuleError: No module named '_cffi_backend'`.

Force `pip` to pull Linux arm64 wheels:

```bash
# Clean out any platform-mismatched vendored deps first
rm -rf server_controller/{cffi,nacl,pycparser,*.dist-info,_cffi_backend*.so,bin}

pip3 install \
  --target server_controller/ \
  --platform manylinux2014_aarch64 \
  --python-version 3.11 \
  --implementation cp \
  --only-binary=:all: \
  --upgrade \
  -r server_controller/requirements.txt
```

After the install, verify `_cffi_backend.cpython-311-aarch64-linux-gnu.so` and `nacl/_sodium.abi3.so` are present:

```bash
find server_controller -name '*.so'
```

The `archive_file` data source in `modules/control/main.tf` hashes the `server_controller/` directory, so on the next `tofu apply` the Lambda zip is rebuilt and the function's `source_code_hash` changes, triggering a code-only update (no infra churn).

**When to re-run this:**
- After modifying `server_controller/requirements.txt`
- When bumping Python runtime version or Lambda architecture
- Any time `tofu apply` succeeds but the Lambda returns `Runtime.ImportModuleError`

---

## How the Discord Interaction Flow Works

Discord requires every interaction endpoint to **ack within 3 seconds** or the user sees `application did not respond`. `/mc start` can take 30+ seconds (EC2 boot), so the controller uses the standard **deferred-response + follow-up** pattern:

```
┌──────────┐   1. signed POST         ┌──────────────────┐
│ Discord  │ ───────────────────────▶ │ Lambda (sync)    │
│          │ ◀─────────────────────── │  verify sig      │
│          │   2. type:5 deferred ack │  async-invoke    │
└──────────┘   (within ~300ms)        └─────────┬────────┘
                                                │ InvocationType=Event
                                                ▼
┌──────────┐   4. PATCH @original     ┌──────────────────┐
│ Discord  │ ◀─────────────────────── │ Lambda (async)   │
│          │   (content goes here)    │  run command     │
└──────────┘                          │  post follow-up  │
                                      └──────────────────┘
```

The flow in `server_controller/controller.py`:

1. **Sync invocation** (Function URL): verifies the Ed25519 signature, then returns response `type: 5` (DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE). The user sees *"app is thinking…"*.
2. **Self-invoke**: before returning, the handler async-invokes its own ARN (`context.invoked_function_arn`) with `InvocationType="Event"` and payload `{"async_command": true, "sub": "...", "application_id": "...", "interaction_token": "..."}`.
3. **Async invocation**: the second Lambda instance runs the real work (EC2 start, RCON query, etc.) — no 3-second pressure, 15 minutes of budget.
4. **Follow-up**: async worker `PATCH`es `https://discord.com/api/v10/webhooks/{application_id}/{interaction_token}/messages/@original` with the final content. Discord replaces the "thinking…" indicator with the message.

**Why an `application_id` isn't in Secrets Manager:** it comes from the interaction payload itself (`payload["application_id"]`), so there's nothing extra to provision.

**IAM requirements:** the controller role needs `lambda:InvokeFunction` on itself (see `InvokeSelfForDeferredDiscord` statement in `modules/control/main.tf`). Without it, step 2 fails silently and the "thinking…" message hangs forever.

**Outbound HTTP User-Agent:** Cloudflare fronts `discord.com` and **blocks default `Python-urllib/*` User-Agents** with error `1010`. Any follow-up request must set a real UA (we use `DiscordBot (…, 1.0)` per Discord's recommended format). See `DISCORD_USER_AGENT` in `controller.py`.

**When NOT to defer:** the Discord `PING` (interaction type 1) during endpoint verification must return a plain `{type: 1}` — not deferred — or the portal rejects the URL.

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

## How to Set Up the Discord Application (Initial)

This is the one-time bootstrap after the first `tofu apply`. The ordering matters — Discord validates the endpoint URL by POSTing a signed ping, and the Lambda needs the public key in Secrets Manager before it can answer.

1. **Create the application** at https://discord.com/developers/applications → *New Application*.

2. **Copy these three values:**
   | Value | Where |
   |---|---|
   | `APPLICATION_ID` | General Information → Application ID |
   | `PUBLIC_KEY` | General Information → Public Key (hex) |
   | `BOT_TOKEN` | Bot → Reset Token (shown once) |

3. **Store the public key in Secrets Manager *first*:**
   ```bash
   aws secretsmanager put-secret-value \
     --secret-id MCServerInstance-discord-signing-key \
     --secret-string '{"public_key":"<PUBLIC_KEY>"}'
   ```
   (Use `put-secret-value`, not `update-secret`, for the initial value — the Terraform-managed secret is created empty.)

4. **Set the Interactions Endpoint URL** in the portal (General Information) to the Lambda Function URL:
   ```bash
   tofu output function_url
   ```
   Paste that value and click *Save Changes*. If save fails, tail Lambda logs: `aws logs tail /aws/lambda/MCServerInstance-server-controller --follow`. The common failures are a bad public key (rejected signature) or a missing secret value (`ResourceNotFoundException`).

5. **Install the bot to your guild.** Open this URL (replace `$APP_ID`), pick the server, click *Authorize*:
   ```
   https://discord.com/oauth2/authorize?client_id=$APP_ID&permissions=0&scope=bot+applications.commands
   ```

6. **Register the `/mc` slash command** — see the next section.

> **Security:** never paste the bot token into a shared terminal or commit it to git. If it leaks (it will end up in your shell history), rotate it immediately via Bot → Reset Token.

---

## How to Register / Update Slash Commands

The `/mc` command takes a required string option named `sub` with choices `start`, `stop`, `status`, `players`. Register it via Discord's REST API. Use guild-scoped registration for instant updates during development; global for production (propagates in up to 1 hour).

```bash
export APP_ID=<application-id>
export BOT_TOKEN=<bot-token>
export GUILD_ID=<server-id>   # Discord → right-click server → Copy Server ID (Developer Mode must be on)

curl -X POST "https://discord.com/api/v10/applications/$APP_ID/guilds/$GUILD_ID/commands" \
  -H "Authorization: Bot $BOT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "mc",
    "description": "Control the Minecraft server",
    "options": [
      {
        "name": "sub",
        "description": "Action to perform",
        "type": 3,
        "required": true,
        "choices": [
          {"name": "start",   "value": "start"},
          {"name": "stop",    "value": "stop"},
          {"name": "status",  "value": "status"},
          {"name": "players", "value": "players"}
        ]
      }
    ]
  }'
```

Drop `/guilds/$GUILD_ID` from the path for a global command.

**If the response is `{"message": "Missing Access", "code": 50001}`:** the bot is not installed in that guild (or was installed without `applications.commands` scope). Run the OAuth URL from the previous section and re-invite.

---

## How to Update the Discord Public Key

Discord owns the signing key pair. Your Lambda only needs the **public key** to verify
incoming interaction signatures. You cannot rotate this key yourself — Discord controls it.

1. **Copy the public key** from the Discord Developer Portal:
   - Go to your Application → General Information.
   - Copy the **Public Key** (hex-encoded string).

2. **Store it in AWS Secrets Manager:**
   ```bash
   aws secretsmanager put-secret-value \
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

## How to Test the Idle-Stop Path Without Waiting 15 Minutes

The natural path requires 15 consecutive minutes of zero players. To force-exercise the EventBridge → Lambda → `ec2:StopInstances` wiring, flip the alarm state manually. **CloudWatch emits EventBridge events on state *transitions*, not on current state** — so if the alarm is already in `ALARM`, setting it to `ALARM` again is a no-op. Always flip to `OK` first:

```bash
aws cloudwatch set-alarm-state --alarm-name MCServerInstance-idle-stop \
  --state-value OK --state-reason "pre-test reset"

aws cloudwatch set-alarm-state --alarm-name MCServerInstance-idle-stop \
  --state-value ALARM --state-reason "test idle-stop"

# Confirm EventBridge fired and Lambda ran:
aws cloudwatch get-metric-statistics --namespace AWS/Events --metric-name Invocations \
  --dimensions Name=RuleName,Value=MCServerInstance-idle-stop \
  --start-time $(date -u -v-5M +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 60 --statistics Sum --output table

# Confirm the instance is stopping:
sleep 15
aws ec2 describe-instances --filters Name=tag:Name,Values=MCServerInstance \
  --query "Reservations[].Instances[].State.Name" --output text
```

Note: setting the alarm state manually is *temporary*. On the next evaluation period (~60s), the alarm re-evaluates the real data and may transition back. That's expected.

---

## How to Test `/mc start` Without Discord

The Lambda has a direct-invocation path that mirrors the Discord slash command without the signature-verification dance. Useful for testing the full start-and-rearm flow in isolation.

```bash
# Trigger start + alarm reset
aws lambda invoke --function-name MCServerInstance-server-controller \
  --payload '{"action":"start"}' --cli-binary-format raw-in-base64-out /tmp/out.json
cat /tmp/out.json
# expect: {"started": true, "instance_id": "...", "alarm_reset": true}

# Confirm alarm is back in OK
aws cloudwatch describe-alarms --alarm-names MCServerInstance-idle-stop \
  --query "MetricAlarms[0].StateValue" --output text
# expect: OK
```

The equivalent `{"action":"stop"}` payload is what EventBridge sends on idle-stop — you can invoke it directly for testing too.

---

## Troubleshooting

Lessons learned during the initial stand-up, preserved here so you don't relearn them.

### `tofu apply` → `dlm:TagResource` AccessDeniedException
Your IAM user needs DLM permissions that aren't in any AWS-managed policy. See *How to Configure IAM for `tofu apply`*.

### `InvalidBlockDeviceMapping: Volume of size 8GB is smaller than snapshot 'snap-…', expect size >= 30GB`
The AL2023 arm64 AMI snapshot grew to 30 GB. `modules/compute/main.tf` sets `root_block_device.volume_size = 30` — do not lower it unless you also use an older AMI.

### `cors.allowMethods failed: Member must have length less than or equal to 6`
Lambda Function URL CORS only accepts HTTP methods of length ≤ 6, so `"OPTIONS"` is invalid. Preflight (OPTIONS) is handled automatically — just list the real methods (`POST`, `GET`, etc.).

### Cloud-init fails to parse user-data YAML (`could not find expected ':'`)
Don't use bash heredocs inside a `runcmd: - |` block scalar — lines that start at column 0 (like `[Unit]`) terminate the YAML block and break parsing. Move file contents into cloud-init's `write_files` directive instead (see `modules/compute/scripts/compute_setup.sh.tpl` for the pattern).

### Changes to `user_data` don't take effect
`aws_instance.user_data_replace_on_change` must be `true` — otherwise Terraform updates the attribute in state but AWS never re-boots, so the new script never runs. It's set in `modules/compute/main.tf`; don't remove it.

### Lambda returns `Runtime.ImportModuleError: No module named '_cffi_backend'`
The vendored Python deps in `server_controller/` were built for the wrong platform (typically macOS instead of Linux arm64), so the C extension is missing. Rebuild them — see *How to Build / Rebuild the Lambda Dependencies*.

### Instance auto-stops immediately after `/mc start`
If `treat_missing_data` on the alarm is `"breaching"`, the 15 minutes of missing data from when the instance was stopped stays in the evaluation window — and re-arming via `set_alarm_state(OK)` only lasts one evaluation period before the alarm flips back. The fix already applied: `treat_missing_data = "notBreaching"` in `modules/monitoring/main.tf`. Don't revert it without a plan.

### `set-alarm-state ALARM` doesn't fire EventBridge
CloudWatch emits events on state *transitions*, not current state. Always set `OK` first, then `ALARM`, to force a transition.

### Discord portal rejects the Interactions Endpoint URL
Discord sends a signed ping as part of *Save Changes*. The Lambda can only answer if:
  - the public key is stored in Secrets Manager as valid JSON: `{"public_key":"<hex>"}`, and
  - the Lambda code imports cleanly (no `_cffi_backend` error).

Tail `aws logs tail /aws/lambda/MCServerInstance-server-controller --follow` while you save and you'll see the exact failure.

### Slash-command registration returns `{"code": 50001}` (Missing Access)
The bot isn't installed in the target guild, or was installed without the `applications.commands` scope. Run the OAuth URL from *How to Set Up the Discord Application* to (re-)install.

### `/mc status` reports the wrong player count
PaperMC's `list` output is `"There are <N> of a max of <M> players online: …"` — a whitespace-tokenizing parser will happily grab `<M>` (the slot max) instead of `<N>`. The handler in `server_controller/controller.py` uses a regex (`r"There are (\d+)"`) that matches the same pattern as the shell-side metric publisher in `compute_setup.sh.tpl`. Keep them aligned if you touch either.

### Discord shows "is thinking…" forever after a slash command
The initial deferred ack succeeded but the async follow-up never reached Discord. Two common causes:

1. **Cloudflare 1010 on the follow-up PATCH.** Logs show `Discord follow-up failed: 403 error code: 1010`. Cloudflare (which fronts `discord.com`) blocks requests whose `User-Agent` is a default Python `urllib` string. Fix: every outbound HTTP call to `discord.com` must set `User-Agent` — see `DISCORD_USER_AGENT` in `server_controller/controller.py`.

2. **Missing `lambda:InvokeFunction` on self.** Logs show only the first (sync) invocation, never a second one with `async=True`. The IAM role needs to allow invoking its own function — see the `InvokeSelfForDeferredDiscord` statement in `modules/control/main.tf`. Verify with:
   ```bash
   aws iam get-role-policy --role-name MCServerInstance-controller-role \
     --policy-name ... | jq '.PolicyDocument.Statement[] | select(.Sid=="InvokeSelfForDeferredDiscord")'
   ```

### `application did not respond` on every slash command
The endpoint returned an error or exceeded 3 seconds before sending a deferred ack. Check CloudWatch for `Runtime.ImportModuleError` (Python deps built for the wrong platform/version — see *How to Build / Rebuild the Lambda Dependencies*) or an exception from `verify_discord_signature` (stale public key in Secrets Manager).

### RCON is reachable from the public internet
By design (SG allows 25575 from `0.0.0.0/0`) — password-protected but not lovely. If you want to tighten, restrict the ingress CIDR in `modules/network/main.tf` or remove the rule and reach RCON via SSM port-forward from the Lambda / admin box.

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

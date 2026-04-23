# mc-server-iac

Infrastructure-as-Code for a shared Minecraft server on AWS EC2, controlled via Discord.

## Overview

`mc-server-iac` deploys a Minecraft server on a single `t4g.large` EC2 instance (Amazon Linux 2023, arm64) with an Elastic IP and Cloudflare DNS record (`mc.rsinema.com`). The server is started and stopped on demand via Discord slash commands — spun up when friends want to play, stopped automatically when idle for 15 minutes.

## Architecture

```mermaid
flowchart LR
    subgraph AWS["AWS (us-west-2)"]
        subgraph Compute["compute"]
            EC2["EC2 Instance<br/>AL2023 arm64<br/>itzg/minecraft-server"]
            EBS["EBS Data Volume<br/>gp3, encrypted<br/>DLM daily snapshots"]
        end

        subgraph Control["control"]
            Lambda["Lambda Function URL<br/>server_controller"]
            Secrets["Secrets Manager<br/>RCON password<br/>Discord signing key"]
        end

        subgraph Monitoring["monitoring"]
            CWAlarm["CloudWatch Alarm<br/>PlayerCount == 0<br/>for 15 min"]
            EB["EventBridge Rule<br/>idle-stop"]
        end

        EIP["Elastic IP"]
    end

    Discord["Discord<br/>/mc start|stop|status|players"]
    Cloudflare["Cloudflare<br/>mc.rsinema.com<br/>A record"]

    Discord -->|"Ed25519-signed POST"| Lambda
    Lambda -->|"ec2:Start/Stop"| EC2
    Lambda -->|"GetSecretValue"| Secrets
    Lambda -->|"RCON"| EC2
    Lambda -->|"cloudwatch put-metric-data"| CWAlarm
    EC2 -->|"PlayerCount metric"| CWAlarm
    CWAlarm -->|"ALARM"| EB
    EB -->|"invoke"| Lambda
    EC2 --> EIP
    EIP --> Cloudflare
    Cloudflare --> Players["Players"]

    style Discord fill:#5865F2,color:#fff
    style Cloudflare fill:#F38020,color:#fff
    style EC2 fill:#FF9900,color:#fff
    style Lambda fill:#FF9900,color:#fff
    style EBS fill:#3FB37B,color:#fff
```

## How Friends Connect

1. Install Minecraft and connect to `mc.rsinema.com:25565`.
2. Ask someone in the Discord to run `/mc whitelist add user:<your_mojang_username>` — the server enforces an allowlist and rejects unrecognized accounts.
3. Use `/mc start` in Discord to spin up the server.
4. The server stops itself 15 minutes after the last player disconnects.
5. Run `/mc help` in Discord to see every available command.

## Quickstart

### Prerequisites

- [OpenTofu](https://opentofu.org/) ≥ 1.8
- Python 3.11 and `pip` (used to build Lambda dependencies as Linux arm64 wheels — the runtime target)
- AWS CLI configured with credentials for the target account
- IAM user/role with the required permissions (EC2, IAM, Secrets Manager, Lambda, CloudWatch, EventBridge, and **DLM including `dlm:TagResource`** — see the runbook)
- `CLOUDFLARE_API_TOKEN` env var (for DNS management)
- Discord application with public key, bot token, and application ID

### Deploy

```bash
git clone https://github.com/rsinema/mc-server-iac.git
cd mc-server-iac

# Set required env vars
export CLOUDFLARE_API_TOKEN=<your-token>

# Build Lambda deps targeting the runtime (Linux arm64, Python 3.11).
# MUST be done before tofu apply, and re-run whenever requirements.txt changes.
pip3 install \
  --target server_controller/ \
  --platform manylinux2014_aarch64 \
  --python-version 3.11 \
  --implementation cp \
  --only-binary=:all: \
  --upgrade \
  -r server_controller/requirements.txt

# Optional: override defaults via terraform.tfvars (not committed)
echo 'discord_webhook_url = "https://discord.com/api/webhooks/..."' > terraform.tfvars

tofu init
tofu validate
tofu plan
tofu apply
```

### Post-deploy Setup

The order below matters: Discord verifies the Interactions Endpoint URL by sending a signed ping, which the Lambda can only answer once the public key is in Secrets Manager.

1. **Store the Discord public key in Secrets Manager** (from Discord Developer Portal → General Information):
   ```bash
   aws secretsmanager put-secret-value \
     --secret-id MCServerInstance-discord-signing-key \
     --secret-string '{"public_key":"<hex>"}'
   ```
2. **Set the Interactions Endpoint URL** in the Discord portal to the Lambda Function URL (from `tofu output function_url`) and click _Save Changes_.
3. **Install the bot** to your guild with the OAuth URL (scopes: `bot applications.commands`).
4. **Register the `/mc` slash command** against your guild via the Discord REST API.

Full step-by-step (including the exact OAuth URL format and the command-registration curl) is in [docs/runbook.md](./docs/runbook.md#how-to-set-up-the-discord-application).

## Cost Estimate

| Resource                                   | Monthly Cost (est.)              |
| ------------------------------------------ | -------------------------------- |
| EC2 `t4g.large` (stopped = no charge)      | ~$0 if stopped most of the month |
| EBS `gp3` 10 GB                            | ~$1.10                           |
| Lambda invocations (start/stop/idle-check) | ~$0.05                           |
| CloudWatch metrics + alarm                 | ~$0.10                           |
| EIP (always allocated)                     | ~$3.65                           |
| Cloudflare DNS                             | $0                               |
| **Total (idle)**                           | **~$4.90/mo**                    |
| **Total (running full-time)**              | **~$53/mo**                      |

Running 2×/week for ~4 hours per session ≈ 32 hours/month → **~$7/mo** (EC2 + fixed costs).

## Documentation

| File                                 | Purpose                                               |
| ------------------------------------ | ----------------------------------------------------- |
| [PLAN.md](./PLAN.md)                 | Single source of truth for the revival effort         |
| [CLAUDE.md](./CLAUDE.md)             | AI session context: conventions, commands, boundaries |
| [AGENTS.md](./AGENTS.md)             | Rules for coding agents                               |
| [docs/runbook.md](./docs/runbook.md) | Ops procedures                                        |

## License

MIT

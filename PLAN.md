# Minecraft Server Revival — Implementation Plan

**Status:** drafted 2026-04-15, not yet executed.
**Owner:** @rsinema
**Scope:** implements GitHub issues #1, #2, #3, #4.

This document is the single source of truth for the revival effort. It is meant to survive across Claude sessions — if you (or a future model) are picking this up cold, read this top-to-bottom before touching code.

---

## 1. Context

`mc-server-iac` was built ~2024 as a learning project to explore Terraform/AWS. It stands up a vanilla Minecraft server on EC2 behind an API-Gateway-fronted Lambda for start/stop/status control. It is being revived in 2026 so the owner and coworkers (based in Utah) can play together on a shared server, spun up ~2x/week.

Four tracked issues drive the work:

- **#1** — Update repo to a more current infra config.
- **#2** — Add documentation (specifically for Claude to use).
- **#3** — Add start/stop automation based on connection status.
- **#4** — Automate DNS so the server is reachable at a stable hostname.

---

## 2. Decisions Log

Locked-in choices (with rationale), so future sessions don't re-litigate them:

| Decision | Choice | Why |
|---|---|---|
| IaC tool | **OpenTofu** (swap from Terraform) | Community-governed, license-clean, drop-in for current HCL. No code cost. |
| Provider versions | AWS `~> 6.x`, random `~> 3.6`, archive `~> 2.6`, cloudflare `~> 5.x` | All currently pinned versions are stale. |
| OS | **Amazon Linux 2023** (arm64) | AL2 EOL is 2026-06-30. AL2023 ships newer Corretto and uses `dnf`. |
| Instance type | **`t4g.large` kept for now** | Confirmed sufficient; revisit if cost matters post-launch. |
| MC server flavor | **PaperMC** via `itzg/minecraft-server` Docker image | Better TPS than vanilla, version bumps become a single env var, RCON + plugin support out of the box, negligible Docker overhead on Graviton. |
| Control plane | **Lambda Function URL + Discord bot** (replaces API Gateway REST) | Collapses ~350 lines of API GW HCL, removes the API-key-in-repo pattern, natively fits Discord's Ed25519-signed interactions, cheaper. |
| Idle-stop pattern | **CloudWatch custom metric + alarm → EventBridge → Lambda** | Metric is observable, alarm is tunable without SSH, reuses existing controller Lambda IAM. |
| Wake pattern | **Discord slash command** (`/mc start`) | Friend group lives in Discord already; doubles as game chat. |
| DNS | **EIP + static `cloudflare_record` A record** for `mc.rsinema.com` | ~$3.60/mo is acceptable; eliminates the dynamic-DNS-via-EventBridge complexity. |
| Shell access | **SSM Session Manager** (drop SSH entirely) | No port 22, no key rotation, no `home_ip` variable, no firewall carve-out. Stronger posture. |
| Port 25565 | **Open to `0.0.0.0/0`** + MC whitelist | Friends join from rotating home IPs; relying on the Minecraft whitelist + strong server props is the pragmatic answer. |
| Region | **`us-west-2`** | Confirmed — Utah-based users, west-coast region is fine. |
| Layout | **Modular** (see §4) | Learning exercise value; cleaner outputs across concerns. |
| State | **Remote (S3 backend + native locking)** | DynamoDB no longer required (AWS added native S3 state locking in 2024). Modules read each other's outputs more cleanly with remote state. |
| Secret hygiene | Treat existing `api_key` in `variables.tf:52` as **leaked**; delete it. | It's in git history. The Function URL swap removes the need for it entirely. |
| Backups (S3 nightly tar) | **Deferred** to next iteration | Will still add DLM daily EBS snapshots now as a safety net. |
| CI (GH Actions) | **Deferred** to next iteration | |

---

## 3. Target Architecture

```
Discord slash command
        │  (Ed25519-signed interaction)
        ▼
Lambda Function URL ───► server_controller (Python)
                               │
                               ├── ec2:StartInstances / StopInstances
                               ├── RCON to running MC container
                               └── cloudwatch:PutMetricData

EC2 (AL2023, arm64, EIP, SSM-managed)
  └── Docker: itzg/minecraft-server (TYPE=PAPER, VERSION=<var>)
        └── EBS data volume (encrypted, DLM daily snapshots)

CloudWatch metric  PlayerCount
        │
        ▼
Alarm: PlayerCount == 0 for 15 min
        │
        ▼
EventBridge rule ─► Lambda (stop instance)

Cloudflare: mc.rsinema.com  A  <EIP>
```

No more API Gateway, no more `home_ip`, no more SSH.

---

## 4. Module Layout

```
.
├── main.tf              # root composition
├── variables.tf         # root-level inputs
├── outputs.tf
├── versions.tf          # (renamed from terraform.tf)
├── backends.tf          # S3 backend
├── PLAN.md              # this file
├── CLAUDE.md            # (added in issue #2)
├── README.md            # (rewritten in issue #2)
└── modules/
    ├── network/         # SG (25565 public, all egress), EIP
    ├── compute/         # AMI lookup, EC2, IAM instance profile (SSM + CW + RCON),
    │                    #   cloud-init user-data that installs Docker + runs itzg
    ├── storage/         # EBS data volume, attachment, DLM snapshot policy
    ├── control/         # Lambda (server_controller) + Function URL + Discord
    │                    #   signature verify, CW permissions
    ├── dns/             # Cloudflare provider, zone lookup, A record
    └── monitoring/      # CloudWatch alarm, EventBridge rule, stop-Lambda wiring
```

Root module wires them together. Each module has its own `variables.tf` / `outputs.tf` / `main.tf`.

---

## 5. Execution Order

Agreed sequence: **#2 → #1 → #4 → #3**.

- #2 first so the docs ground future AI sessions before we start mutating code.
- #1 is the foundation (providers, AL2023, Docker, Lambda Function URL swap, modularization, secret rotation, EBS snapshots, SSM, state backend).
- #4 comes before #3 because DNS/EIP changes the instance's public-IP story, which the monitoring module needs to be aware of.
- #3 lands last and uses the IAM / CloudWatch wiring added in #1.

---

## 6. Issue #2 — Documentation

### Deliverables

- **`CLAUDE.md`** at repo root. Contents:
  - Project summary and revival context (point at this PLAN.md).
  - File/module map.
  - Conventions: naming (`${var.server_name}-*`), tag strategy, sensitive-variable policy (**never** commit defaults for secrets), one-resource-per-concern.
  - Commands: `tofu fmt`, `tofu validate`, `tofu plan`, `tofu apply`, how to read state, how to rotate the Discord bot key.
  - Boundaries: what Claude should *not* touch without confirmation (root module composition, IAM trust policies, state backend config, anything in `modules/*/outputs.tf` that other modules depend on).
- **`AGENTS.md`** — short, rules-of-the-road for any coding agent (lint before commit, run `tofu validate`, don't commit `.tfvars`).
- **`README.md`** — rewritten. Quickstart, architecture Mermaid diagram, cost estimate, "how friends connect" (Discord + `mc.rsinema.com`).
- **`docs/runbook.md`** — small ops runbook: how to bump MC version, how to restore from snapshot, how to rotate the Discord signing key, how to add a friend to the whitelist.

---

## 7. Issue #1 — Modernize infra + OpenTofu migration

Largest chunk. Ordered internally so each step leaves the repo in a plannable state.

1. **Rename `terraform.tf` → `versions.tf`**; drop `required_version` pin or set to `~> 1.8` (OpenTofu). Bump provider versions.
2. **Add S3 backend** (`backends.tf`) with native state locking. Bootstrap the backend bucket manually once, then configure.
3. **Delete `api_key` variable and the entire `api_gateway.tf`**. The `server_controller` Lambda will move behind a Function URL instead.
4. **Module split** per §4. Start by moving existing resources verbatim into the right module; only refactor semantics after the split plans clean.
5. **`network/`:**
   - SG: ingress 25565/tcp from `0.0.0.0/0`, no SSH rule, egress open.
   - `aws_eip` resource.
6. **`compute/`:**
   - `data "aws_ami"` for latest AL2023 arm64.
   - IAM instance profile with: `AmazonSSMManagedInstanceCore` (for Session Manager), `cloudwatch:PutMetricData`, scoped S3 put on the (future) backup bucket, `ec2:StopInstances` on *self* via a condition key.
   - `aws_instance` with `associate_public_ip_address = true`, attached to the EIP via `aws_eip_association`.
   - User-data (new `modules/compute/scripts/compute_setup.sh.tpl`): install Docker, create systemd unit that runs `itzg/minecraft-server` with `TYPE=PAPER`, `VERSION=${minecraft_version}`, `MEMORY=${minecraft_memory}G`, `EULA=TRUE`, `ENABLE_RCON=true`, `RCON_PASSWORD=${rcon_password}`, volume mount `/opt/minecraft:/data`. Includes EBS volume wait/format/mount logic and player-count metric publisher. Wire up the previously-dead `minecraft_version` / `minecraft_memory` vars.
7. **`storage/`:**
   - `aws_ebs_volume` with `encrypted = true`.
   - `aws_volume_attachment` as today.
   - `aws_dlm_lifecycle_policy` — daily snapshot at 05:00 UTC, 7-day retention, targets by `Project=mc-server` tag.
8. **`control/`:**
   - `random_password` for RCON + Discord signing key stored in Secrets Manager (not in plaintext env).
   - Rewritten `server_controller.py`: handles Discord interaction verify (Ed25519 via `nacl`), routes `/mc status|start|stop|players`, calls EC2 APIs + RCON.
   - `aws_lambda_function_url` with `authorization_type = NONE` (Discord's signature is our auth).
   - IAM: EC2 start/stop/describe, Secrets Manager read, CloudWatch put, VPC access not needed (Lambda can be non-VPC since it only talks to AWS APIs + Discord).
9. **Tag strategy** — root-level `default_tags` block on the AWS provider: `Project = "mc-server"`, `ManagedBy = "opentofu"`, `Owner = var.owner_tag`.
10. **Secret rotation** — delete the leaked `api_key` from the repo; on first apply, the old API Gateway and key are destroyed anyway.
11. **Formatting** — `tofu fmt -recursive` before first commit.

---

## 8. Issue #4 — EIP + Cloudflare DNS

Tiny, lives in `modules/dns/`.

1. Add `cloudflare/cloudflare` provider to `versions.tf`.
2. Cloudflare API token provided via `CLOUDFLARE_API_TOKEN` env var (never in `.tfvars`). Token scope: Zone:DNS:Edit on `rsinema.com` only.
3. `data "cloudflare_zone"` for `rsinema.com`.
4. `resource "cloudflare_record"` — name `mc`, type `A`, content `aws_eip.mc.public_ip`, proxied `false` (Cloudflare proxy breaks Minecraft's TCP protocol), ttl `60`.
5. Output the hostname for use in README.

No EventBridge, no dynamic updates — the EIP is stable by definition.

---

## 9. Issue #3 — Idle-stop + Discord wake

Uses IAM + CloudWatch wiring from #1.

### Wake (pull)

Already covered by the Discord bot in `modules/control/`. `/mc start` → Lambda → `ec2:StartInstances` → poll until running → respond in Discord with "server up at `mc.rsinema.com`".

### Idle-stop (push)

1. **Metric publisher** — sidecar inside the EC2, either:
   - Option A (simpler): a cron/systemd-timer on the host that runs `rcon-cli list`, parses the player count, and `aws cloudwatch put-metric-data` every 60s.
   - Option B: a small Python exporter container alongside the MC container, same logic.
   Start with Option A.
2. **CloudWatch alarm** — `PlayerCount == 0` for 15 consecutive minutes → ALARM state.
3. **EventBridge rule** — matches the alarm state change → targets the `server_controller` Lambda with a `{"action": "stop"}` payload.
4. **Grace period** — alarm only evaluates while the instance is running (dimension filter on `InstanceId`), so a stopped instance doesn't get re-triggered.
5. **Discord notification** — Lambda posts "Server stopped — idle for 15m. Use `/mc start` to resume." to a configured Discord channel via webhook.

Tunables exposed as variables: `idle_stop_minutes` (default 15), `idle_check_interval_seconds` (default 60).

---

## 10. Explicitly Deferred

- **S3 world backups** (nightly tar + upload + lifecycle to Glacier). IAM role already permits it; just needs a systemd timer and an `aws_s3_bucket_lifecycle_configuration`. Next iteration.
- **CI** — GitHub Actions for `tofu fmt --check`, `tofu validate`, `tflint`, `trivy`, and plan-on-PR. Next iteration.
- **Downsize to `t4g.medium`** — revisit after a few sessions of real play.
- **AWS Budgets** resource with email alert — small, worth adding when CI lands.
- **Mermaid architecture diagram in README** — produce once the code is real.

---

## 11. Open Followups

- Cloudflare API token: how it's provisioned locally for `tofu apply` (env var vs. 1Password shim) — decide when we get to #4.
- Discord application setup (public key, bot token, application ID) — needs a one-time manual step outside Terraform; document in the runbook.
- Decide whether to manage the Cloudflare token rotation via Terraform or leave it manual.

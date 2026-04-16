# CLAUDE.md

**Project:** `mc-server-iac` — Minecraft Server Revival
**Status:** In progress (see [PLAN.md](./PLAN.md) for full context)
**Owner:** @rsinema

---

## Project Summary

`mc-server-iac` is an Infrastructure-as-Code project to deploy a shared Minecraft server on AWS EC2 (AL2023, arm64), controlled via a Discord bot. The server is spun up on demand (~2x/week) and stopped when idle to save cost. Friends based in Utah connect via `mc.rsinema.com`.

### Key Architecture Decisions (see PLAN.md §2 for full table)

| Decision | Choice |
|---|---|
| IaC tool | **OpenTofu** (drop-in for Terraform HCL) |
| OS | Amazon Linux 2023 (arm64) |
| Instance | `t4g.large` |
| MC server | PaperMC via `itzg/minecraft-server` Docker image |
| Control plane | Lambda Function URL + Discord slash commands |
| Idle-stop | CloudWatch metric → Alarm → EventBridge → Lambda |
| DNS | Cloudflare A record for `mc.rsinema.com` → EIP |
| Shell access | SSM Session Manager (no SSH) |
| State | Remote S3 backend with native locking |

---

## File / Module Map

```
.
├── main.tf              # root composition (modules wired here)
├── variables.tf         # root-level inputs
├── outputs.tf
├── versions.tf          # provider versions and constraints (formerly terraform.tf)
├── backends.tf          # S3 backend configuration
├── README.md
├── CLAUDE.md            # this file
├── AGENTS.md            # rules for coding agents
├── PLAN.md              # single source of truth for revival effort
├── docs/
│   └── runbook.md      # ops procedures
├── modules/compute/scripts/
│   └── compute_setup.sh.tpl  # cloud-init user-data template
├── server_controller/
│   ├── __init__.py
│   ├── controller.py        # Lambda handler
│   └── requirements.txt
└── modules/
    ├── network/         # security group, EIP
    ├── compute/         # EC2 instance, IAM profile, user-data
    ├── storage/         # EBS volume, DLM snapshot policy
    ├── control/         # Lambda Function URL, Discord integration
    ├── dns/             # Cloudflare record
    └── monitoring/      # CloudWatch alarm, EventBridge, stop Lambda
```

---

## Conventions

### Naming
- Resources follow `${var.server_name}-*` pattern (e.g., `MCServerInstance-sg`)
- Tags: `Project = "mc-server"`, `ManagedBy = "opentofu"`, `Owner = var.owner_tag`

### Sensitive Variables
- **Never** commit default values for secrets (API keys, passwords, tokens) to version control.
- Cloudflare API token: provided via `CLOUDFLARE_API_TOKEN` env var.
- Discord signing key: stored in AWS Secrets Manager (not in plaintext env).
- Use `var.*` with no default for required secrets; let terraform fail with a clear error if unset.

### One Resource Per Concern
- Each module owns its own `main.tf`, `variables.tf`, `outputs.tf`.
- No cross-module resource creation in root unless necessary for wiring outputs.

---

## Commands

### OpenTofu Workflow
```bash
# Format HCL (run before committing)
tofu fmt -recursive

# Validate configuration
tofu validate

# Preview changes
tofu plan

# Apply changes (requires AWS credentials)
tofu apply

# Inspect state
tofu state list
tofu state show <resource>
```

### State Management
- Remote state is configured in `backends.tf` — do not modify unless necessary.
- Never edit `terraform.tfstate` directly.
- If you need to debug state: `tofu state pull` to download, `tofu state push` to upload.

### Updating the Discord Public Key
Discord owns the signing key pair — you only store the **public key** for verification.
1. Copy the public key hex from Discord Developer Portal → Application → General Information.
2. Update Secrets Manager: `aws secretsmanager update-secret --secret-id <name> --secret-string '{"public_key":"<hex>"}'`.
3. No `tofu apply` needed — the Lambda reads the key from Secrets Manager on each invocation.

---

## Boundaries

**Do NOT touch without explicit confirmation:**
- Root module composition (`main.tf`) — any structural changes to how modules are wired together.
- IAM trust policies — changes here can affect security posture.
- State backend configuration (`backends.tf`) — state locking depends on this.
- Outputs in `modules/*/outputs.tf` that other modules depend on — changing these breaks downstream consumers.

**Safe to modify without confirmation:**
- `variables.tf` within any module (adding new variables, updating descriptions).
- Documentation files (`*.md`, `docs/*`).
- `scripts/server_setup.sh.tpl` user-data template.
- `server_controller/controller.py` (Lambda handler code).

---

## Current Execution Order

- [x] **#2** Documentation — CLAUDE.md, AGENTS.md, README.md, docs/runbook.md
- [x] **#1** Modernize infra + OpenTofu migration — modularization, AL2023, Docker, Lambda Function URL, IAM fixes, DLM snapshots, SSM
- [x] **#4** EIP + Cloudflare DNS — `modules/dns/` implemented
- [x] **#3** Idle-stop + Discord wake — monitoring module wired, player metric publisher in user-data

See PLAN.md §5 for rationale.

---

## One-Time Setup (Discord Application)

Before the control plane can work, a Discord application must exist outside Terraform:

| Field | Where to Find It |
|---|---|
| `DISCORD_PUBLIC_KEY` | Discord Developer Portal → your Application → General Information |
| `DISCORD_BOT_TOKEN` | Discord Developer Portal → your Application → Bot → Token |
| `DISCORD_APPLICATION_ID` | Discord Developer Portal → your Application → General Information → Application ID |

These values are stored in AWS Secrets Manager and referenced by the control Lambda. See `docs/runbook.md` for the full procedure.

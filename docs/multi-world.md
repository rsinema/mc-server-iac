# Multi-World Support — Implementation Plan

**Status:** Draft (not yet implemented)
**Branch:** `feat/multi-world`
**Goal:** Run several Minecraft worlds (e.g. survival + skyblock) on the *same* EC2 instance, one live at a time, switchable from Discord — without adding instances, ports, or DNS records.

---

## 1. End State

- One EC2 instance, one container, one port (`25565`), one DNS record — all unchanged.
- Multiple **world profiles**, each a self-contained `/data` directory (its own `world/`,
  `server.properties`, `plugins/`, `ops.json`, `whitelist.json`) living side-by-side on the
  existing EBS volume:

  ```
  /opt/minecraft/
    worlds/
      survival/     <- full /data for the survival world
      skyblock/     <- full /data for the skyblock world (own plugins, own generator)
    active            <- (runtime) the profile dir the container currently mounts
  ```

- The active profile is stored in SSM and selected **at container-start time**, so the flow is:

  ```
  /mc world set skyblock   ->   /mc stop   ->   /mc start   ->  skyblock boots
  ```

- All profiles are covered by the existing DLM snapshot policy automatically (same volume).

### Why a full `/data` per world (not just a swapped world folder)

Skyblock is not "a different map" — it is a plugin (e.g. BentoBox + BSkyBlock) plus a
different generator and `server.properties`. Giving each profile its own `/data` lets
survival stay vanilla while skyblock runs its own plugin set and config, with no cross-talk.

---

## 2. Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Where profiles live | `/opt/minecraft/worlds/<name>/` on the current EBS volume | Same volume ⇒ same snapshots, no new storage resources |
| How the active world is chosen | SSM param `/<server_name>/active-world`, read by a wrapper `ExecStart` script at boot | cloud-init `runcmd` only runs on **first** boot; `minecraft.service` starts on **every** boot, so selection must live in the service |
| When a switch takes effect | Next cold start (`stop` → `start`) | Simplest and safe; no live container juggling |
| Switching while running | **Warn and defer** — write the param, tell the caller it applies after `stop`/`start` | Avoids `ssm:SendCommand` and live-restart complexity |
| Unknown / new profile on start | **Auto-create** an empty dir; itzg generates a fresh vanilla world there | New survival-style worlds "just work"; skyblock remains a deliberate pre-provision |
| World registry (for `list` / validation) | SSM param `/<server_name>/world-list`, seeded from `var.world_profiles` | The Lambda has no filesystem access, so the set of known worlds must live in SSM |
| Discord surface | `/mc world list`, `/mc world set <name>` (admin), active world shown in `/mc status` | Mirrors the existing waypoint/register command shape |
| Stats / leaderboard | **Out of scope for now.** Stats sync follows the survival profile only | Keep the existing Enzy pipeline unchanged; skyblock sessions do not feed the leaderboard |

### Critical detail

Starting a **stopped** instance does **not** re-run cloud-init `runcmd` (it runs once per
instance, on first boot). Therefore world selection cannot live in `runcmd` — it must run on
every boot, which means inside `minecraft.service` (a wrapper `ExecStart` script). The EBS
data volume re-mounts on every boot via the existing `/etc/fstab` entry.

---

## 3. Changes by Component

### 3.1 `modules/control/main.tf` — two new SSM params

Same pattern as the existing `waypoints` param (seeded, `ignore_changes = [value]` so the
Lambda owns live contents after create):

```hcl
resource "aws_ssm_parameter" "active_world" {
  name  = "/${var.server_name}/active-world"
  type  = "String"
  value = "survival"
  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "world_list" {
  name  = "/${var.server_name}/world-list"
  type  = "StringList"
  value = join(",", var.world_profiles)  # e.g. "survival,skyblock"
  lifecycle { ignore_changes = [value] }
}
```

### 3.2 `modules/control/main.tf` — IAM + Lambda env

Add one scoped statement to `aws_iam_policy.controller_lambda` (an IAM **permissions** policy
edit — flagged per CLAUDE.md boundaries, though not a trust-policy change):

```hcl
{
  Sid      = "WorldSelectReadWrite"
  Effect   = "Allow"
  Action   = ["ssm:GetParameter", "ssm:PutParameter"]
  Resource = [aws_ssm_parameter.active_world.arn, aws_ssm_parameter.world_list.arn]
}
```

Add to the Lambda `environment.variables`:

```hcl
ACTIVE_WORLD_PARAM = aws_ssm_parameter.active_world.name
WORLD_LIST_PARAM   = aws_ssm_parameter.world_list.name
```

The **instance** side needs no IAM change — `AmazonSSMManagedInstanceCore` (already attached
in `modules/compute/main.tf`) grants `ssm:GetParameter` on `*`, so the box can read
`active-world` directly.

### 3.3 `server_controller/controller.py` — the `/mc world` command

- Add to `COMMAND_REGISTRY`:

  ```python
  ("world", "list"): {"ack": "deferred", "ephemeral": False, "admin": False},
  ("world", "set"):  {"ack": "deferred", "ephemeral": True,  "admin": True},
  ```

- Add a name-validation regex (reuse the waypoint style): `^[a-z0-9_-]{1,32}$`.
- New handlers:
  - `run_world_list()` — read `WORLD_LIST_PARAM` + `ACTIVE_WORLD_PARAM`, render the list with a
    marker (★) on the active one.
  - `run_world_set(instance_id, name)` — validate `name` against the regex **and** membership in
    `world-list`; write `active-world`; if the instance state is `running`, append:
    *"applies after `/mc stop` then `/mc start`."*
- Wire both into `dispatch_async()`.
- Add the active world to `run_status()` output (e.g. `World: \`skyblock\``).
- Update `HELP_TEXT`.
- **Optional (post-MVP):** `/mc world add|remove` to edit `world-list` without a `tofu apply`.

### 3.4 `modules/compute/scripts/compute_setup.sh.tpl` — on-box logic

**(a) New wrapper script** `/opt/mc-server/run.sh` (via `write_files`) — reads SSM at exec time
and mounts the selected profile:

```bash
#!/bin/bash
set -e
DEFAULT=survival
NAME=$(aws ssm get-parameter --name "/${server_name}/active-world" \
        --query 'Parameter.Value' --output text 2>/dev/null || echo "$DEFAULT")
case "$NAME" in *[!a-z0-9_-]*|"") NAME=$DEFAULT ;; esac   # sanitize; fall back to survival
DIR="/opt/minecraft/worlds/$NAME"
mkdir -p "$DIR"                                            # auto-create new profiles
echo "$DIR" > /run/mc-active-world                        # so other scripts follow the active world
exec /usr/bin/docker run --name minecraft --rm \
    -v "$DIR:/data" \
    -e TYPE=PAPER -e PAPER_CHANNEL=experimental -e VERSION=${minecraft_version} \
    -e MEMORY=${minecraft_memory}G -e EULA=TRUE -e ALLOW_FLIGHT=TRUE \
    -e ENABLE_RCON=true -e RCON_PASSWORD=${rcon_password} \
    -e SERVER_PORT=25565 -e ENFORCE_WHITELIST=TRUE \
    -p 25565:25565 -p 25575:25575 \
    itzg/minecraft-server
```

Notes:
- Reading SSM at the moment of `ExecStart` sidesteps the systemd trap where `EnvironmentFile`
  is read *before* `ExecStartPre` runs (so an `ExecStartPre` that wrote the env file would be
  too late for the same start).
- `SEED` and `WHITELIST` are dropped from the forced env so each profile owns its own
  `server.properties` / whitelist — skyblock needs a different generator and plugins than
  survival. Seeds/whitelists become per-profile setup rather than globally forced.

**(b) `minecraft.service`** — change `ExecStart` to `/opt/mc-server/run.sh` and drop the inline
`docker run`. Keep `ExecStop` / `ExecStopPost` as-is.

**(c) One-time migration** in `runcmd` (idempotent — moves today's top-level `/data` into
`worlds/survival` on the first boot after cutover; instant, since it is a rename on the same
ext4 filesystem):

```bash
cd /opt/minecraft
if [ ! -d worlds/survival ] && [ -d world ]; then
  mkdir -p worlds/survival
  find . -maxdepth 1 -mindepth 1 ! -name worlds ! -name 'lost+found' \
       -exec mv -t /opt/minecraft/worlds/survival {} +
fi
mkdir -p worlds/survival
```

**(d) `sync_stats.sh`** — after migration the survival world lives at
`/opt/minecraft/worlds/survival/world`. Stats stay survival-only for now: point the sync at
that fixed path (or read `/run/mc-active-world` and no-op unless it ends in `/survival`), so
skyblock sessions do not feed the leaderboard and `server_stats/export.py` needs no change.

### 3.5 `modules/compute/main.tf` — pass `server_name` into the template

The `templatefile()` call does not currently pass `server_name`; add it so `run.sh` can build
the SSM param path:

```hcl
server_name = var.server_name
```

(`var.server_name` already exists in the compute module.)

### 3.6 Root `variables.tf` — new var + larger volume

```hcl
variable "world_profiles" {
  description = "Known Minecraft world profiles; seeds the world-list SSM param."
  type        = list(string)
  default     = ["survival"]
}
```

Bump `mc_volume_size` (currently **10 GB**) to ~20–30 GB so multiple worlds fit. gp3 growth is
online — resize the volume, then `growpart` / `resize2fs` on the box (add to runbook).

---

## 4. Rollout Order (safety-first)

1. **Snapshot the data volume manually** (`aws ec2 create-snapshot ...`) before applying — the
   migration moves live world data.
2. Implement the code changes; run `tofu fmt -recursive` and `tofu validate`.
3. `tofu apply` — creates the SSM params + IAM and **replaces the instance**
   (`user_data_replace_on_change = true`). The data volume persists; the new instance's
   `runcmd` performs the idempotent migration into `worlds/survival`.
4. Re-register the Discord slash command (the command JSON changes when the `world` subcommand
   group is added).
5. Verify:
   - `/mc start` → confirm survival loads from `worlds/survival` (join, check the base is intact).
   - `/mc world set skyblock` → `/mc stop` → `/mc start` → confirm the skyblock profile boots.
6. **Skyblock provisioning** (one-time, via SSM session): create
   `/opt/minecraft/worlds/skyblock/`, drop in the skyblock plugin (BentoBox + BSkyBlock) and its
   `server.properties` / generator config **before** the first `/mc world set skyblock`.

---

## 5. Testing / Verification

- `tofu validate` + `tofu plan` review (expect: 2 new SSM params, IAM policy update, instance
  replacement, Lambda env update).
- Post-apply smoke test: the rollout step 5 flow above.
- Confirm `/mc status` reports the correct active world.
- Confirm the idle-stop timer and RCON commands still work (they are world-agnostic).

---

## 6. Out of Scope / Future

- Per-world stats / leaderboards (deferred — survival-only for now).
- Live world switching without a restart (would need `ssm:SendCommand` + a restart path).
- `/mc world add|remove` to manage the registry without `tofu apply`.
- Running two worlds simultaneously on different ports (explicitly rejected — RAM + cost).

---

## 7. File Touch List

| File | Change | CLAUDE.md boundary |
|---|---|---|
| `modules/control/main.tf` | 2 SSM params, IAM statement, 2 Lambda env vars | IAM permissions edit — flag |
| `server_controller/controller.py` | `/mc world` command + status line | Safe to modify |
| `modules/compute/scripts/compute_setup.sh.tpl` | wrapper script, service `ExecStart`, migration, stats path | Safe to modify |
| `modules/compute/main.tf` | pass `server_name` into `templatefile()` | Within compute module |
| `variables.tf` (root) | `world_profiles` var, bump `mc_volume_size` | Safe to modify |
| `docs/runbook.md` | skyblock provisioning + volume-resize procedures | Docs — safe |

# Stats Leaderboard Export — Design Document

**Status:** Implemented — `modules/stats/` + `server_stats/`; export Lambda ships with `DRY_RUN=1` until the first deliberate POST
**Owner:** @rsinema
**Last updated:** 2026-05-31
**Related:** [`minecraft-push-job-specs.md`](../minecraft-push-job-specs.md) (Enzy push-job contract)

---

## 1. Goal

Export per-player Minecraft stats from the server on a daily cadence to the Enzy leaderboard API, so the friend group can see who's killed the most mobs, mined the most diamonds, etc. The Enzy side stores dated daily rows; all-time and period totals are derived there by summing dailies.

Constraints that shape everything below:
- The server is **stopped ~5 days/week** (spun up on demand ~2x/week, idle-stopped when empty). The export pipeline must work when the server is **off**.
- **Vanilla stats only** — no Paper plugins, no mods. PaperMC already writes per-player totals to `world/stats/<uuid>.json`.
- The Enzy column set **locks on the first POST** (see §7). Changing it later is a **breaking change** for the leaderboard. We are defining the initial shape from scratch; nothing is provisioned on either side yet.

---

## 2. Decisions (resolved)

| Decision | Choice | Rationale |
|---|---|---|
| Stats source | Vanilla `world/stats/<uuid>.json` only | No plugin to maintain; covers the chosen stats. |
| Tracked stats | creeper kills, deaths, diamonds mined, distance traveled, achievements | Decided by us; see §3. |
| `xpGained` | **Dropped** | Vanilla has no usable lifetime-XP stat (`XpTotal` resets on death; not in stats files). |
| Cadence | Daily EventBridge cron, reads S3 snapshot | Decoupled from server uptime; matches the spec's state/idempotency model. |
| Cron time | Early **Mountain** morning (~11:00 UTC) | Utah players; avoids running during peak play (3am UTC ≈ 8–9pm MT) when the S3 snapshot is stale and the calendar date is wrong. |
| Data staging | On-box `mc-stats-sync` timer (every 5 min) + `ExecStopPost` end-of-session flush on `minecraft.service` | Decouples sync from the 30s-timeout control Lambda; crash-resilient; the stop hook closes the short-session gap. (Chose this over stop-Lambda SSM orchestration.) |
| Delta model | Job computes daily deltas vs. stored cumulative; Enzy sums | Enzy can only SUM, not subtract two snapshots. |
| State store | Previous-cumulative JSON in the stats S3 bucket | Simple, single source; saved only after a successful POST. |
| Player → email map | JSON **SSM Parameter**, written by `/mc register` | Players self-serve via Discord (resolves username→UUID via Mojang); editable without a deploy; keeps coworker emails out of the (public) git repo. |
| Lambda deps | **None** beyond stdlib + boto3 | Dropping XP means no NBT parsing; mirrors `server_controller`'s `urllib`-only style. |

---

## 3. Tracked stats & locked column set

### Column set (locks on first POST — 9 columns)

| Column | Type sent | Meaning |
|---|---|---|
| `snapshotKey` | string | `IdField`. `"<playerEmail>\|<YYYY-MM-DD>"` — unique per (player, day). |
| `playerEmail` | string | Leaderboard user-grouping key; must match a real Enzy user (workspace 52). |
| `snapshotDate` | string | `YYYY-MM-DD 00:00:00` (MySQL datetime). Must be non-blank on every row. |
| `mcUsername` | string | Minecraft username, for display. Resolved from `usercache.json`. |
| `creeperKillsGained` | string (int) | Creepers killed this day (delta). |
| `deathsGained` | string (int) | Deaths this day (delta). |
| `diamondsMinedGained` | string (int) | Diamond ore blocks broken this day (delta). |
| `distanceTraveledGained` | string (int) | Meters/blocks traveled this day (delta). |
| `achievementsGained` | string (int) | Advancements completed this day (delta). |

All values are serialized as **strings** (Enzy stores everything as `tinytext`), max **255 bytes** each.

### Vanilla stat mapping

Stat counters come from `world/stats/<uuid>.json` (`{"stats": {"minecraft:custom": {...}, "minecraft:mined": {...}, "minecraft:killed": {...}}, "DataVersion": N}`); the achievement count comes from `world/advancements/<uuid>.json`. All are monotonic cumulative integers, so daily deltas are `max(0, today − yesterday)`.

| Our column | Vanilla source | Notes |
|---|---|---|
| `creeperKillsGained` | `world/stats` → `minecraft:killed` → `minecraft:creeper` | Creepers specifically (not all mobs). |
| `deathsGained` | `world/stats` → `minecraft:custom` → `minecraft:deaths` | Direct. |
| `diamondsMinedGained` | `world/stats` → `minecraft:mined` → `minecraft:diamond_ore` **+** `minecraft:deepslate_diamond_ore` | **Must sum both variants** (deepslate exists since 1.18). Counts ore *blocks broken*, not item drops — so Fortune does not inflate it. |
| `distanceTraveledGained` | `world/stats` → `minecraft:custom` → sum of every key ending in `_one_cm`, ÷ 100 | Centimeters → meters (1 block = 1 m). Covers walk/sprint/crouch/swim/fly/fall/climb/boat/minecart/horse/pig/strider/elytra. Summing by suffix auto-includes any movement type. |
| `achievementsGained` | `world/advancements/<uuid>.json` → count of entries with `done == true`, **excluding** keys under `minecraft:recipes/` | Recipe unlocks are stored as advancements and would inflate the count by hundreds; exclude them so this tracks real achievements. The file also has a top-level `DataVersion` key to skip. |

> **Design calls worth a sanity check:** "diamonds mined" = ore blocks broken (both variants); "distance traveled" = total by all means, in meters; "achievements" = completed non-recipe advancements. If any is wrong (e.g. you want diamond *items* collected, distance in km, or recipe unlocks counted), say so before first POST — the column *name* is locked, though the computation behind it can change freely later.

---

## 4. Architecture

```
                     ┌─────────────────────────────────────────────┐
   PLAY SESSION  →   │ EC2 (itzg/minecraft-server, /opt/minecraft)  │
   (server up)       │   world/stats/<uuid>.json        (totals)    │
                     │   world/advancements/<uuid>.json (achiev.)   │
                     │   usercache.json                 (uuid→name) │
                     │                                              │
                     │   mc-stats-sync.timer (every 5 min):         │
                     │     rcon-cli save-all flush                  │
                     │     s3 sync stats/ + advancements/           │
                     │          + usercache.json                    │
                     └───────────────┬─────────────────────────────┘
                                     │  s3 sync (push, while up)
                                     ▼
                     ┌─────────────────────────────────────────────┐
   S3 (new bucket)   │ mc-stats-<suffix>/                           │
                     │   raw/stats/<uuid>.json                      │
                     │   raw/advancements/<uuid>.json               │
                     │   raw/usercache.json                         │
                     │   state/previous-cumulative.json             │
                     └───────────────┬─────────────────────────────┘
                                     │  read (server can be OFF)
                                     ▼
                     ┌─────────────────────────────────────────────┐
   DAILY EXPORT      │ Export Lambda (modules/stats/)               │
   (~11:00 UTC,      │  read raw stats + advancements + usercache   │
                     │       + email map (SSM)                      │
    EventBridge)     │  → compute cumulative per player             │
                     │  → diff vs state/previous-cumulative.json    │
                     │  → build rows (skip zero-delta & unmapped)   │
                     │  → POST array to Enzy (retry w/ backoff)     │
                     │  → on success: save new cumulative state     │
                     └───────────────┬─────────────────────────────┘
                                     │  POST /rest/n8n/processSimpleJSONArray
                                     ▼
                              Enzy leaderboard (workspace 52)
```

(The idle-stop flow is unchanged: `handle_stop_action` still just stops the instance. The periodic timer keeps S3 fresh while the server is up, and the `ExecStopPost` hook (§4.1) flushes the final state when the service stops — so even a short session lands in S3.)

### 4.1 On-box sync (`mc-stats-sync` timer)

A systemd timer in the EC2 user-data (`modules/compute/scripts/compute_setup.sh.tpl`), mirroring the existing `mc-monitor` player-count timer, runs `/opt/mc-stats/sync_stats.sh` every 5 minutes:

```bash
docker exec minecraft rcon-cli save-all flush   # flush world/stats to disk
aws s3 sync /opt/minecraft/world/stats/        s3://<bucket>/raw/stats/
aws s3 sync /opt/minecraft/world/advancements/ s3://<bucket>/raw/advancements/
aws s3 cp   /opt/minecraft/usercache.json      s3://<bucket>/raw/usercache.json
```

Every step is best-effort (`|| true`), so it no-ops when the container is down. The instance role gets a scoped `s3:PutObject` on `<bucket>/raw/*` (and `ListBucket` with a `raw/*` prefix condition) — see `modules/compute/main.tf`.

**Why a timer, not the stop Lambda:** the control Lambda's timeout is 30s and `run_start` already races it; bolting a synchronous SSM send-and-poll onto the stop path would be fragile and would only capture data on a *clean* idle-stop. A periodic on-box sync is decoupled, crash-resilient, and costs only a few KB of S3 PUTs per session.

**End-of-session flush (`ExecStopPost`).** The 5-minute timer can miss a session that ends before its first tick (short sessions, or a stop landing right at a boundary — exactly what we hit in testing). To close that gap, `minecraft.service` runs the same sync script as `ExecStopPost`: after `ExecStop=docker stop minecraft` (Paper saves stats to disk on SIGTERM), the hook pushes the final state to S3. Because the unit is `After=network-online.target`, systemd tears it down *before* networking on shutdown, so the `aws s3 sync` still has a live network. It's wrapped in `timeout 60` with a leading `-` so a slow or failed sync can't stall shutdown.

This is **best-effort**, not a guarantee: a graceful `ec2:StopInstances` (both idle-stop and `/mc stop`) runs it, but a forced termination won't — and that's fine, because stats are cumulative, so any un-synced tail is picked up by the next session's sync. The hook improves freshness and short-session capture; it isn't load-bearing for correctness.

**Deployment note:** `modules/compute` sets `user_data_replace_on_change = true`, so adding this timer **replaces the EC2 instance** on the next apply. World data survives (it lives on the separate EBS data volume from `modules/storage`, remounted by the runcmd). Apply during a quiet window.

### 4.2 Daily export Lambda (new `modules/stats/`)

EventBridge cron (~`cron(0 11 * * ? *)` — early MT) → Lambda:

1. Load the UUID→email map (SSM Parameter) and `raw/usercache.json` (UUID→username).
2. Read every `raw/stats/<uuid>.json` and `raw/advancements/<uuid>.json`; reduce to cumulative `{creeperKills, deaths, diamondsMined, distanceMeters, achievements}` per player.
3. Load `state/previous-cumulative.json`.
4. For each mapped player: `gained = max(0, today − previous)` per stat.
   - **Skip unmapped players** (no email) — logged, never posted with a null email.
   - **Skip zero-delta players** — no row when nothing changed (a no-play day produces no rows, consistent with "missed day = no row").
   - **First observation** (no prior state): record baseline, emit all-zero gains → effectively skipped. All-time totals therefore count gains since tracking began.
5. Build the row array (9 string columns each; truncate/skip any value >255 bytes).
6. POST to Enzy with retry/backoff (see §6, §7).
7. **On success only**, write the new cumulative back to `state/previous-cumulative.json`. On failure, leave state untouched so the next run re-diffs against last-good (the next delta spans the gap — acceptable).

---

## 5. New & changed infrastructure

### New — `modules/stats/`

| Resource | Purpose |
|---|---|
| `aws_s3_bucket` `<server>-stats-<suffix>` | Staging (`raw/`) + delta state (`state/`). Public access blocked, versioning on, lifecycle expires noncurrent `raw/` versions after 7 days. |
| `aws_lambda_function` export | Python 3.11, arm64, 120s timeout, `archive_file` zip — mirrors `modules/control`. stdlib + boto3 only, no vendored deps. Ships `DRY_RUN=1`. |
| `aws_cloudwatch_event_rule` + target + permission | Daily cron trigger (`var.stats_export_schedule`). |
| `aws_ssm_parameter` `/<server>/stats/player-email-map` | JSON `{ "<uuid>": {"email": ..., "name": ...}, ... }` (legacy bare-string values still accepted). Seeded empty with `ignore_changes = [value]`; written by the control Lambda's `/mc register`, no deploy to add a player. |
| IAM role/policy (Lambda) | `s3:GetObject`/`PutObject`/`ListBucket` on the bucket, `secretsmanager:GetSecretValue` on the Enzy secret, `ssm:GetParameter` on the map, logs. |

The Enzy secret (`aws_secretsmanager_secret` `<server>-enzy-api-key`) is created in the **root** module (alongside the rcon/Discord secrets) and its ARN passed into `modules/stats`; the value is populated out-of-band (§ runbook).

### Changed — existing modules

| File | Change |
|---|---|
| `modules/compute` (instance role) | Add `s3:PutObject` on `<bucket>/raw/*` and `s3:ListBucket` (prefix `raw/*`). SSM core already attached. |
| `modules/compute/scripts/compute_setup.sh.tpl` | New `mc-stats-sync` service + 5-min timer + `sync_stats.sh`; `stats_bucket` threaded through `templatefile`. |
| root `main.tf` / `variables.tf` | `aws_secretsmanager_secret.enzy_api_key`, `module "stats"`, wire bucket name/arn into `module.compute`; wire the email-map param name/arn into `module.control`; new `enzy_base_url` + `stats_export_schedule` vars. |
| `modules/control` + `server_controller/controller.py` | `/mc register add\|list\|remove` subcommand group: resolves username→UUID via the Mojang API and read-modify-writes the email-map SSM parameter. Adds `ssm:GetParameter`/`PutParameter` (scoped to the param) and a `PLAYER_EMAIL_MAP_PARAM` env var. `add`/`list` are open; `remove` is admin-gated. |

The map value carries the canonical Mojang name alongside the email, so `/mc register list` shows usernames with no extra Mojang calls and the export uses it as an `mcUsername` fallback. SSM has no compare-and-swap, so two simultaneous registrations could lose an update — acceptable at this scale (a few players, ~2x/week); the String parameter (standard tier, ~4 KB) holds ~50 players.

### Config / secrets (export Lambda env)

| Name | Source | Notes |
|---|---|---|
| `ENZY_SECRET_ARN` | Lambda env → Secrets Manager | ARN of the secret holding the `X-Secret-Token`. |
| `ENZY_BASE_URL` | Lambda env | Default `https://api.enzy.co`; allows staging. |
| `STATS_BUCKET` | Lambda env | Bucket name. |
| `PLAYER_EMAIL_MAP_PARAM` | Lambda env → SSM Parameter | UUID → email JSON. |
| `DRY_RUN` | Lambda env | `"1"` = compute + log payload, no POST (default). Flip to `"0"` to go live. |

---

## 6. Enzy contract (target shape)

```
POST {ENZY_BASE_URL}/rest/n8n/processSimpleJSONArray
Content-Type: application/json
X-Secret-Token: <ENZY_API_KEY>
IdField: snapshotKey
FileType: MinecraftStats
```

Body — JSON array of flat all-string objects:

```json
[
  {
    "snapshotKey": "riley.sinema@enzy.co|2026-05-31",
    "playerEmail": "riley.sinema@enzy.co",
    "snapshotDate": "2026-05-31 00:00:00",
    "mcUsername": "riley_mc",
    "creeperKillsGained": "8",
    "deathsGained": "2",
    "diamondsMinedGained": "14",
    "distanceTraveledGained": "5120",
    "achievementsGained": "3"
  }
]
```

Success: `{"success": true}` / HTTP 200. Failure shapes and handling per §7 and the push-job spec.

---

## 7. Correctness considerations & edge cases

- **Column set is locked on first POST.** The first POST fingerprints the column-name set and creates the backing table; adding/removing a column later creates a *new* table and orphans the leaderboard. Our locked set is the **9 columns** in §3. Adding a stat later = a coordinated, breaking change (acknowledged).
- **Reserved column names — never send:** `workspaceId`, `fileType`, `recordId`, `fileName`, `fileDateTime`, `implDateTime`, `serialversionuid`, `accessorder`.
- **`snapshotDate` must be non-blank on every row** — a blank value collapses the row onto the player and destroys that day's history.
- **Timezone.** Use the **Mountain** calendar date for `snapshotKey`/`snapshotDate`, and run the cron in early MT morning so a late-night session has ended and synced, and the date attribution is correct. (3am UTC would be ~8–9pm MT the prior day — wrong date *and* mid-session.)
- **Flush before sync.** `save-all flush` over RCON before the `s3 sync`, or the on-disk files may lag the session.
- **State write ordering.** read → compute → POST → save state. Never save before a confirmed 200.
- **Idempotency.** Re-running the same date overwrites that date's rows (same `snapshotKey`) — safe. A new date appends — that's history accruing.
- **Missed day.** A gap just means no row for that date; the next run diffs against the last stored state (delta spans the gap). No backfill.
- **Counter resets / rollbacks.** Flooring deltas at 0 absorbs server rollbacks or stat resets.
- **Error handling.** Network/5xx → retry 3× with exponential backoff (idempotent, safe). Auth failure → log + alert, do not retry. Body-validation failure → log and stop. Per-player error (value >255 bytes) → drop that player, continue.
- **Value size.** Counters are small ints as strings; the 255-byte limit is never a real risk here, but coerce-and-check anyway.

---

## 8. Cross-team coordination

Since **nothing is provisioned on either side yet**, we own the initial shape and there is no existing table to orphan. The only hard rule: **the very first POST defines the 9 columns forever.** Before that first push, confirm the Enzy side expects `FileType: MinecraftStats` in workspace 52 and that no conflicting `MinecraftStats` table already exists. After the first successful two-day run, verify in the app:

```sql
SELECT * FROM datarecord_<N>
WHERE workspaceId = 52 AND fileType = 'MinecraftStats'
ORDER BY fileDateTime DESC LIMIT 20;
```

Two consecutive days must yield **two distinct rows per player** (different `snapshotKey`/`snapshotDate`), not one overwritten row.

---

## 9. Future / out of scope

- **Adding a stat is a breaking change** — new column → new Enzy table → leaderboard remap. Batch any future stat additions into a single deliberate cutover. (Acknowledged by owner.)
- No backfill of historical days — history accrues forward from first run.
- No new Enzy endpoints/webhooks — only `/rest/n8n/processSimpleJSONArray`.
- No cleanup of old rows — history is the point.
- XP, PvP kill streaks, sessions — would require a plugin; explicitly deferred. (Achievement count is in scope via the vanilla `world/advancements/` files — no plugin needed.)

---

## 10. Implementation shape (as built)

- **Export Lambda** — `server_stats/export.py`, Python 3.11/arm64, stdlib + boto3, no vendored deps:
  - `mountain_today()` — self-contained US-DST calc (no tz database needed) for the Mountain calendar date.
  - `load_email_map()` (SSM), `load_usercache()` (S3) → identity resolution; UUIDs normalized (dashless, lowercase) on both sides.
  - `read_cumulative()` → `{uuid: {creeperKills, deaths, diamondsMined, distanceMeters, achievements}}` from `raw/stats/*.json` + `raw/advancements/*.json`.
  - `load_state()` / `save_state()` → `state/previous-cumulative.json`.
  - `compute_rows(today, previous, email_map, usercache, date_str, snapshot_date)` → 9-string-column rows; first-obs → baseline-only, skips zero-delta & unmapped, 255-byte guard.
  - `post_to_enzy(rows)` → `urllib` POST, 3× backoff on 5xx/network, no retry on auth/validation failure.
  - `lambda_handler`: resolve identity → read → compute → (DRY_RUN logs payload) → post → save-on-success → log counts.
- **On-box sync** — `mc-stats-sync` systemd service + timer in `compute_setup.sh.tpl`; instance role `PutObject`/`ListBucket` on the bucket.
- **Terraform** — `modules/stats/` (bucket, Lambda, schedule, IAM, SSM param); root creates the Enzy secret and wires the bucket name/arn into `modules/compute`.
- **Tests** — pure functions verified locally with fixtures (DST boundaries, distance/achievement math, delta flooring, first-obs/zero/unmapped handling, 9-column string output, state carry-forward).

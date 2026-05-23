# Stats Leaderboard Export — Design Notes

**Status:** Draft / brainstorming
**Owner:** @rsinema
**Last updated:** 2026-05-23

---

## Goal

Export per-player Minecraft stats from the server on a daily cadence to a work-internal leaderboard API, so the friend group can see a leaderboard of who's mined the most, killed the most mobs, etc.

## Scope (current thinking)

- **Stats source:** Vanilla Minecraft stats only (no mods/plugins). PaperMC already writes per-player totals to `world/stats/<uuid>.json` and advancements to `world/advancements/<uuid>.json`.
- **Cadence:** Once per day, early morning (e.g. ~3am local).
- **Destination:** Existing work-internal leaderboard API.

Mods/plugins like Plan (Player Analytics) were considered but skipped — vanilla covers ~80% of what a leaderboard needs (blocks mined, mobs killed, deaths, playtime, distances, items crafted), without adding a JVM plugin to maintain.

---

## Proposed Architecture

The server is stopped most of the time (~2x/week online), so the export pipeline must be decoupled from server uptime. Approach:

1. **On server shutdown** — extend the existing idle-stop Lambda flow. Before the instance is actually stopped, issue an SSM `RunCommand` against the box that runs `aws s3 sync` of `world/stats/` and `world/advancements/` to a new S3 bucket.
2. **Daily export Lambda** — triggered by EventBridge at ~3am. Reads the latest stats JSON from S3, optionally diffs against the previous day's snapshot (also in S3), transforms into the work API's payload format, and POSTs.
3. **Decoupling** — the EC2 instance does not need to be running at 3am. Stats live in S3 between sessions; if no one played that day, the Lambda sees no changes and exits.

### New infrastructure

- **S3 bucket** — `mc-stats-<suffix>` for staging stats JSON between server shutdown and Lambda read. Likely a new `modules/stats/` module, or fold into `modules/monitoring/`.
- **Lambda** — daily export function. Python, similar shape to `server_controller/`.
- **EventBridge rule** — daily cron trigger.
- **IAM** — Lambda needs `s3:GetObject` on the stats bucket and outbound HTTPS to the work API; EC2 instance role needs `s3:PutObject` on the same bucket.
- **Secrets Manager entry** — for the work API credentials (token / key / etc.).

### Changes to existing components

- **`modules/monitoring/`** — idle-stop Lambda needs to invoke SSM `RunCommand` to sync stats to S3 *before* stopping the instance. (Sequence: sync → stop.)
- **EC2 instance role** (`modules/compute/`) — add `s3:PutObject` permission for the stats bucket.

---

## Why This Shape (vs. alternatives)

| Alternative | Why not |
|---|---|
| Cron on the EC2 box | Only fires while the server is up. Unreliable given how rarely the server is online; could miss days entirely. |
| Wake instance at 3am to read stats | Wasteful compute and complexity. The data is small and easily stageable in S3. |
| Push from a Paper plugin | Adds a JVM plugin we'd have to maintain and update; not needed for vanilla stats. |
| Pull from external API hitting the EC2 | Server is off most of the time — nothing to hit. Also opens an inbound surface. |

---

## Open Questions (need input from work engineering buddies)

These need to be answered before implementation can start:

1. **Payload shape — totals or deltas?**
   - Minecraft stats files are running totals (e.g. `minecraft:mined.minecraft:diamond_ore = 47`).
   - **Option A — absolute totals:** Lambda sends current totals; API handles the leaderboard math. Idempotent; recoverable from missed syncs.
   - **Option B — daily deltas:** Lambda diffs today vs. yesterday's S3 snapshot. Required if the API wants "top miner this week" style queries, but a missed sync is lost data.
2. **Auth model** — bearer token, API key, signed request, mTLS, something else? Where does the credential live (Secrets Manager assumed).
3. **Player identity** — Minecraft stats are keyed by player UUID. Does the work API expect a display name, a work-internal user ID, or can it accept the MC UUID directly? If a mapping is needed, where does it live (a config file? a small DynamoDB table? maintained by hand?).
4. **Stat selection** — surface all vanilla stat keys (it's a lot per player — hundreds of `minecraft:mined.*`, `minecraft:killed.*`, etc.), or curate a fixed set? Likely curated set, e.g.:
   - Total blocks mined (sum of `minecraft:mined.*`)
   - Total mobs killed (sum of `minecraft:killed.*`)
   - Deaths (`minecraft:custom.minecraft:deaths`)
   - Play time (`minecraft:custom.minecraft:play_time`, in ticks → convert)
   - Distance traveled (sum of `minecraft:custom.minecraft:*_one_cm`)
5. **Endpoint shape** — single bulk POST with all players, or per-player POSTs? Schema?
6. **Failure handling** — if the API is down at 3am, do we retry, dead-letter, alert? How visible should failures be?

---

## Notes / Reference

- Vanilla stats file format: <https://minecraft.wiki/w/Statistics>
- Paper writes stats on player logout and on periodic intervals while online.
- Stats files persist in the world directory on the EBS volume — they survive instance stop/start.

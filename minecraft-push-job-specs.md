# Minecraft → Enzy Push Job Spec (Daily Snapshots + Deltas)

Build a scheduled job that pulls current player stats from a Minecraft server **once per day**, computes the day's deltas against the previous day, and pushes a dated snapshot row per player to the Enzy API. The Enzy side is already provisioned for dated history — your job formats and POSTs.

> **Why daily snapshots instead of a single overwriting snapshot:** the leaderboard needs dated rows to show "gained this week", progress-over-time charts, and all-time totals. Enzy stores history only if each (player, day) lands as a *distinct row* — see "Row identity" below. This is a deliberate change from a current-snapshot-only design.

## What you're building

A **daily** job (cron/Lambda/EventBridge) that:
1. Fetches the current cumulative stats snapshot for all players.
2. Loads the previous snapshot's cumulative values from local state.
3. Computes per-stat **deltas** (`today_total − yesterday_total`, floored at 0).
4. Emits one row per player with the day's **delta** (`*Gained`) values, tagged with today's date. All-time and period totals are derived by Enzy at query time (SUM of dailies) — no cumulative column is sent.
5. POSTs the array to one Enzy endpoint.
6. Persists today's cumulative values as the new state for tomorrow's diff.

Idempotent within a day: re-running the same calendar day overwrites that day's row (same `snapshotKey`), it does not duplicate.

## Endpoint contract

```
POST https://api.enzy.co/rest/n8n/processSimpleJSONArray
Content-Type: application/json
X-Secret-Token: <provided separately via secrets>
IdField: snapshotKey
FileType: MinecraftStats
```

The `X-Secret-Token` value is stored in AWS Secrets Manager and read by the export Lambda at runtime — **never committed to this repo**. See `docs/stats-leaderboard.md` §5 for the secret name and population procedure.

Body: a JSON array of flat objects, all values as strings.

```json
[
  {
    "snapshotKey": "riley.sinema@enzy.co|2026-05-29",
    "playerEmail": "riley.sinema@enzy.co",
    "snapshotDate": "2026-05-29 00:00:00",
    "mcUsername": "riley_mc",
    "creeperKillsGained": "5",
    "deathsGained": "1",
    "diamondsMinedGained": "3",
    "distanceTraveledGained": "1820",
    "achievementsGained": "2"
  }
]
```

Success: `{"success": true}` with HTTP 200.

Failure shapes:
- `{"success": false, "message": "Authentication Failed"}` — secret token wrong/inactive.
- `{"success": false, "message": "Empty or null JSON array provided"}` — body empty.
- `{"success": false, "message": "Failed to process JSON array: <exception>"}` — parse/write error.

## Row identity (the part that makes history work)

Enzy keys raw rows on `(workspaceId, fileType, recordId)` and re-loads with MySQL `REPLACE`, so a duplicate key overwrites. `recordId` is taken verbatim from the column named by the `IdField` header.

- **`snapshotKey` is the `IdField`** and must be **unique per (player, day)**: `"<playerEmail>|<YYYY-MM-DD>"`. This is what makes each day a new row instead of overwriting yesterday.
- **`playerEmail`** is still required on every row — it's the leaderboard's user-grouping key (separate from `snapshotKey`), and it must match a real Enzy user in workspace 52 to display by name. Players without a known email mapping are skipped.
- **`snapshotDate`** must be present and non-blank on **every** row (the Enzy object layer folds it into the row identity; a blank value silently collapses the row onto the player and destroys that day's history). Format: `YYYY-MM-DD HH:mm:ss` (use `00:00:00` for a daily snapshot). Must be parseable as a MySQL datetime.

## Hard constraints (DO NOT VIOLATE)

1. **Lock the column set.** The first POST fingerprints the column-name set and creates the backing table. Adding/removing a column later creates a *new* table and orphans the leaderboard mapping. The locked set is exactly these 9 columns:

   `snapshotKey`, `playerEmail`, `snapshotDate`, `mcUsername`, `creeperKillsGained`, `deathsGained`, `diamondsMinedGained`, `distanceTraveledGained`, `achievementsGained`.

   The stat columns map to vanilla files: `creeperKillsGained` = `minecraft:killed/creeper`; `deathsGained` = `minecraft:custom/deaths`; `diamondsMinedGained` = `minecraft:mined/diamond_ore` + `deepslate_diamond_ore`; `distanceTraveledGained` = sum of `minecraft:custom` `*_one_cm` ÷ 100 (meters) — all from `world/stats/<uuid>.json`; `achievementsGained` = count of `done == true` entries in `world/advancements/<uuid>.json` excluding `minecraft:recipes/*`. See `docs/stats-leaderboard.md` for the full design.

   Adding a new stat later is a coordinated change with the Enzy side, not a unilateral push-job change.

2. **All values are strings.** Coerce numbers/dates to strings before serializing. Backend stores everything as `tinytext`.

3. **Max 255 bytes per value.** Truncate or skip rather than risk silent loss.

4. **Reserved column names — never include these.** `workspaceId`, `fileType`, `recordId`, `fileName`, `fileDateTime`, `implDateTime`, `serialversionuid`, `accessorder`.

5. **`snapshotKey`, `playerEmail`, and `snapshotDate` must all be populated and stable.** A blank/changed `snapshotKey` makes a stray row; a blank `snapshotDate` collapses the row and loses that day; a missing `playerEmail` means the row can't join to a user.

## Delta computation

The Enzy leaderboard can only **SUM** values over a date range — it cannot subtract two snapshots. So the *job* computes the daily deltas; Enzy aggregates them (all-time = SUM of every daily row; period = SUM within the date filter).

- Keep local state: the previous run's cumulative values per player (`{playerEmail: {creeperKills, deaths, diamondsMined, distanceMeters, achievements}}`), in a small JSON file in S3/SSM or alongside the job. This state is *only* used to compute deltas — the cumulative values are never sent to Enzy.
- On each run: `gained = max(0, today_total − previous_total)` per stat. Floor at 0 to absorb stat resets / server rollbacks.
- **First observation of a player** (no prior state): set all `*Gained` to `0` and record the baseline. All-time totals therefore count gains since tracking started — appropriate for a server tracked from day one. (Any stats accrued *before* tracking began are not counted; if that ever matters, seed the baseline state from a one-time export instead of zero.)

## Cadence and idempotency

- **Cadence**: once per day, at a consistent time (e.g. server-quiet hours). Use the calendar date in that timezone for `snapshotDate`/`snapshotKey`.
- **Idempotency**: re-running the same date overwrites that date's rows (same `snapshotKey`) — safe. Running on a new date appends new rows — that's the history accumulating.
- **Missed day**: a gap just means no row for that date. The next run diffs against the last *stored* state, so the next delta spans the gap (acceptable). Do not try to backfill intermediate days.
- **Batch size**: one POST per run, all players in the array. Populations are small; well under the MySQL packet limit.

## Player → email mapping

Minecraft identifies players by UUID/username; Enzy joins by email. Maintain a mapping on your side (static `player-mapping.json`, an MC-side plugin field, or SSM). Unmapped players are **skipped** (logged), never posted with null/empty email. Emails must be valid (`Util.isValidEmailAddress()` at query time).

## Error handling

- **HTTP/network errors**: retry with backoff (3 attempts, exponential). Idempotency makes retries safe.
- **Auth failure**: surface loudly (log + alert). Key is bad/rotated. Do not retry.
- **Body validation failure**: log and stop. Don't retry malformed payloads.
- **Per-player data error** (value > 255 bytes, etc.): drop that one player, log, continue.

Keep state simple — one previous-cumulative file. If a run fails, the next run diffs against the last good state. No queues, no per-run retry persistence.

## Configuration / secrets

| Name | Source | Notes |
|---|---|---|
| `ENZY_API_KEY` | AWS Secrets Manager / SSM | The `X-Secret-Token` value. |
| `ENZY_BASE_URL` | env, default `https://api.enzy.co` | Allows staging. |
| `MC_SERVER_*` | however you read Minecraft data | Out of scope. |
| `PLAYER_EMAIL_MAP` | config file or SSM | MC UUID → email. |
| `STATE_STORE` | S3 key / SSM param / local path | Previous-cumulative snapshot for delta computation. |

## What you should NOT build

- **No new ObjectDefinitions / leaderboard rows on the Enzy side.** One-time setup (see `enzy-setup.md`).
- **No schema evolution.** New stat = coordinate with the user.
- **No backfill of historical days.** Start from today; history accumulates forward.
- **No new endpoints/webhooks.** Use only `/rest/n8n/processSimpleJSONArray`.
- **No email lookups against Enzy.** Mapping is on your side.
- **No cleanup of old rows.** History is the point; old daily rows are kept forever.

## Verification

```sql
SELECT * FROM datarecord_<N>
WHERE workspaceId = 52 AND fileType = 'MinecraftStats'
ORDER BY fileDateTime DESC LIMIT 20;
```

Run the job two consecutive days and confirm **two distinct rows** per player (different `snapshotKey`/`snapshotDate`), not one overwritten row. Then check the leaderboard views in the Ionic app.

## Suggested implementation shape

(Suggestion, not required.)

- Python, single file (~150–200 lines), Lambda + EventBridge daily schedule.
- `fetch_minecraft_stats() -> Dict[email, Dict[stat, int]]` (cumulative totals).
- `load_state() / save_state()` for previous cumulative.
- `compute_rows(today, previous, date) -> List[Dict[str,str]]` (deltas + identity columns).
- `post_to_enzy(records)`.
- Main: load mapping + state → fetch → compute → post → save state → log.

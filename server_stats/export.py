"""Daily Minecraft → Enzy stats export.

Reads vanilla per-player stat/advancement JSON staged in S3 (synced off the
server by the on-box mc-stats-sync timer), computes the day's deltas against the
previous run's cumulative snapshot, and POSTs one dated row per player to the
Enzy leaderboard API.

Pure standard library + boto3 (boto3 ships in the Lambda runtime), so the source
dir zips with no vendored dependencies.

See docs/stats-leaderboard.md for the full design and the locked 9-column set.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_s3 = boto3.client("s3")
_ssm = boto3.client("ssm")
_secrets = boto3.client("secretsmanager")

# Vanilla stats categories (keys inside world/stats/<uuid>.json -> "stats").
CUSTOM = "minecraft:custom"
MINED = "minecraft:mined"
KILLED = "minecraft:killed"

# Enzy contract.
ENZY_PATH = "/rest/n8n/processSimpleJSONArray"
FILE_TYPE = "MinecraftStats"
ID_FIELD = "snapshotKey"
MAX_VALUE_BYTES = 255

STATE_KEY = "state/previous-cumulative.json"

# Internal cumulative stat fields (NOT the Enzy column names — those carry the
# `*Gained` suffix and are assembled in compute_rows).
STAT_FIELDS = ("creeperKills", "deaths", "diamondsMined", "distanceMeters", "achievements")


# ---------------------------------------------------------------------------
# Mountain calendar date (self-contained US DST rule, no tz database needed)
#
# America/Denver: MST = UTC-7, MDT = UTC-6. DST runs from 02:00 local on the
# 2nd Sunday of March to 02:00 local on the 1st Sunday of November.
# ---------------------------------------------------------------------------

def _nth_sunday(year: int, month: int, n: int) -> datetime:
    first = datetime(year, month, 1)
    # weekday(): Mon=0 .. Sun=6
    offset = (6 - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def mountain_today(utc_now: datetime | None = None) -> datetime:
    utc = (utc_now or datetime.now(timezone.utc)).replace(tzinfo=None)
    # DST boundaries expressed in UTC: 02:00 MST = 09:00 UTC; 02:00 MDT = 08:00 UTC.
    dst_start = _nth_sunday(utc.year, 3, 2) + timedelta(hours=9)
    dst_end = _nth_sunday(utc.year, 11, 1) + timedelta(hours=8)
    is_dst = dst_start <= utc < dst_end
    return utc - timedelta(hours=6 if is_dst else 7)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_uuid(u: str) -> str:
    """Normalize a UUID for matching across stats filenames, usercache, and the
    email map (which may or may not use dashes / case)."""
    return u.replace("-", "").lower()


def _zero() -> dict:
    return {f: 0 for f in STAT_FIELDS}


def _list_keys(bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = _s3.list_objects_v2(**kwargs)
        keys.extend(o["Key"] for o in resp.get("Contents", []))
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            return keys


def _get_json(bucket: str, key: str):
    try:
        obj = _s3.get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read())
    except Exception as e:  # noqa: BLE001 — one bad file shouldn't sink the run
        logger.warning("failed to read s3://%s/%s: %s", bucket, key, e)
        return None


def _uuid_from_key(key: str) -> str:
    base = key.rsplit("/", 1)[-1]
    if base.endswith(".json"):
        base = base[:-5]
    return _norm_uuid(base)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def load_email_map(param_name: str) -> dict:
    """Return {norm_uuid: {"email": str, "name": str}}.

    Accepts both the object form written by the control Lambda's /mc register
    ({"email": ..., "name": ...}) and legacy bare-string values ("a@b.co").
    """
    resp = _ssm.get_parameter(Name=param_name)
    raw = json.loads(resp["Parameter"]["Value"] or "{}")
    out = {}
    for uuid, val in raw.items():
        if isinstance(val, dict):
            email, name = val.get("email", ""), val.get("name", "")
        else:
            email, name = (val if isinstance(val, str) else ""), ""
        if isinstance(email, str) and "@" in email:
            out[_norm_uuid(uuid)] = {"email": email, "name": name or ""}
        else:
            logger.warning("ignoring invalid email-map entry for uuid=%s", uuid)
    return out


def load_usercache(bucket: str) -> dict:
    entries = _get_json(bucket, "raw/usercache.json")
    if not isinstance(entries, list):
        return {}
    out = {}
    for e in entries:
        uuid, name = e.get("uuid"), e.get("name")
        if uuid and name:
            out[_norm_uuid(uuid)] = name
    return out


def _sum_distance_cm(custom: dict) -> int:
    return sum(
        v for k, v in custom.items()
        if k.endswith("_one_cm") and isinstance(v, (int, float))
    )


def _count_achievements(adv: dict) -> int:
    count = 0
    for key, val in adv.items():
        if key == "DataVersion" or key.startswith("minecraft:recipes/"):
            continue
        if isinstance(val, dict) and val.get("done") is True:
            count += 1
    return count


def read_cumulative(bucket: str) -> dict:
    """Return {norm_uuid: {stat_field: cumulative_total}} from raw stats + advancements."""
    players: dict[str, dict] = {}

    for key in _list_keys(bucket, "raw/stats/"):
        if not key.endswith(".json"):
            continue
        data = _get_json(bucket, key) or {}
        stats = data.get("stats", {})
        custom = stats.get(CUSTOM, {})
        mined = stats.get(MINED, {})
        killed = stats.get(KILLED, {})
        p = players.setdefault(_uuid_from_key(key), _zero())
        p["creeperKills"] = int(killed.get("minecraft:creeper", 0))
        p["deaths"] = int(custom.get("minecraft:deaths", 0))
        p["diamondsMined"] = (
            int(mined.get("minecraft:diamond_ore", 0))
            + int(mined.get("minecraft:deepslate_diamond_ore", 0))
        )
        p["distanceMeters"] = int(_sum_distance_cm(custom)) // 100

    for key in _list_keys(bucket, "raw/advancements/"):
        if not key.endswith(".json"):
            continue
        data = _get_json(bucket, key) or {}
        p = players.setdefault(_uuid_from_key(key), _zero())
        p["achievements"] = _count_achievements(data)

    return players


def load_state(bucket: str) -> dict:
    data = _get_json(bucket, STATE_KEY)
    return data if isinstance(data, dict) else {}


def save_state(bucket: str, state: dict) -> None:
    _s3.put_object(
        Bucket=bucket,
        Key=STATE_KEY,
        Body=json.dumps(state).encode("utf-8"),
        ContentType="application/json",
    )


# ---------------------------------------------------------------------------
# Row computation
# ---------------------------------------------------------------------------

def compute_rows(today: dict, previous: dict, email_map: dict, usercache: dict,
                 date_str: str, snapshot_date: str):
    """Build Enzy rows (daily deltas) and the next-run cumulative state.

    Returns (rows, new_state, skipped_unmapped, skipped_zero).
    """
    # Carry every prior baseline forward, then overlay today's cumulative so a
    # player whose file is briefly missing doesn't lose their baseline.
    new_state = dict(previous)
    new_state.update(today)

    rows = []
    skipped_unmapped = 0
    skipped_zero = 0

    for uuid, cur in today.items():
        entry = email_map.get(uuid)
        if not entry:
            skipped_unmapped += 1
            logger.info("skip unmapped uuid=%s", uuid)
            continue
        email = entry["email"]

        prev = previous.get(uuid)
        if prev is None:
            # First observation: establish the baseline (in new_state), emit no row.
            logger.info("baseline established for uuid=%s (%s)", uuid, email)
            continue

        gained = {f: max(0, int(cur.get(f, 0)) - int(prev.get(f, 0))) for f in STAT_FIELDS}
        if not any(gained.values()):
            skipped_zero += 1
            continue

        row = {
            "snapshotKey": f"{email}|{date_str}",
            "playerEmail": email,
            "snapshotDate": snapshot_date,
            "mcUsername": usercache.get(uuid) or entry.get("name") or "",
            "creeperKillsGained": str(gained["creeperKills"]),
            "deathsGained": str(gained["deaths"]),
            "diamondsMinedGained": str(gained["diamondsMined"]),
            "distanceTraveledGained": str(gained["distanceMeters"]),
            "achievementsGained": str(gained["achievements"]),
        }

        oversized = [k for k, v in row.items() if len(v.encode("utf-8")) > MAX_VALUE_BYTES]
        if oversized:
            logger.warning("skip uuid=%s — value(s) exceed %d bytes: %s",
                           uuid, MAX_VALUE_BYTES, oversized)
            continue

        rows.append(row)

    return rows, new_state, skipped_unmapped, skipped_zero


# ---------------------------------------------------------------------------
# Enzy POST
# ---------------------------------------------------------------------------

def post_to_enzy(rows: list, base_url: str, token: str) -> None:
    if not rows:
        logger.info("no rows to post — skipping Enzy call")
        return

    body = json.dumps(rows).encode("utf-8")
    url = base_url.rstrip("/") + ENZY_PATH
    headers = {
        "Content-Type": "application/json",
        "X-Secret-Token": token,
        "IdField": ID_FIELD,
        "FileType": FILE_TYPE,
    }

    delay = 1.0
    for attempt in range(1, 4):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read() or b"{}")
            if payload.get("success") is True:
                logger.info("posted %d rows to Enzy", len(rows))
                return
            # 200 + success:false — auth or body-validation failure. Do not retry.
            msg = payload.get("message", "")
            raise RuntimeError(f"Enzy rejected payload (no retry): {msg}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code in (401, 403):
                raise RuntimeError(f"Enzy auth failed ({e.code}) — check the token: {detail}")
            logger.warning("attempt %d/3 HTTP %d: %s", attempt, e.code, detail)
        except urllib.error.URLError as e:
            logger.warning("attempt %d/3 network error: %s", attempt, e)

        if attempt < 3:
            time.sleep(delay)
            delay *= 2

    raise RuntimeError("Enzy POST failed after 3 attempts")


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    bucket = os.environ["STATS_BUCKET"]
    secret_arn = os.environ["ENZY_SECRET_ARN"]
    base_url = os.environ.get("ENZY_BASE_URL", "https://api.enzy.co")
    param_name = os.environ["PLAYER_EMAIL_MAP_PARAM"]
    dry_run = os.environ.get("DRY_RUN", "0") == "1"

    local = mountain_today()
    date_str = local.strftime("%Y-%m-%d")
    snapshot_date = local.strftime("%Y-%m-%d 00:00:00")

    email_map = load_email_map(param_name)
    usercache = load_usercache(bucket)
    today = read_cumulative(bucket)
    previous = load_state(bucket)

    rows, new_state, n_unmapped, n_zero = compute_rows(
        today, previous, email_map, usercache, date_str, snapshot_date
    )

    logger.info(
        "date=%s players=%d rows=%d skipped_unmapped=%d skipped_zero=%d dry_run=%s",
        date_str, len(today), len(rows), n_unmapped, n_zero, dry_run,
    )

    if dry_run:
        logger.info("DRY_RUN — would POST: %s", json.dumps(rows))
        return {"statusCode": 200, "body": json.dumps({"dry_run": True, "rows": len(rows)})}

    token = _secrets.get_secret_value(SecretId=secret_arn)["SecretString"]
    post_to_enzy(rows, base_url, token)

    # Persist the new baseline only after a successful POST. If the POST raised,
    # state is untouched and the next run re-diffs against the last good state.
    save_state(bucket, new_state)

    return {"statusCode": 200, "body": json.dumps({"posted": len(rows), "date": date_str})}

import json
import boto3
import os
import logging
import re
import time
import urllib.request
import urllib.error
import urllib.parse

import nacl.signing
import nacl.exceptions

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DISCORD_API = "https://discord.com/api/v10"

# Cloudflare fronts discord.com and blocks requests with default urllib User-Agent
# (returns 403 error 1010). Discord's API also expects bots to identify themselves.
DISCORD_USER_AGENT = "DiscordBot (https://github.com/rsinema/mc-server-iac, 1.0)"

# Interaction response types (Discord API)
TYPE_PONG = 1
TYPE_CHANNEL_MESSAGE = 4
TYPE_DEFERRED_MESSAGE = 5

# Discord application command option types
OPT_SUB_COMMAND = 1
OPT_SUB_COMMAND_GROUP = 2

# Mojang username: 3-16 chars, letters/digits/underscore. Also guards against
# RCON command injection — whitelist add takes a raw string, and `; op evil`
# would be parsed as two commands otherwise.
MOJANG_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

# Mojang public profile API — resolves a username to its canonical UUID/name.
# Works while the server is stopped (no RCON), so /mc register doesn't need the
# instance running.
MOJANG_API = "https://api.mojang.com"

# The Enzy leaderboard joins players by email and only @enzy.co accounts exist
# there, so reject anything else at registration time rather than store an
# address that silently never displays.
ENZY_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@enzy\.co$", re.IGNORECASE)

# Waypoint labels are stored in SSM and only ever rendered back into Discord
# messages — they never touch RCON — so this guards display/key sanity, not
# command injection. Allow letters/digits/space/underscore/hyphen, 1-32 chars.
WAYPOINT_NAME_RE = re.compile(r"^[A-Za-z0-9 _-]{1,32}$")

# World profile names double as on-disk directory names (/opt/minecraft/worlds/
# <name>) and are interpolated into the instance's start script, so keep them to
# a strict lowercase slug — the same charset the on-box run.sh sanitizer allows.
WORLD_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

# Minecraft world border tops out near ±30M on X/Z; Y is far smaller but varies
# by dimension and custom worlds, so bound every coordinate to the same generous
# range rather than special-casing Y.
COORD_MIN, COORD_MAX = -30_000_000, 30_000_000

# SSM String parameters cap at 4096 bytes. Refuse a save that would overflow the
# waypoint blob with a clear message instead of letting put_parameter throw.
WAYPOINTS_MAX_BYTES = 4096

HELP_TEXT = (
    "**Minecraft Server Commands** — connect at `mc.rsinema.com:25565`\n\n"
    "`/mc start` — spin up the server (~30s for EC2 + ~30s for MC to start).\n"
    "`/mc stop` — shut it down. Also auto-stops after 15 min of no players.\n"
    "`/mc status` — server state, player count, and IP.\n"
    "`/mc players` — who's currently online.\n"
    "`/mc whitelist add user:<mojang_name>` — add a player to the allowlist.\n"
    "`/mc whitelist remove user:<mojang_name>` — remove a player (admin only).\n"
    "`/mc whitelist list` — show who's whitelisted.\n"
    "`/mc register add user:<mojang_name> email:<you@enzy.co>` — link your "
    "Minecraft account to your Enzy email for the stats leaderboard.\n"
    "`/mc register list` — show who's registered.\n"
    "`/mc register remove user:<mojang_name>` — drop a registration (admin only).\n"
    "`/mc op user:<mojang_name>` — grant in-game operator/admin (admin only).\n"
    "`/mc deop user:<mojang_name>` — revoke operator/admin (admin only).\n"
    "`/mc waypoint save name:<label> x:<x> y:<y> z:<z>` — save coords "
    "(e.g. `name:base x:30 y:166 z:-180`). Works even while the server is off.\n"
    "`/mc waypoint list` — show all saved coordinates.\n"
    "`/mc waypoint remove name:<label>` — delete a saved coordinate (admin only).\n"
    "`/mc world list` — show available world profiles and which is active.\n"
    "`/mc world set name:<world>` — switch the active world, e.g. `name:skyblock`. "
    "Takes effect on the next `/mc start`.\n"
    "`/mc help` — this message.\n\n"
    "Only whitelisted Mojang usernames can join. If you can't connect, ask "
    "someone in the server to run `/mc whitelist add user:<your_name>`."
)


# ---------------------------------------------------------------------------
# Command registry
#
# Each command advertises how it should be acked (immediate vs. deferred via
# async self-invoke), whether the response is ephemeral, and whether it's
# admin-gated. The ephemeral flag is committed at the deferred-ack step — the
# follow-up PATCH can't change it — so this registry is the single source of
# truth the sync handler consults before dispatching.
# ---------------------------------------------------------------------------

COMMAND_REGISTRY = {
    ("start",):              {"ack": "deferred",  "ephemeral": True,  "admin": False},
    ("stop",):               {"ack": "deferred",  "ephemeral": True,  "admin": False},
    ("status",):             {"ack": "deferred",  "ephemeral": True,  "admin": False},
    ("players",):            {"ack": "deferred",  "ephemeral": False, "admin": False},
    ("help",):               {"ack": "immediate", "ephemeral": True,  "admin": False},
    ("whitelist", "add"):    {"ack": "deferred",  "ephemeral": True,  "admin": False},
    ("whitelist", "remove"): {"ack": "deferred",  "ephemeral": True,  "admin": True},
    ("whitelist", "list"):   {"ack": "deferred",  "ephemeral": True,  "admin": False},
    ("register", "add"):     {"ack": "deferred",  "ephemeral": True,  "admin": False},
    ("register", "list"):    {"ack": "deferred",  "ephemeral": True,  "admin": False},
    ("register", "remove"):  {"ack": "deferred",  "ephemeral": True,  "admin": True},
    ("op",):                 {"ack": "deferred",  "ephemeral": True,  "admin": True},
    ("deop",):               {"ack": "deferred",  "ephemeral": True,  "admin": True},
    # World profiles select which /data dir the server boots (see
    # docs/multi-world.md). Both are open to everyone and answer to the channel:
    # a switch only takes effect on the next start and affects all players, so
    # it's announced publicly rather than gated or hidden.
    ("world", "list"):       {"ack": "deferred",  "ephemeral": False, "admin": False},
    ("world", "set"):        {"ack": "deferred",  "ephemeral": False, "admin": False},
    # Waypoints are shared, low-stakes coord notes stored in SSM (no server
    # needed). save/list are public and answer to the channel so everyone sees
    # the coords; remove is admin-gated like the other destructive ops so a
    # typo or griefer can't wipe the shared list.
    ("waypoint", "save"):    {"ack": "deferred",  "ephemeral": False, "admin": False},
    ("waypoint", "list"):    {"ack": "deferred",  "ephemeral": False, "admin": False},
    ("waypoint", "remove"):  {"ack": "deferred",  "ephemeral": True,  "admin": True},
}


# ---------------------------------------------------------------------------
# Module-level AWS clients and caches
#
# Declared at import time so they persist across warm invocations of the
# Lambda. This matters for cold-start latency: a Discord interaction must be
# acknowledged within 3 seconds, and re-creating boto3 clients on every
# invocation eats into that budget.
# ---------------------------------------------------------------------------

_ec2 = boto3.client("ec2")
_cloudwatch = boto3.client("cloudwatch")
_secrets = boto3.client("secretsmanager")
_lambda = boto3.client("lambda")
_ssm = boto3.client("ssm")
_s3 = boto3.client("s3")

# Stats delta state in the export bucket. Seeding a zero baseline here when a new
# player is added makes their first session count, instead of being silently
# absorbed as their baseline by the export's first-observation rule. Keys/fields
# must match server_stats/export.py (STATE_KEY, STAT_FIELDS, normalized UUIDs).
STATS_STATE_KEY = "state/previous-cumulative.json"
STAT_FIELDS = ("creeperKills", "deaths", "diamondsMined", "distanceMeters", "achievements")

_discord_public_key_cache: str | None = None
_rcon_password_cache: str | None = None


# ---------------------------------------------------------------------------
# Discord Ed25519 signature verification
# ---------------------------------------------------------------------------

def verify_discord_signature(body_bytes: bytes, signature: str, timestamp: str, public_key_hex: str) -> bool:
    try:
        public_key = nacl.signing.VerifyKey(bytes.fromhex(public_key_hex))
        message = timestamp.encode() + body_bytes
        public_key.verify(message, signature=bytes.fromhex(signature))
        return True
    except (nacl.exceptions.BadSignatureError, ValueError):
        return False


def get_discord_public_key(secret_arn: str) -> str:
    global _discord_public_key_cache
    if _discord_public_key_cache is None:
        resp = _secrets.get_secret_value(SecretId=secret_arn)
        _discord_public_key_cache = json.loads(resp["SecretString"])["public_key"]
    return _discord_public_key_cache


def get_rcon_password(secret_arn: str) -> str:
    global _rcon_password_cache
    if _rcon_password_cache is None:
        _rcon_password_cache = _secrets.get_secret_value(SecretId=secret_arn)["SecretString"]
    return _rcon_password_cache


# ---------------------------------------------------------------------------
# Interaction parsing / authorization
# ---------------------------------------------------------------------------

def parse_invocation(data: dict) -> tuple[tuple[str, ...], dict]:
    """Walk Discord's nested options tree to (command_path, leaf_args).

    Examples:
        /mc start                        -> (("start",),            {})
        /mc help                         -> (("help",),             {})
        /mc whitelist add user:Steve     -> (("whitelist", "add"),  {"user": "Steve"})
        /mc whitelist list               -> (("whitelist", "list"), {})
    """
    path: list[str] = []
    options = data.get("options", [])
    while options and options[0].get("type") in (OPT_SUB_COMMAND, OPT_SUB_COMMAND_GROUP):
        path.append(options[0]["name"])
        options = options[0].get("options", [])
    args = {o["name"]: o.get("value") for o in options}
    return tuple(path), args


def get_caller_id(payload: dict) -> str | None:
    """Discord puts the invoker in `member.user` for guild interactions and in `user` for DMs."""
    member = payload.get("member")
    if member and "user" in member:
        return member["user"].get("id")
    user = payload.get("user")
    if user:
        return user.get("id")
    return None


def get_caller_display_name(payload: dict) -> str:
    """Best human-readable name for the invoker, for the public announcement.

    Prefers the server nickname, then the account display name, then the
    handle. Returns a plain string (no `<@id>` mention) so the webhook
    announcement names the caller without pinging them.
    """
    member = payload.get("member") or {}
    user = member.get("user") or payload.get("user") or {}
    return member.get("nick") or user.get("global_name") or user.get("username") or "Someone"


def is_admin(user_id: str | None) -> bool:
    if not user_id:
        return False
    admins = {x.strip() for x in os.environ.get("ADMIN_DISCORD_USER_IDS", "").split(",") if x.strip()}
    return user_id in admins


# ---------------------------------------------------------------------------
# EC2 helpers
# ---------------------------------------------------------------------------

def get_instance_state(instance_id: str) -> str:
    resp = _ec2.describe_instances(InstanceIds=[instance_id])
    return resp["Reservations"][0]["Instances"][0]["State"]["Name"]


def get_instance_public_ip(instance_id: str) -> str | None:
    resp = _ec2.describe_instances(InstanceIds=[instance_id])
    return resp["Reservations"][0]["Instances"][0].get("PublicIpAddress")


def reset_idle_alarm(alarm_name: str) -> None:
    # Without this reset, the alarm stays in ALARM from the prior stop cycle,
    # so the next OK→ALARM transition never fires and auto-stop silently breaks.
    _cloudwatch.set_alarm_state(
        AlarmName=alarm_name,
        StateValue="OK",
        StateReason="Reset by /mc start to re-arm idle-stop",
    )


# ---------------------------------------------------------------------------
# RCON helpers (via itzg/minecraft-server container rcon-cli)
# ---------------------------------------------------------------------------

def rcon_command(command: str, rcon_password: str, host: str = "localhost", port: int = 25575) -> str:
    import socket
    import struct

    def _build_packet(request_id: int, packet_type: int, payload: str) -> bytes:
        body = payload.encode("utf-8") + b"\x00\x00"
        return struct.pack("<iii", len(body) + 8, request_id, packet_type) + body

    def _recv_packet(s) -> bytes:
        raw_len = b""
        while len(raw_len) < 4:
            chunk = s.recv(4 - len(raw_len))
            if not chunk:
                return b""
            raw_len += chunk
        (length,) = struct.unpack("<i", raw_len)
        data = b""
        while len(data) < length:
            chunk = s.recv(length - len(data))
            if not chunk:
                break
            data += chunk
        return data

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect((host, port))
        sock.sendall(_build_packet(1, 3, rcon_password))
        _recv_packet(sock)
        sock.sendall(_build_packet(2, 2, command))
        response = _recv_packet(sock)
    finally:
        sock.close()

    if len(response) < 8:
        return ""
    return response[8:].rstrip(b"\x00").decode("utf-8", errors="replace")


def get_player_count(rcon_password: str, rcon_host: str) -> int:
    try:
        output = rcon_command("list", rcon_password, host=rcon_host)
        match = re.search(r"There are (\d+)", output)
        return int(match.group(1)) if match else 0
    except Exception as e:
        logger.warning(f"RCON failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# Discord interaction response helpers
# ---------------------------------------------------------------------------

def _immediate_response(content: str, ephemeral: bool = True) -> dict:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "type": TYPE_CHANNEL_MESSAGE,
            "data": {
                "content": content,
                "flags": 64 if ephemeral else 0,
            },
        }),
    }


def _deferred_response(ephemeral: bool = True) -> dict:
    # Ack within 3s and keep the interaction token valid for 15 minutes of
    # follow-up edits. The user sees "Bot is thinking…" until we PATCH the
    # original message via discord_followup(). The ephemeral flag is locked
    # in here and cannot be changed by the follow-up.
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "type": TYPE_DEFERRED_MESSAGE,
            "data": {"flags": 64 if ephemeral else 0},
        }),
    }


def discord_followup(application_id: str, interaction_token: str, content: str) -> None:
    """PATCH the original deferred interaction message with final content."""
    url = f"{DISCORD_API}/webhooks/{application_id}/{interaction_token}/messages/@original"
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": DISCORD_USER_AGENT,
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        logger.error(f"Discord follow-up failed: {e.code} {e.read().decode(errors='replace')}")
    except Exception as e:
        logger.error(f"Discord follow-up error: {e}")


def discord_webhook_notify(webhook_url: str, message: str) -> None:
    """Fire-and-forget message to a standalone Discord webhook (idle-stop / start notice).

    Mentions are suppressed (`allowed_mentions.parse = []`) so neither a stray
    `@everyone` in the text nor a member name resolves to a ping — these are
    informational channel posts, not call-outs.
    """
    if not webhook_url:
        return
    try:
        data = json.dumps({
            "content": message,
            "allowed_mentions": {"parse": []},
        }).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": DISCORD_USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        logger.error(f"Discord webhook failed: {e}")


# ---------------------------------------------------------------------------
# Command implementations — return the follow-up content string.
# These run inside the async self-invocation, not the initial Discord request.
# ---------------------------------------------------------------------------

def run_start(instance_id: str, caller_name: str = "Someone", webhook_url: str = "") -> str:
    # The string we return goes to the caller's *ephemeral* follow-up (private
    # progress/errors). The public channel announcement is a separate webhook
    # post, fired only once the server is actually reachable.
    state = get_instance_state(instance_id)
    if state == "running":
        ip = get_instance_public_ip(instance_id)
        discord_webhook_notify(
            webhook_url,
            f"🎮 **{caller_name}** is hopping on — the Minecraft server is already up at "
            f"`mc.rsinema.com:25565`. Come play!",
        )
        return f"Server is already running at `{ip}:25565`."

    _ec2.start_instances(InstanceIds=[instance_id])

    alarm_name = os.environ.get("IDLE_STOP_ALARM_NAME")
    if alarm_name:
        try:
            reset_idle_alarm(alarm_name)
        except Exception as e:
            logger.warning(f"Failed to reset idle-stop alarm: {e}")

    for _ in range(24):
        time.sleep(5)
        state = get_instance_state(instance_id)
        if state == "running":
            ip = get_instance_public_ip(instance_id)
            discord_webhook_notify(
                webhook_url,
                f"🎮 **{caller_name}** started the Minecraft server! Hop on at "
                f"`mc.rsinema.com:25565` — give it ~30s for the world to finish loading.",
            )
            return f"Server is up at `{ip}:25565`! Give it ~30s for Minecraft to fully start."

    # Timed out before EC2 reported `running` — don't announce a server that
    # may not be reachable yet; the caller alone sees this hint.
    return "Server is starting but is taking longer than expected. Check again in a minute."


def run_stop(instance_id: str) -> str:
    state = get_instance_state(instance_id)
    if state == "stopped":
        return "Server is already stopped."

    _ec2.stop_instances(InstanceIds=[instance_id])
    return "Server is stopping. See you next time!"


def run_status(instance_id: str, rcon_password: str) -> str:
    state = get_instance_state(instance_id)
    world = _get_active_world()
    world_line = f"\nActive world: `{world}`" if world else ""
    if state == "stopped":
        return "Server is stopped. Use `/mc start` to spin it up." + world_line

    ip = get_instance_public_ip(instance_id) or "pending"
    players = get_player_count(rcon_password, rcon_host=ip) if state == "running" and ip != "pending" else 0
    return f"Server is running at `{ip}:25565`{world_line}\nPlayers online: {players}"


def run_players(instance_id: str, rcon_password: str) -> str:
    state = get_instance_state(instance_id)
    if state != "running":
        return "Server is not running. Use `/mc start` first."

    ip = get_instance_public_ip(instance_id)
    if not ip:
        return "Server is starting — public IP not yet assigned."
    try:
        output = rcon_command("list", rcon_password, host=ip)
        return f"```\n{output}\n```"
    except Exception as e:
        return f"Could not fetch player list: {e}"


def _require_running_server(instance_id: str) -> tuple[str | None, str | None]:
    """Return (error_message, ip) — if server isn't ready for RCON, error_message is non-None."""
    state = get_instance_state(instance_id)
    if state != "running":
        return "Server must be running for this command. Use `/mc start` first.", None
    ip = get_instance_public_ip(instance_id)
    if not ip:
        return "Server is starting — public IP not yet assigned.", None
    return None, ip


def run_whitelist_add(instance_id: str, rcon_password: str, username: str) -> str:
    if not MOJANG_USERNAME_RE.match(username or ""):
        return f"`{username}` is not a valid Mojang username (3-16 chars, letters/digits/underscore)."

    err, ip = _require_running_server(instance_id)
    if err:
        return err

    try:
        output = rcon_command(f"whitelist add {username}", rcon_password, host=ip)
    except Exception as e:
        return f"Failed to add `{username}` to whitelist: {e}"

    stripped = output.strip() if output else ""
    msg = f"Whitelisted `{username}`." + (f"\n```\n{stripped}\n```" if stripped else "")

    # Seed a zero stats baseline (best-effort) so this new player's stats count
    # from now. Server-down can't happen here (RCON just succeeded); a Mojang
    # outage just skips the seed — the export would baseline them on first sight.
    try:
        resolved = resolve_mojang_uuid(username)
    except Exception as e:
        logger.warning(f"Mojang lookup failed seeding baseline for {username}: {e}")
        resolved = None
    if resolved and seed_zero_baseline(resolved[0]) == "seeded":
        msg += "\nStat tracking baseline set — their stats count from now (register them with `/mc register add` to appear on the leaderboard)."
    return msg


def run_whitelist_remove(instance_id: str, rcon_password: str, username: str) -> str:
    if not MOJANG_USERNAME_RE.match(username or ""):
        return f"`{username}` is not a valid Mojang username (3-16 chars, letters/digits/underscore)."

    err, ip = _require_running_server(instance_id)
    if err:
        return err

    try:
        output = rcon_command(f"whitelist remove {username}", rcon_password, host=ip)
    except Exception as e:
        return f"Failed to remove `{username}`: {e}"

    stripped = output.strip() if output else ""
    return f"Removed `{username}` from whitelist." + (f"\n```\n{stripped}\n```" if stripped else "")


def run_whitelist_list(instance_id: str, rcon_password: str) -> str:
    err, ip = _require_running_server(instance_id)
    if err:
        return err

    try:
        output = rcon_command("whitelist list", rcon_password, host=ip)
        return f"```\n{output or 'No players whitelisted.'}\n```"
    except Exception as e:
        return f"Failed to list whitelist: {e}"


def run_op(instance_id: str, rcon_password: str, username: str) -> str:
    """Grant Minecraft operator (admin) status to a player via RCON `op`.

    Admin-gated in the registry — operator status grants in-game admin powers
    (gamemode, kick/ban, world edits), so only Discord IDs in
    ADMIN_DISCORD_USER_IDS may run it. There is no RCON `op list`; ops live in
    `ops.json` on the instance, so a list subcommand isn't offered here.
    """
    if not MOJANG_USERNAME_RE.match(username or ""):
        return f"`{username}` is not a valid Mojang username (3-16 chars, letters/digits/underscore)."

    err, ip = _require_running_server(instance_id)
    if err:
        return err

    try:
        output = rcon_command(f"op {username}", rcon_password, host=ip)
    except Exception as e:
        return f"Failed to op `{username}`: {e}"

    stripped = output.strip() if output else ""
    return f"Granted operator (admin) to `{username}`." + (f"\n```\n{stripped}\n```" if stripped else "")


def run_deop(instance_id: str, rcon_password: str, username: str) -> str:
    """Revoke Minecraft operator status from a player via RCON `deop`."""
    if not MOJANG_USERNAME_RE.match(username or ""):
        return f"`{username}` is not a valid Mojang username (3-16 chars, letters/digits/underscore)."

    err, ip = _require_running_server(instance_id)
    if err:
        return err

    try:
        output = rcon_command(f"deop {username}", rcon_password, host=ip)
    except Exception as e:
        return f"Failed to deop `{username}`: {e}"

    stripped = output.strip() if output else ""
    return f"Revoked operator (admin) from `{username}`." + (f"\n```\n{stripped}\n```" if stripped else "")


# ---------------------------------------------------------------------------
# Stats registration (UUID→email map in SSM, consumed by the export Lambda)
#
# The export job keys players by Minecraft UUID, but a Discord user only knows
# their username, so registration resolves username→UUID via the Mojang public
# API (authoritative, works while the server is stopped). The map value stores
# {email, name} so /mc register list can show real usernames with no extra
# Mojang calls; export.py reads the email and treats the name as an mcUsername
# fallback.
# ---------------------------------------------------------------------------

def resolve_mojang_uuid(username: str) -> tuple[str, str] | None:
    """Resolve a Mojang username to (normalized_uuid, canonical_name).

    Returns None if no such account exists. Raises on network/API errors so the
    caller can distinguish "doesn't exist" from "couldn't check".
    """
    url = f"{MOJANG_API}/users/profiles/minecraft/{urllib.parse.quote(username)}"
    req = urllib.request.Request(url, headers={"User-Agent": DISCORD_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (404, 204):  # unknown username (Mojang has used both)
            return None
        raise
    if not raw:  # 200 with empty body also means "not found"
        return None
    data = json.loads(raw)
    uuid = data.get("id")
    if not uuid:
        return None
    return uuid.replace("-", "").lower(), data.get("name") or username


def _load_email_map() -> dict:
    param = os.environ["PLAYER_EMAIL_MAP_PARAM"]
    resp = _ssm.get_parameter(Name=param)
    try:
        data = json.loads(resp["Parameter"]["Value"] or "{}")
    except (ValueError, KeyError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_email_map(email_map: dict) -> None:
    param = os.environ["PLAYER_EMAIL_MAP_PARAM"]
    _ssm.put_parameter(Name=param, Value=json.dumps(email_map), Type="String", Overwrite=True)


def seed_zero_baseline(uuid: str) -> str:
    """Seed a zero stats baseline for `uuid` in the export's delta state, so the
    player's stats count from now rather than having their first session absorbed
    as the baseline.

    Seed-if-absent: an existing baseline is never overwritten (re-adding a player
    must not reset their accrued totals to zero and dump a fake delta). Returns
    "seeded", "exists", or "skipped". Best-effort — callers ignore failures so
    whitelisting/registration still succeed.
    """
    bucket = os.environ.get("STATS_BUCKET")
    if not bucket or not uuid:
        return "skipped"
    try:
        try:
            obj = _s3.get_object(Bucket=bucket, Key=STATS_STATE_KEY)
            state = json.loads(obj["Body"].read() or b"{}")
            if not isinstance(state, dict):
                state = {}
        except _s3.exceptions.NoSuchKey:
            state = {}
        if uuid in state:
            return "exists"
        state[uuid] = {f: 0 for f in STAT_FIELDS}
        _s3.put_object(
            Bucket=bucket,
            Key=STATS_STATE_KEY,
            Body=json.dumps(state).encode("utf-8"),
            ContentType="application/json",
        )
        logger.info("seeded zero stats baseline for uuid=%s", uuid)
        return "seeded"
    except Exception as e:
        logger.warning("baseline seed failed for uuid=%s: %s", uuid, e)
        return "skipped"


def _entry_email(val) -> str:
    """Read the email from a map value (object form or legacy bare string)."""
    return val.get("email", "") if isinstance(val, dict) else (val or "")


def _entry_name(val) -> str:
    return val.get("name", "") if isinstance(val, dict) else ""


def run_register_add(username: str, email: str) -> str:
    if not MOJANG_USERNAME_RE.match(username or ""):
        return f"`{username}` is not a valid Mojang username (3-16 chars, letters/digits/underscore)."

    email = (email or "").strip()
    if not ENZY_EMAIL_RE.match(email):
        return f"`{email}` is not a valid `@enzy.co` email address. Use the address tied to your Enzy account."

    try:
        resolved = resolve_mojang_uuid(username)
    except Exception as e:
        logger.warning(f"Mojang lookup failed for {username}: {e}")
        return "Couldn't reach Mojang to verify that username right now. Try again in a minute."
    if resolved is None:
        return f"No Minecraft account named `{username}` was found. Mojang usernames are exact — check the spelling."

    uuid, canonical = resolved
    email_map = _load_email_map()
    existed = uuid in email_map
    email_map[uuid] = {"email": email, "name": canonical}
    _save_email_map(email_map)

    # Seed a zero baseline (if absent) so a brand-new player's stats count from
    # now rather than being absorbed as their first-observation baseline.
    seeded = seed_zero_baseline(uuid) == "seeded"

    verb = "Updated registration for" if existed else "Registered"
    msg = f"{verb} `{canonical}` → `{email}` for the stats leaderboard."
    if seeded:
        msg += " Stat tracking baseline set — stats count from now."
    return msg


def run_register_list() -> str:
    email_map = _load_email_map()
    if not email_map:
        return "No players are registered yet. Use `/mc register add user:<name> email:<you@enzy.co>` to link your account."

    lines = []
    for uuid, val in sorted(email_map.items(), key=lambda kv: (_entry_name(kv[1]).lower() or kv[0])):
        name = _entry_name(val) or "(unknown name)"
        lines.append(f"{name} -> {_entry_email(val)}")
    body = "\n".join(lines)
    return f"**Registered players ({len(lines)})**\n```\n{body}\n```"


def run_register_remove(username: str) -> str:
    if not MOJANG_USERNAME_RE.match(username or ""):
        return f"`{username}` is not a valid Mojang username (3-16 chars, letters/digits/underscore)."

    email_map = _load_email_map()

    # Match by the stored canonical name first — no network call needed.
    target_uuid = next(
        (u for u, v in email_map.items() if _entry_name(v).lower() == username.lower()),
        None,
    )
    # Fall back to resolving the UUID via Mojang (covers legacy string entries
    # or a player who changed their name since registering).
    if target_uuid is None:
        try:
            resolved = resolve_mojang_uuid(username)
        except Exception as e:
            logger.warning(f"Mojang lookup failed for {username}: {e}")
            return "Couldn't reach Mojang to look up that username. Try again in a minute."
        if resolved and resolved[0] in email_map:
            target_uuid = resolved[0]

    if target_uuid is None:
        return f"`{username}` is not registered."

    removed = email_map.pop(target_uuid)
    _save_email_map(email_map)
    return f"Removed `{username}` (`{_entry_email(removed)}`) from leaderboard tracking."


# ---------------------------------------------------------------------------
# Waypoints (shared coordinate notes in SSM, no running server required)
#
# Stored as JSON {"<lowercased-label>": {"name": "<display>", "x": int,
# "y": int, "z": int, "by": "<discord-name>"}}. Keyed by the lowercased label
# so lookups/overwrites are case-insensitive while the original casing is kept
# for display. The whole blob lives in one SSM String parameter (4KB cap), the
# same pattern as the register email map — cheap, durable, and readable while
# the EC2 instance is stopped.
# ---------------------------------------------------------------------------

def _load_waypoints() -> dict:
    param = os.environ["WAYPOINTS_PARAM"]
    try:
        resp = _ssm.get_parameter(Name=param)
        data = json.loads(resp["Parameter"]["Value"] or "{}")
    except (ValueError, KeyError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_waypoints(waypoints: dict) -> None:
    param = os.environ["WAYPOINTS_PARAM"]
    _ssm.put_parameter(Name=param, Value=json.dumps(waypoints), Type="String", Overwrite=True)


def _parse_coord(value) -> int | None:
    """Coerce a Discord option to an int within Minecraft world bounds, or None."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if COORD_MIN <= n <= COORD_MAX else None


def run_waypoint_save(name: str, x, y, z, caller_name: str = "Someone") -> str:
    name = (name or "").strip()
    if not WAYPOINT_NAME_RE.match(name):
        return (
            f"`{name}` isn't a valid waypoint name (1-32 chars: letters, digits, "
            "spaces, `_` or `-`)."
        )

    coords = {"x": _parse_coord(x), "y": _parse_coord(y), "z": _parse_coord(z)}
    bad = [axis for axis, val in coords.items() if val is None]
    if bad:
        return (
            f"Coordinate(s) {', '.join('`' + b + '`' for b in bad)} must be whole "
            f"numbers between {COORD_MIN:,} and {COORD_MAX:,}."
        )

    waypoints = _load_waypoints()
    key = name.lower()
    existed = key in waypoints
    waypoints[key] = {"name": name, "by": caller_name, **coords}

    # Guard the 4KB SSM ceiling before writing so an overflow reads as a clear
    # "list is full" message rather than a raw ParameterMaxLimitExceeded error.
    if len(json.dumps(waypoints).encode("utf-8")) > WAYPOINTS_MAX_BYTES:
        return (
            "The waypoint list is full (4KB limit reached). Remove an old one with "
            "`/mc waypoint remove` before adding more."
        )

    _save_waypoints(waypoints)
    verb = "Updated" if existed else "Saved"
    return f"{verb} waypoint **{name}** → `{coords['x']} {coords['y']} {coords['z']}`."


def run_waypoint_list() -> str:
    waypoints = _load_waypoints()
    if not waypoints:
        return "No waypoints saved yet. Add one with `/mc waypoint save name:<label> x:<x> y:<y> z:<z>`."

    rows = []
    for entry in sorted(waypoints.values(), key=lambda e: e.get("name", "").lower()):
        label = entry.get("name", "(unnamed)")
        coord = f"{entry.get('x')} {entry.get('y')} {entry.get('z')}"
        by = entry.get("by")
        rows.append(f"{label}: {coord}" + (f"  (by {by})" if by else ""))
    body = "\n".join(rows)
    return f"**Waypoints ({len(rows)})**\n```\n{body}\n```"


def run_waypoint_remove(name: str) -> str:
    name = (name or "").strip()
    if not WAYPOINT_NAME_RE.match(name):
        return f"`{name}` isn't a valid waypoint name."

    waypoints = _load_waypoints()
    entry = waypoints.pop(name.lower(), None)
    if entry is None:
        return f"No waypoint named **{name}**. Use `/mc waypoint list` to see saved ones."

    _save_waypoints(waypoints)
    coord = f"{entry.get('x')} {entry.get('y')} {entry.get('z')}"
    return f"Removed waypoint **{entry.get('name', name)}** (`{coord}`)."


# ---------------------------------------------------------------------------
# World profiles (which /data dir the server boots)
#
# The active world and the registry of known worlds live in two SSM params
# (ACTIVE_WORLD_PARAM / WORLD_LIST_PARAM) written by Terraform's control module.
# The instance reads active-world at container-start (modules/compute run.sh) and
# mounts /opt/minecraft/worlds/<name> as /data, so a switch only takes effect on
# the next cold start. See docs/multi-world.md.
# ---------------------------------------------------------------------------

def _get_active_world() -> str:
    """Current active world profile, or '' if unset/unreadable (best-effort)."""
    param = os.environ.get("ACTIVE_WORLD_PARAM")
    if not param:
        return ""
    try:
        return _ssm.get_parameter(Name=param)["Parameter"]["Value"].strip()
    except Exception as e:
        logger.warning(f"Failed to read active world: {e}")
        return ""


def _get_world_list() -> list[str]:
    """Known world profiles from the StringList param (comma-separated Value)."""
    param = os.environ.get("WORLD_LIST_PARAM")
    if not param:
        return []
    try:
        raw = _ssm.get_parameter(Name=param)["Parameter"]["Value"]
    except Exception as e:
        logger.warning(f"Failed to read world list: {e}")
        return []
    return [w.strip() for w in raw.split(",") if w.strip()]


def run_world_list() -> str:
    worlds = _get_world_list()
    if not worlds:
        return "No world profiles are configured. Ask an admin to set `world_profiles` in the infra config."

    active = _get_active_world()
    rows = [f"{w}{'  ★ (active)' if w == active else ''}" for w in sorted(worlds)]
    body = "\n".join(rows)
    return (
        f"**World profiles ({len(worlds)})**\n```\n{body}\n```\n"
        "Switch with `/mc world set <name>` (admin), then `/mc stop` and `/mc start`."
    )


def run_world_set(instance_id: str, name: str) -> str:
    name = (name or "").strip().lower()
    if not WORLD_NAME_RE.match(name):
        return f"`{name}` isn't a valid world name (1-32 chars: lowercase letters, digits, `_` or `-`)."

    worlds = _get_world_list()
    if worlds and name not in worlds:
        available = ", ".join(f"`{w}`" for w in sorted(worlds))
        return f"`{name}` is not a known world profile. Available: {available}."

    param = os.environ.get("ACTIVE_WORLD_PARAM")
    if not param:
        return "World selection isn't configured (ACTIVE_WORLD_PARAM unset)."
    _ssm.put_parameter(Name=param, Value=name, Type="String", Overwrite=True)

    msg = f"Active world set to **{name}**."
    if get_instance_state(instance_id) == "running":
        msg += " The server is running now — the switch takes effect after `/mc stop` then `/mc start`."
    else:
        msg += " It loads on the next `/mc start`."
    return msg


def dispatch_async(path: tuple, args: dict, instance_id: str, rcon_password: str,
                   caller_name: str = "Someone", webhook_url: str = "") -> str:
    """Route a parsed command path to its implementation (async worker side)."""
    if path == ("start",):
        return run_start(instance_id, caller_name=caller_name, webhook_url=webhook_url)
    if path == ("stop",):
        return run_stop(instance_id)
    if path == ("status",):
        return run_status(instance_id, rcon_password)
    if path == ("players",):
        return run_players(instance_id, rcon_password)
    if path == ("whitelist", "add"):
        return run_whitelist_add(instance_id, rcon_password, args.get("user", ""))
    if path == ("whitelist", "remove"):
        return run_whitelist_remove(instance_id, rcon_password, args.get("user", ""))
    if path == ("whitelist", "list"):
        return run_whitelist_list(instance_id, rcon_password)
    if path == ("register", "add"):
        return run_register_add(args.get("user", ""), args.get("email", ""))
    if path == ("register", "list"):
        return run_register_list()
    if path == ("register", "remove"):
        return run_register_remove(args.get("user", ""))
    if path == ("op",):
        return run_op(instance_id, rcon_password, args.get("user", ""))
    if path == ("deop",):
        return run_deop(instance_id, rcon_password, args.get("user", ""))
    if path == ("waypoint", "save"):
        return run_waypoint_save(
            args.get("name", ""), args.get("x"), args.get("y"), args.get("z"),
            caller_name=caller_name,
        )
    if path == ("waypoint", "list"):
        return run_waypoint_list()
    if path == ("waypoint", "remove"):
        return run_waypoint_remove(args.get("name", ""))
    if path == ("world", "list"):
        return run_world_list()
    if path == ("world", "set"):
        return run_world_set(instance_id, args.get("name", ""))
    return f"Unknown subcommand: `/mc {' '.join(path)}`. Try `/mc help`."


# ---------------------------------------------------------------------------
# EventBridge / direct invocation handlers (idle-stop trigger, etc.)
# ---------------------------------------------------------------------------

def handle_stop_action(instance_id: str, webhook_url: str = None) -> dict:
    state = get_instance_state(instance_id)
    if state != "running":
        logger.info(f"Instance {instance_id} already {state}, nothing to stop")
        return {"statusCode": 200, "body": json.dumps({"skipped": True, "state": state})}

    _ec2.stop_instances(InstanceIds=[instance_id])
    logger.info(f"Instance {instance_id} stopped by idle-stop trigger")

    if webhook_url:
        discord_webhook_notify(
            webhook_url,
            "Server stopped — idle for 15 minutes. Use `/mc start` to resume.",
        )

    return {"statusCode": 200, "body": json.dumps({"stopped": True, "instance_id": instance_id})}


def handle_start_action(instance_id: str) -> dict:
    state = get_instance_state(instance_id)
    if state == "running":
        logger.info(f"Instance {instance_id} already running")
        return {"statusCode": 200, "body": json.dumps({"skipped": True, "state": state})}

    _ec2.start_instances(InstanceIds=[instance_id])
    logger.info(f"Instance {instance_id} started by direct action")

    alarm_reset = False
    alarm_name = os.environ.get("IDLE_STOP_ALARM_NAME")
    if alarm_name:
        try:
            reset_idle_alarm(alarm_name)
            alarm_reset = True
        except Exception as e:
            logger.warning(f"Failed to reset idle-stop alarm: {e}")

    return {"statusCode": 200, "body": json.dumps({
        "started": True,
        "instance_id": instance_id,
        "alarm_reset": alarm_reset,
    })}


# ---------------------------------------------------------------------------
# Async self-invocation handler — runs the slow command and posts follow-up.
# ---------------------------------------------------------------------------

def handle_async_command(event: dict) -> dict:
    path = tuple(event.get("path", []))
    args = event.get("args", {})
    application_id = event["application_id"]
    interaction_token = event["interaction_token"]
    instance_id = os.environ["INSTANCE_ID"]

    rcon_arn = os.environ.get("RCON_PASSWORD_SECRET_ARN")
    rcon_pw = get_rcon_password(rcon_arn) if rcon_arn else ""

    caller_name = event.get("caller_name", "Someone")
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")

    try:
        content = dispatch_async(path, args, instance_id, rcon_pw,
                                 caller_name=caller_name, webhook_url=webhook_url)
    except Exception as e:
        logger.exception("Async command failed")
        content = f"Command `/mc {' '.join(path)}` failed: {e}"

    discord_followup(application_id, interaction_token, content)
    return {"statusCode": 200, "body": json.dumps({"path": list(path), "delivered": True})}


# ---------------------------------------------------------------------------
# Main Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    logger.info(f"Event type: action={event.get('action')} async={event.get('async_command')}")

    instance_id = os.environ.get("INSTANCE_ID")
    discord_signing_key_arn = os.environ.get("DISCORD_SIGNING_KEY_SECRET_ARN")
    discord_webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")

    # -------------------------------------------------------------------------
    # Route: async self-invocation (deferred Discord command worker)
    # -------------------------------------------------------------------------
    if event.get("async_command"):
        return handle_async_command(event)

    # -------------------------------------------------------------------------
    # Route: EventBridge / direct invocation
    # -------------------------------------------------------------------------
    if event.get("action") == "stop":
        logger.info("Handling idle-stop action from EventBridge")
        return handle_stop_action(instance_id, discord_webhook_url)

    if event.get("action") == "start":
        logger.info("Handling direct start action")
        return handle_start_action(instance_id)

    # -------------------------------------------------------------------------
    # Route: Discord interaction via Function URL
    # -------------------------------------------------------------------------
    headers = event.get("headers", {})
    signature = headers.get("x-signature-ed25519", "")
    timestamp = headers.get("x-signature-timestamp", "")

    body_bytes = event.get("body", "").encode() if isinstance(event.get("body"), str) else event.get("body", b"")
    if isinstance(body_bytes, str):
        body_bytes = body_bytes.encode()

    if not signature or not timestamp:
        logger.error("Missing Discord signature headers")
        return {"statusCode": 401, "body": json.dumps({"error": "Missing signature"})}

    try:
        public_key_hex = get_discord_public_key(discord_signing_key_arn)
    except Exception as e:
        logger.error(f"Failed to fetch Discord public key: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": "Failed to verify interaction"})}

    if not verify_discord_signature(body_bytes, signature, timestamp, public_key_hex):
        logger.error("Invalid Discord signature")
        return {"statusCode": 401, "body": json.dumps({"error": "Invalid signature"})}

    try:
        payload = json.loads(body_bytes) if body_bytes else {}
    except Exception:
        payload = {}

    # Discord PING for endpoint registration.
    if payload.get("type") == 1:
        return {"statusCode": 200, "body": json.dumps({"type": TYPE_PONG})}

    data = payload.get("data", {})
    if data.get("name") != "mc":
        return _immediate_response("Unknown interaction type.")

    path, args = parse_invocation(data)
    logger.info(f"Routing command: /mc {' '.join(path)} args={args}")

    entry = COMMAND_REGISTRY.get(path)
    if entry is None:
        return _immediate_response(f"Unknown command: `/mc {' '.join(path) or '(no subcommand)'}`. Try `/mc help`.")

    # Admin gate — check before deferring so we can return a clean immediate error.
    if entry["admin"]:
        caller = get_caller_id(payload)
        if not is_admin(caller):
            logger.info(f"Admin-gated command denied for user {caller}: /mc {' '.join(path)}")
            return _immediate_response(f"`/mc {' '.join(path)}` is admin-only.")

    # Immediate commands (help): respond inline, no async round-trip.
    if entry["ack"] == "immediate":
        if path == ("help",):
            return _immediate_response(HELP_TEXT, ephemeral=entry["ephemeral"])
        return _immediate_response(f"No handler for `/mc {' '.join(path)}`.")

    # Deferred commands: ack now, run work in a self-invocation.
    application_id = payload.get("application_id")
    interaction_token = payload.get("token")
    if not application_id or not interaction_token:
        logger.error("Interaction payload missing application_id or token")
        return _immediate_response("Interaction is malformed — cannot defer.")

    try:
        _lambda.invoke(
            FunctionName=context.invoked_function_arn,
            InvocationType="Event",
            Payload=json.dumps({
                "async_command": True,
                "path": list(path),
                "args": args,
                "application_id": application_id,
                "interaction_token": interaction_token,
                "caller_name": get_caller_display_name(payload),
            }).encode("utf-8"),
        )
    except Exception as e:
        logger.exception("Failed to async-invoke self")
        return _immediate_response(f"Could not dispatch `/mc {' '.join(path)}`: {e}")

    return _deferred_response(ephemeral=entry["ephemeral"])

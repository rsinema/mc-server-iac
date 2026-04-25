import json
import boto3
import os
import logging
import re
import time
import urllib.request
import urllib.error

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

HELP_TEXT = (
    "**Minecraft Server Commands** — connect at `mc.rsinema.com:25565`\n\n"
    "`/mc start` — spin up the server (~30s for EC2 + ~30s for MC to start).\n"
    "`/mc stop` — shut it down. Also auto-stops after 15 min of no players.\n"
    "`/mc status` — server state, player count, and IP.\n"
    "`/mc players` — who's currently online.\n"
    "`/mc whitelist add user:<mojang_name>` — add a player to the allowlist.\n"
    "`/mc whitelist remove user:<mojang_name>` — remove a player (admin only).\n"
    "`/mc whitelist list` — show who's whitelisted.\n"
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
    """Fire-and-forget message to a standalone Discord webhook (idle-stop notice)."""
    if not webhook_url:
        return
    try:
        data = json.dumps({"content": message}).encode("utf-8")
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

def run_start(instance_id: str) -> str:
    state = get_instance_state(instance_id)
    if state == "running":
        ip = get_instance_public_ip(instance_id)
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
            return f"Server is up at `{ip}:25565`! Give it ~30s for Minecraft to fully start."

    return "Server is starting but is taking longer than expected. Check again in a minute."


def run_stop(instance_id: str) -> str:
    state = get_instance_state(instance_id)
    if state == "stopped":
        return "Server is already stopped."

    _ec2.stop_instances(InstanceIds=[instance_id])
    return "Server is stopping. See you next time!"


def run_status(instance_id: str, rcon_password: str) -> str:
    state = get_instance_state(instance_id)
    if state == "stopped":
        return "Server is stopped. Use `/mc start` to spin it up."

    ip = get_instance_public_ip(instance_id) or "pending"
    players = get_player_count(rcon_password, rcon_host=ip) if state == "running" and ip != "pending" else 0
    return f"Server is running at `{ip}:25565`\nPlayers online: {players}"


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
    return f"Whitelisted `{username}`." + (f"\n```\n{stripped}\n```" if stripped else "")


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


def dispatch_async(path: tuple, args: dict, instance_id: str, rcon_password: str) -> str:
    """Route a parsed command path to its implementation (async worker side)."""
    if path == ("start",):
        return run_start(instance_id)
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

    try:
        content = dispatch_async(path, args, instance_id, rcon_pw)
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
            }).encode("utf-8"),
        )
    except Exception as e:
        logger.exception("Failed to async-invoke self")
        return _immediate_response(f"Could not dispatch `/mc {' '.join(path)}`: {e}")

    return _deferred_response(ephemeral=entry["ephemeral"])

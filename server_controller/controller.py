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

# Commands that take longer than Discord's 3-second ack window and must be
# handled via deferred-response + async self-invoke.
DEFERRED_SUBCOMMANDS = {"start", "stop", "status", "players"}


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
    # original message via discord_followup().
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


def dispatch_subcommand(sub: str, instance_id: str, rcon_password: str) -> str:
    if sub == "start":
        return run_start(instance_id)
    if sub == "stop":
        return run_stop(instance_id)
    if sub == "status":
        return run_status(instance_id, rcon_password)
    if sub == "players":
        return run_players(instance_id, rcon_password)
    return f"Unknown subcommand: `{sub}`. Use `/mc start|stop|status|players`."


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
    sub = event["sub"]
    application_id = event["application_id"]
    interaction_token = event["interaction_token"]
    instance_id = os.environ["INSTANCE_ID"]

    rcon_arn = os.environ.get("RCON_PASSWORD_SECRET_ARN")
    rcon_pw = get_rcon_password(rcon_arn) if rcon_arn else ""

    try:
        content = dispatch_subcommand(sub, instance_id, rcon_pw)
    except Exception as e:
        logger.exception("Async command failed")
        content = f"Command `{sub}` failed: {e}"

    discord_followup(application_id, interaction_token, content)
    return {"statusCode": 200, "body": json.dumps({"sub": sub, "delivered": True})}


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

    command_name = payload.get("data", {}).get("name", "")
    options = {
        o["name"]: o.get("value")
        for o in payload.get("data", {}).get("options", [])
    }

    logger.info(f"Routing command: {command_name} options={options}")

    if command_name != "mc":
        return _immediate_response("Unknown interaction type.")

    sub = options.get("sub") or options.get("action", "status")

    if sub not in DEFERRED_SUBCOMMANDS:
        return _immediate_response(f"Unknown subcommand: `{sub}`. Use `/mc start|stop|status|players`.")

    application_id = payload.get("application_id")
    interaction_token = payload.get("token")
    if not application_id or not interaction_token:
        logger.error("Interaction payload missing application_id or token")
        return _immediate_response("Interaction is malformed — cannot defer.")

    # Fire-and-forget self-invoke, then ack within 3 seconds.
    try:
        _lambda.invoke(
            FunctionName=context.invoked_function_arn,
            InvocationType="Event",
            Payload=json.dumps({
                "async_command": True,
                "sub": sub,
                "application_id": application_id,
                "interaction_token": interaction_token,
            }).encode("utf-8"),
        )
    except Exception as e:
        logger.exception("Failed to async-invoke self")
        return _immediate_response(f"Could not dispatch `/mc {sub}`: {e}")

    return _deferred_response(ephemeral=True)

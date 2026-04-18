import json
import boto3
import os
import logging
import re
import time
import urllib.request

import nacl.signing
import nacl.exceptions

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Discord Ed25519 signature verification
# ---------------------------------------------------------------------------

def verify_discord_signature(body_bytes: bytes, signature: str, timestamp: str, public_key_hex: str) -> bool:
    """Verify the Ed25519 signature on a Discord interaction request."""
    try:
        public_key = nacl.signing.VerifyKey(bytes.fromhex(public_key_hex))
        message = timestamp.encode() + body_bytes
        public_key.verify(message, signature=bytes.fromhex(signature))
        return True
    except (nacl.exceptions.BadSignatureError, ValueError):
        return False


def get_discord_public_key(secret_arn: str) -> str:
    """Fetch the Discord public key from Secrets Manager."""
    secrets = boto3.client("secretsmanager")
    resp = secrets.get_secret_value(SecretId=secret_arn)
    raw = resp["SecretString"]
    parsed = json.loads(raw)
    return parsed["public_key"]


# ---------------------------------------------------------------------------
# EC2 helpers
# ---------------------------------------------------------------------------

def get_instance_state(instance_id: str) -> str:
    ec2 = boto3.client("ec2")
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    return resp["Reservations"][0]["Instances"][0]["State"]["Name"]


def get_instance_public_ip(instance_id: str) -> str | None:
    ec2 = boto3.client("ec2")
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    return resp["Reservations"][0]["Instances"][0].get("PublicIpAddress")


def reset_idle_alarm(alarm_name: str) -> None:
    # Without this reset, the alarm stays in ALARM from the prior stop cycle,
    # so the next OK→ALARM transition never fires and auto-stop silently breaks.
    cloudwatch = boto3.client("cloudwatch")
    cloudwatch.set_alarm_state(
        AlarmName=alarm_name,
        StateValue="OK",
        StateReason="Reset by /mc start to re-arm idle-stop",
    )


# ---------------------------------------------------------------------------
# RCON helpers (via itzg/minecraft-server container rcon-cli)
# ---------------------------------------------------------------------------

def get_rcon_password(secret_arn: str) -> str:
    secrets = boto3.client("secretsmanager")
    return secrets.get_secret_value(SecretId=secret_arn)["SecretString"]


def rcon_command(command: str, rcon_password: str, host: str = "localhost", port: int = 25575) -> str:
    """Send an RCON command to the Minecraft server via the Source RCON protocol."""
    import socket
    import struct

    def _build_packet(request_id: int, packet_type: int, payload: str) -> bytes:
        body = payload.encode("utf-8") + b"\x00\x00"  # null-terminated + padding
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

        # Authenticate (type 3 = SERVERDATA_AUTH)
        sock.sendall(_build_packet(1, 3, rcon_password))
        _recv_packet(sock)

        # Send command (type 2 = SERVERDATA_EXECCOMMAND)
        sock.sendall(_build_packet(2, 2, command))
        response = _recv_packet(sock)
    finally:
        sock.close()

    if len(response) < 8:
        return ""
    # Response body starts after request_id (4) + type (4), strip trailing nulls
    return response[8:].rstrip(b"\x00").decode("utf-8", errors="replace")


def get_player_count(rcon_password: str, rcon_host: str = None) -> int:
    """Get the current player count via RCON."""
    try:
        output = rcon_command("list", rcon_password, host=rcon_host or get_instance_public_ip(os.environ["INSTANCE_ID"]) or "localhost")
        # PaperMC: "There are <N> of a max of <M> players online: <names>"
        match = re.search(r"There are (\d+)", output)
        return int(match.group(1)) if match else 0
    except Exception as e:
        logger.warning(f"RCON failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# Discord interaction helpers
# ---------------------------------------------------------------------------

def discord_response(content: str, ephemeral: bool = True) -> dict:
    """Build a Discord interaction response payload."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "type": 4,  # CHANNEL_MESSAGE_WITH_SOURCE
            "data": {
                "content": content,
                "flags": 64 if ephemeral else 0  # EPHEMERAL
            }
        })
    }


def discord_webhook_notify(webhook_url: str, message: str):
    """Send a message via Discord webhook (for idle-stop notification)."""
    if not webhook_url:
        return
    try:
        data = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        logger.error(f"Discord webhook failed: {e}")


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def handle_start(instance_id: str, rcon_password: str) -> dict:
    state = get_instance_state(instance_id)
    if state == "running":
        ip = get_instance_public_ip(instance_id)
        return discord_response(f"Server is already running at `{ip}:25565`.")

    ec2 = boto3.client("ec2")
    ec2.start_instances(InstanceIds=[instance_id])

    alarm_name = os.environ.get("IDLE_STOP_ALARM_NAME")
    if alarm_name:
        try:
            reset_idle_alarm(alarm_name)
        except Exception as e:
            logger.warning(f"Failed to reset idle-stop alarm: {e}")

    # Poll until running (up to 2 minutes)
    for _ in range(24):
        time.sleep(5)
        state = get_instance_state(instance_id)
        if state == "running":
            ip = get_instance_public_ip(instance_id)
            return discord_response(f"Server is up at `{ip}:25565`! Give it ~30s for Minecraft to fully start.")

    return discord_response("Server is starting but is taking longer than expected. Check again in a minute.")


def handle_stop(instance_id: str) -> dict:
    state = get_instance_state(instance_id)
    if state == "stopped":
        return discord_response("Server is already stopped.")

    ec2 = boto3.client("ec2")
    ec2.stop_instances(InstanceIds=[instance_id])
    return discord_response("Server is stopping. See you next time!")


def handle_status(instance_id: str, rcon_password: str) -> dict:
    state = get_instance_state(instance_id)
    if state == "stopped":
        return discord_response("Server is stopped. Use `/mc start` to spin it up.")

    ip = get_instance_public_ip(instance_id) or "pending"
    players = get_player_count(rcon_password, rcon_host=ip) if state == "running" else 0
    return discord_response(
        f"Server is running at `{ip}:25565`\n"
        f"Players online: {players}"
    )


def handle_players(instance_id: str, rcon_password: str) -> dict:
    state = get_instance_state(instance_id)
    if state != "running":
        return discord_response("Server is not running. Use `/mc start` first.")

    ip = get_instance_public_ip(instance_id) or "localhost"
    try:
        output = rcon_command("list", rcon_password, host=ip)
        return discord_response(f"```\n{output}\n```", ephemeral=False)
    except Exception as e:
        return discord_response(f"Could not fetch player list: {e}")


# ---------------------------------------------------------------------------
# EventBridge / direct invocation handler (idle-stop trigger)
# ---------------------------------------------------------------------------

def handle_stop_action(instance_id: str, webhook_url: str = None) -> dict:
    state = get_instance_state(instance_id)
    if state != "running":
        logger.info(f"Instance {instance_id} already {state}, nothing to stop")
        return {"statusCode": 200, "body": json.dumps({"skipped": True, "state": state})}

    ec2 = boto3.client("ec2")
    ec2.stop_instances(InstanceIds=[instance_id])
    logger.info(f"Instance {instance_id} stopped by idle-stop trigger")

    if webhook_url:
        discord_webhook_notify(
            webhook_url,
            "Server stopped — idle for 15 minutes. Use `/mc start` to resume."
        )

    return {"statusCode": 200, "body": json.dumps({"stopped": True, "instance_id": instance_id})}


def handle_start_action(instance_id: str) -> dict:
    state = get_instance_state(instance_id)
    if state == "running":
        logger.info(f"Instance {instance_id} already running")
        return {"statusCode": 200, "body": json.dumps({"skipped": True, "state": state})}

    ec2 = boto3.client("ec2")
    ec2.start_instances(InstanceIds=[instance_id])
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
# Main Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    logger.info(f"Event: {json.dumps(event)}")

    instance_id = os.environ.get("INSTANCE_ID")
    discord_signing_key_arn = os.environ.get("DISCORD_SIGNING_KEY_SECRET_ARN")
    rcon_password_arn = os.environ.get("RCON_PASSWORD_SECRET_ARN")
    discord_webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")

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

    # Verify Discord signature — reject requests with missing headers
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

    # Parse interaction payload
    try:
        payload = json.loads(body_bytes) if body_bytes else {}
    except Exception:
        payload = {}

    # Handle Discord ping (required for slash command registration)
    if payload.get("type") == 1:
        return {"statusCode": 200, "body": json.dumps({"type": 1})}

    # Extract command name and options
    command_name = (
        payload.get("data", {})
        .get("name", "")
    )
    options = {
        o["name"]: o.get("value")
        for o in payload.get("data", {}).get("options", [])
    }

    rcon_pw = get_rcon_password(rcon_password_arn) if rcon_password_arn else ""

    logger.info(f"Routing command: {command_name} options={options}")

    if command_name == "mc":
        sub = options.get("sub") or options.get("action", "status")
        if sub == "start":
            return handle_start(instance_id, rcon_pw)
        elif sub == "stop":
            return handle_stop(instance_id)
        elif sub == "status":
            return handle_status(instance_id, rcon_pw)
        elif sub == "players":
            return handle_players(instance_id, rcon_pw)
        else:
            return discord_response(f"Unknown subcommand: `{sub}`. Use `/mc start|stop|status|players`.")

    # Generic action routes (backward compat with event['action'])
    action = event.get("action") or options.get("action")
    if action in ("start", "stop", "status"):
        return discord_response(f"Use `/mc {action}` instead.")

    return discord_response("Unknown interaction type.")

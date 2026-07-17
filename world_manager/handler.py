"""World-manager web UI backend (see docs/webui.md).

A small, bearer-token-authenticated HTTP API behind a Lambda Function URL that
manages Minecraft world profiles for the shared server. It is deliberately
separate from server_controller (the Discord control plane): that Lambda is
shaped around Discord's Ed25519 signature + deferred-interaction model, whereas
this one speaks plain JSON to a browser SPA.

State model (unchanged from multi-world, see docs/multi-world.md):
  - Each world is a JSON "profile document" in SSM at WORLDS_PREFIX + <name>.
  - The active world is a single SSM String param (ACTIVE_WORLD_PARAM).
  - The registry of known worlds is a StringList param (WORLD_LIST_PARAM).
  - The on-box run.sh reconciles the active profile document into the world dir
    at container-start time, so world changes take effect on the next cold start.

This handler therefore never touches the instance's filesystem directly; it only
edits SSM params and starts/stops EC2. Provisioning (downloading plugins/mods to
their exact paths) is done declaratively by run.sh from the profile document.
"""

import hmac
import json
import logging
import os
import re
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ec2 = boto3.client("ec2")
_ssm = boto3.client("ssm")
_secrets = boto3.client("secretsmanager")
_cloudwatch = boto3.client("cloudwatch")

_token_cache: str | None = None
_rcon_password_cache: str | None = None

WORLD_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
# Server types run.sh / itzg understand. PAPER + VANILLA are v1 (plugins); the
# loaders are v2 (modded). Kept permissive here so v2 needs no backend change.
ALLOWED_TYPES = {"PAPER", "VANILLA", "FABRIC", "FORGE", "NEOFORGE", "SPIGOT", "PURPUR", "QUILT"}
VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
# Artifact-list token (Modrinth slug/id, Spiget id, CurseForge file id). Kept
# tight so nothing exotic reaches an itzg env var / download path.
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
PROP_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _get_token() -> str:
    global _token_cache
    if _token_cache is None:
        arn = os.environ["WEBUI_TOKEN_SECRET_ARN"]
        _token_cache = _secrets.get_secret_value(SecretId=arn)["SecretString"].strip()
    return _token_cache


def require_auth(headers: dict) -> bool:
    """Constant-time compare of the Bearer token against the stored secret."""
    auth = (headers or {}).get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    presented = auth[7:].strip()
    try:
        expected = _get_token()
    except Exception as e:
        logger.error(f"Failed to load webui token secret: {e}")
        return False
    if not expected:
        return False
    return hmac.compare_digest(presented, expected)


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
    _cloudwatch.set_alarm_state(
        AlarmName=alarm_name,
        StateValue="OK",
        StateReason="Reset by world-manager start to re-arm idle-stop",
    )


def start_instance(instance_id: str) -> None:
    if get_instance_state(instance_id) == "running":
        return
    _ec2.start_instances(InstanceIds=[instance_id])
    alarm_name = os.environ.get("IDLE_STOP_ALARM_NAME")
    if alarm_name:
        try:
            reset_idle_alarm(alarm_name)
        except Exception as e:
            logger.warning(f"Failed to reset idle-stop alarm: {e}")


def stop_instance(instance_id: str) -> None:
    if get_instance_state(instance_id) == "stopped":
        return
    _ec2.stop_instances(InstanceIds=[instance_id])


def wait_for_state(instance_id: str, want: str, tries: int = 24, delay: int = 5) -> str:
    state = get_instance_state(instance_id)
    for _ in range(tries):
        if state == want:
            return state
        time.sleep(delay)
        state = get_instance_state(instance_id)
    return state


# ---------------------------------------------------------------------------
# RCON (read-only player count) — copied from server_controller.controller so
# this Lambda stays self-contained. Keep the two in sync if the protocol changes.
# ---------------------------------------------------------------------------


def rcon_command(command: str, rcon_password: str, host: str, port: int = 25575) -> str:
    import socket
    import struct

    def _build(request_id: int, ptype: int, payload: str) -> bytes:
        body = payload.encode("utf-8") + b"\x00\x00"
        return struct.pack("<iii", len(body) + 8, request_id, ptype) + body

    def _recv(s) -> bytes:
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
        sock.sendall(_build(1, 3, rcon_password))
        _recv(sock)
        sock.sendall(_build(2, 2, command))
        response = _recv(sock)
    finally:
        sock.close()

    if len(response) < 8:
        return ""
    return response[8:].rstrip(b"\x00").decode("utf-8", errors="replace")


def get_player_count(host: str) -> int:
    arn = os.environ.get("RCON_PASSWORD_SECRET_ARN")
    if not arn:
        return 0
    global _rcon_password_cache
    if _rcon_password_cache is None:
        _rcon_password_cache = _secrets.get_secret_value(SecretId=arn)["SecretString"]
    try:
        out = rcon_command("list", _rcon_password_cache, host=host)
        m = re.search(r"There are (\d+)", out)
        return int(m.group(1)) if m else 0
    except Exception as e:
        logger.warning(f"RCON player count failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# World registry / profile documents (SSM)
# ---------------------------------------------------------------------------


def _worlds_prefix() -> str:
    p = os.environ["WORLDS_PREFIX"]
    return p if p.endswith("/") else p + "/"


def get_world_list() -> list[str]:
    param = os.environ["WORLD_LIST_PARAM"]
    try:
        raw = _ssm.get_parameter(Name=param)["Parameter"]["Value"]
    except _ssm.exceptions.ParameterNotFound:
        return []
    return [w.strip() for w in raw.split(",") if w.strip()]


def put_world_list(worlds: list[str]) -> None:
    _ssm.put_parameter(
        Name=os.environ["WORLD_LIST_PARAM"],
        Value=",".join(worlds),
        Type="StringList",
        Overwrite=True,
    )


def get_active_world() -> str:
    try:
        return _ssm.get_parameter(Name=os.environ["ACTIVE_WORLD_PARAM"])["Parameter"]["Value"].strip()
    except _ssm.exceptions.ParameterNotFound:
        return ""


def set_active_world(name: str) -> None:
    _ssm.put_parameter(Name=os.environ["ACTIVE_WORLD_PARAM"], Value=name, Type="String", Overwrite=True)


def get_profile(name: str) -> dict | None:
    try:
        raw = _ssm.get_parameter(Name=_worlds_prefix() + name)["Parameter"]["Value"]
    except _ssm.exceptions.ParameterNotFound:
        return None
    try:
        return json.loads(raw)
    except Exception:
        logger.warning(f"Profile document for '{name}' is not valid JSON")
        return None


def put_profile(name: str, profile: dict) -> None:
    _ssm.put_parameter(
        Name=_worlds_prefix() + name,
        # Compact + no newlines so the on-box `aws ssm get-parameter --output
        # text` reads it back intact for jq.
        Value=json.dumps(profile, separators=(",", ":")),
        Type="String",
        Overwrite=True,
    )


def delete_profile(name: str) -> None:
    try:
        _ssm.delete_parameter(Name=_worlds_prefix() + name)
    except _ssm.exceptions.ParameterNotFound:
        pass


# ---------------------------------------------------------------------------
# Profile validation — mirror of what run.sh will accept (docs/webui.md §2).
# Returns (cleaned_profile, error_message). error_message is None on success.
# ---------------------------------------------------------------------------


def _clean_token_list(value, field: str) -> tuple[list[str], str | None]:
    if value is None:
        return [], None
    if not isinstance(value, list):
        return [], f"{field} must be a list"
    out = []
    for item in value:
        s = str(item).strip()
        if not s:
            continue
        if not TOKEN_RE.match(s):
            return [], f"{field} entry '{s}' has invalid characters"
        out.append(s)
    return out, None


def validate_profile(profile) -> tuple[dict, str | None]:
    if not isinstance(profile, dict):
        return {}, "profile must be an object"

    cleaned: dict = {}

    wtype = str(profile.get("type", "PAPER")).strip().upper() or "PAPER"
    if wtype not in ALLOWED_TYPES:
        return {}, f"type must be one of {', '.join(sorted(ALLOWED_TYPES))}"
    cleaned["type"] = wtype

    version = profile.get("version")
    if version not in (None, ""):
        if not VERSION_RE.match(str(version)):
            return {}, "version has invalid characters"
        cleaned["version"] = str(version)

    mem = profile.get("memory_gb")
    if mem not in (None, ""):
        try:
            mem_i = int(mem)
        except (TypeError, ValueError):
            return {}, "memory_gb must be an integer"
        if not (1 <= mem_i <= 64):
            return {}, "memory_gb must be between 1 and 64"
        cleaned["memory_gb"] = mem_i

    plugins_in = profile.get("plugins") or {}
    if not isinstance(plugins_in, dict):
        return {}, "plugins must be an object"
    plugins = {}
    for key in ("spiget", "modrinth", "urls"):
        if key == "urls":
            urls, err = _validate_urls(plugins_in.get(key), f"plugins.{key}")
        else:
            urls, err = _clean_token_list(plugins_in.get(key), f"plugins.{key}")
        if err:
            return {}, err
        if urls:
            plugins[key] = urls
    if plugins:
        cleaned["plugins"] = plugins

    mods_in = profile.get("mods") or {}
    if not isinstance(mods_in, dict):
        return {}, "mods must be an object"
    mods = {}
    for key in ("modrinth", "curseforge", "urls"):
        if key == "urls":
            vals, err = _validate_urls(mods_in.get(key), f"mods.{key}")
        else:
            vals, err = _clean_token_list(mods_in.get(key), f"mods.{key}")
        if err:
            return {}, err
        if vals:
            mods[key] = vals
    if mods:
        cleaned["mods"] = mods

    files_in = profile.get("files") or []
    if not isinstance(files_in, list):
        return {}, "files must be a list"
    files = []
    for i, f in enumerate(files_in):
        if not isinstance(f, dict):
            return {}, f"files[{i}] must be an object"
        url = str(f.get("url", "")).strip()
        dest = str(f.get("dest", "")).strip()
        if not url.startswith("https://"):
            return {}, f"files[{i}].url must be an https:// URL"
        if not dest or dest.startswith("/") or ".." in dest.split("/"):
            return {}, f"files[{i}].dest must be a relative path without '..'"
        files.append({"url": url, "dest": dest})
    if files:
        cleaned["files"] = files

    props_in = profile.get("properties") or {}
    if not isinstance(props_in, dict):
        return {}, "properties must be an object"
    props = {}
    for k, v in props_in.items():
        key = str(k).strip().upper()
        if not PROP_KEY_RE.match(key):
            return {}, f"properties key '{k}' is invalid (use UPPER_SNAKE_CASE)"
        if isinstance(v, bool):
            props[key] = "true" if v else "false"
        elif isinstance(v, (int, float, str)):
            sval = str(v)
            if "\n" in sval or len(sval) > 256:
                return {}, f"properties value for '{key}' is too long or multiline"
            props[key] = sval
        else:
            return {}, f"properties value for '{key}' must be a scalar"
    if props:
        cleaned["properties"] = props

    cf = profile.get("cf_api_key")
    if cf not in (None, ""):
        cleaned["cf_api_key"] = str(cf).strip()

    notes = profile.get("notes")
    if notes not in (None, ""):
        cleaned["notes"] = str(notes)[:512]

    return cleaned, None


def _validate_urls(value, field: str) -> tuple[list[str], str | None]:
    if value is None:
        return [], None
    if not isinstance(value, list):
        return [], f"{field} must be a list"
    out = []
    for item in value:
        s = str(item).strip()
        if not s:
            continue
        if not s.startswith("https://"):
            return [], f"{field} entry must be an https:// URL"
        out.append(s)
    return out, None


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _resp(status: int, body, content_type: str = "application/json") -> dict:
    if content_type == "application/json" and not isinstance(body, str):
        body = json.dumps(body)
    return {
        "statusCode": status,
        "headers": {"Content-Type": content_type, "Cache-Control": "no-store"},
        "body": body,
    }


_spa_cache: str | None = None


def _spa() -> str:
    global _spa_cache
    if _spa_cache is None:
        with open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8") as fh:
            _spa_cache = fh.read()
    return _spa_cache


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------


def api_list_worlds() -> dict:
    worlds = get_world_list()
    active = get_active_world()
    out = []
    for name in sorted(worlds):
        out.append({"name": name, "active": name == active, "profile": get_profile(name)})
    return _resp(200, {"worlds": out, "active": active})


def api_create_world(payload: dict) -> dict:
    name = str(payload.get("name", "")).strip().lower()
    if not WORLD_NAME_RE.match(name):
        return _resp(400, {"error": "name must be 1-32 chars: a-z, 0-9, '_' or '-'"})
    worlds = get_world_list()
    if name in worlds:
        return _resp(409, {"error": f"world '{name}' already exists"})
    profile, err = validate_profile(payload.get("profile") or {})
    if err:
        return _resp(400, {"error": err})
    put_profile(name, profile)
    put_world_list(sorted(worlds + [name]))
    return _resp(201, {"name": name, "profile": profile})


def api_update_world(name: str, payload: dict) -> dict:
    if name not in get_world_list():
        return _resp(404, {"error": f"unknown world '{name}'"})
    profile, err = validate_profile(payload.get("profile") or {})
    if err:
        return _resp(400, {"error": err})
    put_profile(name, profile)
    return _resp(200, {"name": name, "profile": profile})


def api_delete_world(name: str) -> dict:
    worlds = get_world_list()
    if name not in worlds:
        return _resp(404, {"error": f"unknown world '{name}'"})
    if name == get_active_world():
        return _resp(409, {"error": "cannot delete the active world; switch to another world first"})
    # Soft delete: drop from the registry and remove the profile document. The
    # world directory on the data volume is intentionally left in place (see
    # docs/webui.md §9) — reclaim disk manually if needed.
    put_world_list([w for w in worlds if w != name])
    delete_profile(name)
    return _resp(200, {"deleted": name})


def api_activate_world(name: str, payload: dict, instance_id: str) -> dict:
    if name not in get_world_list():
        return _resp(404, {"error": f"unknown world '{name}'"})
    set_active_world(name)
    restart = bool(payload.get("restart"))
    state = get_instance_state(instance_id)
    if not restart:
        note = (
            "takes effect after a stop/start"
            if state == "running"
            else "loads on the next start"
        )
        return _resp(200, {"active": name, "restarted": False, "note": note})

    # Switch & restart: stop (if running), wait for stopped, then start.
    if state == "running":
        stop_instance(instance_id)
        state = wait_for_state(instance_id, "stopped")
        if state != "stopped":
            return _resp(
                202,
                {"active": name, "restarted": False, "note": "stop is taking longer than expected; start it manually once stopped"},
            )
    start_instance(instance_id)
    return _resp(200, {"active": name, "restarted": True, "note": "server is starting with the new world"})


def api_server_status(instance_id: str) -> dict:
    state = get_instance_state(instance_id)
    ip = get_instance_public_ip(instance_id)
    players = get_player_count(ip) if state == "running" and ip else 0
    return _resp(200, {"state": state, "ip": ip, "players": players, "active_world": get_active_world()})


def api_server_start(instance_id: str) -> dict:
    start_instance(instance_id)
    return _resp(200, {"state": get_instance_state(instance_id)})


def api_server_stop(instance_id: str) -> dict:
    stop_instance(instance_id)
    return _resp(200, {"state": get_instance_state(instance_id)})


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def lambda_handler(event, context):
    ctx = event.get("requestContext", {}).get("http", {})
    method = ctx.get("method", "GET")
    path = event.get("rawPath", "/")
    headers = event.get("headers", {}) or {}

    # The SPA shell is public (it can't do anything without the token); every
    # /api/* route requires the bearer token.
    if method == "GET" and path == "/":
        return _resp(200, _spa(), content_type="text/html; charset=utf-8")

    if not path.startswith("/api/"):
        return _resp(404, {"error": "not found"})

    if not require_auth(headers):
        return _resp(401, {"error": "unauthorized"})

    try:
        body = json.loads(event["body"]) if event.get("body") else {}
        if not isinstance(body, dict):
            body = {}
    except Exception:
        return _resp(400, {"error": "invalid JSON body"})

    instance_id = os.environ["INSTANCE_ID"]

    try:
        # /api/worlds and /api/worlds/{name}[/activate]
        if path == "/api/worlds":
            if method == "GET":
                return api_list_worlds()
            if method == "POST":
                return api_create_world(body)
            return _resp(405, {"error": "method not allowed"})

        m = re.match(r"^/api/worlds/([^/]+)(/activate)?$", path)
        if m:
            name = m.group(1).lower()
            is_activate = m.group(2) is not None
            if is_activate and method == "POST":
                return api_activate_world(name, body, instance_id)
            if not is_activate and method == "PUT":
                return api_update_world(name, body)
            if not is_activate and method == "DELETE":
                return api_delete_world(name)
            return _resp(405, {"error": "method not allowed"})

        if path == "/api/server" and method == "GET":
            return api_server_status(instance_id)
        if path == "/api/server/start" and method == "POST":
            return api_server_start(instance_id)
        if path == "/api/server/stop" and method == "POST":
            return api_server_stop(instance_id)

        return _resp(404, {"error": "not found"})
    except Exception as e:
        logger.exception("Request failed")
        return _resp(500, {"error": str(e)})

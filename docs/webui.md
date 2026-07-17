# World Manager Web UI — Implementation Plan

**Status:** Draft (not yet implemented)
**Depends on:** multi-world profiles (shipped — see [multi-world.md](./multi-world.md))
**Goal:** A small, authenticated web UI to manage worlds on the shared server — full CRUD —
so that provisioning a **plugin world** (skyblock/BentoBox today, more later) and, in v2, a
**modded world** (Fabric/Forge) is a form you fill out, not a hand-run SSM shell session.

---

## 1. Motivation

Today the everyday world ops (`list`, `set`, `start`, `stop`) are one-liners in Discord, but
*creating* a non-trivial world is entirely manual (runbook §"Multi-World Profiles"):

```
aws ssm start-session --target <id>
sudo mkdir -p /opt/minecraft/worlds/skyblock/plugins/BentoBox/addons
curl -L ... -o plugins/BentoBox-x.y.z.jar
curl -L ... -o plugins/BentoBox/addons/BSkyBlock-x.y.z.jar
printf 'MC_VERSION=26.1.2\n' > profile.env
chown -R 1000:1000 /opt/minecraft/worlds/skyblock
# then /mc world set skyblock -> /mc stop -> /mc start, tail journalctl
```

That's the headache this UI removes. It is also the exact shape of what modded worlds need
(a loader + a list of mod artifacts at specific paths), so the design targets **plugin worlds
in v1 and modded worlds in v2 with the same core machinery.**

---

## 2. The core idea — a declarative "world profile" reconciled at cold start

The multi-world design already established the invariant: **SSM is the source of truth, and it
is realized when the container next starts** (`run.sh` reads SSM at `ExecStart`). We extend that
same invariant rather than fighting it.

Each world becomes a **profile document** (JSON) stored in SSM. `run.sh` grows from "read a name,
mount a dir" into a small **reconciler**: read the active profile doc, make the world directory
match it (download declared artifacts to their exact paths if missing), render the itzg
environment from it, then `docker run`. The itzg image itself handles the well-trodden download
paths (`SPIGET_RESOURCES`, `MODRINTH_PROJECTS`, `MODS`); the profile's explicit `files` list
covers the fiddly placements itzg has no concept of (e.g. a BentoBox **addon** belongs in
`plugins/BentoBox/addons/`, not `plugins/`).

Consequences:

- **No `ssm:SendCommand` needed for provisioning.** Creating a plugin world = write a profile doc
  + add the name to the registry, entirely while the instance is *stopped*. The files appear on
  the next `start`. This keeps the whole system in the "everything happens at cold start" model.
- **The UI is a CRUD editor over profile docs** plus the existing start/stop/switch verbs. Almost
  no new server-side *logic* — mostly a new authenticated surface and a richer `run.sh`.
- **Idempotent + self-healing.** A missing artifact re-downloads on next boot; a hand-edited box
  and the doc converge.

### Profile document schema (v1)

Stored at SSM `/<server_name>/stats/worlds/<name>` (String, JSON — reusing the `/stats/` prefix
to stay inside the deploy user's existing IAM path grant, per the multi-world decision):

```jsonc
{
  "type": "PAPER",                 // v1: PAPER | VANILLA. v2 adds FABRIC | FORGE | NEOFORGE
  "version": "26.1.2",             // MC version pin; omit to track the server default
  "memory_gb": null,               // optional per-world RAM override; null => server default
  "plugins": {
    "spiget":   ["1234"],          // -> SPIGET_RESOURCES (SpigotMC resource IDs)
    "modrinth": ["bentobox"],      // -> MODRINTH_PROJECTS (slugs/ids)
    "urls":     []                 // -> PLUGINS (direct jar URLs, dropped in plugins/)
  },
  "files": [                       // explicit artifact -> exact destination (relative to /data)
    { "url": "https://.../BSkyBlock-3.x.jar",
      "dest": "plugins/BentoBox/addons/BSkyBlock.jar" }
  ],
  "properties": {                  // itzg-mapped server.properties (LEVEL_TYPE, DIFFICULTY, ...)
    "DIFFICULTY": "normal"
  },
  "notes": "BentoBox skyblock"
}
```

`run.sh` maps this to itzg env vars and reconciles `files` before launch. Unknown/empty sections
are no-ops. The existing `profile.env` (MC_VERSION/PAPER_CHANNEL) stays supported as a fallback so
already-provisioned worlds keep working; the doc supersedes it when present.

---

## 3. Architecture

```
Browser ──HTTPS──> Lambda Function URL (webui)
                        │  Authorization: Bearer <token>
                        ▼
              validates the bearer token (constant-time compare vs. Secrets Manager)
                        │
        ┌───────────────┼─────────────────────────┐
        ▼               ▼                           ▼
   SSM params      EC2 Start/Stop/Describe     RCON (running server only)
 (profiles,        (existing control verbs)    (players/whitelist echo)
  active-world,
  world-list)

On the box: run.sh reads the active profile doc from SSM at ExecStart and reconciles the
world dir (itzg auto-downloads + explicit files), then docker run.
```

The Function URL's AWS hostname is used directly (optionally fronted by a friendly
`mc-admin.rsinema.com` CNAME). Since the only gate is the bearer token, the token must be
treated as the sole secret — see §7.

### Key design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Provisioning model | **Declarative profile doc, reconciled by `run.sh` at cold start** | Reuses the multi-world invariant; no `SendCommand`; works while stopped; self-healing |
| Where profiles live | SSM `/<server_name>/stats/worlds/<name>` (JSON) | Same store/prefix/IAM path as existing world params; readable by the instance role (`ssm:GetParameter` on `*`) |
| Plugin/mod download | itzg env (`SPIGET_RESOURCES`, `MODRINTH_PROJECTS`, `MODS`) for standard cases; profile `files[]` for exact-path artifacts (addons/configs) | Lets itzg do the heavy lifting; `files[]` covers what it can't express |
| Backend | **New `modules/webui/` Lambda + Function URL**, separate from the Discord control Lambda | The control Lambda is Discord-signature-shaped; a browser JSON+HTML API doesn't belong there. Shared ops factored into an importable helper (see 4.4) |
| Frontend hosting | **Served by the same Lambda** (GET returns a single-file SPA; POST is the JSON API) | No S3/CloudFront to manage for a handful of users; one deployable |
| Auth | **Shared bearer token** stored in Secrets Manager, checked in the Lambda (constant-time compare) | Dead simple for a trusted handful of users; no SSO/JWT plumbing. Trade-off accepted: no per-user identity or central revocation beyond rotating the token — see §7 |
| Applying a switch | UI "Switch & restart" does `set active` → `stop` → poll → `start` and streams status | Makes the cold-start reality a single button with honest progress, not three steps |
| Delete | v1 **soft delete** (remove from registry + delete profile doc); directory left as reclaimable data | Keeps blast radius tiny and needs no `SendCommand`. Hard delete (reclaim disk) is a gated follow-up |
| World templates | Ship presets (e.g. "BentoBox Skyblock") that pre-fill the create form | Turns the skyblock headache into "pick template → name it → create" |

### Where this hooks into existing code (the seams, confirmed)

- **EC2 start/stop** already has a signature-free path in `controller.py`
  (`handle_start_action` / `handle_stop_action`, `{"action":"start"|"stop"}`). The webui Lambda
  can reuse that logic directly.
- **SSM params** `active-world` / `world-list` already exist and are the switch + registry.
- **`run.sh`** already reads SSM at `ExecStart` and already reads a per-profile `profile.env` —
  extending it to read a JSON doc is an incremental change, not a rewrite.

---

## 4. Changes by component (v1 — plugin worlds)

### 4.1 `modules/compute/scripts/compute_setup.sh.tpl` — `run.sh` becomes a reconciler

Extend the existing `run.sh` (currently lines 34–108). After resolving `NAME`/`DIR` and the
shared whitelist/ops seeding, add a reconcile step **before** the `docker run`:

1. `aws ssm get-parameter --name /<server_name>/stats/worlds/$NAME` → parse JSON
   (use `jq`; add to the dnf install list, or a minimal `python3 -c` parse to avoid a new dep).
2. Charset-guard every value that lands in a shell command or a URL. Only allow `https://`
   URLs; reject anything else. `dest` must be a relative path with no `..` segment.
3. For each `files[]`: if the destination is missing, `mkdir -p` its parent, `curl -fsSL` to a
   temp file, move into place, `chown 1000:1000`.
4. Render itzg env from the doc: `TYPE`, `VERSION`, `MEMORY`, `SPIGET_RESOURCES`,
   `MODRINTH_PROJECTS`, plus `properties` (each mapped to its itzg env var), merged over the
   current forced defaults (`EULA`, `ENABLE_RCON`, `RCON_PASSWORD`, ports, `ENFORCE_WHITELIST`).
5. `docker run` as today, with the assembled env.

Keep the current behavior when no profile doc exists (fall back to `profile.env` then to the
server defaults), so existing survival/skyblock worlds are unaffected.

> **Boundary note (CLAUDE.md):** `compute_setup.sh.tpl` is listed as safe to modify. This is a
> meaningful behavior change to the boot path, so it still warrants a careful `tofu plan` review
> and the instance-replacement rollout in §6.

### 4.2 `modules/webui/` — new module (Lambda + Function URL)

Mirror `modules/control/` structure (`main.tf`, `variables.tf`, `outputs.tf`):

- `aws_lambda_function.world_manager` — python3.11, arm64, packaged from `world_manager/`.
- `aws_lambda_function_url` — `authorization_type = "NONE"` (auth enforced in code via the shared
  bearer token; see §7), `POST` + `GET`, CORS locked to the app origin.
- `aws_secretsmanager_secret` for the webui bearer token (value set out-of-band, not in tfvars —
  matches the "never commit secrets" convention; generate with `openssl rand -hex 32`).
- IAM policy for the Lambda role:
  - `ssm:GetParameter`, `ssm:PutParameter`, `ssm:DeleteParameter`, `ssm:GetParametersByPath`
    scoped to `/<server_name>/stats/worlds/*` **plus** `active-world` and `world-list`.
  - `ec2:StartInstances`, `ec2:StopInstances`, `ec2:DescribeInstances` (reuse the control
    Lambda's statements).
  - `cloudwatch:SetAlarmState` (to reset the idle alarm on start, matching `run_start`).
  - `secretsmanager:GetSecretValue` on the **webui token secret** and the RCON secret (the latter
    for read-only players/whitelist echo).
- Env vars: `INSTANCE_ID`, `ACTIVE_WORLD_PARAM`, `WORLD_LIST_PARAM`, `WORLDS_PREFIX`,
  `WEBUI_TOKEN_SECRET_ARN`, RCON secret ARN.
- Output the Function URL (target for the optional friendly CNAME).

### 4.3 `world_manager/` — new Lambda handler

`world_manager/handler.py` with a tiny router on method + path:

- `GET /` → return the single-file SPA (HTML+JS inlined).
- `GET  /api/worlds` → list registry + active + each profile doc (for the table).
- `POST /api/worlds` → create: validate name (`^[a-z0-9_-]{1,32}$`) + schema, write profile doc,
  append to `world-list`.
- `PUT  /api/worlds/{name}` → edit profile doc.
- `POST /api/worlds/{name}/activate` → write `active-world`; optional `restart: true` runs
  stop→poll→start.
- `DELETE /api/worlds/{name}` → soft delete (remove from `world-list`, delete profile doc);
  refuse if it's the active world.
- `POST /api/server/{start|stop}` and `GET /api/server/status` → reuse control logic.

Every handler first calls `require_auth(event)` — extract the `Authorization: Bearer <token>`
header, constant-time compare (`hmac.compare_digest`) against the token read from Secrets Manager
(cache it across warm invocations), 401 on mismatch/missing. The `GET /` SPA shell is served
without the token; the browser prompts for the token once and sends it on every `/api/*` call.
Validation of the profile schema lives in one place and is shared with `run.sh`'s expectations
(document the contract at the top of both).

### 4.4 `server_controller/` — factor out shared ops (light touch)

To avoid duplicating EC2/RCON/SSM helpers across two Lambdas, extract the reusable, Discord-free
functions (`ec2` start/stop/describe, `rcon_command`, SSM get/put helpers) into a small module
importable by both (e.g. `server_controller/ops.py`), leaving `controller.py`'s Discord handling
in place. If cross-Lambda packaging is awkward, acceptable v1 fallback is a modest copy in
`world_manager/` — note the duplication and keep the two in sync.

### 4.5 `modules/dns/` + root wiring

- **Optional:** a Cloudflare CNAME `mc-admin.rsinema.com` → the webui Function URL host, for a
  friendly URL. Not required — the raw Function URL works, and the bearer token is the gate either
  way. If added, `proxied = true` is fine (adds Cloudflare's TLS/WAF) but not load-bearing here.
- Wire `module.webui` in root `main.tf` (module composition — **flagged per CLAUDE.md**;
  confirm before wiring).
- Root `variables.tf`: none required for auth (the token lives in Secrets Manager). Add the
  `mc-admin` hostname var only if you opt into the friendly CNAME.

### 4.6 Docs

- `docs/runbook.md` — replace the manual skyblock provisioning steps with "create a plugin world
  from the UI"; add the Cloudflare Access setup and the JWT/email allowlist procedure; keep the
  manual SSM path as the break-glass fallback.

---

## 5. v2 — modded worlds (Fabric / Forge / NeoForge)

Same machinery; the profile schema and `run.sh` do most of the work already. Deltas:

| Area | v2 addition |
|---|---|
| Loader | Allow `type` ∈ `FABRIC | FORGE | NEOFORGE`; `run.sh` passes it straight to itzg `TYPE`. itzg installs the loader and picks the right Java for the MC version automatically |
| Mod sources | Profile `mods: { modrinth: [...], curseforge: [...], urls: [...] }` → itzg `MODRINTH_PROJECTS` / `CURSEFORGE_FILES` / `MODS`. Modrinth **modpacks** via `MODRINTH_MODPACK` |
| CurseForge auth | CurseForge downloads need an API key → store in Secrets Manager, inject as `CF_API_KEY`. Add the secret + IAM read + `run.sh` passthrough |
| Memory / disk | Modded worlds are heavier — surface `memory_gb` prominently in the UI; likely bump `mc_volume_size` and consider whether `t4g.large` (8 GB) is enough for the heavier packs (may want a per-world instance-type note, out of scope to change automatically) |
| Client parity | Mods require a **matching client**. The UI should display the world's loader + version + mod list, and ideally emit a client-side manifest (e.g. a Modrinth `.mrpack` reference or a copy-paste mod list) so players can install the right pack. This is the main *new* UX surface in v2 |
| Version validation | Validate loader/MC/mod compatibility as best-effort warnings in the create form (e.g. flag when a pinned MC version has no build for the chosen loader) |

Everything else — CRUD, switch-and-restart, auth, hosting — is unchanged from v1.

---

## 6. Rollout order (safety-first)

1. **Snapshot the data volume** before any `run.sh` change (matches the multi-world rollout).
2. Land `run.sh` reconciler + schema, `tofu fmt`/`validate`; `tofu apply` (replaces the instance,
   data volume persists). Verify existing survival + skyblock still boot unchanged (fallback path).
3. Land `modules/webui/` + `world_manager/`; deploy; hit the Function URL directly to confirm the
   API and that requests without a valid bearer token are rejected (401).
4. Set the token secret (`openssl rand -hex 32` → `aws secretsmanager put-secret-value`); confirm
   the SPA prompts for it and that `/api/*` calls fail without it. Optionally add the friendly
   `mc-admin` CNAME.
5. End-to-end: create a **new** BentoBox skyblock world from a template → switch & restart →
   confirm it boots with the addon in `plugins/BentoBox/addons/` and no shell session was used.
6. Update the runbook; keep the manual SSM procedure as break-glass.

v2 ships as a follow-up: extend the schema + `run.sh` for loaders, add CurseForge secret, add the
client-manifest view.

---

## 7. Auth — shared bearer token

**Chosen:** a single shared secret token, stored in AWS Secrets Manager, checked by the Lambda on
every `/api/*` request (`Authorization: Bearer <token>`, constant-time compare via
`hmac.compare_digest`). The Function URL is `authorization_type = NONE`; the token is the gate.

- **Token lifecycle:** generate with `openssl rand -hex 32`; set it in Secrets Manager
  out-of-band (never in tfvars/git). Rotate by overwriting the secret value — the Lambda picks it
  up on the next cold start (or clear the warm cache with a no-op deploy). Share it with your
  friends however you already share the RCON-level trust.
- **Client handling:** the SPA prompts for the token once and keeps it in `sessionStorage`
  (or `localStorage` if you want it to persist); it's sent on each API call.

Trade-offs accepted (fine for a trusted handful, matching the Discord control-plane trust model):
- No per-user identity or audit — every caller is "whoever has the token."
- Revocation is all-or-nothing (rotate the token; everyone re-enters the new one).
- The token must not leak (it grants start/stop + world CRUD). Serve only over HTTPS (Function
  URLs are HTTPS-only), and don't log the header.

If per-user identity or central revocation ever matters, the upgrade path is Cloudflare Access
(JWT validation + email allowlist) or Cognito — but that's explicitly out of scope now.

---

## 8. Effort estimate

| Piece | Rough lift |
|---|---|
| `run.sh` reconciler + profile schema | 0.5–1 day (careful shell + validation, plus a safe rollout) |
| `modules/webui/` + `world_manager/` API | 1 day (mostly wiring; ops logic is reused) |
| Bearer-token auth (Secrets Manager + compare) | ~1–2 hours |
| Single-file SPA (table + create/edit form + templates) | 1 day |
| v1 total | **~3 days**, or a focused weekend |
| v2 (modded: loaders, CurseForge, client manifest) | **+1–2 days** |

---

## 9. Out of scope

- Live world switching without a restart (still requires a stop/start — same as multi-world).
- Running multiple worlds concurrently (rejected in multi-world: RAM + cost).
- Editing the client for players automatically (v2 provides a manifest/list, not installation).
- Hard delete with disk reclaim in v1 (soft delete only; hard delete is a gated follow-up that
  would add `ssm:SendCommand` or a boot-time sweep).

---

## 10. File touch list (v1)

| File | Change | CLAUDE.md boundary |
|---|---|---|
| `modules/compute/scripts/compute_setup.sh.tpl` | `run.sh` reconciler (profile doc → files + env) | Safe to modify (but boot-path change — review) |
| `modules/webui/{main,variables,outputs}.tf` | New module: Lambda + Function URL + IAM | New module |
| `world_manager/handler.py` (+ SPA asset, requirements) | New browser API + static UI | New code |
| `server_controller/ops.py` | Extract shared EC2/RCON/SSM helpers | Refactor of safe-to-modify code |
| `modules/dns/` | *Optional* friendly CNAME for `mc-admin` | Within dns module |
| `main.tf` (root) | Wire `module.webui` | Module composition — **flag & confirm** |
| `variables.tf` (root) | Optional `mc-admin` hostname (token lives in Secrets Manager) | Safe to modify |
| `docs/runbook.md` | UI-based world creation + token setup/rotation; manual as break-glass | Docs — safe |

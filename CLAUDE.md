# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Rialú

Rialú (Irish: "control") is a personal DevOps command centre — a single-user FastAPI + SQLite app that tracks projects, work sessions, cloud deployments, budget, API usage, MCP connector health, and threat intelligence across Todd's project portfolio. Also serves as the backend for the Faire desktop client (Tauri) and exposes an MCP server for Claude tool access. Auth: Cloudflare Access (Google OAuth) for the web SPA, Bearer token for Faire/MCP.

**URL:** `rialu.ie` · **Fly app:** `rialu` · **Region:** `lhr`

## Key Vault

The vault (keys, logins, and the Secrets Wizard) has been fully extracted to **Taisce** (`taisce.irish`) — a standalone encrypted vault app — and **removed from Rialú** (2026-06-03). The `key_store`/`credential_store` tables are dropped on startup via migration 020. Taisce is the sole source of truth for secrets. See `/home/Projects/taisce/CLAUDE.md` for details.

## Commands

```bash
# Run dev server (port 8080)
python main.py

# Run all tests (317 collected across 30 files)
RIALU_TEST=1 python -m pytest tests/ -v

# Run a single test file
RIALU_TEST=1 python -m pytest tests/test_projects.py -v

# Deploy to Fly.io
fly deploy

# Seed database (idempotent)
python seed_config.py

# Compute portfolio divergence flags in-process against the shared DB (no HTTP)
cli/rialu divergence-run [--window-days N]
```

## Architecture

**FastAPI app** (`main.py`) with lifespan that initializes SQLite (`db.py`) and starts APScheduler pollers (`poller.py`).

**Request flow:** Browser → Cloudflare Access → Fly.io → FastAPI → SQLite (WAL mode)

**Routers** (`routers/`): Each maps to an API domain, all mounted under `/api/`:
- `projects.py` — CRUD + milestones + sessions + per-project dashboard + constellation grouping + status refresh
- `commits.py` — Commit activity endpoints (per-project + global) with CSV export, parsed from `[auto-git]` worklog rows
- `divergence.py` — Portfolio divergence digest: the inverse of `milestone_review.py`. Flags *absence* of progress vs. declared status (stale-active, no-trigger). Reads ONLY local `projects`/`worklog` tables, makes NO external calls (so a scheduled run is reliable). Core logic in `run_divergence()`, shared by the HTTP route and the `cli/rialu divergence-run` CLI. Also owns the second, **orthogonal** axis — `narrative-stale` (`projects.narrative_health`): commits landed since the free-text `phase`/`notes` were authored, above `narrative_threshold` (default 15). `health` is status-vs-activity; this is narrative-vs-activity, and a project can be `healthy` and `narrative-stale` at once. See `docs/cc-brief-narrative-staleness.md`
- `worklog.py` — Work sessions with LOC tracking + stats + GitHub LOC refresh
- `deployments.py` — Cached deploy status from Fly.io and Railway pollers
- `budget.py` — Platform costs (EUR) + API registry + billing refresh + cost-by-project
- `usage.py` — Anthropic token usage (CSV import from console.anthropic.com)
- `spend.py` — Suim spend-rollup receiver: `POST /api/spend` upserts per-project Claude spend on `rollup_key` (idempotent, accepts unknown/NULL slugs); `GET /api/spend/summary` exposes recent $/hr vs `projects.cost_limit_hr` (`over_budget` flag — Rialú computes the breach, services enforce). Stored in `project_spend`. Complements (not replaces) `usage.py`. See `docs/suim-spend-rollup-receiver-prd.md`
- `sentinel.py` — Threat intelligence dashboard (proxies Sentinel API + recent events)
- `mcp_status.py` — Health checker for all 4 MCP connectors
- `milestone_review.py` — Automated milestone verification against GitHub repos
- `machines.py` — rialu-agent heartbeats, action queue, WebSocket terminal. `normalise_heartbeat()` is the single shape-reducer shared with `ws_hub.py`: it accepts both the legacy flat payload (`cpu_pct`, scalar `gpu_pct`) and the per-die shape (`cpu:{load_pct,temp_c}`, `gpus:[…]`), and always derives the legacy scalars so Faire renders unchanged during a rolling fleet upgrade. `gpu_pct` = **max** across cards, never mean
- `mnemos.py` — Mnemos memory integration (stats, search, ingest proxy)
- `github.py` — GitHub repo discovery, adoption, and repo creation
- `export.py` — CSV exports (projects, worklog, budget, usage, sentinel)
- `decisions.py` — Faire decision queue (create, respond, list)
- `agents.py` — Faire agent registry and event stream

**Core modules:**
- `auth.py` — Bearer token verification for Faire/MCP clients
- `mcp_server.py` — MCP server at `/mcp` (OAuth 2.1, project tools: list/get/create/update)
- `faire_hub.py` — WebSocket broadcast hub for Faire desktop clients
- `ws_hub.py` — WebSocket hub for rialu-agent connections, plus `/ws/viewer`, a read-only telemetry feed for Teas (same HMAC as the agent socket, snapshot on connect, heartbeats fanned out *before* the SQLite write, every inbound type except `ping` refused)

**Pollers** (`poller.py`):
- Fly.io GraphQL (60s) — app/machine status
- Railway GraphQL (60s) — service/deploy status
- Fly.io billing (1hr) — cost estimation per app
- GitHub LOC (6hr) — commit stats per project
- GitHub repos (6hr) — cache all user repos, detect untracked
- Project status sync (2min) — promotes status based on deploys/commits/milestones (never demotes), updates `runtime` field from deploy cache

The divergence digest is **not** an APScheduler job — it's triggered externally (`scripts/divergence_selfcall.py` POSTs `/api/divergence/run`) so it can run on a weekly cron independent of the app process.

**Frontend:** Single-file vanilla JS SPA (`static/index.html`). Tabs: Projects (cards/list/kanban/timeline views), Work log, Machines, Deployments, Sentinel, Budget & APIs, Mnemos, MCP. 4 themes (dark/light/slate/terminal). Chart.js 4.x from CDN for commit activity graphs. Also serves as backend for Faire (Tauri desktop client) via WebSocket + REST.

**Database:** SQLite WAL mode, foreign keys enforced. Schema managed via idempotent migrations array in `db.py`. Connection via `with db() as conn:` context manager with auto-commit/rollback.

## Key Patterns

- **DB access:** Always use `with db() as conn:` — never open raw connections. `row_to_dict()` converts rows.
- **Dynamic DB_PATH:** `_db_path()` reads `RIALU_DB` env var per-call so tests can override it.
- **Pydantic models:** `*In` for create, `*Update` (with Optional fields) for update.
- **Poller graceful degradation:** Missing API tokens → log warning and skip, never crash.
- **Test isolation:** Each test gets its own SQLite file via `fresh_db` fixture (autouse). `no_scheduler` fixture stubs APScheduler.
- **All costs in EUR.** DB column is still named `cost_gbp` (SQLite rename limitation) but values are EUR.
- **CanonicalHostMiddleware** — `rialu.fly.dev` locked down (421) except health, MCP OAuth, API, and WS paths. When hitting the app from inside the Fly machine, pass `Host: rialu.ie` header.
- **MCP OAuth 2.1** — DCR + PKCE, auto-approve, file-backed state at `/data/oauth_state.json`. Session manager runs in app lifespan. Connector URL: `https://rialu.fly.dev/mcp`

## Fly Secrets Required

`FLY_API_TOKEN`, `RAILWAY_API_TOKEN`, `GITHUB_PAT`, `RIALU_AGENT_KEY`, `SENTINEL_URL`, `SENTINEL_API_KEY`, `MNEMOS_API_KEY`, `FAIRE_WS_TOKEN`, `RIALU_MCP_KEY`

(`RIALU_VAULT_KEY` is no longer used — safe to `fly secrets unset RIALU_VAULT_KEY` after the vault-removal deploy.)

## Current State (2026-06-15)

- **Phase 1-2:** Complete (foundation, pollers, SPA, machine agents)
- **Phase 3:** Complete. Anthropic usage, MCP status, Sentinel (stats + recent events), GitHub LOC, project dashboard, milestone auto-review, budget refresh, Timeline (date-based gantt), Kanban (drag-drop), API cost attribution per project
- **Phase 4:** Complete. Mnemos integration (stats/search/auto-ingest), GitHub repo discovery + adoption + creation, Faire Phase 1 (decisions queue, agents registry, WS broadcast hub, CC stream-json wrapper, event pipeline)
- **Phase 5-6:** Complete. Bearer token auth, HMAC enforcement, FastMCP server at /mcp (project tools), timeline + agent-events API, Faire desktop client support (CORS, WebSocket hub)
- **Phase 7:** In progress. Constellation grouping for projects
- **MCP connector:** Live on Claude.ai at `rialu.fly.dev/mcp` — project tools (list/get/create/update). Vault tools removed 2026-06-03 (migrated to Taisce).
- **Auto status sync:** Projects promote (research→development→deployed→shipped) based on deploy health, git commits, and milestone completion. Never demotes. Separate `runtime` field tracks infrastructure state (running/sleeping/stopped/deploying/error).
- **Commit activity:** Per-project and global commit graphs (Chart.js) with LOC overlay, 30d/90d/1y range, CSV export. Cards layout default with `commits_7d` count.
- **Machine fleet:** rialu-agent runs on **Daisy**, **Iris**, **Lily**, and **Lava** (systemd, WebSocket to `wss://rialu.ie/ws/agent` through Cloudflare Access via a service token).
  - **Lily** has **two dissimilar GPUs** (RTX 5080 + RTX 5070 Ti). It is the reason `gpus` is a list at every layer and `gpu_pct` is a **max** rather than a mean — averaging a 0%-idle card against a busy one hides both. Any code that special-cases "the GPU" is a bug against Lily.
  - **Lava is Todd's laptop**, so it is intermittent **by design** — a stale "last seen" card is normal and is *not* an incident. Do not treat a gap in Lava's heartbeats as a fault, and do not Remove it. It is also the only member running from `/home/todd/dev/rialu` rather than `/home/Projects/rialu` — its unit paths and `repo_dirs` are edited accordingly (see `agent/ADDING-A-MACHINE.md`).
  - Heartbeats report per-die CPU/GPU load and temperature, project processes, and per-repo git state. The auto-git worklog ingested from agent commits **merges by hash across machines** (union, never clobber; minutes = max of each reporter), so multiple machines reporting a shared repo no longer overwrite each other. Down machines render as dimmed "last seen" cards after 5 min; a **Remove** button (`DELETE /api/machines/{name}`, refused while WS-connected) clears retired ones — see the Lava caveat above before using it.
- **Narrative staleness:** `narrative_written_at` records when `phase`/`notes` were last *authored* — written only on a real value change (never on an incidental edit, which is why `updated_at` can't be used). `commits_since_narrative` is computed live from `worklog` and returned by `GET /api/projects`, `/api/divergence/latest`, and the MCP `list_projects` projection; the card annotates it next to the phase text.
- **Per-die telemetry (Teas):** Heartbeats carry one entry per GPU in `gpus[]` (lily has two cards; the old `get_gpu_pct()` returned only index 0) plus CPU temperature in `cpu.temp_c` — `null` on the WSL2 hosts, which must still heartbeat. Stored in `machine_heartbeats.gpus_json` / `cpu_temp_c` (migration 026); no history table, one row per machine. The agent paces itself: 30s normally, **2s while any die is at/above `TEAS_WARN_C`** (default 75), returning to 30s only after 60s clear of a 5°C hysteresis band — a fixed 30s beat measured Teas's 30s dwell alarm from one or two samples. `get_cpu_pct()` is now non-blocking (`interval=None`, primed at startup); the old `interval=1` would have spent half the agent's life blocked at the 2s rate. See `docs/cc-brief-teas-per-die-telemetry.md`
- **CSV exports:** All major data types downloadable from the SPA
- **Security (post-incident 2026-08-21 — full writeup: `docs/incident-2026-08-21-access-bypass.md`):** `rialu.ie` was serving the full app — and an unauthenticated root shell via `/ws/terminal/{machine}` — to the public internet. Two failures compounded: the DNS record was not proxied (so Cloudflare Access was never in the request path), and the app's own "lockdown" only checked the **Host header**, which any direct-to-origin caller sets freely. Three layers now, none of which trusts an unverified header:
  1. `CanonicalHostMiddleware` verifies the **peer address** against Cloudflare's published edge ranges via `Fly-Client-IP` — not `Host`, not `Cf-*`. Direct-to-origin requests get 403. A valid Bearer token is an accepted alternative (the weekly `divergence_selfcall.py` POSTs to the Fly edge and never traverses Cloudflare). Exempt, as before: `/api/health`, `/ws/`, `/mcp`, `/.well-known`, OAuth endpoints
  2. `cf_access.py` verifies the **signed** Cloudflare Access JWT (`Cf-Access-Jwt-Assertion`) — checks `iss`, `aud`, and selects the key by `kid` (never `public_cert`, which breaks on rotation). Team `todd427.cloudflareaccess.com`
  3. `/ws/terminal` and `/ws/pane` authenticate themselves — a verified Access JWT on the upgrade, or the agent's HMAC handshake. **Never reintroduce a dependency on an upstream proxy for these routes**
- **Security:** `rialu.fly.dev` locked down, MCP self-authenticating via OAuth 2.1
- **Tests:** 327 collected across 31 test files (6 pre-existing failures in `test_usage.py`/`test_export.py` — the fixture CSV is dated outside the query window)

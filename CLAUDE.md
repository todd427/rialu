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

# Run all tests (265 collected across 28 files)
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
- `divergence.py` — Portfolio divergence digest: the inverse of `milestone_review.py`. Flags *absence* of progress vs. declared status (stale-active, no-trigger). Reads ONLY local `projects`/`worklog` tables, makes NO external calls (so a scheduled run is reliable). Core logic in `run_divergence()`, shared by the HTTP route and the `cli/rialu divergence-run` CLI
- `worklog.py` — Work sessions with LOC tracking + stats + GitHub LOC refresh
- `deployments.py` — Cached deploy status from Fly.io and Railway pollers
- `budget.py` — Platform costs (EUR) + API registry + billing refresh + cost-by-project
- `usage.py` — Anthropic token usage (CSV import from console.anthropic.com)
- `sentinel.py` — Threat intelligence dashboard (proxies Sentinel API + recent events)
- `mcp_status.py` — Health checker for all 4 MCP connectors
- `milestone_review.py` — Automated milestone verification against GitHub repos
- `machines.py` — rialu-agent heartbeats, action queue, WebSocket terminal
- `mnemos.py` — Mnemos memory integration (stats, search, ingest proxy)
- `github.py` — GitHub repo discovery, adoption, and repo creation
- `export.py` — CSV exports (projects, worklog, budget, usage, sentinel)
- `decisions.py` — Faire decision queue (create, respond, list)
- `agents.py` — Faire agent registry and event stream

**Core modules:**
- `auth.py` — Bearer token verification for Faire/MCP clients
- `mcp_server.py` — MCP server at `/mcp` (OAuth 2.1, project tools: list/get/create/update)
- `faire_hub.py` — WebSocket broadcast hub for Faire desktop clients
- `ws_hub.py` — WebSocket hub for rialu-agent connections

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
- **Machine fleet:** rialu-agent runs on **Daisy** and **Iris** (systemd, WebSocket to `wss://rialu.ie/ws/agent` through Cloudflare Access via a service token). Heartbeats report CPU/RAM/GPU, project processes, and per-repo git state. The auto-git worklog ingested from agent commits **merges by hash across machines** (union, never clobber; minutes = max of each reporter), so multiple machines reporting a shared repo no longer overwrite each other. Down machines render as dimmed "last seen" cards after 5 min; a **Remove** button (`DELETE /api/machines/{name}`, refused while WS-connected) clears retired ones.
- **CSV exports:** All major data types downloadable from the SPA
- **Security:** `rialu.fly.dev` locked down, MCP self-authenticating via OAuth 2.1
- **Tests:** 265 collected across 28 test files

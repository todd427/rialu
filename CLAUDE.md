# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Rialú

Rialú (Irish: "control") is a personal DevOps command centre — a single-user FastAPI + SQLite app that tracks projects, work sessions, cloud deployments, budget, API usage, MCP connector health, and threat intelligence across Todd's project portfolio. Also serves as the backend for the Faire desktop client (Tauri) and exposes an MCP server for Claude tool access. Auth: Cloudflare Access (Google OAuth) for the web SPA, Bearer token for Faire/MCP.

**URL:** `rialu.ie` · **Fly app:** `rialu` · **Region:** `lhr`

## Key Vault

All API keys are stored in the Rialú key vault (AES-256-GCM encrypted with Shamir secret sharing for recovery). **Always check the vault first** before reading `/home/Projects/Keys/`. See `/home/Projects/CLAUDE.md` for vault access commands.

## Commands

```bash
# Run dev server (port 8080)
python main.py

# Run all tests (203 passing)
RIALU_TEST=1 python -m pytest tests/ -v

# Run a single test file
RIALU_TEST=1 python -m pytest tests/test_projects.py -v

# Deploy to Fly.io
fly deploy

# Seed database (idempotent)
python seed_config.py
```

## Architecture

**FastAPI app** (`main.py`) with lifespan that initializes SQLite (`db.py`) and starts APScheduler pollers (`poller.py`).

**Request flow:** Browser → Cloudflare Access → Fly.io → FastAPI → SQLite (WAL mode)

**Routers** (`routers/`): Each maps to an API domain, all mounted under `/api/`:
- `projects.py` — CRUD + milestones + sessions + per-project dashboard + constellation grouping
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
- `keys.py` — Encrypted key vault with audit logging

**Core modules:**
- `auth.py` — Bearer token verification for Faire/MCP clients
- `mcp_server.py` — FastMCP server at `/mcp` (vault + project tools)
- `faire_hub.py` — WebSocket broadcast hub for Faire desktop clients
- `ws_hub.py` — WebSocket hub for rialu-agent connections

**Pollers** (`poller.py`):
- Fly.io GraphQL (60s) — app/machine status
- Railway GraphQL (60s) — service/deploy status
- Fly.io billing (1hr) — cost estimation per app
- GitHub LOC (6hr) — commit stats per project
- GitHub repos (6hr) — cache all user repos, detect untracked

**Frontend:** Single-file vanilla JS SPA (`static/index.html`). Tabs: Projects (list/kanban/timeline views), Work log, Machines, Deployments, Sentinel, Budget & APIs, Mnemos, MCP, Keys. 4 themes (dark/light/slate/terminal). Also serves as backend for Faire (Tauri desktop client) via WebSocket + REST.

**Database:** SQLite WAL mode, foreign keys enforced. Schema managed via idempotent migrations array in `db.py`. Connection via `with db() as conn:` context manager with auto-commit/rollback.

## Key Patterns

- **DB access:** Always use `with db() as conn:` — never open raw connections. `row_to_dict()` converts rows.
- **Dynamic DB_PATH:** `_db_path()` reads `RIALU_DB` env var per-call so tests can override it.
- **Pydantic models:** `*In` for create, `*Update` (with Optional fields) for update.
- **Poller graceful degradation:** Missing API tokens → log warning and skip, never crash.
- **Test isolation:** Each test gets its own SQLite file via `fresh_db` fixture (autouse). `no_scheduler` fixture stubs APScheduler.
- **All costs in EUR.** DB column is still named `cost_gbp` (SQLite rename limitation) but values are EUR.
- **CanonicalHostMiddleware** redirects non-rialu.ie hosts. When hitting the app from inside the Fly machine, pass `Host: rialu.ie` header.

## Fly Secrets Required

`FLY_API_TOKEN`, `RAILWAY_API_TOKEN`, `GITHUB_PAT`, `RIALU_VAULT_KEY`, `RIALU_AGENT_KEY`, `SENTINEL_URL`, `SENTINEL_API_KEY`, `MNEMOS_API_KEY`, `FAIRE_WS_TOKEN`, `RIALU_MCP_KEY`

## Current State (2026-03-24)

- **Phase 1-2:** Complete (foundation, pollers, SPA, machine agents, key vault)
- **Phase 3:** Complete. Anthropic usage, MCP status, Sentinel (stats + recent events), GitHub LOC, project dashboard, milestone auto-review, budget refresh, Timeline (date-based gantt), Kanban (drag-drop), API cost attribution per project
- **Phase 4:** Complete. Mnemos integration (stats/search/auto-ingest), GitHub repo discovery + adoption + creation, Faire Phase 1 (decisions queue, agents registry, WS broadcast hub, CC stream-json wrapper, event pipeline)
- **Phase 5-6:** Complete. Bearer token auth, HMAC enforcement, FastMCP server at /mcp (vault + project tools), timeline + agent-events API, Faire desktop client support (CORS, WebSocket hub)
- **Phase 7:** In progress. Constellation grouping for projects
- **Remaining:** Nothing — all planned phases complete
- **Tests:** 213 passing across 24 test files

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Rialú

Rialú (Irish: "control") is a personal DevOps command centre — a single-user FastAPI + SQLite app that tracks projects, work sessions, cloud deployments, budget, API usage, MCP connector health, and threat intelligence across Todd's project portfolio. Auth is handled externally by Cloudflare Access (Google OAuth); the app itself has no auth layer.

**URL:** `rialu.ie` · **Fly app:** `rialu` · **Region:** `lhr`

## Key Vault

All API keys are stored in the Rialú key vault (AES-256-GCM encrypted with Shamir secret sharing for recovery). **Always check the vault first** before reading `/home/Projects/Keys/`. See `/home/Projects/CLAUDE.md` for vault access commands.

## Commands

```bash
# Run dev server (port 8080)
python main.py

# Run all tests (128 passing)
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
- `projects.py` — CRUD + milestones + sessions + per-project dashboard
- `worklog.py` — Work sessions with LOC tracking + stats + GitHub LOC refresh
- `deployments.py` — Cached deploy status from Fly.io and Railway pollers
- `budget.py` — Platform costs (EUR) + API registry + billing refresh
- `usage.py` — Anthropic token usage (CSV import from console.anthropic.com)
- `sentinel.py` — Threat intelligence dashboard (proxies Sentinel API)
- `mcp_status.py` — Health checker for all 4 MCP connectors
- `milestone_review.py` — Automated milestone verification against GitHub repos
- `machines.py` — rialu-agent heartbeats, action queue, WebSocket terminal
- `mnemos.py` — Mnemos memory integration (stats, search, ingest proxy)
- `github.py` — GitHub repo discovery, adoption, and repo creation
- `keys.py` — Encrypted key vault with audit logging

**Pollers** (`poller.py`):
- Fly.io GraphQL (60s) — app/machine status
- Railway GraphQL (60s) — service/deploy status
- Fly.io billing (1hr) — cost estimation per app
- GitHub LOC (6hr) — commit stats per project
- GitHub repos (6hr) — cache all user repos, detect untracked

**Frontend:** Single-file vanilla JS SPA (`static/index.html`). Tabs: Projects, Work log, Machines, Deployments, Sentinel, Budget & APIs, MCP, Keys. 4 themes (dark/light/slate/terminal).

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

`FLY_API_TOKEN`, `RAILWAY_API_TOKEN`, `GITHUB_PAT`, `RIALU_VAULT_KEY`, `RIALU_AGENT_KEY`, `SENTINEL_URL`, `SENTINEL_API_KEY`, `MNEMOS_API_KEY`

## Current State (2026-03-23)

- **Phase 1-2:** Complete (foundation, pollers, SPA, machine agents, key vault)
- **Phase 3:** Complete. Anthropic usage tracking, MCP status tab, Sentinel dashboard, GitHub LOC poller, project dashboard, milestone auto-review, budget refresh, Timeline view (real date-based gantt), Kanban view (drag-and-drop status changes), API cost attribution per project
- **Phase 4:** Partially complete. Done: Mnemos integration (stats dashboard, search, auto-ingest sessions/milestones), GitHub repo discovery + adoption + creation. Remaining: CSV exports, rialu-agent OAuth 2.1
- **Tests:** 148 passing across 17 test files

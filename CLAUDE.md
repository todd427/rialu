# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Rialú

Rialú (Irish: "control") is a personal DevOps dashboard — a single-user FastAPI + SQLite app that tracks projects, work sessions, cloud deployments, and budget across Todd's side-project portfolio. Auth is handled externally by Cloudflare Access (Google OAuth); the app itself has no auth layer.

## Commands

```bash
# Setup
python -m venv ../venvs/rialu-env && source ../venvs/rialu-env/bin/activate
pip install -r requirements.txt && pip install -r requirements-test.txt

# Run dev server (port 8080)
python main.py

# Seed database (idempotent, safe to re-run)
python seed_config.py

# Run all tests
pytest tests/ -v

# Run a single test file / single test
pytest tests/test_projects.py -v
pytest tests/test_projects.py::test_create_project -v

# Deploy to Fly.io
fly deploy
```

## Architecture

**FastAPI app** (`main.py`) with lifespan that initializes SQLite (`db.py`) and starts APScheduler pollers (`poller.py`). Background pollers hit Fly.io and Railway GraphQL APIs every 60s to cache deployment status.

**Request flow:** Browser → Cloudflare Access → Fly.io → FastAPI → SQLite (WAL mode)

**Routers** (`routers/`): Each maps to an API domain — `projects`, `worklog`, `deployments`, `budget`, `machines` (Phase 2 stub). All mounted under `/api/`.

**Frontend:** Single-file vanilla JS SPA (`static/index.html`, ~700 lines). Served at root with catch-all fallback for SPA routing. Supports 4 themes via CSS custom properties.

**Database:** SQLite with WAL mode and foreign keys enforced. 12 tables across project data, cost tracking, deployment cache, and machine agents. All schema managed via idempotent migrations array in `db.py`. Connection via context manager (`with db() as conn`) with auto-commit/rollback.

## Key Patterns

- **DB access:** Always use `with db() as conn:` — never open raw connections. `row_to_dict()` converts rows.
- **Dynamic DB_PATH:** `_db_path()` reads `RIALU_DB` env var per-call so tests can override it.
- **Pydantic models:** `*In` for create, `*Update` (with Optional fields) for update. Dynamic SET clauses filter out None values.
- **Poller graceful degradation:** If API tokens are missing, pollers log a warning and skip (no crash).
- **Test isolation:** Each test gets its own SQLite file via `fresh_db` fixture (autouse). `no_scheduler` fixture stubs APScheduler. Tests use FastAPI's `TestClient`.
- **Seeding:** `INSERT OR IGNORE` / `INSERT OR REPLACE` — always idempotent.

## Build Phases

Phase 1 (done): FastAPI + SQLite + pollers + SPA + tests (44 tests across 5 modules).
Phase 2–4 (not started): rialu-agent daemon, Anthropic poller, timeline/kanban views, exports, Mnemos integration.

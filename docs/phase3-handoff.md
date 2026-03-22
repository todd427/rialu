# Rialú Phase 3 — Handoff Context

**For Claude Code — read this before touching anything.**

---

## What Rialú is

Personal command centre for Todd McCaffrey / FoxxeLabs. Single user. Runs at `rialu.ie` on Fly.io. FastAPI + SQLite + APScheduler + vanilla JS SPA.

Full PRD: `docs/rialu-prd-v1.md` — read it.
Phase 2 handoff: `docs/phase2-handoff.md`

---

## Current state

Phase 1: FastAPI skeleton, all routers, SQLite schema, SPA, tests. ✓  
Phase 2: Machine agents (heartbeat receiver, rialu-agent daemon, machines tab). ✓  
Phase 3: Three dashboard features — all complete. ✓

---

## Phase 3 — what was built

### 1. Timeline view (Projects tab)

Real date-based Gantt chart using `created_at` / `updated_at` from each project row.

- Dynamic month headers: computed from the actual date range of project data, not hardcoded
- Running / deployed projects extend their bar to today
- Replaces the previous pseudo-random bar positioning

### 2. Kanban view (Projects tab)

6 columns: **research → development → running → deployed → paused → shipped**

- HTML5 drag-and-drop — drag a card between columns
- On drop: `PUT /api/projects/{id}` with updated status
- Visual drag-over feedback on columns
- Toggle between Kanban and Timeline via view switcher in the Projects tab header

### 3. API cost attribution per project (Budget tab)

New endpoint: `GET /api/apis/costs-by-project?days=30`

- Aggregates `api_usage` table by `project_id`
- Includes an **Unattributed** row for usage rows without a `project_id`
- New "API costs by project" table rendered in the Budget tab
- 2 new tests: empty state and populated state

### Test suite

130 passing (was 128 before Phase 3).

---

## Architecture notes (unchanged)

- FastAPI + SQLite + APScheduler
- Vanilla JS SPA in `static/index.html` — no framework, no bundler
- Fly.io deployment (`fly.toml` + `Dockerfile` — do not touch)
- Cloudflare Access for browser auth; HMAC-SHA256 for agent auth
- All routers in `routers/` — add new ones there, don't modify existing ones unless fixing bugs

---

## What NOT to change

- `fly.toml` — do not touch
- `Dockerfile` — do not touch
- `docs/rialu-prd-v1.md` — do not touch
- Existing HMAC auth pattern
- Existing test fixtures in `conftest.py`

---

## Known issues / things to watch

- `tioniol.irish` has invalid nameservers in Cloudflare — unrelated to Rialú but worth noting
- No CI pipeline — tests run locally with `pytest`

---

## Next candidates (Phase 4 ideas)

- Action buttons on machine cards (send command to agent via `POST /api/agent/action`)
- Worklog timer integration in the SPA (start/stop billable session from UI)
- Notifications: alert when a machine goes offline or a deployment fails
- Export budget data as CSV

---

*Context written by Claude (claude.ai) — 2026-03-22*

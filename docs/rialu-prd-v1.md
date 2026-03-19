# Rialú — Product Requirements Document

**Version:** 1.0  
**Date:** 2026-03-19  
**Author:** Todd McCaffrey / FoxxeLabs  
**Status:** Draft

---

## Name

**Rialú** *(Irish: RIAL-oo)*

The Irish word for *control*, *regulation*, *governance*. More precise than *faire* (watch/vigil) — Rialú is not passive observation, it's active control. The tool lets you govern projects, machines, and deployments from a single interface.

**Domain:** `rialu.ie`  
**Deployment target:** `rialu.ie` (Cloudflare DNS → Fly.io)  
**Repo:** `todd427/rialu`  
**Demo wireframe:** `foxxelabs.ie/ops-dashboard/`

---

## Vision

Rialú is a personal command centre for a solo developer running interconnected projects across multiple physical machines and cloud platforms. It replaces the cognitive overhead of context-switching between Railway dashboards, Fly.io logs, terminal windows, and scattered notes with a single, always-available interface that shows the current state of everything and enables corrective action without leaving the browser.

The primary view is **Projects** — not deployments, not system metrics. Rialú is project-first because the developer's mental model is project-first. Infrastructure visibility exists to support projects, not the other way around.

---

## Problem

A typical working session involves:

- 4+ machines (Rose, Iris, Daisy, Lava), each potentially running different local processes
- 7+ cloud services across Fly.io, Railway, Cloudflare Pages
- 10+ active or developing projects in various states
- Multiple AI API costs accumulating with no unified view
- Work sessions that are never logged, making time and effort invisible
- No single place to record "what am I doing, what's next, what's running where"

The current answer is a constellation of terminal windows, browser tabs, and memory. Rialú replaces that.

---

## User

Single user: Todd McCaffrey, FoxxeLabs. No multi-tenancy required. No public access. Auth is Cloudflare Access (Google OAuth, zero app code).

---

## Core Concepts

### Projects
A project is any named unit of work — deployed service, research effort, local experiment, writing project, or tool in development. Each project has:
- A status (research / development / running / deployed / paused / shipped)
- A machine or platform it lives on (rose / iris / daisy / fly.io / railway / cf pages)
- A set of milestones with completion state
- A free-form notes field (last session context)
- A link to its git repo
- A log of work sessions

### Machine agents (rialu-agent)
A lightweight Python daemon running on each local machine (Rose, Iris, Daisy, optionally Lava). Each agent:
- Heartbeats every 30s to the Rialú hub with: process list (filtered to known project names), git repo states for `/home/Projects/`, and resource stats (CPU / RAM / GPU)
- Exposes a local HTTP endpoint (never public) — the hub is the only caller, authenticated via shared HMAC-SHA256 key
- Accepts action commands proxied from the hub: `git pull`, restart/kill a process, run a named script

### Cloud poller
An APScheduler background job in the hub that polls:
- Fly.io API — app status, last deploy timestamp (60s interval)
- Railway GraphQL — service status, deploy logs (60s)
- Cloudflare Pages API — build status (5m)
- GitHub API — last commit per repo (5m)
- Anthropic usage API — token usage, cost (daily)

### Work log
A simple session log: project, type (code / debug / deploy / research / writing / design / meeting), duration in minutes, free-form notes. Provides rolling stats (this week's minutes, streak, top project). Sessions can be opened directly from the expanded project detail row (pre-populates project field).

### Budget & API registry
Two-level cost tracking:
1. **Platform costs** — fixed monthly charges per service (Fly.io, Railway, Cloudflare, domains)
2. **API costs** — per-token or per-call charges (Anthropic primarily), polled daily, attributed to projects via a project map and session estimates

---

## Features

### Tab 1 — Projects (primary)

**List view (default)**
- Expandable rows. Click to expand in-place — chevron rotates, border highlights in accent colour, detail block slides open beneath the row.
- Each expanded row shows: description, location (machine + path + branch), repo URL, milestones (with checkbox completion state), last session note, per-project action buttons.
- Action buttons per project: Log session, Add milestone, View on [machine] (links to Machines tab filtered), Deploy now (if cloud-deployed), View logs.
- "Pause project" action (destructive, shown separately in warn colour).
- Add new project: inline form drops above the list in accent-coloured block. Fields: name, status, machine/platform, repo URL, current phase/milestone, notes. Cancel restores cleanly.

**Kanban view**
- Four columns: Research / Development / Running / Deployed
- Cards show project name and brief meta (machine, last note snippet)
- Column counts shown in header

**Timeline view**
- Gantt-style horizontal bars, Jan–Jun 2026 window (configurable)
- One row per project
- Bar colour matches project status (warn for time-pressured, ok for deployed, purple for local running, blue for in-development)
- Bar label shows phase or key milestone

### Tab 2 — Work log

- Summary metric cards: minutes this week, session count last 7 days, top project, streak
- Log session inline form (accent green): project (dropdown), type (dropdown), duration (minutes), notes (text). Opens above the log table, collapses on save or cancel.
- Session table: date, project (colour-coded pill), type, duration, notes
- Log session from within an expanded project row pre-populates the project field

### Tab 3 — Machines

- Machine cards in a grid: one per machine (Rose, Iris, Daisy, Lava)
- Each card: machine name, spec line, last heartbeat pill (ok/warn/err by age), resource bars (CPU/RAM/GPU), active process list (project tag + script name + PID + uptime), action buttons (git pull all, repo status, project-specific restart/kill)
- Offline machine: card dimmed, last-seen timestamp
- Repo state table below cards: machine, repo name, branch, clean/dirty status, ahead/behind count, last commit hash + message
- Actions confirm before executing (single confirm step, not a full dialog)

### Tab 4 — Deployments

- Cloud service cards: platform pill, last deploy age, health pill
- Click a card to expand in-place: shows last 5 deploy events, last commit, deploy duration, direct link to service URL
- Recent deploys table: when, service, result (success/warn/err pill), commit message, duration
- Manual refresh button (triggers immediate poll, not cached result)

### Tab 5 — Budget & APIs

- Metric cards: Fly.io/month, Railway/month, Cloudflare, Anthropic 30d, total all-in
- API registry table: API name, provider, billing model, estimated monthly cost, project count, poll frequency, status
- API cost attribution: per-project breakdown populated when Anthropic usage API integration is active; shows manual estimates with "manual" badge until then
- Inline form for adding/editing API registry entries and platform costs

---

## Data Model

### SQLite tables

```sql
projects
  id, name, slug, phase, status, notes, repo_url, machine, platform, created_at, updated_at

milestones
  id, project_id, title, due_date, done, sort_order, created_at

sessions  -- work sessions, not machine sessions
  id, project_id, session_type, notes, started_at, ended_at, duration_minutes

worklog
  id, project_id, date, minutes, session_type, notes, created_at

budget
  id, platform, service_name, cost_gbp, period, active, notes, created_at

api_registry
  id, name, provider, auth_key_ref, billing_model, cost_unit,
  cost_per_unit_gbp, billing_url, usage_api_endpoint, notes, active, created_at

api_project_map
  id, api_id, project_id, usage_description, is_primary

api_usage
  id, api_id, project_id, period_start, period_end,
  tokens_in, tokens_out, call_count, cost_gbp, source,
  raw_response, recorded_at

deployments_cache
  id, platform, service_name, status, last_deploy_at, url,
  last_commit_hash, last_commit_message, deploy_duration_s, checked_at

machine_heartbeats
  id, machine_name, cpu_pct, ram_pct, gpu_pct,
  processes_json, repos_json, received_at

agent_actions
  id, machine_name, action_type, payload, status, result, created_at
```

---

## Architecture

```
Browser (any machine, any location)
  │
  └── Cloudflare Access (Google OAuth — DNS layer, zero app code)
        │
        └── rialu.ie  [Fly.io]
              │
              ├── FastAPI application (main.py)
              │     ├── /api/projects, /api/milestones, /api/sessions, /api/worklog
              │     ├── /api/deployments  (cached from poller)
              │     ├── /api/machines     (cached from agent heartbeats)
              │     ├── /api/budget, /api/apis
              │     └── /static/index.html  (single-file SPA)
              │
              ├── APScheduler background poller
              │     ├── Fly.io API        (60s)
              │     ├── Railway GraphQL   (60s)
              │     ├── Cloudflare API    (5m)
              │     ├── GitHub API        (5m)
              │     └── Anthropic usage   (daily)
              │
              ├── SQLite (persistent volume)
              └── rialu-agent receiver  (/api/agent/heartbeat, /api/agent/result)
                    │
        ┌───────────┼───────────┐
        │           │           │
  Rose agent   Iris agent  Daisy agent
  (30s HB)     (30s HB)    (30s HB)
```

---

## Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Hub runtime | Python 3.11 | Consistent with rest of stack |
| Web framework | FastAPI | Consistent with Mnemos, git-mcp |
| Database | SQLite (WAL mode) | Zero ops, sufficient for single user |
| Background jobs | APScheduler | Already in Mnemos |
| Frontend | Vanilla JS, single `index.html` | No build step, no TypeScript errors blocking deploy |
| Deploy | Fly.io | Consistent with Mnemos |
| Auth | Cloudflare Access | Zero app code, sits at DNS, Google OAuth |
| Agent | Python 3.11 + FastMCP or plain uvicorn | Lightweight, same pattern as other local servers |
| Agent auth | HMAC-SHA256 shared secret | Simple, no external dependency |

---

## Authentication

Cloudflare Access is the only auth layer. The FastAPI app trusts the `Cf-Access-Authenticated-User-Email` header injected by Cloudflare. No session management, no tokens, no magic links.

Setup (one-time, ~10 minutes in Cloudflare dashboard):
1. Create a Cloudflare Access application for `rialu.ie`
2. Add a Google OAuth identity provider
3. Create a policy: allow email = `todd@foxxelabs.ie`
4. Done — all other requests are blocked at the network layer

---

## Build Phases

### Phase 1 — Foundation
- FastAPI skeleton: `main.py`, `db.py`, `poller.py`
- SQLite schema migrations (all tables)
- Deployments tab: Fly.io + Railway poller, service card grid, recent deploys table
- Projects tab: list view, expand-in-place, add project form
- Work log tab: metric cards, inline log form, session table
- Budget tab: metric cards, API registry table (manual data only)
- Cloudflare Access integration
- `fly.toml`, deploy to `rialu.ie`
- Test suite: unit tests for db layer, integration tests for API endpoints

### Phase 2 — Machine agents
- `rialu-agent`: Python daemon, heartbeat endpoint, process filtering, git status, resource stats
- Agent receiver on hub: `POST /api/agent/heartbeat`
- Machines tab: card grid, resource bars, process list, repo state table
- Action proxy: hub → agent for `git pull`, restart, kill
- Action confirm step (single click confirm, no modal)
- `systemd` unit file and `screen` fallback instructions for each machine

### Phase 3 — Intelligence layer
- Anthropic usage API poller (daily)
- API cost attribution per project via session token estimates
- Budget tab: per-project breakdown, manual → auto transition
- Timeline view in Projects tab
- Kanban view in Projects tab
- Milestone due-date alerts (banner in Projects tab when something is overdue)
- Cloudflare Pages + GitHub pollers

### Phase 4 — Polish
- Confirm dialogs for destructive actions (pause project, kill process)
- Export: work log CSV, budget CSV
- Mnemos integration: session summaries auto-ingested on save
- `rialu-agent` upgrade: OAuth 2.1 (same pattern as `mnemos/server/oauth_provider.py`)

---

## Non-Goals

- Multi-user support
- Mobile-native app
- Real-time streaming (websockets) — polling is sufficient
- Kubernetes or container orchestration
- External alerting (email/Slack notifications) — Phase 1
- Public access or white-labelling

---

## File Structure

```
rialu/
├── fly.toml
├── requirements.txt
├── requirements-test.txt
├── seed_config.py
├── main.py
├── db.py
├── poller.py
├── routers/
│   ├── projects.py
│   ├── worklog.py
│   ├── deployments.py
│   ├── machines.py
│   └── budget.py
├── agent/
│   ├── rialu-agent.py
│   ├── rialu-agent.service
│   └── README.md
├── static/
│   └── index.html
└── tests/
    ├── test_db.py
    ├── test_projects.py
    ├── test_worklog.py
    ├── test_deployments.py
    └── test_agent.py
```

---

## Open Questions

1. **Agent transport:** Plain `uvicorn` HTTP or FastMCP StreamableHTTP? FastMCP adds MCP accessibility (Claude could query machine state directly) but adds complexity. Lean FastMCP given existing pattern.
2. **Theme persistence:** `localStorage` under key `rialu-theme`, dark default. Four themes: dark, light, slate, terminal.
3. **GitHub access from agent:** Agent on each machine has git CLI access — `git status`, `git log`, `git pull` run locally. No GitHub API token needed for basic repo state.
4. **Lava (laptop):** Intermittent availability. Agent should fail gracefully — card shows "offline" with last-seen timestamp. No error state on hub side.
5. **Anseo ops inclusion:** Project entries are not 1:1 with deployments — one project can have both a Railway deployment and a local dev checkout entry.

---

## Wireframe

Interactive wireframe: `foxxelabs.ie/ops-dashboard/`  
Source: `todd427/foxxelabs-astro/public/ops-dashboard/index.html`

---

*Rialú PRD v1.0 — FoxxeLabs — 2026-03-19*

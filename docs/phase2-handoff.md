# Rialú Phase 2 — Agent Handoff Context

**For Claude Code — read this before touching anything.**

---

## What Rialú is

Personal command centre for Todd McCaffrey / FoxxeLabs. Single user. Runs at `rialu.ie` on Fly.io. FastAPI + SQLite + APScheduler + vanilla JS SPA.

Full PRD: `docs/rialu-prd-v1.md` — read it.

---

## Current state

Phase 1 is complete and committed:
- FastAPI skeleton: `main.py`, `db.py`, `poller.py`
- All routers: `routers/projects.py`, `worklog.py`, `deployments.py`, `machines.py`, `budget.py`
- SQLite schema with all tables
- SPA: `static/index.html`
- Test suite passing: `tests/`
- Dockerfile + fly.toml present
- Not yet deployed to Fly

Phase 2 (your job) is the **machine agents**.

---

## Phase 2 — what to build

### 1. `agent/rialu-agent.py`

A lightweight Python daemon that runs on each local machine (Rose, Iris, Daisy).

Every 30 seconds it POSTs to the Rialú hub at `https://rialu.ie/api/agent/heartbeat` with:

```json
{
  "machine": "daisy",
  "cpu_pct": 12.4,
  "ram_pct": 34.1,
  "gpu_pct": 0.0,
  "processes": [
    {"name": "sceal", "script": "train.py", "pid": 12345, "uptime_s": 3600}
  ],
  "repos": [
    {
      "name": "mnemos",
      "path": "/home/Projects/mnemos",
      "branch": "master",
      "clean": true,
      "ahead": 0,
      "behind": 0,
      "last_commit": "abc1234",
      "last_message": "Fix date index"
    }
  ]
}
```

Auth: HMAC-SHA256. The agent signs the JSON body with a shared secret (`RIALU_AGENT_KEY` env var). The hub verifies the signature before accepting.

HMAC header: `X-Rialu-Sig: sha256=<hex>`

Process filtering: only report processes whose name matches a known project name (read from a local config file `~/.rialu-agent.json` or `/etc/rialu-agent.json`). Don't dump the entire process table.

Repos: scan `/home/Projects/` for git repos. Run `git status --porcelain`, `git log -1 --format="%H %s"`, `git rev-list HEAD...origin/HEAD --count` for ahead/behind. Handle failures gracefully — a repo with no remote is fine, just report what you can.

Resources: use `psutil` for CPU/RAM. For GPU use `nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits` via subprocess — if it fails (no GPU or no nvidia-smi), report `gpu_pct: null`.

### 2. `agent/rialu-agent.service`

systemd unit file for running the agent as a service. Must:
- Start after network
- Restart on failure (5s delay)
- Run as the `todd` user
- Load env from `/etc/rialu-agent.env`
- Point at the correct python path (assume venv at `~/agentEnv`)

Also include a `screen` fallback one-liner in a comment for machines without systemd.

### 3. Hub receiver — `routers/machines.py`

The machines router is already scaffolded. Add:

```
POST /api/agent/heartbeat  — receive + verify heartbeat, upsert machine_heartbeats table
POST /api/agent/result     — receive action result, update agent_actions table
POST /api/agent/action     — proxy an action to a machine (future — stub it for now)
```

HMAC verification must happen in a dependency, not inline. Reject with 401 if signature missing or invalid.

The `machine_heartbeats` table already exists in the schema (see `db.py`). Upsert by machine name — one row per machine, updated on each heartbeat.

### 4. Machines tab in `static/index.html`

The SPA already has a Machines tab placeholder. Wire it up:

- Grid of machine cards (one per machine seen in heartbeats)
- Each card: machine name, last heartbeat age (green < 60s, amber < 5m, red > 5m)
- Resource bars: CPU / RAM / GPU (skip GPU bar if null)
- Process list: project name, script, PID, uptime formatted as "2h 34m"
- Repo table below cards: repo name, branch, clean/dirty pill, ahead/behind count, last commit message (truncated to 60 chars)
- Offline card: dimmed, shows "last seen X minutes ago"

No action buttons in Phase 2 — those are Phase 3.

---

## Machines and paths

| Machine | OS | Projects path | GPU |
|---------|-----|--------------|-----|
| Daisy | Ubuntu native | `/home/Projects/` | RTX 5060 Ti 16GB |
| Rose | Windows 11 + WSL2 | `/home/Projects/` | RTX 5070 12GB |
| Iris | Ubuntu | `/home/Projects/` | RTX 5080 |
| Lava | Ubuntu (laptop, intermittent) | `/home/Projects/` | none |

Start with Daisy. It's the primary development machine.

---

## Auth pattern

Same HMAC pattern used elsewhere in the stack. Example verification:

```python
import hashlib
import hmac
import os

AGENT_KEY = os.environ.get("RIALU_AGENT_KEY", "").encode()

def verify_hmac(body: bytes, sig_header: str) -> bool:
    if not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(AGENT_KEY, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)
```

---

## Env vars needed

Hub (Fly secrets):
- `RIALU_AGENT_KEY` — shared secret for HMAC verification

Agent (local `/etc/rialu-agent.env`):
- `RIALU_HUB_URL=https://rialu.ie`
- `RIALU_AGENT_KEY=<same secret>`
- `RIALU_MACHINE_NAME=daisy`

---

## Dependencies to add to requirements.txt

```
psutil>=5.9.0
```

The agent itself can use only stdlib + psutil + requests (already in requirements.txt).

---

## Tests to write

- `tests/test_agent.py` — test HMAC verification, heartbeat upsert, malformed payload rejection
- Test the machines router endpoints directly (same pattern as existing tests)
- Use `fresh_db` fixture from `conftest.py`

---

## What NOT to change

- `fly.toml` — do not touch
- `Dockerfile` — do not touch
- `docs/rialu-prd-v1.md` — do not touch
- Existing router logic — add to machines.py, don't rewrite others
- Auth model — Cloudflare Access handles browser auth, HMAC handles agent auth. No changes.

---

## When done

Commit with message: `Phase 2: machine agents — heartbeat receiver, rialu-agent daemon, machines tab`

Then tell Todd: ready to deploy + install agent on Daisy.

---

*Context written by Claude (claude.ai) — 2026-03-20*

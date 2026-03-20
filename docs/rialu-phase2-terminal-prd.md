# Rialú Phase 2 — Browser Terminal & Claude Code Monitor
## Product Requirements Document v1.0

**Date:** 2026-03-20
**Author:** Todd McCaffrey / FoxxeLabs
**Status:** Approved — ready for implementation
**Repo:** `todd427/rialu`
**Depends on:** Phase 1 complete (rialu.fly.dev live, 42/42 tests passing)

---

## Goal

Turn Rialú from a passive monitor into an active remote operations hub. The architect (Todd) should be able to:

1. Open a live terminal on any connected machine (Rose, Iris, Daisy) directly from the browser
2. See all running Claude Code sessions across all machines in real time
3. Read Claude Code output as it streams — including pause/question prompts
4. Respond to Claude Code from the browser (type answers, press Enter, approve/reject)
5. Do all of this simultaneously across multiple machines — running 10+ projects in parallel from a single browser tab

---

## Background

The current Phase 1 Machines tab is a stub. Phase 2 makes it real. The long-term use case is Todd acting purely as architect — Claude Code instances run autonomously on Rose, Iris, and Daisy; Todd monitors from Rialú, intervenes when Claude pauses, approves decisions, and redirects work without ever opening a terminal locally.

---

## Architecture

### Overview

```
Browser (rialu.ie, Cloudflare Access auth)
  │
  └── xterm.js WebSocket connection
        │
        └── Rialú hub (Fly.io) — WebSocket proxy
              │  (hub never initiates outbound — agent calls home)
              │
              └── rialu-agent on Rose / Iris / Daisy
                    │
                    ├── /ws/terminal        — spawn shell, pipe to browser
                    ├── /ws/pane/{pane_id}  — attach to existing tmux pane
                    ├── GET /tmux           — enumerate sessions + panes
                    └── POST /send          — inject keystrokes into pane
```

### Key constraint: reverse tunnel

The hub lives on Fly.io. Machines are on a home/lab network behind NAT. The **agent initiates** a persistent outbound WebSocket to the hub on startup and keeps it alive. The hub routes browser terminal traffic through this pipe. The machine never needs an open inbound port. This is the same pattern used by Cloudflare Tunnel, ngrok, and Tailscale.

---

## Components

---

### 1. rialu-agent (new — `agent/rialu-agent.py`)

A Python daemon running on each local machine (Rose, Iris, Daisy). Replaces the Phase 1 stub.

**Responsibilities:**
- Maintain a persistent authenticated WebSocket connection to the hub
- Heartbeat: send system stats (CPU, RAM, GPU) every 30 seconds
- Terminal sessions: spawn a shell (`/bin/bash`) and pipe stdin/stdout/stderr over WebSocket
- tmux: enumerate sessions and panes, stream pane output, inject keystrokes
- Git: run `git status`, `git log`, `git pull` on repos under `/home/Projects/`
- Process list: `ps aux` filtered to known project names

**Auth:** HMAC-SHA256. Every message includes a timestamp and HMAC signature computed with `RIALU_AGENT_SECRET`. The hub verifies before processing.

**Startup:** `systemd` unit file provided. Screen fallback documented.

**Dependencies:** Python 3.11, `websockets`, `psutil`, standard library only. No GPU libraries required — GPU stats via `nvidia-smi` subprocess.

---

### 2. Hub — WebSocket proxy (additions to `main.py` / new `routers/machines.py`)

**New endpoints:**

```
GET  /api/machines                      — list machines + last heartbeat + stats
GET  /api/machines/{machine}/tmux       — list tmux sessions + panes for machine
WS   /ws/machines/{machine}/terminal    — open shell terminal on machine
WS   /ws/machines/{machine}/pane/{id}   — attach to existing tmux pane
POST /api/machines/{machine}/send       — inject keystrokes into a pane
POST /api/machines/{machine}/action     — git pull, restart process, kill process
```

**Internal routing:** Hub maintains a registry of connected agents keyed by `machine_name`. When a browser opens `/ws/machines/rose/terminal`, the hub creates a sub-channel on Rose's existing agent WebSocket and bridges the two streams.

**Message protocol (JSON envelope):**

```json
{
  "type": "terminal_data | pane_data | heartbeat | tmux_list | send_keys | action",
  "machine": "rose",
  "channel": "uuid",
  "payload": "...",
  "ts": 1742428800,
  "hmac": "sha256hex"
}
```

---

### 3. Claude Code awareness

The agent monitors tmux panes for Claude Code activity.

**Detection:** A pane is flagged as a Claude Code session if:
- The process running in it matches `claude` or `claude-code`
- OR the pane output contains Claude Code's signature prompts

**Claude Code pause patterns to detect:**
```
"Do you want to proceed?"
"Press Enter to confirm"
"Press Escape to cancel"
"▌"  (Claude's thinking cursor, blinking block)
"Continue? [Y/n]"
"Allow"  (tool approval prompt)
```

When a pause pattern is detected, the agent sends a `claude_waiting` event to the hub, which broadcasts it to all subscribed browser clients.

**Pane metadata sent with each update:**
```json
{
  "pane_id": "rose:0:1",
  "session": "main",
  "window": 0,
  "pane": 1,
  "pid": 12345,
  "is_claude": true,
  "claude_state": "running | waiting | idle",
  "last_lines": ["...last 20 lines of pane output..."],
  "waiting_prompt": "Do you want to proceed?"
}
```

---

### 4. SPA — Machines tab (additions to `static/index.html`)

Phase 1 Machines tab showed a "Phase 2 placeholder" message. Replace entirely.

#### 4a. Machine cards (always visible)
- Machine name, spec line
- Status pill: online (green) / offline (gray) with last-seen time
- CPU / RAM / GPU resource bars (updated from heartbeat)
- Connection indicator: agent connected / disconnected

#### 4b. Process list per machine
- All processes matching known project names
- PID, uptime, project tag
- Quick-kill button (single confirm step)

#### 4c. tmux pane browser
- List all tmux sessions and panes per machine
- Each pane shows: window name, pane index, running command, last line of output
- Click any pane → opens full xterm.js terminal attached to that pane

#### 4d. Claude Code panel (the key feature)
A dedicated section on the Machines tab showing **all active Claude Code panes across all machines**:

```
┌─────────────────────────────────────────────────────────┐
│  Claude Code Sessions                                    │
├────────────┬─────────────┬────────────┬─────────────────┤
│ Machine    │ Project     │ State      │ Last output      │
├────────────┼─────────────┼────────────┼─────────────────┤
│ Rose       │ rialu       │ ● WAITING  │ "Do you want to │
│            │             │            │  proceed?"       │
│            │             │ [Yes] [No] │ [Open terminal]  │
├────────────┼─────────────┼────────────┼─────────────────┤
│ Daisy      │ sceal       │ ○ running  │ "Rendering seg   │
│            │             │            │  042/219..."     │
│            │             │            │ [Open terminal]  │
├────────────┼─────────────┼────────────┼─────────────────┤
│ Iris       │ legion      │ ○ running  │ "Phase 2: agent  │
│            │             │            │  handshake..."   │
│            │             │            │ [Open terminal]  │
└────────────┴─────────────┴────────────┴─────────────────┘
```

**WAITING state** (Claude paused):
- Row highlighted in amber
- Prompt text shown in full
- Quick-action buttons: **Yes** / **No** / **Enter** / **Escape** (inject directly, no terminal needed)
- **Open terminal** button → full xterm.js panel for that pane

**Running state:**
- Last line of output updates every 2 seconds
- **Open terminal** button available

#### 4e. Full terminal panel (xterm.js)
When "Open terminal" is clicked for any pane or machine:
- Full-screen overlay (or side-panel) with xterm.js
- Connected to that machine's shell or pane via WebSocket
- Standard terminal: keyboard input, ANSI colour, resize
- Close button returns to Machines overview
- Multiple terminals can be open simultaneously (tabbed)

#### 4f. Repo state table (from Phase 1 spec, now wired to real data)
- Machine, repo name, branch, clean/dirty, ahead/behind, last commit
- "Git pull" button per repo (runs via action proxy, shows output in mini terminal)

---

## File structure additions

```
rialu/
├── agent/
│   ├── rialu-agent.py       — main daemon
│   ├── rialu-agent.service  — systemd unit file
│   └── README.md            — install + config per machine
├── routers/
│   └── machines.py          — replace stub with full implementation
└── static/
    └── index.html           — Machines tab fully implemented
```

---

## Data models (SQLite additions)

```sql
-- Already exists:
machine_heartbeats (id, machine_name, cpu_pct, ram_pct, gpu_pct,
                    processes_json, repos_json, received_at)

agent_actions (id, machine_name, action_type, payload,
               status, result, created_at)

-- New:
terminal_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_name    TEXT NOT NULL,
    channel_id      TEXT NOT NULL UNIQUE,  -- UUID for routing
    pane_id         TEXT,                  -- tmux pane ref, null = fresh shell
    opened_at       TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at       TEXT
)
```

No terminal content is persisted — output streams in real time and is not stored.

---

## Security

- **Auth:** Cloudflare Access handles browser auth (Google OAuth, `todd@foxxelabs.ie` only)
- **Agent auth:** HMAC-SHA256 with `RIALU_AGENT_SECRET` (set via `fly secrets set`)
- **Terminal scope:** Agent only spawns shells as the user running the agent process (Todd's user). No privilege escalation.
- **Action allowlist:** The action proxy only accepts a defined set of operations: `git_pull`, `process_restart`, `process_kill`, `tmux_send_keys`. No arbitrary shell execution via the action endpoint — that's what the terminal is for.
- **TLS:** All WebSocket connections are WSS (Fly.io provides TLS termination, Cloudflare provides TLS to the user)

---

## Environment variables

**On each machine (agent):**
```
RIALU_HUB_URL=wss://rialu.fly.dev/ws/agent
RIALU_AGENT_SECRET=<shared secret, same as hub>
RIALU_MACHINE_NAME=rose  # or iris, daisy, lava
RIALU_PROJECTS_DIR=/home/Projects
```

**On Fly.io (hub):**
```
RIALU_AGENT_SECRET=<same shared secret>
```

Set via: `fly secrets set RIALU_AGENT_SECRET=$(openssl rand -hex 32) -a rialu`
Capture the value first: `S=$(openssl rand -hex 32) && echo $S && fly secrets set RIALU_AGENT_SECRET=$S -a rialu`

---

## Build order

Implement in this order — each step is independently testable:

1. **Agent heartbeat** — agent connects to hub, sends stats every 30s, hub stores in `machine_heartbeats`, Machines tab shows live resource bars. (Replaces Phase 1 stub entirely.)

2. **tmux enumeration** — agent lists tmux sessions/panes, hub exposes via `GET /api/machines/{machine}/tmux`, SPA shows pane browser.

3. **Claude Code detection** — agent watches pane output for pause patterns, sends `claude_waiting` events, SPA highlights waiting sessions with quick-action buttons.

4. **Quick actions** — Yes/No/Enter/Escape buttons inject keystrokes via `POST /api/machines/{machine}/send` → `tmux send-keys`. No terminal needed for basic Claude Code approval flow.

5. **Full terminal** — xterm.js panel, WebSocket shell, full keyboard + colour support. The power tool for when quick actions aren't enough.

6. **Repo state table** — `git status` / `git log` per repo, git pull via action proxy.

7. **systemd unit files** — install instructions for Rose, Iris, Daisy.

---

## xterm.js integration notes

- CDN: `https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js` + CSS
- FitAddon for auto-resize: `https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js`
- WebSocket message format: raw bytes (terminal data), framed in the same JSON envelope as other messages
- Handle terminal resize: browser sends `{type: "resize", cols: N, rows: N}` → agent calls `stty cols N rows N` on the pty
- Use `node-pty` pattern in Python: `import pty; import os` — spawn shell with a pseudo-terminal so ANSI codes work correctly

---

## Definition of done

Phase 2 is complete when:

- [ ] `rialu-agent.py` running on Rose, Iris, Daisy via systemd
- [ ] Machines tab shows live resource bars for all three machines
- [ ] tmux pane browser lists all sessions across all machines
- [ ] Claude Code sessions detected and highlighted automatically
- [ ] Yes/No/Enter/Escape quick-action buttons work without opening a terminal
- [ ] Full xterm.js terminal opens on any pane or as a fresh shell
- [ ] Repo state table shows current git status per machine
- [ ] All existing Phase 1 tests still pass
- [ ] New tests: agent heartbeat, tmux enumeration, action proxy, Claude detection

---

## What this enables

Once Phase 2 is running, the workflow becomes:

1. Todd opens Rialú on any device (phone, tablet, laptop, another machine)
2. Machines tab shows all three machines: Rose/Iris/Daisy, resource usage, active projects
3. Claude Code sessions visible at a glance — running green, waiting amber
4. When Claude pauses: read the question in Rialú, click Yes/No/Enter from the browser
5. When something needs more attention: click Open Terminal → full shell on that machine
6. 10+ Claude Code sessions running in parallel, all monitored from one browser tab
7. Todd never needs to be at a specific machine — full remote ops from anywhere

---

*Rialú Phase 2 Terminal PRD v1.0 — FoxxeLabs — 2026-03-20*

# PRD: Portfolio Divergence Digest

**Project:** Rialú
**Branch:** `main`
**Status:** Ready for implementation
**Author:** Todd McCaffrey / FoxxeLabs

---

## Context

Rialú stores rich project *state* (`projects.status`, `phase`, `runtime`, `updated_at`)
and rich *activity* data (commit-derived `worklog` rows via `commit_worklog.py`,
surfaced by `routers/commits.py`). What it does **not** do today is compare the two
to surface **contradictions** — projects that *claim* to be active but have gone
quiet, and projects parked in research/paused with no defined trigger to ever
revisit them (the "un-killed backlog" attention leak).

This is the inverse of `routers/milestone_review.py`. That module looks for
*evidence of progress* and auto-closes milestones when GitHub confirms work
happened. This module looks for the *absence of progress* relative to declared
status. They are complementary, not overlapping — and they deliberately share the
same shape (compute → write flags → log every decision → expose a `latest`/`log`
read endpoint).

**Key design fact:** all inputs already live in Rialú's own SQLite database. This
feature reads `projects` and `worklog`. It does **not** call git-mcp, GitHub, or
any external service, and therefore carries **none** of the PAT/auth-failure
fragility that affects `milestone_review.py`. The scheduled run touches no secret.

---

## Goals

1. Compute a **health flag** per project on a weekly cadence by joining declared
   status against actual commit activity.
2. Surface the flag two ways on the existing **Projects** tab: a per-card pill
   (already built — see Part 3a) **and** an at-a-glance summary strip pinned to the
   top of the tab (Part 3b) so the week's flagged projects are visible the instant
   Rialú opens, with no navigation and no scanning 38 cards for amber.
3. Log every decision for transparency (copy `milestone_review_log` pattern).
4. Run automatically via a **Fly scheduled machine** in Rialú's existing `fly.toml`
   — no new repo, no new app, no new MCP, no new secret.

## Non-goals

- No cost or income tracking (that is the separate Financial PRD; cost tables
  `budget` / `api_registry` / `api_usage` / `anthropic_usage` already exist;
  income lives in the Anseo/Stór Django app, gated on VAT OSS).
- No GitHub/git-mcp calls. Commit data is already in `worklog`.
- **No dedicated tab.** A separate tab is the wrong surface — it adds a click, an
  extra `loadedTabs` entry, and a panel that reads mostly-empty most weeks (so it
  gets forgotten). The at-a-glance view belongs at the top of Projects, the
  default landing tab. See Part 3b rationale.

---

## Part 1 — Divergence computation

### 1a. New file: `routers/divergence.py`

Follow the structure of `routers/milestone_review.py`: a `POST /run` that computes
and persists, plus read endpoints. Router prefix `/api/divergence`.

### 1b. The rules

For each project, classify into exactly one flag:

| Flag | Condition | Meaning |
|---|---|---|
| `stale-active` | `status` in (`development`, `running`) **AND** zero commits in last `window_days` (default 30) **AND** no non-auto-git worklog row in the same window | Claims active, data says quiet |
| `no-trigger` | `status` in (`research`, `paused`) **AND** `notes` contains no trigger marker (see 1c) | Un-killed backlog — parked with no defined way back |
| `healthy` | `status` active-class **AND** commits within `window_days` | Recorded so absence-of-flag is meaningful |
| `dormant-ok` | `status` in (`archived`, `shipped`, `deployed`) with no expectation of ongoing commits, **OR** parked **with** a trigger marker | Quiet by design — not a problem |

Notes on the rules:

- **Commit counting must parse the notes field**, not count rows.
  `commit_worklog.py` writes one `[auto-git]` row per project-day with
  pipe-delimited commits. True count =
  `len(notes.replace("[auto-git] ", "", 1).split(" | "))`. Reuse the helper
  `_parse_commit_count` from `routers/commits.py` (import it or factor it into a
  shared util — do not duplicate the logic).
- **Respect `runtime` vs `status`.** `status` is lifecycle; `runtime` is the
  separate operational-state column (migration 019). A project can be
  `status=deployed` (lifecycle done) and legitimately receive no commits — that is
  `dormant-ok`, not `stale-active`. Only the active-class lifecycle states
  (`development`, `running`) trigger `stale-active`.
- **`deployed` is ambiguous.** A deployed project may still be under active
  development. Resolution: `deployed` is treated as active-class for staleness
  **only if** it has had *any* commit in the last 90 days; otherwise `dormant-ok`.
  This avoids flagging genuinely-finished shipped services (Mnemos, Colainn,
  Sentinel) every single week.

### 1c. Trigger marker convention

`no-trigger` detection needs a way to tell "parked with a plan" from "parked and
forgotten". Phase-1 cheapest approach: a substring check on `projects.notes` for a
case-insensitive `trigger:` line (e.g. `trigger: post-viva`, `trigger: after Cló
MVP ships`). Document this convention in the project notes help text.

Phase-1.5 (optional, cleaner): a dedicated `projects.revisit_trigger TEXT` column
(new migration). The PRD recommends the column — it makes "parked with a plan" a
first-class queryable field rather than a fragile substring scan, consistent with
the house preference for correct structure over quick string-matching. If the
column is added, `no-trigger` = parked AND `revisit_trigger IS NULL OR ''`.

### 1d. Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/divergence/run` | Compute flags for all projects, persist current flag, append to `divergence_log`. Returns summary. Auth-guarded (see `auth.py`). Accepts optional `window_days` (default 30). |
| `GET` | `/api/divergence/latest` | Current flag per project **plus** an aggregate `counts` block and `days_to_deadline` (see 3b). Drives both the pills and the summary strip. |
| `GET` | `/api/divergence/log?limit=50` | Recent divergence decisions, newest first. |

`POST /run` response shape (mirror `milestone_review`):
```json
{
  "checked": 38,
  "flags": {"stale-active": 4, "no-trigger": 6, "healthy": 9, "dormant-ok": 19},
  "window_days": 30,
  "results": [
    {"project_id": 23, "project": "Litir", "flag": "stale-active",
     "detail": "0 commits in 30d; status=development; last worklog 2026-04-02"}
  ]
}
```

`GET /api/divergence/latest` response shape (drives the frontend):
```json
{
  "counts": {"stale-active": 4, "no-trigger": 6, "healthy": 9, "dormant-ok": 19},
  "days_to_deadline": 7,
  "deadline_label": "viva",
  "projects": [
    {"project_id": 23, "name": "Litir", "health": "stale-active",
     "health_detail": "0 commits in 30d; status=development"}
  ]
}
```

---

## Part 2 — Schema changes

Append to the `MIGRATIONS` list in `db.py` (next sequential numbers, idempotent,
duplicate-column-tolerant — follow the existing pattern exactly):

```sql
-- 021 — divergence digest log
CREATE TABLE IF NOT EXISTS divergence_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    project_name TEXT NOT NULL,
    flag        TEXT NOT NULL,
    detail      TEXT,
    window_days INTEGER NOT NULL DEFAULT 30,
    checked_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_divergence_checked ON divergence_log(checked_at);

-- 022 — current health on projects (first-class, avoids max-by-date subquery)
ALTER TABLE projects ADD COLUMN health TEXT;
ALTER TABLE projects ADD COLUMN health_checked_at TEXT;

-- 023 — revisit trigger (recommended; enables clean no-trigger detection)
ALTER TABLE projects ADD COLUMN revisit_trigger TEXT;
```

`init_db()` already swallows `duplicate column` errors, so these are safe on every
startup. The `health` column already has frontend support — `healthPill()` and the
`HEALTH_PILL` map in `static/index.html` already render `p.health`. They show
nothing until the backend populates the column. This PRD's backend work is what
lights up the pills that already exist.

---

## Part 3 — Frontend

### 3a. Per-card pill (ALREADY BUILT — verify only)

`static/index.html` already contains `healthPill(h, detail)`, the `HEALTH_PILL`
colour map (`stale-active`=warn/amber, `no-trigger`=gray, `healthy`=ok/green,
`dormant-ok`=nothing), and the call site in `_cardHtml`:
`${healthPill(p.health, p.health_detail)}`. **No new pill work is needed** — just
confirm `GET /api/projects` returns `health` and `health_detail` per project so the
existing code has data. The pill is the per-project *confirmation* view (you're
already looking at the card), not the at-a-glance view.

### 3b. At-a-glance summary strip (NEW — the main ask)

**Rationale.** The pills alone fail at-a-glance: with ~19 of 38 projects
`dormant-ok` (no pill) and the flagged handful scattered across the card grid,
finding "what needs attention this week" means visually scanning every card for
amber. The summary strip fixes this — it puts the counts and the deadline where the
eye lands first, on the default Projects tab, with one-click drill-down.

**Placement.** A thin row of stat cards (reuse the existing `.mcards` / `.mcard`
component, as on the Work Log and Budget tabs) inserted at the **top of the
`#projects` panel**, above the `.row` that holds the view toggle. Visible in every
project view (cards/list/kanban/timeline) since it sits above the view container.

**Content.** From `GET /api/divergence/latest`:

| Card | Value | Colour |
|---|---|---|
| Deadline | `days_to_deadline` + `deadline_label` (e.g. "7 days · viva") | `--err` if ≤14, else `--warn` |
| Stale-active | `counts['stale-active']` | `--warn` |
| No trigger | `counts['no-trigger']` | `--t2` (grey) |
| Healthy | `counts['healthy']` | `--ok` |

Omit a `dormant-ok` card — the quiet-by-design majority is not a number you act on.

**Drill-down.** Clicking the Stale-active or No-trigger card filters the project
views below to just those projects. Reuse the existing filter machinery: set a
module-level `_healthFilter`, have `filterProjects()` additionally filter on
`p.health === _healthFilter` when set, and re-render. Clicking the active card
again clears the filter. This needs no new filtering engine — `filterProjects()`,
`renderProjectCards()`, `renderProjectList()` already exist and already re-render
from `allProjects`.

**Deadline source.** `days_to_deadline` is computed server-side in
`/api/divergence/latest`. For Phase 1, hardcode the viva deadline date as a module
constant in `routers/divergence.py` (the dissertation/viva is the one binding
deadline through June 2026). A later iteration can read the nearest unmet milestone
`due_date` across projects — but do not over-build that now; the constant is correct
and honest for the current window.

**No chart, no new tab, no new panel.** Four stat cards + one filter hook.

---

## Part 4 — Scheduled run (Fly)

Rialú already deploys via its existing `fly.toml` on Fly.io. Add a **weekly
scheduled machine** that invokes the divergence run. Two acceptable
implementations — prefer (A):

**(A) CLI subcommand (preferred).** Add `divergence-run` to `cli/rialu`. It calls
the same computation function `routers/divergence.py` uses (factor the core into a
plain function, e.g. `run_divergence(window_days=30) -> dict`, called by both the
route handler and the CLI). The scheduled machine runs
`python -m cli.rialu divergence-run` (or the equivalent entrypoint) in-process
against the shared `/data/rialu.db` volume, then exits. No HTTP self-call, full app
context.

**(B) HTTP self-call.** Scheduled machine runs an authenticated
`curl -X POST $RIALU_URL/api/divergence/run`. Simpler infra, but needs the auth
token in the machine env and makes a web request for a batch job. Acceptable
fallback only.

Schedule: weekly, Monday 07:00 Europe/Dublin (note Fly schedules are UTC — set
accordingly, or fix to a UTC time and accept the DST drift for a weekly job).

The core function must be **idempotent** and safe to re-run (re-running on the same
day simply overwrites `projects.health` and appends a fresh log row).

---

## Part 5 — Tests

Add `tests/test_divergence.py` following `tests/test_milestone_review.py` /
`tests/test_commits.py` patterns (temp `RIALU_DB`). Cover:

- `stale-active`: active-status project with no commits in window → flagged.
- `healthy`: active-status project with a recent `[auto-git]` row → healthy.
- `no-trigger`: paused project, notes without `trigger:` → flagged;
  with `trigger:` (or `revisit_trigger` set) → `dormant-ok`.
- `dormant-ok`: `deployed` project with no commits in 90d → not flagged stale.
- `deployed` + recent commit → treated active-class.
- Commit counting parses pipe-delimited notes (3 commits in one row counts as 3,
  not 1).
- `POST /run` writes `divergence_log` rows and updates `projects.health`.
- `GET /latest` returns a `counts` block and `days_to_deadline`.
- Idempotency: running twice in one day leaves one health value, two log rows.

---

## Acceptance criteria

- [ ] `routers/divergence.py` created, registered in `main.py`.
- [ ] `POST /api/divergence/run` computes and persists flags; returns summary.
- [ ] `GET /api/divergence/latest` returns per-project flags + `counts` +
      `days_to_deadline`.
- [ ] `GET /api/divergence/log` returns recent decisions newest-first.
- [ ] Migrations 021–023 appended to `db.py`, idempotent on restart.
- [ ] Commit counting reuses `commits.py` parsing (no duplicated logic).
- [ ] `deployed`/`shipped`/`archived` quiet projects are `dormant-ok`, not stale.
- [ ] `runtime` column respected — lifecycle-done ≠ stale.
- [ ] `health` + `health_detail` added to `GET /api/projects`; existing pills light up.
- [ ] Summary strip renders at top of Projects tab with 4 stat cards.
- [ ] Clicking Stale-active / No-trigger cards filters project views; click again clears.
- [ ] CLI `divergence-run` subcommand works against the shared DB.
- [ ] Fly scheduled machine added to `fly.toml` (weekly).
- [ ] `tests/test_divergence.py` passes; existing suite still green.

---

## Notes for CC

- **Read first:** `routers/milestone_review.py` (shape to mirror),
  `routers/commits.py` (commit parsing — reuse `_parse_commit_count`),
  `db.py` (migration pattern: numbered, idempotent, `init_db` swallows
  duplicate-column), `static/index.html` (`healthPill`/`HEALTH_PILL` already
  present; `.mcards` strip pattern used on Work Log + Budget tabs; `filterProjects`
  for the drill-down hook).
- **The pills are already built.** Do not re-add them. Your job is the backend that
  populates `health`/`health_detail`, plus the new summary strip + filter hook.
- **Do not call git-mcp or GitHub.** Commit activity is already in `worklog`. This
  is the whole reason the scheduled job is reliable — keep it that way.
- **`status` is lifecycle, `runtime` is operational state** (migration 019). Do not
  conflate. Only `development`/`running` (and conditionally `deployed`) are
  active-class for staleness.
- **The majority flag will be `dormant-ok`** — that's correct and healthy. The
  signal is the small set of `stale-active` and `no-trigger`. No pill and no
  summary card for `dormant-ok`; noise defeats the purpose.
- **Factor the core into a function** (`run_divergence()`) shared by the route and
  the CLI — don't implement the logic twice.
- **No dedicated tab.** The summary strip on the Projects tab is the deliberate
  choice; a separate tab would be checked rarely and forgotten.
- `static/index.html` is the single-page frontend, inline JS, no build step.
  Follow existing patterns.
- This is Phase 1. The Financial PRD (cost pollers into existing `budget` /
  `api_usage` / `anthropic_usage` tables — note `anthropic_usage` already has a
  CSV-import path; income in Anseo gated on VAT OSS) is a separate brief.

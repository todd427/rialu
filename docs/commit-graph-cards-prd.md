# PRD: Commit Activity Graph + Project Cards Layout

**Project:** Rialú  
**Branch:** `main`  
**Status:** Ready for implementation  
**Author:** Todd McCaffrey / FoxxeLabs  

---

## Context

Rialú already collects commit-derived data via `commit_worklog.py`, which populates the `worklog` table with per-day entries containing `lines_added`, `lines_removed`, and commit messages (prefixed `[auto-git]`). The `project_dashboard` endpoint (`GET /api/projects/{id}/dashboard`) already surfaces LOC stats. This PRD adds:

1. A **commit activity API** that returns daily commit counts and LOC deltas in a graph-ready format, with CSV export
2. A **graphical commit timeline** on the Project detail view (per-project) and a global view (all projects)
3. A **Cards layout** for the Projects list page as the default, with clickable cards matching the detail available from the existing List view

---

## Part 1 — Commit Activity API

### 1a. New endpoint: `GET /api/projects/{project_id}/commits`

Returns daily commit activity for a single project, sourced from the `worklog` table (rows where `notes LIKE '[auto-git]%'`).

**Query parameters:**
- `days` — integer, default `90`, max `365`
- `format` — `json` (default) or `csv`

**JSON response:**
```json
{
  "project_id": 4,
  "project_name": "anseo",
  "from": "2025-12-19",
  "to": "2026-03-28",
  "total_commits": 187,
  "total_days_active": 52,
  "peak_day": {"date": "2026-01-14", "commits": 12},
  "series": [
    {
      "date": "2026-03-28",
      "commits": 3,
      "messages": ["feat: reading library", "fix: cover aspect ratio", "docs: PRD"]
    }
  ]
}
```

`commits` per day is derived by parsing the pipe-delimited `notes` field of `[auto-git]` worklog rows. Each row represents one day (not one commit), and individual commits are joined with ` | `. In Python: `len(notes.replace("[auto-git] ", "", 1).split(" | "))`. **Note:** `lines_added`/`lines_removed` are **not** populated by `commit_worklog.py` — that poller only writes `minutes` and `notes`. LOC data comes from the separate GitHub LOC poller (6hr interval). The commits endpoint should source LOC from worklog rows where `session_type = 'code'` and `notes NOT LIKE '[auto-git]%'` for the same project/date, or omit LOC fields and leave that to the existing `project_dashboard` endpoint.

**CSV response** (when `?format=csv`):
```
date,commits,messages
2026-03-28,3,"feat: reading library | fix: cover aspect ratio | docs: PRD"
...
```

Content-type: `text/csv`. Content-Disposition: `attachment; filename="{slug}-commits.csv"`.

---

### 1b. New endpoint: `GET /api/commits`

Global commit activity across all projects. Same parameters as above.

**JSON response:**
```json
{
  "from": "2025-12-19",
  "to": "2026-03-28",
  "series": [
    {
      "date": "2026-03-28",
      "total_commits": 5,
      "by_project": [
        {"project_id": 4, "name": "anseo", "commits": 3},
        {"project_id": 7, "name": "sentinel", "commits": 2}
      ]
    }
  ]
}
```

CSV export supported here too (`?format=csv`), one row per project per day:
```
date,project,commits
```

---

### 1c. Router location

Add to a new file: `routers/commits.py`. Register in `main.py` alongside existing routers.

---

## Part 2 — Frontend: Commit Graph

### 2a. Per-project graph on Project detail view

The Project detail panel (currently showing worklog entries, deploy status, LOC) gets a **Commit Activity** section:

- **Daily bar chart** — commits per day, last 90 days by default
- **Controls:** 30d / 90d / 1y toggle; Download CSV button
- **Implementation:** Chart.js (already a safe dependency choice for a vanilla JS frontend). Load from CDN if not already present.
- **Colours:** bars in Rialú's accent colour for active days; zero days grey

Chart renders inline below the existing LOC/deploy stats block.

### 2b. Global commit graph on Work Log page

The Work Log page gets a **Build Activity** section above the worklog entry list:

- Same bar chart, but global (all projects)
- Stacked bars by project (top 5 by commit count; rest grouped as "Other")
- 90d default, same toggle and CSV download

### 2c. Chart.js integration

Use Chart.js 4.x from CDN:
```html
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
```

Do not bundle. Single `<canvas>` element per chart. Destroy and re-create on range toggle to avoid canvas reuse errors.

---

## Part 3 — Project Cards Layout

### 3a. Cards as default layout

The Projects page currently defaults to a list layout. Change the default to **Cards**. Persist the user's layout preference in `localStorage` under key `rialu_projects_layout` (`"cards"` or `"list"`).

### 3b. Card design

Each project card displays:

| Field | Source |
|---|---|
| Project name | `projects.name` |
| Status badge | `projects.status` (colour-coded: deployed=green, development=blue, paused=amber, archived=grey) |
| Phase | `projects.phase` |
| Platform | `projects.platform` (Fly.io, Railway, etc.) |
| Machine | `projects.machine` |
| Last active | `projects.updated_at` (relative: "2 hours ago") |
| Commits this week | derived from worklog ([auto-git] rows, last 7 days) — can be 0 |
| Repo link | icon link to `projects.repo_url` if set |
| Site link | icon link to `projects.site_url` if set |

Card is fully clickable (entire card surface) → opens the same project detail panel as clicking a list row. No separate click target needed for the card vs. the icon links — icon links open in a new tab and stop propagation.

### 3c. Card grid layout

```
grid-template-columns: repeat(auto-fill, minmax(280px, 1fr))
gap: 16px
```

Responsive — collapses to single column on narrow viewports.

### 3d. Layout toggle

Existing list/card toggle buttons remain. Cards is now the default. Toggle state persists via `localStorage`.

### 3e. Commits-this-week on cards

The card needs a lightweight commits-this-week count without a full chart. Source this from a new optional field in the `GET /api/projects` response:

Add `commits_7d` to the project list response — a **true commit count** (not row count) for the last 7 days. Since `commit_worklog.py` writes one row per project per day with pipe-delimited commit messages, row count would only give "days active" (max 7). The commit count must be derived by parsing the notes field.

Recommended approach — compute in Python after the query:

```python
# After fetching projects with their [auto-git] worklog rows for last 7 days
for project in projects:
    total = 0
    for row in auto_git_rows:  # WHERE date >= date('now', '-6 days') AND notes LIKE '[auto-git]%'
        notes = row["notes"].replace("[auto-git] ", "", 1)
        total += len(notes.split(" | "))
    project["commits_7d"] = total
```

Alternatively, use a SQL approximation counting pipe separators:
```sql
SELECT project_id,
       SUM(LENGTH(notes) - LENGTH(REPLACE(notes, ' | ', '  ')) + 1) as commits_7d
FROM worklog
WHERE date >= date('now', '-6 days')
  AND notes LIKE '[auto-git]%'
GROUP BY project_id
```

The Python approach is cleaner and less fragile. Since the project list is small (single-user app), the extra query is negligible.

---

## Data model changes

None. All new data is derived from the existing `worklog` table.

---

## API summary

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/projects/{id}/commits` | Per-project daily commit series |
| `GET` | `/api/commits` | Global daily commit series |
| `GET` | `/api/projects` | Augmented with `commits_7d` field |

---

## Acceptance criteria

- [ ] `GET /api/projects/{id}/commits?format=json` returns correct series
- [ ] `GET /api/projects/{id}/commits?format=csv` triggers file download with correct filename
- [ ] `GET /api/commits?format=csv` — global CSV with project column
- [ ] `GET /api/projects` includes `commits_7d` per project
- [ ] Per-project chart renders on project detail, 30d/90d/1y toggle works
- [ ] Global chart renders on Work Log page, stacked by project
- [ ] Download CSV button works on both charts
- [ ] Projects page defaults to Cards layout
- [ ] Cards are fully clickable, open same detail as list rows
- [ ] Status badges are colour-coded
- [ ] Layout toggle persists in localStorage
- [ ] `commits_7d` shown on each card

---

## Notes for CC

- `commit_worklog.py` is the source of truth for how commit data lands in `worklog`. It writes **one row per project per day**, not one row per commit. Individual commits are pipe-delimited in the `notes` field: `[auto-git] <hash> <msg> | <hash> <msg> | ...`.
- **Commit counting: parse the notes field.** True commit count = `len(notes.replace("[auto-git] ", "", 1).split(" | "))`. Row counting only gives "days active."
- **LOC fields (`lines_added`/`lines_removed`) are NOT populated by `commit_worklog.py`** — it only writes `minutes` and `notes`. LOC comes from the separate GitHub LOC poller. The commits endpoint omits LOC; use the existing `project_dashboard` for LOC stats.
- Chart.js from CDN only — do not add to `requirements.txt`.
- The existing `project_dashboard` endpoint already has LOC stats. The new commits endpoint is additive, not a replacement.
- `static/index.html` is the single-page frontend. All JS is inline or in `<script>` blocks. Follow existing patterns — no build step, no npm.

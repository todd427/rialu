# Outstanding briefs scan — CC brief

**For:** a Rialú Claude Code session.
**Status:** done
**Authored:** 17 Sep 2026
**Pairs with:** `timire/briefs/standup-outstanding-briefs.md` (the display side)

---

## 1. Problem

Briefs and PRDs are written into `docs/` and `briefs/` folders across the fleet
and then nothing reads them. Two were pushed today (`git-mcp` 6112c58,
`rialu` 0aa1760) and the only record that they exist and are unbuilt is a
`Status:` line inside each file. That is where continuous improvement dies:
plan written, act never scheduled, nobody notices.

Rialú already clones every registered repo for the divergence digest, so it is
the one component that can see every brief in the fleet without a new clone
path. Timire already has the morning slot. So: **Rialú scans, Timire shows.**

## 2. Status vocabulary — fixed here, used fleet-wide

Every brief carries one line, anywhere in its first 30 lines, of the form

    **Status:** <value>

with `<value>` one of:

| value | meaning | shown in standup? |
|---|---|---|
| `not-started` | written, nobody has begun | yes |
| `ready` | reviewed, can be picked up as-is | yes |
| `in-progress` | a CC session is on it | yes, flagged |
| `parked` | deliberately deferred | no |
| `done` | implemented and merged | no |

Anything else, or no line at all, is reported as `unknown` — never guessed.
Matching is case-insensitive and tolerates surrounding whitespace and the
legacy phrasings that already exist in the fleet, mapped as: "Not started" →
`not-started`; "Ready for implementation" / "Ready" / "build-ready" → `ready`;
"Implemented" / "Shipped" / "Done" → `done`. Timire's own briefs, which carry
`**Type:** brief` and no status line, are `unknown` until they gain one — which
is the point.

**Closing rule for CLAUDE.md in every repo (add it):** when a CC session
implements a brief, it flips that brief's `Status:` line to `done` in the same
commit as the implementation. That is the only discipline the scheme needs.

## 3. What Rialú builds

1. **Scanner** — in the existing per-repo pass that feeds the divergence
   digest, glob `docs/*prd*.md`, `docs/cc-brief-*.md`, `docs/*-handoff.md`,
   `briefs/*.md`. For each: repo, path, title (first `# ` heading, span tags
   stripped), status (§2), `age_days` (from the file's last commit date via
   `git log -1 --format=%cI -- <path>`, not mtime), and `authored` if a
   `**Authored:**` or `**Authored** ` line exists. Store in a table:
   ```sql
   CREATE TABLE IF NOT EXISTS briefs (
       repo        TEXT NOT NULL,
       path        TEXT NOT NULL,
       title       TEXT,
       status      TEXT NOT NULL,        -- §2 vocabulary or 'unknown'
       age_days    INTEGER NOT NULL,
       last_commit TEXT,
       scanned_at  TEXT NOT NULL,
       PRIMARY KEY (repo, path)
   );
   ```
   Full replace per scan for a repo (delete rows for that repo, insert fresh),
   so a deleted brief disappears rather than lingering.

2. **Endpoint** — `GET /api/briefs?status=open&limit=5`
   - `status=open` (default) = `not-started`, `ready`, `in-progress`, `unknown`
   - `status=all` = everything
   - Sorted `age_days` descending, then repo, then path.
   - Returns `{"count_open": n, "shown": [...], "by_status": {...}}` where
     `shown` is the first `limit` rows. `limit` default 5, max 50.
   Auth: same as the rest of `/api/*` — CF Access for browsers, Bearer for
   service callers. Timire will send `Authorization: Bearer <RIALU_TOKEN>`.

3. **MCP tool** — `briefs(status: str = "open", limit: int = 10)` on
   `mcp_server.py`, same projection as the endpoint. Keep the row shape lean
   (six short fields); the list is bounded by `limit` so the truncation
   concern from `list_projects` does not arise.

4. **Dashboard** — a small "Outstanding briefs" panel next to the divergence
   digest, same data, same ordering, top ten with a count of the rest. No
   colouring by age; age is the sort key, that is enough.

## 4. Cap and tone

The standup line is Timire's (paired brief), but the endpoint enforces the cap
so no caller can accidentally dump forty rows. The report is a list, not an
alarm: no thresholds, no "overdue", no red. Age sorted oldest-first says
everything a threshold would, without training the reader to ignore red.

## 5. Out of scope

- Reading brief bodies, extracting tasks, or estimating effort.
- Any write to the briefs (status is flipped by the implementing session, §2).
- Non-registered repos. If a repo is not in Rialú it is not scanned; that is a
  ghost-repo problem and has its own PRD lineage.

## 6. Done when

- `pytest -q` passes with new tests: a fixture repo containing five briefs in
  the five statuses plus one with no status line and one with the legacy
  "Ready for implementation" phrasing → `by_status` counts exactly as expected,
  `unknown` for the missing line, `ready` for the legacy phrasing; `limit`
  respected; a deleted brief vanishes on rescan.
- `GET /api/briefs` with a Bearer token returns this brief itself at
  `not-started` on first scan.
- `briefs()` answers over an authenticated MCP call (not just `/version`).
- CLAUDE.md in `rialu` carries the closing rule from §2.

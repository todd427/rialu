# Rian lifecycle sync — PRD / handoff

**For:** a Rialú Claude Code session.
**From:** the 2026-09-17 project census. Authored 17 Sep 2026.
**Status:** done (Rialú side; the Rian `sync-rialu` client of §7 is still to be written)

---

## 1. Context

Asked 2026-09-17: "how many projects did we start this year, and how many are
deployed?" Rialú answered the second (99 registered; 38 `deployed`, 45 in
production counting `live`/`running`/`shipped`). It could not answer the first.
`projects.created_at` is the *registration* date, and the register began in March
2026, so every backfilled row says 2026 regardless of when the project actually
began. Anseo, CyberSafer and Sentinel are all in that batch and all predate the year.

Two sources know better:

- **Rian** (id 75) computes a lifecycle per project from the chat exports — first
  appearance, last appearance, salience — and git-joins each to its repo. First
  mention is "started" in the sense that matters: the day the thing was first
  talked about, usually ahead of the repo.
- **GitHub** knows `created_at` per repo. A git-mcp PRD
  (`docs/git-list-remote-repos-created-at-prd.md`, 6112c58) exposes it.

Decision (2026-09-17): **no MCP on Rian.** Rian is a batch extractor; Rialú is the
live register with the MCP surface everything already talks to. Two servers
answering "what's the state of project X" from different data with different
freshness is the divergence problem restated. So Rian *writes into* Rialú, and
Rialú serves the answer.

## 2. The wire contract — pinned here; Rian will implement to it (§7)

**Endpoint:** `POST /api/lifecycle`

**Request body** — a batch, one element per project Rian could match:
```json
{
  "source": "rian",
  "run_at": "2026-09-17T14:00:00+00:00",
  "items": [
    {
      "repo_url": "https://github.com/todd427/aigne",
      "first_seen": "2026-06-20T09:12:00+00:00",
      "last_seen":  "2026-09-13T19:40:00+00:00",
      "mentions": 412
    }
  ]
}
```

Field notes:
- `repo_url` — the join key. Rialú matches on `projects.repo_url` exactly after
  normalising both sides (strip trailing slash and `.git`, lowercase host). Rian's
  git-join already produces this; where a Rian project has no repo, Rian omits it
  (Rialú cannot place it).
- `first_seen` / `last_seen` — UTC ISO-8601. From Rian's lifecycle: first and last
  conversation timestamp in which the project appears.
- `mentions` — integer, informational. Stored; not used for anything yet.
- **Idempotent by construction:** a re-send with the same `run_at` and items is a
  no-op. Rialú upserts per `repo_url`, keeping the *earliest* `first_seen` and the
  *latest* `last_seen` ever received (monotone), so partial or repeated batches
  can never move a date the wrong way.
- **Accept-not-reject:** an item whose `repo_url` matches no project is counted in
  the response as `unmatched` and otherwise ignored. Never 4xx a well-formed batch
  for an unknown repo.

**Response:** `2xx` with `{"matched": n, "unmatched": m, "unmatched_urls": [...]}`.
Rian logs `unmatched_urls`; they are the ghost-repo signal the git-mcp PRD talked
about.

**Auth:** same as the rest of `/api/*` — Cloudflare Access for browsers, Bearer for
service callers. Rian sends `Authorization: Bearer <RIALU_TOKEN>` (and a
`Host: rialu.ie` override when hitting the Fly app directly), exactly as Suim does
for `/api/spend`. **No authorisation from Rialú to Rian is needed or built:** Rian is
the client, Rialú the server; Rian's Cloudflare Access front stays as it is.

## 3. What Rialú builds

Adapt to conventions: `with db() as conn:`, idempotent migrations array in `db.py`,
router under `routers/`, CF/Bearer auth, lean MCP projection.

1. **Schema — three columns on `projects`** (idempotent `ALTER TABLE … ADD COLUMN`
   guarded by a `PRAGMA table_info` check, as the other migrations do):
   ```sql
   first_seen        TEXT,   -- from Rian; earliest ever received
   last_seen         TEXT,   -- from Rian; latest ever received
   started_at        TEXT,   -- derived, see §4
   started_at_source TEXT    -- 'rian' | 'github' | 'manual' | NULL
   ```
   Plus one audit table so a bad run can be seen and reverted:
   ```sql
   CREATE TABLE IF NOT EXISTS lifecycle_runs (
       id         INTEGER PRIMARY KEY,
       source     TEXT NOT NULL,
       run_at     TEXT NOT NULL,
       received_at TEXT NOT NULL DEFAULT (datetime('now')),
       matched    INTEGER NOT NULL,
       unmatched  INTEGER NOT NULL,
       payload    TEXT NOT NULL              -- raw JSON as received
   );
   ```

2. **Endpoint — `routers/lifecycle.py`:** `POST /api/lifecycle` validating §2,
   upserting per matched project with the monotone rule, recomputing `started_at`
   (§4) for each touched row, writing one `lifecycle_runs` row, returning the counts.
   Broadcast `project.update` over the Faire hub for touched rows, like
   `_broadcast_project`.

3. **GitHub fallback — extend `routers/github.py`** (it already exists) with a
   `POST /api/lifecycle/github` admin action that calls git-mcp's
   `git_list_remote_repos` (after 6112c58 ships `created_at`) or GitHub directly,
   matches on `repo_url`, and fills `started_at` **only where it is NULL**, with
   `started_at_source='github'`. Idempotent; re-running never overwrites a Rian date.

4. **Read surfaces:**
   - `get_project` — return all four new fields.
   - `list_projects` (MCP lean projection) — add `started_at` and
     `started_at_source` only. Two short strings per row; well inside the
     truncation budget that motivated the lean shape. `first_seen`/`last_seen`
     stay in the full record.
   - `GET /api/projects/census?year=2026` — `{started: n, by_status: {...},
     in_production: n}` where `in_production` = status in
     (`deployed`,`live`,`running`,`shipped`). This is the question that prompted
     the PRD, answered in one call, and it should also be an MCP tool
     (`project_census(year)`), since that is where the question gets asked.

5. **Dashboard:** show `started_at` on the project card with a small source glyph
   (Rian / GitHub / manual). Nothing more.

## 4. `started_at` rule — decided, not for CC to revisit

`started_at` = `first_seen` when Rian has one, else GitHub `created_at`, else NULL.
Source recorded alongside. Both raw dates are kept, so if they disagree the
disagreement is visible rather than resolved by deletion. First-mention wins
because a project starts when it is first thought about, not when `gh repo create`
is run; the repo date is the fallback for things that never appeared in chat.

A manual override (`started_at_source='manual'`) set via `update_project` is
honoured and never overwritten by either sync.

## 5. Freshness — stated so nobody mistakes the fields

`last_seen` is bounded by how often chat exports are fed to Rian. It will lag
`updated_at` and must never be read as activity. The dashboard should not colour
on it. `started_at` does not have this problem: it only ever moves earlier.

## 6. Out of scope

- Any Rian MCP surface (decided against, §1).
- Serving Rian's lineage / why / displacement data through Rialú. If wanted later:
  one tool, `project_history(id)`, reading Rian's per-project artefact from disk.
  Separate PRD.
- Automating the chat-export step. Exports are manual; `refresh` stays manual.

## 7. Rian side — small, after this ships

In `rian`: a `sync-rialu` subcommand (or a final step of `refresh`) that builds the
§2 batch from the lifecycle + git-join tables and POSTs it with `RIALU_URL`,
`RIALU_TOKEN` (from the Taisce vault, same pattern as Suim). Log `unmatched_urls`.
Contract test against a stub, mirroring Suim's `test_drain_contract.py`. That is the
whole Rian change; no server, no auth surface.

## 8. Acceptance

- `POST /api/lifecycle` upserts monotonically; a re-send with identical items changes
  nothing; a later batch with an earlier `first_seen` moves it earlier, a later
  `first_seen` does not.
- Unknown `repo_url`s are counted and returned, not rejected.
- `POST /api/lifecycle/github` fills only NULL `started_at`, tagged `github`.
- `project_census(2026)` returns a started count, a status breakdown, and
  `in_production`, in one MCP call.
- `list_projects` still returns 99 rows in one response (truncation guard).
- Manual `started_at` survives both syncs.

# CC Brief — Rialú: narrative staleness (the axis divergence doesn't cover)

**Project:** Rialú
**Date:** 29 July 2026
**Requested by:** a session that read four stale `phase`/`notes` fields as current state and got four project assessments wrong
**Status:** Ready for implementation
**Extends:** `divergence-digest-prd.md` — does **not** replace or duplicate it

---

## <span style="color:#1D9E75">Context</span>

The Portfolio Divergence Digest is built and working. `routers/divergence.py` computes a `health` flag per project by joining declared `status` against commit activity in `worklog`, and the flags are populated — as of `health_checked_at` 2026-07-24, Rian reads `healthy`, Imeall `dormant-ok`, Úire `stale-active`.

Those flags were all **correct** and the reader was still misled, because they answer a question adjacent to the one that matters. Observed on 2026-07-29:

| Project | `health` flag | Was the flag right? | What `phase`/`notes` claimed | Actual state |
|---|---|---|---|---|
| Rian (75) | `healthy` | Yes — committing steadily | "phase-1 (scaffold)" | All 5 phases shipped, deployed to Fly |
| Imeall (70) | `dormant-ok` | Yes — quiet by design | notes: "NO src/code yet — Phase 1 build not started", under a *"git-verified"* header | Layer 1 implemented, FastAPI UI, ~25 test modules |
| Úire (76) | `stale-active` | Yes — but for a commit gap | `docs/roadmap.md`: "manifold empty, 0 scored" | 73-paper manifold, publishing nightly since 10 July |

The existing feature detects **status versus activity**. The failure mode above is **narrative versus activity**: the free-text `phase` and `notes` are dated reasoning records, they were accurate when written, and nothing tells a reader how far behind them the code has moved. Rian's phase string was 31 commits stale while its health flag was a clean `healthy`.

This is one flag, one column, and one field on the MCP projection. It reads `worklog`, like divergence does, and calls nothing external.

---

## <span style="color:#1D9E75">Part 1 — Schema</span>

Append to the `MIGRATIONS` list in `db.py`, next sequential numbers, idempotent, duplicate-column-tolerant, following the existing pattern.

```sql
-- 0NN — when the narrative fields were last authored
ALTER TABLE projects ADD COLUMN narrative_written_at TEXT;
```

### <span style="color:#0F6E56">Why `updated_at` will not do</span>

This is the crux of the brief. `updated_at` moves on **any** field change, so it does not track when the prose was authored. Concrete demonstration: on 2026-07-29 all three projects above had `phase` rewritten *and* had unrelated fields touched in the same session, so their `updated_at` now reads today and a naive "commits since `updated_at`" computes zero staleness for records that were badly stale an hour earlier. Worse, the failure is silent and self-erasing — any incidental field edit resets the apparent freshness of prose nobody re-read.

`narrative_written_at` must therefore be set in `routers/projects.py` / the MCP `update_project` path **only when `phase` or `notes` actually changes value**, compared against the stored value. Not on every update, not on the presence of the key in the request body.

`# TODO:` confirm how `update_project` currently sets `updated_at` and whether a value-comparison guard already exists anywhere in that path; reuse it rather than adding a second convention.

Backfill: set `narrative_written_at = updated_at` for existing rows. It will be wrong for records touched since their prose was written — including the three above — but it is the only defensible starting point, and it self-corrects on the next real narrative edit. Do not attempt to reconstruct authorship dates.

---

## <span style="color:#1D9E75">Part 2 — The flag</span>

Add one flag to `run_divergence()`. It composes with the existing flags rather than competing: a project can be `healthy` on activity and narrative-stale at the same time, which is precisely the Rian case.

| Flag | Condition |
|---|---|
| `narrative-stale` | commits in `worklog` dated after `narrative_written_at` exceeds `narrative_threshold` (default **15**) |

Notes on the rule:

- Reuse `_parse_commit_count` from `routers/commits.py` — the same pipe-delimited `[auto-git]` parsing divergence already depends on. Do not duplicate it and do not count rows.
- **Do not fold this into the single `health` column.** `health` holds one value and is already spoken for. This needs its own column (`narrative_health` or a plain integer `commits_since_narrative`), because the two axes are orthogonal and collapsing them loses the case that actually caused the problem.
- The threshold is a guess, not a derived figure. 15 commits is roughly where Rian's phase string became actively misleading. Expect to tune it; put it in the same config surface as `window_days`.
- Projects with `narrative_written_at IS NULL` are not flagged. Absence is not staleness.
- Log to `divergence_log` alongside the existing decisions, same shape.

---

## <span style="color:#1D9E75">Part 3 — Expose it on `list_projects` (the actual deliverable)</span>

Everything above is plumbing. This is the part that would have prevented the failure.

`list_projects` in `mcp_server.py` returns a deliberately lean projection — `notes` is excluded so the response doesn't truncate (see `cc-brief-list-projects-truncation.md`). Add **one integer** per row:

```
commits_since_narrative: <int>
```

One integer per project against a ~90-project list is a trivial payload change and does not threaten the truncation budget that motivated the lean projection.

Had `list_projects` returned `commits_since_narrative: 31` next to Rian's phase string, the correct move — open the repo — would have been obvious without reading a single line of prose. That is the whole value of this brief; the flag and the log are supporting structure.

Also surface it in `GET /api/projects` so the frontend can render it. A small "N commits since" annotation next to the phase text on the card is sufficient. `# TODO:` decide whether it earns a `.mcard` on the summary strip; the divergence PRD's argument against noise applies — probably not.

---

## <span style="color:#1D9E75">Part 4 — Tests</span>

Extend `tests/test_divergence.py`, following its existing temp-`RIALU_DB` pattern:

- `narrative_written_at` is set when `phase` changes value.
- `narrative_written_at` is **not** touched when `phase` is present in the request but identical to the stored value.
- `narrative_written_at` is **not** touched when only unrelated fields change (this is the regression that matters — it is the bug this brief exists to prevent).
- A project with commits after `narrative_written_at` above threshold flags `narrative-stale`.
- A project flagged `healthy` on activity can be simultaneously narrative-stale (orthogonality).
- `narrative_written_at IS NULL` does not flag.
- Commit counting parses pipe-delimited notes (3 commits in one row counts as 3).
- `list_projects` includes `commits_since_narrative` and the response does not truncate at full project count.

---

## <span style="color:#1D9E75">Non-goals</span>

- **No auto-generated prose. This is the most important non-goal in the brief.** The `notes` fields contain things no commit-message summariser could produce: that arXiv categories are venues rather than subjects; that the pull window must overlap because arXiv does not index the current day; that sorting on `updated` instead of the cutoff field dropped 158 of 159 announced revisions; that `relevance.py` was deleted leaving `relevance_score` orphaned in the DB and 1130 territory vectors orphaned on disk. That is the most valuable content in the registry, and generation would flatten it into changelog mush. The fields are not broken — they are dated records that lacked a discount rate. Add the signal; leave the prose alone.
- **No git-mcp or GitHub calls.** Commit data is in `worklog`. This is the reason the scheduled divergence job is reliable; keep it that way.
- **No changes to the existing flags** (`stale-active`, `no-trigger`, `healthy`, `dormant-ok`) or their rules. This is additive.
- **No writes from Rian.** Rian consumes Rialú's `status` as an independent input to `reconcile_lifecycle(chat_state, rialu_status, git_recent)`. If Rian wrote its reconciled output back to a field it reads, its own inference would become indistinguishable from Todd's declaration on the next run, and the loop would drift silently. Rialú detects; Rian reads. Not the reverse.
- No new tab, no new app, no new secret.

---

## <span style="color:#1D9E75">Acceptance criteria</span>

- [ ] Migration appended to `db.py`, idempotent on restart, existing rows backfilled from `updated_at`.
- [ ] `narrative_written_at` updates only on a real value change to `phase` or `notes`, via both the HTTP and MCP update paths.
- [ ] `run_divergence()` computes `narrative-stale` and logs to `divergence_log`.
- [ ] Commit counting reuses `commits.py` parsing.
- [ ] `commits_since_narrative` present in `list_projects` output; no truncation at full project count.
- [ ] `commits_since_narrative` present in `GET /api/projects`.
- [ ] `narrative-stale` is orthogonal to `health` — both can be set on one project.
- [ ] `tests/test_divergence.py` extended and passing; existing suite still green.

---

## <span style="color:#1D9E75">Notes for CC</span>

- **Read first:** `routers/divergence.py` (the shape to extend), `routers/commits.py` (`_parse_commit_count` — reuse), `db.py` (migration pattern), `mcp_server.py` (`list_projects` projection), `cc-brief-list-projects-truncation.md` (why that projection is lean).
- **Divergence is already built.** Do not re-implement it. This is one flag, one column, one projection field.
- **The `updated_at` trap is the whole design problem.** If you find yourself computing staleness from `updated_at`, stop — re-read Part 1. A field that resets on any incidental edit cannot measure prose age.
- **`status` is lifecycle, `runtime` is operational state** (migration 019). This brief touches neither.
- Three worked examples for manual verification are in the Context table above. Rian (75) with `commits_since_narrative` near zero after today's edit is the expected — and correct — post-backfill behaviour; the test suite, not the live data, is what proves the guard works.

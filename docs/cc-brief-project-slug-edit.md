# <span style="color:#2e7d32">CC Brief — project slugs: editable, with history, from one slugify</span>

**Date:** 2026-11-10
**Authored:** 2026-11-10, Claude (chat) for Todd
**Severity:** low for the rename itself; medium for what the slug silently keys
**Origin:** renaming Rialú id 49 to "Geall (watchdog)" on 2026-11-10 fixed the name and could not fix the slug
**Component:** `mcp_server.py` (`update_project`, `create_project`, `_slugify`), `routers/projects.py` (`ProjectUpdate`, `slugify`, `create_project`), `routers/github.py` (`_slugify`), `routers/spend.py` (`summary`), `db.py`
**Status:** ready

---

## <span style="color:#1565c0">1. Symptom</span>

Two projects are called Geall. Id 49 (June 2026, the watchdog PRD in `foxxelabs-config/geall`) holds the slug `geall`. Id 130 (October 2026, the norms programme, repo `todd427/geall`) was created second and got `geall-49`.

`update_project` can change `name` and seven other fields. It cannot change `slug`, over MCP or over `PUT /api/projects/{id}`. So the newer project, whose repo is actually named `geall`, cannot be given the slug that matches it.

`geall-49` is also a misleading string: the suffix is the id of the project that *already held* the slug, not the id of the row being created (`slug = f"{slug}-{existing['id']}"` in `routers/projects.py` and `mcp_server.py`). Project 130's slug names project 49.

## <span style="color:#1565c0">2. Why the slug is not cosmetic</span>

Read from the code at the head of `main` on 2026-11-10. The slug is a join key in five places:

| Where | What the slug does |
| --- | --- |
| `commit_worklog._map_commits_to_projects` | Maps an agent-reported repo name to a project by `slug` only (exact, lower-cased, then `-`/`_` swapped). No match means the repo's commits are skipped silently. |
| `routers/spend.py` `summary` | `project_spend.project_id` is a slug (TEXT, no foreign key). The budget lookup is `limits[slug]`. |
| `routers/projects.py`, `poller.py`, `routers/milestone_review.py` | Deploy matching: `LOWER(service_name) = p.slug`, and fuzzy `LIKE '%slug%'`. |
| `routers/github.py` | Repo linking: `html_url LIKE '%/' || p.slug`. |
| `agent/rialu-agent.py` | The tmux target of a Claude Code session is `faire-{slug}`. |

Two consequences follow.

**A rename has consumers.** Changing a slug changes which commits, which spend rows and which deployment a project matches. It needs a rule, not just a column in the `UPDATE`.

**Many current slugs do not match their repos.** `slugify` uses `\w`, which in Python 3 is Unicode-aware, so it keeps diacritics: `slugify("Dréacht")` is `dréacht`, checked by running the function. Repo names are ASCII. From `list_projects` on 2026-11-10, a non-exhaustive sample:

| Project | Slug | Repo basename |
| --- | --- | --- |
| Dréacht | `dréacht` | `dreacht` |
| Moncaí | `moncaí` | `moncai` |
| Síol | `síol` | `siol` |
| Bunús | `bunús` | `bunus` |
| Foghlaim | `foghliam` | `foghlaim` |
| Explore Ireland | `explore-islands` | `explore_ireland` |
| Eric | `eric` | `mark` |
| AfterWords | `afterwords` | `awords` |
| Geall (norms) | `geall-49` | `geall` |

**Inference, not verified:** by the mapping code, none of these repos' commits reach the auto-git worklog, so `commits_7d` reads zero for them however busy they are. I have read the code path and the registry, not the `worklog` table. Step 5's report measures it.

## <span style="color:#1565c0">3. What to build</span>

### <span style="color:#6a1b9a">3.1 One slugify</span>

There are three copies: `slugify` in `routers/projects.py`, `_slugify` in `mcp_server.py`, `_slugify` in `routers/github.py`. `docs/mcp-projects-crud-prd.md` asked for the copy; this brief reverses that. Put one function in a new module `slugs.py` and import it in all three places.

```python
# slugs.py
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SLUG_MAX = 64

_NO_DECOMPOSITION = {"ł": "l", "ø": "o", "đ": "d", "ð": "d", "þ": "th", "ß": "ss", "æ": "ae", "œ": "oe"}

def slugify(name: str) -> str:
    """ASCII slug. Folds diacritics (NFKD, drop combining marks), maps the
    letters NFKD cannot decompose, then drops anything still non-ASCII."""

def validate_slug(slug: str) -> str | None:
    """None if the slug is acceptable, else the reason as a sentence."""
```

`slugify` must return a string that `validate_slug` accepts, or the empty string. `slugify("Dréacht") == "dreacht"`, `slugify("Cło") == "clo"` (the registry holds that name with a Polish ł, which NFKD leaves alone, hence the map), `slugify("Geall (watchdog)") == "geall-watchdog"`, `slugify("explore_ireland") == "explore-ireland"`.

This changes slugs for **new** rows only. Existing rows keep their slugs until someone renames them (3.2), because of section 2.

### <span style="color:#6a1b9a">3.2 `slug` on update, over MCP and HTTP</span>

Add `slug: Optional[str] = None` to the MCP `update_project` tool and to `ProjectUpdate`.

- The value is validated with `validate_slug`. It is **not** passed through `slugify`: a caller who sends `Geall` gets an error saying slugs are lower-case ASCII, not a silently different slug.
- If another project holds the slug, refuse and name the holder. MCP: `{"error": "slug 'geall' is held by project 49 (Geall (watchdog))"}`. HTTP: 409 with the same sentence. No suffixing on update.
- Setting a project's slug to the value it already has is a no-op, not an error, and writes no history row.
- A slug change does not touch `narrative_written_at` (it is not prose) and does move `updated_at`.
- Update the tool docstring: say what a slug keys (commits, spend, deploy matching) in two lines, so the next caller knows a rename is not cosmetic.

There is no swap operation. Giving 130 the slug `geall` is two calls: 49 to `geall-watchdog`, then 130 to `geall`. The `UNIQUE` constraint makes any other order fail, which is the behaviour wanted.

### <span style="color:#6a1b9a">3.3 Slug history, so old slugs still resolve</span>

New table:

```sql
CREATE TABLE IF NOT EXISTS project_slug_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slug        TEXT NOT NULL,
    from_ts     TEXT NOT NULL,      -- UTC, datetime('now') form
    to_ts       TEXT                -- NULL while this is the project's current slug
);
CREATE INDEX IF NOT EXISTS idx_slug_history_slug ON project_slug_history(slug, from_ts);
```

- **Backfill** in `init_db`, idempotent: every project with no history row gets one, `slug` = its current slug, `from_ts` = `created_at`, `to_ts` NULL.
- **Create** (all three create paths) writes the opening row in the same transaction as the insert.
- **Rename** closes the open row (`to_ts = datetime('now')`) and opens a new one, in the same transaction as the `UPDATE projects`.
- One resolver in `slugs.py`: `project_for_slug(conn, slug, at: str | None) -> int | None`. With `at`, the project whose row covers that instant (`from_ts <= at < to_ts`, open row has no upper bound). Without `at`, the current holder.

Then change `routers/spend.py` `summary` to resolve each rollup through `project_for_slug(conn, row.project_id, row.window_start)` and aggregate under the resolved project's **current** slug. Rows that resolve to nothing stay reported under the raw string they arrived with, as now (the accept-not-reject rule in `docs/suim-spend-rollup-receiver-prd.md` stands). `project_spend` rows are not rewritten.

**Why history and not a rewrite of `project_spend` on rename.** A rewrite is one `UPDATE` and no new table. It fails in two ways. Suim re-sends a rollup under the identical `rollup_key` after a lost ack, and the upsert sets `project_id = excluded.project_id`, which puts the old slug back. And a freed slug can be claimed by another project, which is exactly the Geall case: after the two renames `geall` means project 130, and any earlier spend row under `geall` belonged to 49. Only a dated record tells them apart. The cost is one table, one resolver and one changed query.

The resolver is deliberately not wired into commit mapping or deploy matching in this brief (section 6).

### <span style="color:#6a1b9a">3.4 Create: honest collision suffix, optional explicit slug</span>

- MCP `create_project` and `ProjectIn` gain `slug: Optional[str] = None`. Given, it is validated and a collision is refused as in 3.2. Omitted, it is derived with `slugify(name)`.
- When a **derived** slug collides, suffix with the **new** row's id, not the holder's: insert with a temporary unique slug, then set `{slug}-{own id}` in the same transaction. The created record carries `slug_collision_with: <holder id>` so the caller sees it happened.
- `routers/github.py` keeps its `-gh` and `-new` suffixes for now but derives through the shared `slugify`. If `{slug}-gh` or `{slug}-new` is itself taken, fall back to `{slug}-{own id}` instead of raising on the `UNIQUE` constraint, which is what the current code would do.
- If `slugify(name)` is empty (a name with no ASCII letters or digits after folding), derive `project-{own id}`.

### <span style="color:#6a1b9a">3.5 A report, read-only</span>

`GET /api/projects/slug-report` (declared before `/{project_id}`, as `/census` is, and for the same reason) and an MCP tool `slug_report()`. One row per project that has at least one of:

| Flag | Meaning |
| --- | --- |
| `non_ascii` | `validate_slug(slug)` fails |
| `repo_mismatch` | `repo_url` is a GitHub URL and the current commit-mapping logic would not map its basename to this project (run the real matching rules from `commit_worklog`, do not reimplement them loosely) |
| `shared_repo` | another project has the same `repo_url` basename |
| `suffix_slug` | the slug ends in `-<digits>`, `-gh` or `-new` |

Each row: `id`, `name`, `slug`, `repo` (basename or null), `flags`, `suggested` (`slugify` of the repo basename when there is one and no other project wants it, else `slugify(name)`, else null), `autogit_rows_30d` (count of `[auto-git]` worklog rows for the project in the last 30 days) and `spend_rows` (count of `project_spend` rows whose `project_id` equals the current slug). Plus a top-level `counts` by flag.

The report changes nothing. Todd decides which slugs to rename, with the last two columns showing what each rename would carry. Keep the row lean; this goes over MCP and `list_projects` has already been bitten by payload size (`docs/cc-brief-list-projects-truncation.md`).

## <span style="color:#1565c0">4. Tests</span>

New file `tests/test_slugs.py`, plus additions to `tests/test_mcp_update_project.py`, `tests/test_mcp_projects_crud.py`, `tests/test_projects.py`, `tests/test_spend.py`, `tests/test_github.py`.

1. `slugify` folds: the four examples in 3.1, and an all-non-ASCII name returns `""`.
2. `validate_slug` rejects upper case, diacritics, leading/trailing/double hyphens, underscores, the empty string and 65 characters; accepts `a`, `geall-49`, 64 characters.
3. MCP update with a valid free slug changes it, moves `updated_at`, leaves `narrative_written_at` alone.
4. MCP update with a held slug returns an error naming the holder's id and name; nothing changes.
5. MCP update with an invalid slug returns the validator's sentence; nothing changes.
6. Same-value update is a no-op and writes no history row.
7. HTTP `PUT` mirrors 3 to 6, with 409 for the collision and 422 or 400 for the invalid value (pick one, state it in the docstring).
8. The two-step Geall rename: A holds `x`, B holds `x-1`; A to `x-old`, then B to `x`, succeeds; B to `x` first fails.
9. History: backfill is idempotent across two `init_db()` calls; create opens a row; rename closes one and opens one; `project_for_slug` returns the old holder for an instant before the rename, the new holder after, and the current holder with no `at`.
10. Spend summary: a rollup under an old slug with a window before the rename is aggregated under the project's current slug and gets that project's `cost_limit_hr`. After the freed slug is claimed by a second project, an old-window rollup still resolves to the first project and a new-window rollup to the second. An unknown slug is still reported as it arrived.
11. Spend re-send: posting the same `rollup_key` twice after a rename does not change which project the summary attributes it to.
12. Create collision: the derived slug of the second project is `{slug}-{its own id}` and the record carries `slug_collision_with`. Explicit colliding slug on create is refused.
13. GitHub import: `-gh` already taken falls back to `{slug}-{own id}` and does not raise.
14. Report: a fixture with one project per flag returns each flag once, `counts` adds up, and a clean project is absent.
15. The existing suite stays green. `test_create_project_slug_collision` will need its expected suffix changed; say so in the commit.

## <span style="color:#1565c0">5. Acceptance</span>

- All of section 4 passes locally.
- Deployed, and then verified by **authenticated MCP calls**, not by `/health`: `slug_report()` returns; `update_project(49, slug="geall-watchdog")` then `update_project(130, slug="geall")` both succeed; `get_project(130)["slug"] == "geall"`. A conversion is not verified until an authenticated call succeeds (the rule recorded on the foxxe-mcp project after ainm's 421).
- The report's output is pasted into the commit body or a short `docs/slug-report-2026-11.md`, so the section 2 inference is replaced by a measurement.
- This brief's `**Status:**` line is set to `done`, and `CLAUDE.md` gains one line for `slugs.py`.

## <span style="color:#1565c0">6. Not in this brief</span>

- **Renaming any existing slug other than the two Geall ones.** The report is the input to that decision and the decision is Todd's.
- **Commit mapping keyed on `repo_url`.** Mapping a repo to a project by slug is the wrong key: the project already records its `repo_url`, and `routers/briefs.py` already has `repo_full_name()` to parse it. Keying `commit_worklog` on the repo basename would fix every `repo_mismatch` row without renaming anything. It is left out because several projects share one repo (`anseo`: Anseo, Litir, Stór; `foxxelabs-config`: Coinne, Dialann, Geall (watchdog), Tairseach) and which project a shared repo's commits belong to needs a rule from Todd. The `shared_repo` flag sizes that question. This is the follow-on brief.
- **Deploy matching.** The fuzzy `LIKE '%slug%'` match means a short slug such as `geall` matches any service whose name contains it. Not changed here; note it in the report doc if a rename changes a project's matched deployment.
- **Suim.** Suim decides which slug a rollup is sent under. Nothing here tells Suim about a rename; the history table is what keeps old and new slugs both resolving. If Suim needs a slug map, that is a brief in `todd427/suim`.
- **Running Claude Code sessions.** A session started before a rename keeps its tmux target `faire-{old slug}`. `stop_cc_session` already accepts an explicit `target`. No change.
- **A slug field in the web UI.** API and MCP only.

## <span style="color:#1565c0">7. Notes for the implementer</span>

- Do not auto-migrate existing slugs to their folded form in `init_db`. It looks tidy and it would change, in one deploy, which commits and spend rows match several dozen projects.
- Do not make `update_project` slugify its `slug` argument. Refuse and explain.
- `slug` stays `NOT NULL UNIQUE`. No schema change to `projects`.
- `from_ts` and `to_ts` use the same `datetime('now')` text form as the rest of the schema; Suim's `window_start` is ISO-8601 with a `T`. Compare parsed datetimes in the resolver (`routers/spend.py` already has `_parse_dt`), not strings.
- If anything in section 2's table turns out wrong when you read the code closely, say so in the commit and fix the brief; the table was written from a grep and five file reads.

---

*Rialú · 2026-11-10*

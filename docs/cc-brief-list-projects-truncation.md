# CC Brief — `list_projects` under-reports project count (response truncation)

**Date:** 2026-07-15
**Severity:** medium — silently corrupts every programmatic read of the registry
**Origin:** FoxxeLabs full-corpus audit, 2026-07-15 (`foxxelabs-config/docs/audit/2026-07-corpus-audit.md`, Task 1)
**Component:** `mcp_server.py` → `list_projects` tool

---

## Symptom

`list_projects` (MCP) returns fewer projects than the Rialú UI shows. Observed: **~57 via the tool vs 76 in the UI** during an audit session. Because the count is used as ground truth by downstream consumers, the whole portfolio picture came out wrong.

## Root cause — NOT a query bug

The query is correct and returns **all** rows:

```sql
SELECT id, name, slug, phase, status, platform, repo_url, site_url, machine, notes, updated_at
FROM projects ORDER BY updated_at DESC
```

No `LIMIT`, no `WHERE`, no pagination. Re-running the tool this session returned all **76** rows, matching the UI. **The server is fine.**

The real fault is **response size**. The projection includes the full free-text `notes` column for every project, producing a **~100 KB payload** (measured 101,606 characters). A response that large gets **truncated in the client** rendering/consuming it — and a truncated render counted by hand lands around ~57. (Corroboration: an MCP client consuming this exceeded its token limit on the response and had to spill it to a file — independent evidence of the size-truncation mechanism, not a row-count limit.)

## Why it matters

Every programmatic consumer of the registry — Eric, scheduled/CC audit runs, any script calling `list_projects` — silently receives a **partial list with no error**. This is precisely the "three sources, three different counts" failure that triggered the audit.

## Fix

1. **Lean projection in `list_projects`.** Drop or clip `notes` from the list response; keep `id, name, slug, status, phase, platform, repo_url, site_url` (and `updated_at`). Detail (including full `notes`) is already available via `get_project(project_id)`.
2. **Optional:** add `limit` / `offset` params for explicit paging, defaulting to "all".
3. **Regression test:** assert `len(list_projects()) == SELECT COUNT(*) FROM projects` (a fixture with N > payload-truncation-threshold projects, or a direct count comparison), so a future re-bloat of the row shape fails CI instead of silently under-reporting.

## Acceptance

- `list_projects` returns all rows regardless of registry size, with a payload small enough not to truncate in standard MCP clients.
- Full `notes` remains reachable via `get_project`.
- A test guards `count(list_projects) == count(projects)`.

## Notes for the implementer

- Do **not** add a `LIMIT` as the fix — that would cap the count, the opposite of what's needed. The fix is payload shrinkage, not row limiting.
- `get_project`, `create_project`, `update_project` are unaffected — they already return single records.

---

*FoxxeLabs Limited · 2026-07-15*

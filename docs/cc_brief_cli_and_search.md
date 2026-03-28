# CC Brief — Rialú: Keys search, sort, and CLI

**Date:** 2026-03-28
**Repo:** `/home/Projects/rialu`
**Priority:** Medium

---

## Context

The Rialú key vault currently has 45+ keys with no search or sort in the UI,
and no CLI for machine-level access (scripts, cron jobs, CC itself).

This brief covers three additions:
1. Search + sort in the Keys UI
2. A `rialu` CLI tool (Python, installable, GitHub CLI-style auth)

---

## Part 1 — Keys UI: search and sort

### Backend

Add query params to `GET /api/keys`:

```
GET /api/keys?q=azure&sort=name&order=asc
```

- `q` — case-insensitive substring match on `name`, `provider`, `env_var`, `notes`
- `sort` — one of: `name` (default), `provider`, `created_at`, `updated_at`
- `order` — `asc` (default) or `desc`

Update `routers/keys.py` → `list_keys()` to accept and apply these params.
SQLite supports `LOWER()` and `ORDER BY` natively — no ORM needed.

### Frontend (`static/index.html`)

In the Keys section, add above the table:
- A search input (debounced 200ms, calls the API with `?q=`)
- A sort dropdown: Name A–Z / Name Z–A / Provider / Newest / Oldest
- Show result count: "Showing 12 of 45 keys"

The existing table should re-render on search/sort change without page reload.
Use the existing fetch pattern already in the SPA — no new framework needed.

---

## Part 2 — Rialú CLI

### Overview

A `rialu` CLI tool installable from the repo, GitHub CLI-style:

```bash
rialu auth login          # store credentials
rialu auth status         # show who's logged in
rialu key list            # list all keys (metadata only)
rialu key list --search azure
rialu key list --sort provider
rialu key get <NAME>      # reveal key value by name
rialu key set <NAME>      # prompt for value, store/update
rialu key env             # output all keys as export KEY=value (for sourcing)
```

### Auth

Auth uses the same Cloudflare Access service token mechanism:
- `rialu auth login` — prompts for CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET
- Stores them in `~/.config/rialu/credentials.json` (chmod 600)
- `rialu auth status` — reads and validates stored creds against `/api/health`

No browser OAuth flow needed — machine credentials only.

### Implementation

- Single file: `cli/rialu` (already exists as a stub — check current contents)
- Pure Python 3, stdlib only (`urllib`, `json`, `getpass`, `argparse`, `pathlib`)
- No `requests`, no `click`, no external deps
- Shebang: `#!/usr/bin/env python3`
- Installation: `ln -s /home/Projects/rialu/cli/rialu ~/.local/bin/rialu`

### Key commands in detail

**`rialu key list`**
```
NAME                     PROVIDER       HINT    ENV VAR
Azure Speech API         Microsoft      ••X17T  AZURE_SPEECH_KEY
azure-speech-region      microsoft      ••rope  AZURE_SPEECH_REGION
anthropic-api-key        anthropic      ••dAAA  ANTHROPIC_API_KEY
...
```
Supports `--search <term>` and `--sort name|provider|created|updated`.

**`rialu key get <NAME>`**
```bash
rialu key get "Azure Speech API"
# → prints value to stdout (so it can be captured with $())
AZURE_KEY=$(rialu key get "Azure Speech API")
```

**`rialu key env`**
```bash
source <(rialu key env)
# Sources all keys with env_var set as export statements
```
Useful in cron jobs instead of hardcoding credentials.

**`rialu key set <NAME>`**
```
Provider [Microsoft Azure]:
Value: ••••••••••••  (hidden input via getpass)
Notes (optional):
Saved.
```
Uses `PUT /api/keys/{id}` if key exists, `POST /api/keys` if new.

### Error handling
- Missing credentials → `rialu auth login` prompt
- 403 Forbidden → "CF Access token rejected. Run: rialu auth login"
- 404 Not found → "Key '<NAME>' not found. Use: rialu key list"
- Network error → clean message, exit 1

---

## File changes

| File | Change |
|---|---|
| `routers/keys.py` | Add `q`, `sort`, `order` query params to `list_keys()` |
| `static/index.html` | Add search input + sort dropdown to Keys section |
| `cli/rialu` | Full implementation (replace stub) |

---

## Testing

Existing tests in `tests/test_keys.py` — add cases for:
- `GET /api/keys?q=azure` returns only matching keys
- `GET /api/keys?sort=provider&order=desc` returns correct order
- `GET /api/keys?q=nonexistent` returns empty list (not 404)

CLI has no automated tests — manual smoke test is sufficient:
```bash
rialu auth login
rialu key list
rialu key list --search azure
rialu key get "Azure Speech API"
```

---

## Done when

- [ ] Search + sort working in the UI
- [ ] `rialu key list` works from Daisy terminal
- [ ] `rialu key get "Azure Speech API"` returns the key value
- [ ] `rialu key env` can be sourced in a script
- [ ] Credentials stored at `~/.config/rialu/credentials.json`

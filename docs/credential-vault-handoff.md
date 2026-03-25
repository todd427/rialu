# Rialú — Credential Vault — Handoff Context

**For Claude Code — read this before touching anything.**

---

## What this adds

A **Credentials** tab in the Rialú SPA: a personal password manager backed by the existing AES-256-GCM key vault infrastructure. Single user (Todd). No sharing, no teams.

This is distinct from the existing Key Vault (API keys / env vars). Credentials are human-facing login records: email, username, password, URL, notes.

---

## Data model

New table: `credentials`

```sql
CREATE TABLE IF NOT EXISTS credentials (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,           -- human name, e.g. "Google Cloud Console"
    url         TEXT,                    -- site URL
    username    TEXT,                    -- email or username
    password    TEXT NOT NULL,           -- AES-256-GCM encrypted (same key_vault.py as key_store)
    notes       TEXT,
    tags        TEXT,                    -- comma-separated, e.g. "google,oauth,work"
    strength    INTEGER,                 -- 0-4 zxcvbn score, computed on write
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);
```

Reuse `encrypt_key` / `decrypt_key` from `key_vault.py`. No new crypto.

---

## Router

New file: `routers/credentials.py`

Endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/credentials` | List all — metadata only (no plaintext password) |
| POST | `/api/credentials` | Create new |
| GET | `/api/credentials/{id}` | Get one — includes decrypted password |
| PUT | `/api/credentials/{id}` | Update |
| DELETE | `/api/credentials/{id}` | Delete |
| POST | `/api/credentials/generate` | Generate password (see below) |
| POST | `/api/credentials/{id}/check-strength` | Re-evaluate strength of existing |

`GET /api/credentials` returns `strength`, `hint` (last 4 chars), `username`, `label`, `url`, `tags` — never the plaintext password.

`GET /api/credentials/{id}` returns plaintext password for copy/paste. Log to `key_audit_log` (action: `'revealed'`, detail: `'credential via API'`).

---

## Password strength

Use **zxcvbn** (Python: `zxcvbn-python` package). Score 0–4. Store in the `strength` column on create/update. Return in list and detail responses.

Display in SPA:

- 0 = Very Weak (red)
- 1 = Weak (orange)
- 2 = Fair (yellow)
- 3 = Strong (green)
- 4 = Very Strong (blue)

---

## Password generation

`POST /api/credentials/generate` — body: `{ "mode": "phrase" | "random", "hint": "optional note" }`

### Mode: `phrase` (preferred default)

Generate a **memorable passphrase** from a short dictionary word + number substitutions + symbol. Pattern:

- Pick a common short word (4–6 letters) from a small embedded wordlist
- Apply leet-ish substitutions: e→3, a→4, i→1, o→0, s→$, etc. — but sparingly, not every letter
- Append a 1–2 digit number + a symbol from `@!#%`

Examples of the style (not hardcoded — generated):
- `G3ni34m@` (Genie + 4 Matt → `@`)
- `C0br4z!7`
- `P4nth3r#2`

Target: memorable but not a dictionary word, 8–12 chars, score ≥ 3.

Endpoint returns 3 candidates so the user can pick one.

### Mode: `random`

Standard `secrets.token_urlsafe`-based, 16 chars. For when memorability doesn't matter.

---

## SPA — Credentials tab

Add a **Credentials** tab alongside Projects, Worklog, etc.

Layout:
- Search/filter bar (searches label, username, url, tags)
- Tag filter pills
- Card list — each card shows: label, username (truncated), URL favicon if available, strength badge, tags
- **Copy password** button — one click, no modal — calls `GET /api/credentials/{id}`, copies to clipboard, clears clipboard after 30 seconds
- **Add / Edit** inline form (expand-in-place, same UX pattern as rest of SPA)
- **Generate** button inside Add form — calls generate endpoint, shows 3 phrase candidates as clickable chips; clicking one fills the password field

No password is ever displayed in plain text in the UI. Copy-only.

---

## Tests

Add `tests/test_credentials.py`. Cover:

- CRUD round-trip (create, list, retrieve decrypted, update, delete)
- Strength score is computed and stored on write
- Generate endpoint returns 3 candidates (phrase mode), all score ≥ 2
- Revealed passwords logged to audit trail
- List endpoint never returns plaintext

---

## Migration

Add the `credentials` table creation to `init_db()` in `db.py`. No migration file needed — SQLite, `CREATE TABLE IF NOT EXISTS` pattern already used throughout.

---

## What NOT to change

- `fly.toml` — do not touch
- `Dockerfile` — do not touch
- `key_vault.py` — do not touch (reuse as-is)
- `docs/rialu-prd-v1.md` — do not touch
- Existing test fixtures in `conftest.py`

---

## Dependencies to add to `requirements.txt`

```
zxcvbn-python
```

---

*Context written by Claude (claude.ai) — 2026-03-25*

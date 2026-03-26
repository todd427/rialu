# Rialú — Secrets Wizard — Handoff Context

**For Claude Code — read this before touching anything.**

---

## What this is

A **Secrets Wizard** tab in the Rialú SPA.

The wizard guides Todd step-by-step through obtaining any secret from any external provider — one screen at a time, no cognitive overhead. At the end of each flow, the secret is stored directly into the Rialú key vault. No copying to a text file, no losing track of which client is which.

The wizard is driven by a **recipe library** — structured JSON recipes, one per provider/secret type. Recipes know the exact navigation path, what to look for on each screen, what to copy, and what env var it maps to. New recipes can be added without touching application code.

---

## Problem statement

Getting secrets from external vendors involves:
- Navigating unfamiliar console UIs (Google Cloud, Fly.io, Railway, Cloudflare, etc.)
- Multiple screens with non-obvious naming ("Service Account" vs "OAuth Client" vs "API Key")
- Credentials that expire, rotate, or only display once
- No guidance on which of several similar-looking items is the right one
- The secret landing somewhere temporary (clipboard, text file, forgotten terminal)

The wizard eliminates all of this.

---

## Recipe library

Recipes live in `wizard_recipes/` as JSON files, one per secret type.

### Recipe schema

```json
{
  "id": "google-oauth-web-client",
  "name": "Google OAuth 2.0 — Web Application Client",
  "provider": "Google",
  "description": "Creates or retrieves a Web Application OAuth client secret from Google Cloud Console.",
  "env_var": "GOOGLE_CLIENT_SECRET",
  "tags": ["google", "oauth", "web"],
  "prereqs": [
    "A Google Cloud project must already exist.",
    "The OAuth consent screen must be configured (APIs & Services → OAuth consent screen)."
  ],
  "steps": [
    {
      "id": "open-console",
      "title": "Open Google Cloud Console",
      "instruction": "Go to https://console.cloud.google.com — make sure you're in the right project (check the project name in the top bar).",
      "url": "https://console.cloud.google.com",
      "screenshot_hint": "Top bar shows project name. If wrong, click it and switch."
    },
    {
      "id": "navigate-credentials",
      "title": "Navigate to Credentials",
      "instruction": "In the left sidebar: APIs & Services → Credentials.",
      "screenshot_hint": "You'll see a list of API Keys, OAuth 2.0 Client IDs, and Service Accounts."
    },
    {
      "id": "find-client",
      "title": "Find your OAuth client",
      "instruction": "Under 'OAuth 2.0 Client IDs', find the client named for your app (type: Web application). If it doesn't exist yet, click '+ CREATE CREDENTIALS' → 'OAuth client ID' → 'Web application'.",
      "screenshot_hint": "Do NOT select 'Desktop app' or 'Service account' — you want 'Web application'."
    },
    {
      "id": "get-secret",
      "title": "Copy the Client Secret",
      "instruction": "Click the client name to open it. You'll see 'Client ID' and 'Client Secret'. Copy the Client Secret. If you can't see it, click the eye icon or 'RESET SECRET' to generate a new one (this invalidates any existing secret).",
      "input_label": "Paste your Client Secret here",
      "input_type": "secret",
      "warning": "If you reset the secret, any existing deployments using the old secret will break until updated."
    },
    {
      "id": "also-grab-client-id",
      "title": "Also copy the Client ID",
      "instruction": "While you're here — copy the Client ID too. It's not secret but you'll need it.",
      "input_label": "Paste your Client ID here (optional)",
      "input_type": "text",
      "env_var_override": "GOOGLE_CLIENT_ID",
      "optional": true
    }
  ],
  "vault_entry": {
    "name_template": "{app}-google-client-secret",
    "provider": "Google",
    "notes": "Web application OAuth 2.0 client secret."
  }
}
```

### Initial recipe library

Ship with recipes for:

| ID | Provider | Secret |
|----|----------|--------|
| `google-oauth-web-client` | Google | OAuth Web Client Secret |
| `google-oauth-desktop-client` | Google | OAuth Desktop Client Secret |
| `fly-api-token` | Fly.io | Personal API token |
| `fly-app-secret` | Fly.io | App-level secret (via `fly secrets set`) |
| `railway-api-token` | Railway | Account API token |
| `anthropic-api-key` | Anthropic | API key |
| `cloudflare-api-token` | Cloudflare | Scoped API token |
| `cloudflare-access-service-token` | Cloudflare | Access Service Token (client ID + secret) |
| `github-pat` | GitHub | Personal Access Token (classic or fine-grained) |
| `openai-api-key` | OpenAI | API key |
| `elevenlabs-api-key` | ElevenLabs | API key |

---

## SPA — Secrets Wizard tab

### Entry screen

- Search/filter recipes by provider or tag
- Card grid — one card per recipe (icon, name, one-line description)
- "Add a new recipe" button (opens recipe editor — Phase 2)

### Wizard flow (per recipe)

One step at a time. No scrolling to see what's next. Full attention on the current step.

Layout per step:
- **Step counter**: "Step 2 of 5"
- **Title**: large, clear
- **Instruction**: plain English — what to look for, where to click, what to avoid
- **URL button** (if step has a URL): opens in new tab
- **Screenshot hint**: subtle italic note below instruction — extra orientation for confusing UIs
- **Warning** (if present): amber callout box
- **Input field** (if step has `input_type`):
  - `secret`: masked field + paste button + reveal toggle
  - `text`: plain text field
  - `optional` steps can be skipped
- **Prereqs** shown on first step if recipe has them
- **Back / Next / Skip** navigation

### Completion screen

- Summary of what was captured
- "Store in Rialú vault" button — one click, calls `store_key` for each captured secret
- Confirmation of storage with vault hint shown
- Option to also copy the secret to clipboard
- "Run another wizard" shortcut

---

## Backend

### New router: `routers/wizard.py`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/wizard/recipes` | List all recipes (metadata only) |
| GET | `/api/wizard/recipes/{id}` | Full recipe with steps |
| POST | `/api/wizard/recipes` | Create a new recipe (Phase 2) |
| PUT | `/api/wizard/recipes/{id}` | Update a recipe (Phase 2) |

Recipes loaded from `wizard_recipes/*.json` at startup. No DB table needed — filesystem is the source of truth.

### Vault integration

Wizard completion calls the existing `store_key` / `update_key` logic from `key_vault.py` directly (not via HTTP round-trip). Reuse the same audit log.

---

## AI-assisted fallback (Phase 2)

For providers not yet in the recipe library, add a **"Guide me anyway"** mode:

- Todd types the provider name and what secret he needs
- Rialú calls the Anthropic API with a structured prompt: "Give me step-by-step instructions for obtaining [secret] from [provider], formatted as wizard steps."
- Response rendered as a live wizard — same step-by-step UI
- After completing, Todd can save the flow as a named recipe for next time

This makes the wizard self-extending without requiring manual recipe authoring.

---

## Tests

Add `tests/test_wizard.py`. Cover:

- Recipe list endpoint returns all recipes in `wizard_recipes/`
- Recipe detail endpoint returns steps
- Invalid recipe ID returns 404
- Recipes are valid JSON matching schema (required fields present)

---

## File layout

```
rialu/
  wizard_recipes/
    google-oauth-web-client.json
    fly-api-token.json
    railway-api-token.json
    anthropic-api-key.json
    cloudflare-api-token.json
    cloudflare-access-service-token.json
    github-pat.json
    openai-api-key.json
    elevenlabs-api-key.json
  routers/
    wizard.py       ← new
```

---

## What NOT to change

- `fly.toml` — do not touch
- `Dockerfile` — do not touch
- `key_vault.py` — do not touch
- `docs/rialu-prd-v1.md` — do not touch
- Existing test fixtures in `conftest.py`

---

## Design principles

- **One thing at a time.** Never show the next step until the current one is done.
- **Plain English.** No jargon. If the screen says "OAuth 2.0 Client ID", explain what that means in the instruction.
- **Warn before destructive actions.** Resetting a secret, revoking a token — always warn first.
- **Nothing lives in limbo.** Every wizard flow ends with the secret in the vault or explicitly discarded. No clipboard orphans.

---

*Context written by Claude (claude.ai) — 2026-03-26*

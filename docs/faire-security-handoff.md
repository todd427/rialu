# Faire ↔ Rialú Security Hardening — Feature Spec

**Author:** Claude (Faire session) | **Date:** 2026-03-25 | **Priority:** High
**Relates to:** Faire desktop app (`todd427/faire`), Rialú API (`todd427/rialu`)

---

## Problem

Faire currently connects to `rialu.fly.dev` — the raw Fly.io URL that **bypasses Cloudflare entirely**. To make this work, the `CanonicalHostMiddleware` in `main.py` has a bypass that allows all `/api/` and `/ws/` paths through on any host. This bypass:

- Keeps getting overwritten by other Rialú sessions that rightly tighten the middleware
- Exposes all API endpoints without Cloudflare Access protection
- Means anyone who knows the `fly.dev` URL can hit the API directly
- Bypasses Cloudflare WAF, DDoS protection, and rate limiting

A shared Bearer token (`FAIRE_WS_TOKEN`) provides minimal application-layer auth, but it's not real identity-based authentication.

## Solution

Switch Faire to connect through `rialu.ie` using a **Cloudflare Access Service Token** for machine-to-machine authentication.

---

## Implementation

### 1. Create Cloudflare Access Service Token

**Where:** Cloudflare Zero Trust dashboard → Access → Service Auth → Create Service Token

**Output:** Two values:
- `CF-Access-Client-Id` (UUID)
- `CF-Access-Client-Secret` (long random string)

**Store in Rialú vault:**
```
Name: faire-cf-access-client-id
Env: CF_ACCESS_CLIENT_ID
Provider: cloudflare

Name: faire-cf-access-client-secret
Env: CF_ACCESS_CLIENT_SECRET
Provider: cloudflare
```

### 2. Rialú middleware changes (`main.py`)

**Remove** the `/api/` and `/ws/` bypass from `CanonicalHostMiddleware`:

```python
# REMOVE this line:
if path.startswith("/api/") or path.startswith("/ws/"):
    return await call_next(request)
```

The middleware should enforce `rialu.ie` host for everything except:
- `/api/health` — Fly.io internal monitoring
- `/mcp` + OAuth endpoints — MCP has its own auth
- `TEST_MODE` — local development

**Keep the existing Bearer token check** (`verify_faire_token` in `auth.py`) as defence-in-depth on sensitive endpoints (decisions, agents list, timeline).

**Keep HMAC** (`verify_hmac`) on agent ingestion endpoints.

### 3. Faire configuration (`~/.rialu/faire.env`)

Add CF Access credentials:

```bash
RIALU_BASE_URL=https://rialu.ie
FAIRE_WS_TOKEN=<existing token>
CF_ACCESS_CLIENT_ID=<from step 1>
CF_ACCESS_CLIENT_SECRET=<from step 1>
RIALU_AGENT_KEY=<existing key>
```

### 4. Faire frontend changes (`src/main.js`, `src/config.js`)

**config.js** — add CF Access fields:
```javascript
window.FAIRE_CONFIG = {
  RIALU_BASE_URL: 'https://rialu.ie',
  FAIRE_WS_TOKEN: '<token>',
  CF_ACCESS_CLIENT_ID: '<client-id>',
  CF_ACCESS_CLIENT_SECRET: '<client-secret>',
};
```

**main.js** — update `api()` function to include CF headers:
```javascript
async function api(path, opts = {}) {
  const headers = { ...opts.headers };
  if (CONFIG.wsToken) headers['Authorization'] = `Bearer ${CONFIG.wsToken}`;
  if (CONFIG.cfClientId) {
    headers['CF-Access-Client-Id'] = CONFIG.cfClientId;
    headers['CF-Access-Client-Secret'] = CONFIG.cfClientSecret;
  }
  // ... fetch
}
```

**WebSocket** — CF Access Service Tokens work with WebSocket upgrade requests. The headers are sent during the HTTP upgrade handshake:
```javascript
// Note: browser WebSocket API doesn't support custom headers.
// Workaround: pass CF token as a query parameter or use a proxy.
// See "WebSocket auth" section below.
```

### 5. WebSocket auth challenge

The browser `WebSocket` API **does not support custom headers**. This means we can't send `CF-Access-Client-Id` on the WS upgrade request directly.

**Options (choose one):**

**A. CF Access cookie approach:**
- On Faire startup, make one authenticated fetch to `rialu.ie` with the CF Service Token headers
- Cloudflare returns a `CF_Authorization` cookie
- The WebSocket connection to `wss://rialu.ie/ws/{token}` automatically includes this cookie
- Simplest approach, requires one pre-flight request

**B. Token-in-URL approach (current):**
- Keep the WS token in the URL path: `/ws/{token}`
- Cloudflare Access must be configured to exclude `/ws/` paths from service auth
- The application-layer token provides auth instead
- Less secure (token in URL visible in logs) but pragmatic

**C. Proxy approach:**
- Add a Rialú endpoint `GET /api/ws-ticket` that returns a one-time ticket
- Faire fetches ticket (with CF headers), then connects WS with ticket as token
- Most secure but most complex

**Recommendation:** Option A (cookie approach) — simplest and fully secure.

### 6. Faire Tauri changes (`src-tauri/src/lib.rs`)

Update `get_config` to read the new env vars:
```rust
for key in &["RIALU_BASE_URL", "FAIRE_WS_TOKEN", "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET", "RIALU_AGENT_KEY"] {
    if let Ok(val) = std::env::var(key) {
        config.insert(key.to_string(), val);
    }
}
```

Update `spawn_dream` to pass CF headers to cc_wrapper (or cc_wrapper reads them from env).

### 7. cc_wrapper changes (`agent/cc_wrapper.py`)

If cc_wrapper runs on Daisy (not on Fly.io), it needs CF Access headers too:

```python
def _cf_headers(self) -> dict:
    client_id = os.environ.get("CF_ACCESS_CLIENT_ID", "")
    client_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")
    if client_id and client_secret:
        return {
            "CF-Access-Client-Id": client_id,
            "CF-Access-Client-Secret": client_secret,
        }
    return {}
```

Add these headers to every `httpx` request alongside the existing HMAC signature.

### 8. rialu-agent changes

The rialu-agent on Daisy sends heartbeats and events to Rialú. If connecting through `rialu.ie`, it also needs CF Access headers. Add to agent config:

```yaml
# ~/.rialu/agent.yaml
rialu_base: https://rialu.ie
cf_access_client_id: <from step 1>
cf_access_client_secret: <from step 1>
```

**Alternative:** If rialu-agent runs on Fly.io (same network), it can use `rialu.internal` or the Fly.io private IP — no Cloudflare needed for internal traffic.

---

## Security layers (after implementation)

```
Faire desktop (Daisy)
  │
  ├─ REST: GET/POST https://rialu.ie/api/*
  │   → CF Edge: DDoS + WAF + rate limiting
  │   → CF Access: Service Token validation (CF-Access-Client-Id/Secret)
  │   → Rialú app: Bearer token check (Authorization header)
  │   → Endpoint handler
  │
  ├─ WebSocket: wss://rialu.ie/ws/{token}
  │   → CF Edge: DDoS + WAF
  │   → CF Access: Cookie auth (from pre-flight) or excluded path
  │   → Rialú app: Token validation in faire_hub.connect()
  │   → Persistent connection
  │
  └─ Agent events: POST https://rialu.ie/api/agents/{id}/event
      → CF Edge + CF Access (same as REST)
      → Rialú app: HMAC-SHA256 signature verification
      → Event stored + broadcast

rialu-agent (Daisy, local)
  │
  └─ Heartbeat: POST via WebSocket /ws/agent
      → CF Edge + CF Access (if external) or Fly internal (if on Fly)
      → Rialú app: HMAC verification
      → Heartbeat stored + broadcast to Faire
```

---

## Files to modify

| File | Repo | Change |
|------|------|--------|
| `main.py` | rialu | Remove `/api/` `/ws/` bypass from middleware |
| `auth.py` | rialu | No change (Bearer check stays) |
| `routers/agents.py` | rialu | No change (HMAC stays) |
| `agent/cc_wrapper.py` | rialu | Add CF Access headers to httpx requests |
| `agent/rialu-agent.py` | rialu | Add CF Access headers to requests |
| `src/main.js` | faire | Add CF headers to fetch(), switch baseUrl to rialu.ie |
| `src/config.js` | faire | Add CF_ACCESS_CLIENT_ID/SECRET |
| `src/config.js.example` | faire | Document new fields |
| `src/decision-popup.html` | faire | Add CF headers to fetch() |
| `src/dreaming.html` | faire | Add CF headers to fetch() |
| `src-tauri/src/lib.rs` | faire | Read new env vars, pass to spawn_dream |
| `~/.rialu/faire.env` | config | Add CF credentials |
| `~/.rialu/dream-mcp.json` | config | No change |

---

## Testing

1. Create CF Service Token in dashboard
2. Store credentials in `~/.rialu/faire.env`
3. Update Faire config.js with `rialu.ie` base URL and CF credentials
4. Remove `/api/` bypass from Rialú middleware
5. Deploy Rialú
6. Verify: `curl -H "CF-Access-Client-Id: X" -H "CF-Access-Client-Secret: Y" https://rialu.ie/api/projects` returns 200
7. Verify: `curl https://rialu.fly.dev/api/projects` returns 421 (blocked)
8. Start Faire — all tabs load, WS connects, Dream works
9. Run Playwright tests against rialu.ie

---

## Rollback

If something breaks, re-add the bypass to the middleware:
```python
if path.startswith("/api/") or path.startswith("/ws/"):
    return await call_next(request)
```
This restores the current (insecure) behaviour while debugging.

---

*Spec written for handoff to Rialú session. Do not implement in Faire until CF Service Token is created and tested.*

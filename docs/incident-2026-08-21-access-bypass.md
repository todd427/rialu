# Incident — Zero Trust bypass and unauthenticated shell exposure

**Date discovered:** 21 August 2026
**Exposure window:** ~18 Aug 2026 22:30 UTC → 21 Aug 2026 ~09:00 UTC (≈58 hours)
**Severity:** Critical — unauthenticated remote code execution as `todd` on Daisy
**Status:** Contained and fixed. Credential rotation outstanding.

---

## Summary

`rialu.ie` served the full application, its data APIs, and a **live root-capable
shell** to anyone on the public internet for roughly two and a half days.
Access was confirmed by a third party who contacted Todd directly.

Two failures had to coincide, and neither was visible from the dashboard:

1. **Cloudflare Access was not in the request path.** The DNS record was
   DNS-only (grey cloud), so traffic went straight to Fly. No policy was ever
   evaluated.
2. **The application had no authentication of its own.** Its "lockdown"
   checked the `Host` header — which the caller sets — and it trusted
   `Cf-Access-Authenticated-User-Email`, a plain header anyone can add.

Either alone would have been survivable. Together they meant Zero Trust was
decorative and nothing behind it was defended.

---

## Root cause

Fly can only issue or renew a TLS certificate for a hostname if that hostname
resolves **to Fly** at validation time. Proxying through Cloudflare hides the
origin and the validation fails. So obtaining a cert for `rialu.ie` required
un-proxying the DNS record — and nothing ever put it back.

The evidence is the certificate Fly presents on the origin:

```
subject = CN = rialu.ie
issuer  = Let's Encrypt
notBefore = Aug 18 23:56:42 2026 GMT
```

Which lines up exactly with the Cloudflare Security Insights findings:

| Time (18 Aug 2026) | Event |
|---|---|
| 22:31 | Cloudflare flags Bot Fight Mode, Security.txt on `rialu.ie` |
| 23:07 | Cloudflare flags "Exposed RDP Servers" |
| **23:56** | **Let's Encrypt issues Fly's cert for `rialu.ie`** |

Those findings were not a coincidence — they were the *consequence*.
Un-proxying made the Fly origin directly visible, so Cloudflare's own scanner
could suddenly probe it. The "Exposed RDP" finding was a false positive (Fly's
anycast edge completes a TCP handshake on **every** port, and nothing spoke
RDP), but it was a true signal wearing the wrong label: the origin had become
reachable. That signal went unread for three days.

---

## Impact

`/ws/terminal/{machine}` bridged a browser WebSocket to a PTY on the named
machine with **no authentication whatsoever**. An anonymous connection
returned a live prompt:

```
{"type": "terminal_data", "data": "[?2004h🌼 agent$ "}
```

`agent/rialu-agent.service` sets `User=todd`, so that shell was Todd's own
account, not a sandbox. Readable with one `cat`:

- `~/.ssh/id_ed25519`
- `~/.fly/config.yml` — Fly API token (deploy/destroy on every app)
- `~/.config/gh/hosts.yml` — GitHub PAT
- `/home/Projects/Keys/` (324K)
- 32 `.env` files under `/home/Projects`
- Taisce vault, reachable from Daisy
- Chrome's cookie store, including the live Cloudflare Access session

`sudo` required a password, so there was no free path to root. It was the
account, not the box.

Also readable unauthenticated: `/api/projects` (86 projects with notes),
`/api/machines` (hostnames, repo paths, running processes), `/api/worklog`.

**Fly logs retain only minutes, so what was actually accessed cannot be
reconstructed.** Assume everything above is compromised.

---

## Fixes (commit `6a49fe2`)

Three layers, none of which trusts an unverified header.

1. **Peer verification** — `CanonicalHostMiddleware` checks the real client
   address from `Fly-Client-IP` against Cloudflare's 22 published edge ranges.
   Not `Host`, not `Cf-*`. Unparseable addresses fail closed. A valid Bearer
   token is an accepted alternative, because the guard exists to stop
   *unauthenticated* access, not to force everything through Cloudflare —
   `scripts/divergence_selfcall.py` POSTs to the Fly edge and never traverses
   it. Without that exemption the weekly digest would have failed silently.
2. **Signed Access JWT** — `cf_access.py` verifies `Cf-Access-Jwt-Assertion`:
   `iss`, `aud` (so a token minted for another app in the team cannot be
   replayed), key selected by `kid` rather than `public_cert`, which Cloudflare
   warn breaks during rotation via stale caches.
3. **Self-authenticating terminal sockets** — `/ws/terminal` and `/ws/pane`
   require a verified Access JWT on the upgrade (which a browser cannot set and
   an origin-direct caller cannot forge) *or* the agent's HMAC handshake.

Verified against production: direct-to-origin 403; Access 401 via normal DNS;
`/mcp` and `/api/health` unaffected; no shell served to a forged JWT header.
Tests in `tests/test_cf_access.py` (10, offline, throwaway RSA key).

---

## Recurrence risk — read this before November

**Fly will attempt to renew the `rialu.ie` certificate around 16 November 2026
and it will fail**, because validation requires the record to be un-proxied.
`fly certs list` already reports `Not verified`. The failure will look like
something to fix, and the obvious fix — un-proxying the record — is exactly
what caused this incident.

Two ways to defuse it:

1. Confirm Cloudflare SSL/TLS mode is **Full**, not Full (strict), then let the
   cert lapse and `fly certs remove rialu.ie`. Cloudflare terminates TLS at the
   edge; the origin cert stops mattering and nothing ever wants the record
   un-proxied again.
2. Run a **Cloudflare Tunnel** from inside the Fly container — no public
   origin, no cert to validate, no bypass surface. Strictly better, more work.

The origin-side defences now hold independently, so a repeat un-proxying would
be an inconvenience rather than an incident.

---

## Outstanding

- [ ] **Rotate credentials.** Nothing rotated yet. Order: Fly API token (it can
      destroy every app, including these fixes) → `RIALU_AGENT_KEY` (now one of
      the two credentials guarding the terminal socket; must change on Daisy,
      Iris, Lava *and* Fly secrets together) → GitHub PAT → SSH key →
      `/home/Projects/Keys/` → the 32 `.env` files.
- [ ] **Update the agents.** Daisy, Iris and Lava still run pre-fix code.
- [ ] Cloudflare Audit Log (Manage Account → Audit Log, `rialu.ie` zone, 18 Aug
      22:00–23:00) to name the exact action. Not required for remediation.

---

## Invariants worth keeping

- **"It's behind Access" is not a property the origin can assume — it must be
  one the origin can check.** A proxy in front is a control you do not own.
- **A publicly-addressable origin makes Zero Trust advisory.** The Fly IPs are
  permanently public: they were in DNS, in Cloudflare's scan data, and in every
  passive-DNS database.
- **Never gate on `Host` or any `Cf-*` header.** Both are attacker-controlled
  on a direct connection. Verify the peer, or verify a signature.
- **Any route that hands out a shell authenticates itself, always.**
- A security scanner finding on the *wrong* port can still be a true signal
  about the *right* problem. "Exposed RDP" meant "your origin is exposed."

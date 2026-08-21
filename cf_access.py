"""
cf_access.py — Verify Cloudflare Access JWTs at the origin.

Why this exists
---------------
On 2026-08-21 rialu.ie was serving the full app, and an unauthenticated root
shell, to the public internet. Two assumptions failed together:

  1. The DNS record was not proxied, so Cloudflare Access was not in the
     request path at all and enforced nothing.
  2. The app trusted `Cf-Access-Authenticated-User-Email` — a plain header —
     as proof of identity, and gated everything else on the `Host` header.
     Both are set by the caller on a direct request to the Fly origin.

The lesson is that "it's behind Access" is not a property the origin can
assume; it has to be one the origin can *check*. A Cloudflare Access JWT is
signed by Cloudflare's private key, so a caller reaching the Fly IP directly
cannot manufacture one.

Contract (developers.cloudflare.com/cloudflare-one — Validate JWTs):
  - token arrives in the `Cf-Access-Jwt-Assertion` header
  - `iss` must equal https://<team>.cloudflareaccess.com
  - `aud` must equal this application's AUD tag, so a token minted for another
    app in the same team cannot be replayed here
  - select the key by `kid`, NOT the `public_cert` field — Cloudflare warn
    that relying on public_cert breaks during key rotation via stale caches
"""

import logging
import os
import threading
import time

import httpx
import jwt

log = logging.getLogger("cf_access")

# Neither value is a secret: the team domain is public and the AUD is an
# identifier, not a credential. Overridable so a second deployment does not
# need a code change.
TEAM_DOMAIN = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "todd427.cloudflareaccess.com")
AUD = os.environ.get(
    "CF_ACCESS_AUD",
    "ac8753872a73b25dde762443a754057ec7e0f42ce1528841fffde99173daa2de",
)
ISSUER = f"https://{TEAM_DOMAIN}"
CERTS_URL = f"{ISSUER}/cdn-cgi/access/certs"

JWT_HEADER = "cf-access-jwt-assertion"

# Cloudflare rotate these keys; cache them but re-fetch on an unseen kid so a
# rotation does not lock everyone out until the TTL lapses.
_CACHE_TTL = 3600
_lock = threading.Lock()
_keys: dict = {}
_fetched_at = 0.0


def _fetch_keys() -> dict:
    """Pull the JWKS and index it by kid."""
    resp = httpx.get(CERTS_URL, timeout=10)
    resp.raise_for_status()
    jwks = resp.json()
    return {
        k["kid"]: jwt.algorithms.RSAAlgorithm.from_jwk(k)
        for k in jwks.get("keys", [])
        if k.get("kid")
    }


def _key_for(kid: str):
    """Public key for this kid, refreshing the cache if it looks stale."""
    global _keys, _fetched_at
    with _lock:
        fresh = (time.time() - _fetched_at) < _CACHE_TTL
        if kid in _keys and fresh:
            return _keys[kid]
        # Unknown kid, or cache expired — refetch. An unknown kid is the normal
        # signal that Cloudflare rotated keys.
        try:
            _keys = _fetch_keys()
            _fetched_at = time.time()
        except Exception:
            log.exception("Could not fetch Cloudflare Access certs")
            # Fall through to whatever is cached; a stale hit still beats a
            # blanket outage if Cloudflare is briefly unreachable.
        return _keys.get(kid)


def verify(token: str):
    """Return the token's claims, or None if it is not a valid Access JWT.

    Never raises: callers treat None as "not authenticated" and this sits on
    the request path.
    """
    if not token:
        return None
    try:
        kid = jwt.get_unverified_header(token).get("kid", "")
    except Exception:
        return None
    key = _key_for(kid)
    if key is None:
        return None
    try:
        return jwt.decode(
            token, key=key, algorithms=["RS256"], audience=AUD, issuer=ISSUER
        )
    except Exception as e:
        log.warning("Access JWT rejected: %s", type(e).__name__)
        return None


def identity(headers) -> str:
    """Email or service-token name from a verified Access JWT, else ''.

    `headers` is any mapping supporting .get() — Starlette's Headers, a WS
    handshake's headers, or a plain dict.
    """
    claims = verify(headers.get(JWT_HEADER, ""))
    if not claims:
        return ""
    # Service tokens carry common_name rather than email.
    return claims.get("email") or claims.get("common_name") or "verified"

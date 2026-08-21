"""
tests/test_cf_access.py — Cloudflare Access JWT verification at the origin.

Regression guard for the 2026-08-21 incident: the app trusted the plain
`Cf-Access-Authenticated-User-Email` header and gated everything else on the
`Host` header, both of which a direct-to-origin caller sets freely. These
assert that only a genuinely signed token for THIS application is accepted.

A throwaway RSA key stands in for Cloudflare's, injected into the module's
key cache, so nothing here touches the network.
"""

import time

import pytest

jwt = pytest.importorskip("jwt")
from cryptography.hazmat.primitives.asymmetric import rsa

import cf_access


@pytest.fixture
def signing_key(monkeypatch):
    """Install a fake Cloudflare signing key under a known kid."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(cf_access, "_keys", {"test-kid": key.public_key()})
    monkeypatch.setattr(cf_access, "_fetched_at", time.time())
    return key


def _token(key, **overrides):
    claims = {
        "aud": cf_access.AUD,
        "iss": cf_access.ISSUER,
        "email": "todd427@gmail.com",
        "exp": int(time.time()) + 600,
        "iat": int(time.time()) - 10,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-kid"})


def test_valid_token_accepted(signing_key):
    claims = cf_access.verify(_token(signing_key))
    assert claims and claims["email"] == "todd427@gmail.com"


def test_identity_reads_email(signing_key):
    headers = {cf_access.JWT_HEADER: _token(signing_key)}
    assert cf_access.identity(headers) == "todd427@gmail.com"


def test_service_token_identity_uses_common_name(signing_key):
    """Agents authenticate with service tokens, which carry common_name."""
    tok = _token(signing_key, email=None, common_name="rialu-agent")
    assert cf_access.identity({cf_access.JWT_HEADER: tok}) == "rialu-agent"


def test_missing_token_is_not_identity():
    assert cf_access.identity({}) == ""
    assert cf_access.verify("") is None


def test_garbage_token_rejected(signing_key):
    assert cf_access.verify("not-a-jwt") is None
    assert cf_access.identity({cf_access.JWT_HEADER: "..."}) == ""


def test_token_for_another_app_rejected(signing_key):
    """The AUD check is what stops a token from another Access app being replayed."""
    assert cf_access.verify(_token(signing_key, aud="some-other-application")) is None


def test_wrong_issuer_rejected(signing_key):
    assert cf_access.verify(_token(signing_key, iss="https://evil.example.com")) is None


def test_expired_token_rejected(signing_key):
    stale = _token(signing_key, exp=int(time.time()) - 60, iat=int(time.time()) - 600)
    assert cf_access.verify(stale) is None


def test_token_signed_by_a_different_key_rejected(signing_key):
    """The whole point: an attacker cannot mint one without Cloudflare's key."""
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {"aud": cf_access.AUD, "iss": cf_access.ISSUER, "email": "attacker@evil.com",
         "exp": int(time.time()) + 600},
        attacker, algorithm="RS256", headers={"kid": "test-kid"},
    )
    assert cf_access.verify(forged) is None


def test_unknown_kid_does_not_crash(signing_key, monkeypatch):
    """A rotated kid triggers a refetch; a failed refetch must not raise."""
    monkeypatch.setattr(cf_access, "_fetch_keys", lambda: (_ for _ in ()).throw(OSError()))
    tok = jwt.encode({"aud": cf_access.AUD, "iss": cf_access.ISSUER,
                      "exp": int(time.time()) + 600},
                     signing_key, algorithm="RS256", headers={"kid": "rotated-kid"})
    assert cf_access.verify(tok) is None

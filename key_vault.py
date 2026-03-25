"""
key_vault.py — AES-256-GCM encrypted key storage.

Keys are encrypted before hitting SQLite, decrypted only in memory on
explicit request. The master key comes from RIALU_VAULT_KEY env var
(set via `fly secrets set`).

Designed for future multi-user extraction — all operations take explicit
parameters, no global state beyond the master key.
"""

import base64
import os
import secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _master_key() -> bytes:
    """Get the 32-byte master key from env, deriving from passphrase if needed."""
    raw = os.environ.get("RIALU_VAULT_KEY", "")
    if not raw:
        raise ValueError("RIALU_VAULT_KEY not configured")
    # If it's a hex string (64 chars), decode directly
    if len(raw) == 64:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    # Otherwise derive a 32-byte key via SHA-256
    import hashlib
    return hashlib.sha256(raw.encode()).digest()


def encrypt_key(plaintext: str) -> str:
    """
    Encrypt a key value with AES-256-GCM.

    Returns a base64-encoded string: nonce (12 bytes) + ciphertext + tag.
    """
    key = _master_key()
    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_key(encrypted: str) -> str:
    """
    Decrypt a key value from its base64-encoded AES-256-GCM form.

    Raises ValueError on tampered/invalid data.
    """
    key = _master_key()
    raw = base64.b64decode(encrypted)
    if len(raw) < 13:
        raise ValueError("Invalid encrypted data")
    nonce = raw[:12]
    ct = raw[12:]
    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ct, None)
        return plaintext.decode("utf-8")
    except Exception:
        raise ValueError("Decryption failed — wrong key or corrupted data")


def key_hint(plaintext: str) -> str:
    """Return a safe hint: '••••xxxx' showing only last 4 chars."""
    if len(plaintext) <= 4:
        return "••••"
    return "••••" + plaintext[-4:]


def generate_random_key(length: int = 32, encoding: str = "hex") -> str:
    """
    Generate a cryptographically random key.

    Args:
        length:   Number of random bytes (8–64). Default 32 → 256-bit.
        encoding: 'hex' (default) or 'base64' (URL-safe, no padding).

    Returns the key as a string in the requested encoding.
    """
    raw = secrets.token_bytes(length)
    if encoding == "base64":
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return raw.hex()

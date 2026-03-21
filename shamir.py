"""
shamir.py — Shamir's Secret Sharing over GF(256).

Splits a byte string into N shares where any K can reconstruct the original.
Uses arithmetic in GF(2^8) with the AES irreducible polynomial (0x11b).

No external dependencies — pure Python, constant-time field operations.
"""

import os
import secrets


# ── GF(2^8) arithmetic ──────────────────────────────────────────────────────

# Precompute exp/log tables for GF(2^8) with polynomial 0x11b
_EXP = [0] * 512
_LOG = [0] * 256


def _gf_mul_direct(a: int, b: int) -> int:
    """Multiply a*b in GF(2^8) with polynomial x^8+x^4+x^3+x+1."""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1b
        b >>= 1
    return p


def _init_tables():
    """Build exp/log tables for GF(2^8) using generator 3."""
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x = _gf_mul_direct(x, 3)
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]

_init_tables()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _gf_inv(a: int) -> int:
    if a == 0:
        raise ValueError("Cannot invert zero")
    return _EXP[255 - _LOG[a]]


# ── Polynomial evaluation & interpolation ────────────────────────────────────

def _eval_poly(coeffs: list[int], x: int) -> int:
    """Evaluate polynomial at x in GF(256). coeffs[0] is the constant (secret)."""
    # Horner's method: ((c_{k-1} * x + c_{k-2}) * x + ...) * x + c_0
    result = coeffs[-1]
    for i in range(len(coeffs) - 2, -1, -1):
        result = _gf_mul(result, x) ^ coeffs[i]
    return result


def _lagrange_interpolate(points: list[tuple[int, int]], x: int = 0) -> int:
    """Lagrange interpolation at x=0 to recover the secret."""
    k = len(points)
    result = 0
    for i in range(k):
        xi, yi = points[i]
        num = yi
        for j in range(k):
            if i == j:
                continue
            xj = points[j][0]
            # num *= (x - xj) / (xi - xj)  in GF(256)
            num = _gf_mul(num, x ^ xj)
            num = _gf_mul(num, _gf_inv(xi ^ xj))
        result ^= num
    return result


# ── Public API ───────────────────────────────────────────────────────────────

def split(secret: bytes, n: int, k: int) -> list[bytes]:
    """
    Split secret into n shares, any k of which can reconstruct it.

    Args:
        secret: The bytes to protect
        n: Total number of shares (2-255)
        k: Threshold needed to reconstruct (2-n)

    Returns:
        List of n shares, each prefixed with a 1-byte share index.
    """
    if not (2 <= k <= n <= 255):
        raise ValueError(f"Need 2 <= k <= n <= 255, got k={k}, n={n}")

    # Each share is: [index_byte] + [share_bytes...]
    shares = [bytearray([i + 1]) for i in range(n)]

    for byte_val in secret:
        # Random polynomial of degree k-1 with byte_val as constant term
        coeffs = [byte_val] + [secrets.randbelow(256) for _ in range(k - 1)]
        for i in range(n):
            x = i + 1  # x values are 1..n (never 0)
            shares[i].append(_eval_poly(coeffs, x))

    return [bytes(s) for s in shares]


def combine(shares: list[bytes]) -> bytes:
    """
    Reconstruct the secret from k or more shares.

    Args:
        shares: List of share byte strings (as returned by split)

    Returns:
        The original secret bytes.
    """
    if len(shares) < 2:
        raise ValueError("Need at least 2 shares")

    # All shares must be same length
    length = len(shares[0])
    if not all(len(s) == length for s in shares):
        raise ValueError("Share length mismatch")

    # Extract x values (first byte of each share)
    xs = [s[0] for s in shares]
    if len(set(xs)) != len(xs):
        raise ValueError("Duplicate share indices")

    secret = bytearray()
    for byte_idx in range(1, length):
        points = [(s[0], s[byte_idx]) for s in shares]
        secret.append(_lagrange_interpolate(points))

    return bytes(secret)


# ── Hex convenience ──────────────────────────────────────────────────────────

def split_hex(secret_hex: str, n: int = 3, k: int = 2) -> list[str]:
    """Split a hex-encoded secret into n hex-encoded shares."""
    secret = bytes.fromhex(secret_hex)
    shares = split(secret, n, k)
    return [s.hex() for s in shares]


def combine_hex(share_hexes: list[str]) -> str:
    """Reconstruct a hex-encoded secret from hex-encoded shares."""
    shares = [bytes.fromhex(h) for h in share_hexes]
    return combine(shares).hex()

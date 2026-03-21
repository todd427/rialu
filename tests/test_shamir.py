"""
tests/test_shamir.py — Tests for Shamir's Secret Sharing.
"""

import secrets
import pytest
from shamir import split, combine, split_hex, combine_hex


def test_basic_2_of_3():
    secret = b"hello world secret"
    shares = split(secret, n=3, k=2)
    assert len(shares) == 3
    # Any 2 shares should reconstruct
    assert combine([shares[0], shares[1]]) == secret
    assert combine([shares[0], shares[2]]) == secret
    assert combine([shares[1], shares[2]]) == secret


def test_all_3_shares():
    secret = b"test"
    shares = split(secret, n=3, k=2)
    assert combine(shares) == secret


def test_single_share_insufficient():
    secret = b"test"
    shares = split(secret, n=3, k=2)
    with pytest.raises(ValueError, match="at least 2"):
        combine([shares[0]])


def test_3_of_5():
    secret = b"three of five threshold"
    shares = split(secret, n=5, k=3)
    assert len(shares) == 5
    # Any 3 should work
    assert combine([shares[0], shares[2], shares[4]]) == secret
    assert combine([shares[1], shares[3], shares[4]]) == secret


def test_hex_roundtrip():
    key = secrets.token_hex(32)  # 64-char hex = 32 bytes
    shares = split_hex(key, n=3, k=2)
    assert len(shares) == 3
    assert combine_hex([shares[0], shares[1]]) == key
    assert combine_hex([shares[1], shares[2]]) == key


def test_32_byte_key():
    """Test with an actual AES-256 key."""
    key = secrets.token_bytes(32)
    shares = split(key, n=3, k=2)
    assert combine([shares[0], shares[2]]) == key


def test_wrong_shares_give_wrong_result():
    """Two different secrets should not reconstruct each other."""
    s1 = b"secret one"
    s2 = b"secret two"
    shares1 = split(s1, n=3, k=2)
    shares2 = split(s2, n=3, k=2)
    # Mixing shares from different secrets should NOT give either secret
    mixed = combine([shares1[0], shares2[1]])
    assert mixed != s1
    assert mixed != s2


def test_empty_secret():
    secret = b""
    shares = split(secret, n=3, k=2)
    assert combine([shares[0], shares[1]]) == secret


def test_single_byte():
    secret = b"\x42"
    shares = split(secret, n=3, k=2)
    assert combine([shares[0], shares[2]]) == secret


def test_invalid_params():
    with pytest.raises(ValueError):
        split(b"test", n=1, k=1)
    with pytest.raises(ValueError):
        split(b"test", n=3, k=4)

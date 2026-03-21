"""
tests/test_keys.py — Tests for encrypted key vault.
"""

import os
import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db, db
from key_vault import encrypt_key, decrypt_key, key_hint

client = TestClient(app)

VAULT_KEY = "test-vault-key-for-unit-tests-only"


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setenv("RIALU_VAULT_KEY", VAULT_KEY)
    init_db()


# ── key_vault module ────────────────────────────────────────────────────────

def test_encrypt_decrypt_roundtrip():
    secret = "sk-ant-api03-verysecretkey1234"
    encrypted = encrypt_key(secret)
    assert encrypted != secret
    assert decrypt_key(encrypted) == secret


def test_encrypt_produces_different_ciphertext():
    """Each encryption should produce different output (random nonce)."""
    secret = "my-secret"
    e1 = encrypt_key(secret)
    e2 = encrypt_key(secret)
    assert e1 != e2
    assert decrypt_key(e1) == decrypt_key(e2) == secret


def test_key_hint():
    assert key_hint("sk-ant-api03-abcdef1234") == "••••1234"
    assert key_hint("ab") == "••••"
    assert key_hint("") == "••••"


def test_decrypt_invalid_data():
    with pytest.raises(ValueError):
        decrypt_key("not-valid-base64-data!!!")


# ── API endpoints ────────────────────────────────────────────────────────────

def test_list_keys_empty():
    resp = client.get("/api/keys")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_key():
    resp = client.post("/api/keys", json={
        "name": "Anthropic API",
        "provider": "Anthropic",
        "value": "sk-ant-test-key-1234",
        "env_var": "ANTHROPIC_API_KEY",
        "notes": "Main API key",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Anthropic API"
    assert data["hint"] == "••••1234"
    assert "value" not in data  # value should NOT be in response


def test_list_keys_no_values():
    client.post("/api/keys", json={
        "name": "Test Key",
        "provider": "Test",
        "value": "secret-value-here",
    })
    resp = client.get("/api/keys")
    keys = resp.json()
    assert len(keys) == 1
    assert "encrypted_value" not in keys[0]
    assert keys[0]["hint"] == "••••here"


def test_get_key_metadata():
    resp = client.post("/api/keys", json={
        "name": "My Key",
        "provider": "Provider",
        "value": "the-actual-secret",
    })
    key_id = resp.json()["id"]
    resp = client.get(f"/api/keys/{key_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "My Key"
    assert "encrypted_value" not in data


def test_reveal_key():
    resp = client.post("/api/keys", json={
        "name": "Reveal Test",
        "provider": "Test",
        "value": "super-secret-123",
    })
    key_id = resp.json()["id"]
    resp = client.post(f"/api/keys/{key_id}/reveal")
    assert resp.status_code == 200
    assert resp.json()["value"] == "super-secret-123"


def test_reveal_creates_audit():
    resp = client.post("/api/keys", json={
        "name": "Audit Test",
        "provider": "Test",
        "value": "secret",
    })
    key_id = resp.json()["id"]
    client.post(f"/api/keys/{key_id}/reveal")
    resp = client.get(f"/api/keys/{key_id}/audit")
    assert resp.status_code == 200
    actions = [e["action"] for e in resp.json()]
    assert "created" in actions
    assert "revealed" in actions


def test_update_key_value():
    resp = client.post("/api/keys", json={
        "name": "Update Test",
        "provider": "Test",
        "value": "old-value",
    })
    key_id = resp.json()["id"]
    client.put(f"/api/keys/{key_id}", json={"value": "new-value"})
    resp = client.post(f"/api/keys/{key_id}/reveal")
    assert resp.json()["value"] == "new-value"


def test_update_key_metadata():
    resp = client.post("/api/keys", json={
        "name": "Meta Test",
        "provider": "OldProvider",
        "value": "val",
    })
    key_id = resp.json()["id"]
    client.put(f"/api/keys/{key_id}", json={"provider": "NewProvider", "notes": "updated"})
    resp = client.get(f"/api/keys/{key_id}")
    assert resp.json()["provider"] == "NewProvider"
    assert resp.json()["notes"] == "updated"


def test_delete_key():
    resp = client.post("/api/keys", json={
        "name": "Delete Me",
        "provider": "Test",
        "value": "val",
    })
    key_id = resp.json()["id"]
    resp = client.delete(f"/api/keys/{key_id}")
    assert resp.status_code == 204
    resp = client.get(f"/api/keys/{key_id}")
    assert resp.status_code == 404


def test_duplicate_name_rejected():
    client.post("/api/keys", json={
        "name": "Unique Name",
        "provider": "Test",
        "value": "val1",
    })
    resp = client.post("/api/keys", json={
        "name": "Unique Name",
        "provider": "Test",
        "value": "val2",
    })
    assert resp.status_code == 409


def test_key_not_found():
    assert client.get("/api/keys/9999").status_code == 404
    assert client.post("/api/keys/9999/reveal").status_code == 404
    assert client.delete("/api/keys/9999").status_code == 404

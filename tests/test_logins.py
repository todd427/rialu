"""
tests/test_logins.py — Tests for /api/logins credential store.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db

client = TestClient(app)


VAULT_KEY = "test-vault-key-for-unit-tests-only"


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setenv("RIALU_VAULT_KEY", VAULT_KEY)
    init_db()


def test_list_empty():
    resp = client.get("/api/logins")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_login():
    resp = client.post("/api/logins", json={
        "name": "Test Service",
        "url": "https://example.com/login",
        "username": "todd@example.com",
        "password": "s3cret!",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Service"
    assert data["username"] == "todd@example.com"
    assert "pwd_hint" in data
    assert "password" not in data  # not in list response


def test_create_without_username():
    resp = client.post("/api/logins", json={
        "name": "No User", "password": "justpass",
    })
    assert resp.status_code == 201
    assert resp.json()["username"] == ""


def test_create_duplicate_rejected():
    client.post("/api/logins", json={
        "name": "Dup", "username": "a", "password": "b",
    })
    resp = client.post("/api/logins", json={
        "name": "Dup", "username": "c", "password": "d",
    })
    assert resp.status_code == 409


def test_reveal_login():
    r = client.post("/api/logins", json={
        "name": "Reveal Me", "username": "user@test.com", "password": "hunter2",
    })
    lid = r.json()["id"]
    resp = client.post(f"/api/logins/{lid}/reveal")
    assert resp.status_code == 200
    data = resp.json()
    assert data["password"] == "hunter2"
    assert data["username"] == "user@test.com"


def test_reveal_with_totp():
    r = client.post("/api/logins", json={
        "name": "With TOTP", "username": "u", "password": "p",
        "totp_secret": "JBSWY3DPEHPK3PXP",
    })
    lid = r.json()["id"]
    resp = client.post(f"/api/logins/{lid}/reveal")
    assert resp.json()["totp_secret"] == "JBSWY3DPEHPK3PXP"


def test_reveal_not_found():
    resp = client.post("/api/logins/9999/reveal")
    assert resp.status_code == 404


def test_update_login():
    r = client.post("/api/logins", json={
        "name": "Update Me", "username": "old@test.com", "password": "oldpass",
    })
    lid = r.json()["id"]
    resp = client.put(f"/api/logins/{lid}", json={
        "username": "new@test.com", "password": "newpass",
    })
    assert resp.status_code == 200
    assert resp.json()["username"] == "new@test.com"
    # Verify new password
    revealed = client.post(f"/api/logins/{lid}/reveal").json()
    assert revealed["password"] == "newpass"


def test_delete_login():
    r = client.post("/api/logins", json={
        "name": "Delete Me", "username": "x", "password": "y",
    })
    lid = r.json()["id"]
    assert client.delete(f"/api/logins/{lid}").status_code == 204
    assert client.get("/api/logins").json() == []


def test_list_masks_password():
    client.post("/api/logins", json={
        "name": "Masked", "username": "u", "password": "supersecret123",
    })
    logins = client.get("/api/logins").json()
    assert len(logins) == 1
    assert "password" not in logins[0]
    assert "encrypted_pwd" not in logins[0]
    assert "pwd_hint" in logins[0]

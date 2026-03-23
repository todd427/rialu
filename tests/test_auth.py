"""
tests/test_auth.py — Tests for Bearer token authentication.
"""

import os
import pytest
from fastapi.testclient import TestClient

from db import init_db

TOKEN = "test-faire-token-xyz"


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setenv("FAIRE_WS_TOKEN", TOKEN)
    monkeypatch.delenv("RIALU_TEST", raising=False)
    init_db()


@pytest.fixture
def auth_client():
    """Client without RIALU_TEST=1 — auth is enforced."""
    from main import app
    return TestClient(app)


def test_decisions_requires_auth(auth_client):
    resp = auth_client.get("/api/decisions")
    assert resp.status_code == 401


def test_decisions_with_valid_token(auth_client):
    resp = auth_client.get("/api/decisions", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200


def test_decisions_with_invalid_token(auth_client):
    resp = auth_client.get("/api/decisions", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


def test_agents_list_requires_auth(auth_client):
    resp = auth_client.get("/api/agents")
    assert resp.status_code == 401


def test_agents_list_with_token(auth_client):
    resp = auth_client.get("/api/agents", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200


def test_agents_timeline_requires_auth(auth_client):
    resp = auth_client.get("/api/agents/timeline")
    assert resp.status_code == 401


def test_agents_timeline_with_token(auth_client):
    resp = auth_client.get("/api/agents/timeline", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200

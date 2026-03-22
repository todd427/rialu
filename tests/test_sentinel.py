"""
tests/test_sentinel.py — Tests for /api/sentinel proxy endpoints.
"""

import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from main import app
from db import init_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup():
    init_db()


def test_sentinel_overview_no_key(monkeypatch):
    """Without SENTINEL_API_KEY, should return null stats gracefully."""
    monkeypatch.setenv("SENTINEL_API_KEY", "")
    resp = client.get("/api/sentinel/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["stats"] is None
    assert data["blocklist_count"] == 0


def test_sentinel_blocklist_no_key(monkeypatch):
    monkeypatch.setenv("SENTINEL_API_KEY", "")
    resp = client.get("/api/sentinel/blocklist")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["ips"] == []


def test_sentinel_stats_no_key(monkeypatch):
    monkeypatch.setenv("SENTINEL_API_KEY", "")
    resp = client.get("/api/sentinel/stats")
    assert resp.status_code == 200

"""
tests/test_mcp_status.py — Tests for /api/mcp endpoint.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup():
    init_db()


def test_mcp_status_returns_list():
    resp = client.get("/api/mcp")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 4  # sentinel, mnemos, git-mcp, flyer


def test_mcp_status_has_expected_fields():
    resp = client.get("/api/mcp")
    for server in resp.json():
        assert "name" in server
        assert "url" in server
        assert "health" in server
        assert "oauth" in server
        assert "mcp" in server


def test_mcp_servers_list():
    resp = client.get("/api/mcp/servers")
    assert resp.status_code == 200
    names = [s["name"] for s in resp.json()]
    assert "Sentinel" in names
    assert "Mnemos" in names
    assert "git-mcp" in names
    assert "Flyer" in names

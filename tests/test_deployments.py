"""
tests/test_deployments.py — Integration tests for /api/deployments endpoints.
"""

import os
import tempfile
import pytest
from fastapi.testclient import TestClient

os.environ["RIALU_DB"] = tempfile.mktemp(suffix=".db")

from main import app
from db import init_db, db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup():
    init_db()


def _seed_deployment(service_name="mnemos", platform="fly.io", status="healthy"):
    with db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO deployments_cache
               (platform, service_name, status, url, checked_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (platform, service_name, status, f"https://{service_name}.example.com"),
        )


def test_deployments_empty():
    resp = client.get("/api/deployments")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_deployments_returns_cached_data():
    _seed_deployment("mnemos", "fly.io", "healthy")
    _seed_deployment("anseo", "railway", "healthy")
    resp = client.get("/api/deployments")
    assert resp.status_code == 200
    names = [d["service_name"] for d in resp.json()]
    assert "mnemos" in names
    assert "anseo" in names


def test_deployment_status_field():
    _seed_deployment("cybersafer", "cf-pages", "healthy")
    resp = client.get("/api/deployments")
    svc = next((d for d in resp.json() if d["service_name"] == "cybersafer"), None)
    assert svc is not None
    assert svc["status"] == "healthy"
    assert svc["platform"] == "cf-pages"


def test_deploy_history_empty():
    resp = client.get("/api/deployments/history")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_refresh_endpoint():
    """Refresh endpoint should return 200 — pollers will no-op without tokens."""
    resp = client.post("/api/deployments/refresh")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_endpoint():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_machines_stub():
    resp = client.get("/api/machines")
    assert resp.status_code == 200
    assert resp.json() == []

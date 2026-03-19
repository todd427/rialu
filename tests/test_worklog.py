"""
tests/test_worklog.py — Integration tests for /api/worklog endpoints.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup():
    init_db()


@pytest.fixture
def project_id():
    """Create a fresh project for each test that needs one."""
    r = client.post("/api/projects", json={"name": "Worklog Test", "status": "development"})
    assert r.status_code == 201
    return r.json()["id"]


def test_worklog_empty():
    resp = client.get("/api/worklog")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_create_worklog_entry(project_id):
    resp = client.post("/api/worklog", json={
        "project_id": project_id,
        "minutes": 90,
        "session_type": "research",
        "notes": "Deep work on Legion",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["minutes"] == 90
    assert data["session_type"] == "research"


def test_worklog_appears_in_list(project_id):
    client.post("/api/worklog", json={
        "project_id": project_id,
        "minutes": 60,
        "session_type": "code",
    })
    resp = client.get("/api/worklog")
    assert any(e["minutes"] == 60 for e in resp.json())


def test_worklog_stats(project_id):
    client.post("/api/worklog", json={
        "project_id": project_id,
        "minutes": 120,
        "session_type": "writing",
    })
    resp = client.get("/api/worklog/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "minutes_this_week" in data
    assert "sessions_7d" in data
    assert "top_project" in data
    assert "streak_days" in data
    assert data["minutes_this_week"] >= 120


def test_delete_worklog_entry(project_id):
    r = client.post("/api/worklog", json={
        "project_id": project_id,
        "minutes": 30,
        "session_type": "deploy",
    })
    eid = r.json()["id"]
    resp = client.delete(f"/api/worklog/{eid}")
    assert resp.status_code == 204


def test_worklog_limit_param(project_id):
    for _ in range(5):
        client.post("/api/worklog", json={
            "project_id": project_id, "minutes": 10, "session_type": "code"
        })
    resp = client.get("/api/worklog?limit=3")
    assert len(resp.json()) <= 3

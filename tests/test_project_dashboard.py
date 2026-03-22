"""
tests/test_project_dashboard.py — Tests for project dashboard endpoint.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db, db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup():
    init_db()


def _create_project(name="TestProject", repo_url=None):
    resp = client.post("/api/projects", json={
        "name": name, "status": "development", "repo_url": repo_url,
    })
    return resp.json()["id"]


def test_dashboard_empty_project():
    pid = _create_project()
    resp = client.get(f"/api/projects/{pid}/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert data["loc_week"] == {"added": 0, "removed": 0}
    assert data["loc_total"] == {"added": 0, "removed": 0}
    assert data["minutes_week"] == 0
    assert data["deploy"] is None
    assert data["recent_work"] == []


def test_dashboard_with_worklog():
    pid = _create_project()
    client.post("/api/worklog", json={
        "project_id": pid, "minutes": 60, "session_type": "code",
        "lines_added": 150, "lines_removed": 30,
    })
    resp = client.get(f"/api/projects/{pid}/dashboard")
    data = resp.json()
    assert data["loc_week"]["added"] == 150
    assert data["loc_week"]["removed"] == 30
    assert data["minutes_week"] == 60
    assert len(data["recent_work"]) == 1


def test_dashboard_with_deploy():
    pid = _create_project("mnemos")
    with db() as conn:
        conn.execute(
            """INSERT INTO deployments_cache (platform, service_name, status, url, checked_at)
               VALUES ('fly.io', 'mnemos', 'healthy', 'https://mnemos.fly.dev', datetime('now'))"""
        )
    resp = client.get(f"/api/projects/{pid}/dashboard")
    data = resp.json()
    assert data["deploy"] is not None
    assert data["deploy"]["status"] == "healthy"


def test_dashboard_404():
    resp = client.get("/api/projects/9999/dashboard")
    assert resp.status_code == 404


def test_dashboard_recent_work_limit():
    pid = _create_project()
    for i in range(10):
        client.post("/api/worklog", json={
            "project_id": pid, "minutes": 30, "session_type": "code",
            "lines_added": i * 10, "lines_removed": i,
        })
    resp = client.get(f"/api/projects/{pid}/dashboard")
    assert len(resp.json()["recent_work"]) == 5  # limited to 5

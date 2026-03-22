"""
tests/test_worklog_loc.py — Tests for LOC tracking in worklog.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup():
    init_db()


def _create_project(name="LOCProject"):
    resp = client.post("/api/projects", json={"name": name, "status": "development"})
    return resp.json()["id"]


def test_create_worklog_with_loc():
    pid = _create_project()
    resp = client.post("/api/worklog", json={
        "project_id": pid, "minutes": 90, "session_type": "code",
        "lines_added": 200, "lines_removed": 50,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["lines_added"] == 200
    assert data["lines_removed"] == 50


def test_create_worklog_default_loc():
    pid = _create_project()
    resp = client.post("/api/worklog", json={
        "project_id": pid, "minutes": 30, "session_type": "debug",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["lines_added"] == 0
    assert data["lines_removed"] == 0


def test_stats_include_loc():
    pid = _create_project()
    client.post("/api/worklog", json={
        "project_id": pid, "minutes": 60, "session_type": "code",
        "lines_added": 300, "lines_removed": 100,
    })
    client.post("/api/worklog", json={
        "project_id": pid, "minutes": 30, "session_type": "code",
        "lines_added": 150, "lines_removed": 25,
    })
    resp = client.get("/api/worklog/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["lines_added_week"] == 450
    assert data["lines_removed_week"] == 125


def test_worklog_list_includes_loc():
    pid = _create_project()
    client.post("/api/worklog", json={
        "project_id": pid, "minutes": 45, "session_type": "code",
        "lines_added": 500, "lines_removed": 80,
    })
    resp = client.get("/api/worklog")
    entries = resp.json()
    assert len(entries) == 1
    assert entries[0]["lines_added"] == 500
    assert entries[0]["lines_removed"] == 80

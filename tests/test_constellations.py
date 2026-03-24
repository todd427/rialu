"""
tests/test_constellations.py — Tests for constellation field and timeline.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db, db

client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def setup_db():
    init_db()
    with db() as conn:
        conn.execute(
            "INSERT INTO projects (name, slug, status) VALUES ('Mnemos', 'mnemos', 'deployed')"
        )
        conn.execute(
            "INSERT INTO projects (name, slug, status) VALUES ('Legion', 'legion', 'development')"
        )


def test_constellation_field_exists():
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
    assert "constellation" in cols


def test_set_constellation():
    resp = client.put("/api/projects/1", json={"constellation": "ai-research"})
    assert resp.status_code == 200
    assert resp.json()["constellation"] == "ai-research"


def test_get_project_has_constellation():
    client.put("/api/projects/1", json={"constellation": "ai-research"})
    resp = client.get("/api/projects/1")
    assert resp.json()["constellation"] == "ai-research"


def test_list_projects_includes_constellation():
    client.put("/api/projects/1", json={"constellation": "ai-research"})
    client.put("/api/projects/2", json={"constellation": "ai-research"})
    resp = client.get("/api/projects")
    projects = resp.json()
    ai = [p for p in projects if p["constellation"] == "ai-research"]
    assert len(ai) == 2


def test_constellation_null_by_default():
    resp = client.get("/api/projects/1")
    assert resp.json()["constellation"] is None


def test_timeline_empty():
    resp = client.get("/api/agents/timeline")
    assert resp.status_code == 200
    assert resp.json() == []


def test_timeline_with_decisions():
    client.post("/api/decisions", json={
        "project_id": 1,
        "trigger_type": "cost_threshold",
        "payload": {"summary": "Test"},
    })
    resp = client.get("/api/agents/timeline?limit=5")
    events = resp.json()
    assert len(events) >= 1
    assert events[0]["type"] == "decision"


def test_timeline_filter_by_project():
    client.post("/api/decisions", json={
        "project_id": 1,
        "trigger_type": "cost_threshold",
        "payload": {"summary": "For Mnemos"},
    })
    client.post("/api/decisions", json={
        "project_id": 2,
        "trigger_type": "deploy_gate",
        "payload": {"summary": "For Legion"},
    })
    resp = client.get("/api/agents/timeline?project_id=1&limit=10")
    events = resp.json()
    assert all(e.get("project_id") == 1 for e in events if e.get("project_id"))


def test_agent_events_list():
    resp = client.get("/api/agents/events?limit=5")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)

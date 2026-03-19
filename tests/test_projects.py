"""
tests/test_projects.py — Integration tests for /api/projects endpoints.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db

client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def setup_db():
    init_db()


def test_list_projects_empty():
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_project():
    resp = client.post("/api/projects", json={
        "name": "Legion",
        "status": "development",
        "machine": "iris",
        "phase": "peekaboo",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Legion"
    assert data["slug"] == "legion"
    assert data["status"] == "development"
    assert data["machine"] == "iris"


def test_list_projects_after_create():
    client.post("/api/projects", json={"name": "Alpha", "status": "research"})
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def test_get_project():
    r = client.post("/api/projects", json={"name": "Beta", "status": "deployed"})
    pid = r.json()["id"]
    resp = client.get(f"/api/projects/{pid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == pid


def test_get_project_not_found():
    resp = client.get("/api/projects/99999")
    assert resp.status_code == 404


def test_update_project():
    r = client.post("/api/projects", json={"name": "Gamma", "status": "research"})
    pid = r.json()["id"]
    resp = client.put(f"/api/projects/{pid}", json={"status": "deployed", "notes": "Live!"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "deployed"
    assert resp.json()["notes"] == "Live!"


def test_delete_project():
    r = client.post("/api/projects", json={"name": "Ephemeral", "status": "paused"})
    pid = r.json()["id"]
    resp = client.delete(f"/api/projects/{pid}")
    assert resp.status_code == 204
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_create_milestone():
    r = client.post("/api/projects", json={"name": "Delta", "status": "development"})
    pid = r.json()["id"]
    resp = client.post(f"/api/projects/{pid}/milestones", json={"title": "First milestone"})
    assert resp.status_code == 201
    assert resp.json()["title"] == "First milestone"
    assert resp.json()["done"] == 0


def test_list_milestones():
    r = client.post("/api/projects", json={"name": "Epsilon", "status": "development"})
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/milestones", json={"title": "M1"})
    client.post(f"/api/projects/{pid}/milestones", json={"title": "M2"})
    resp = client.get(f"/api/projects/{pid}/milestones")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_toggle_milestone():
    r = client.post("/api/projects", json={"name": "Zeta", "status": "development"})
    pid = r.json()["id"]
    m = client.post(f"/api/projects/{pid}/milestones", json={"title": "Toggle me"}).json()
    mid = m["id"]
    resp = client.put(f"/api/projects/milestones/{mid}", json={"done": True})
    assert resp.status_code == 200
    assert resp.json()["done"] == 1


def test_delete_milestone():
    r = client.post("/api/projects", json={"name": "Eta", "status": "development"})
    pid = r.json()["id"]
    m = client.post(f"/api/projects/{pid}/milestones", json={"title": "Delete me"}).json()
    mid = m["id"]
    resp = client.delete(f"/api/projects/milestones/{mid}")
    assert resp.status_code == 204


def test_milestones_cascade_delete():
    r = client.post("/api/projects", json={"name": "Cascade", "status": "research"})
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/milestones", json={"title": "Should vanish"})
    client.delete(f"/api/projects/{pid}")
    resp = client.get(f"/api/projects/{pid}/milestones")
    assert resp.json() == []


def test_log_session_creates_worklog():
    r = client.post("/api/projects", json={"name": "Session Test", "status": "development"})
    pid = r.json()["id"]
    resp = client.post(f"/api/projects/{pid}/sessions", json={
        "session_type": "code",
        "notes": "Testing",
        "duration_minutes": 45,
    })
    assert resp.status_code == 201
    wl = client.get("/api/worklog").json()
    assert any(e["project_id"] == pid and e["minutes"] == 45 for e in wl)

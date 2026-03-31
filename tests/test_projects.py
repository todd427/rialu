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


def test_refresh_no_change():
    """Refresh a project with no site_url and no deploy cache — status stays the same."""
    r = client.post("/api/projects", json={"name": "Refresh Test", "status": "development"})
    pid = r.json()["id"]
    resp = client.post(f"/api/projects/{pid}/refresh")
    assert resp.status_code == 200
    data = resp.json()
    assert data["changed"] is False
    assert data["old_status"] == "development"
    assert data["new_status"] == "development"


def test_refresh_promotes_from_deploy_cache():
    """If deployments_cache says healthy, a paused project gets promoted."""
    from db import db
    r = client.post("/api/projects", json={"name": "CacheTest", "status": "paused"})
    pid = r.json()["id"]
    with db() as conn:
        conn.execute(
            "INSERT INTO deployments_cache (platform, service_name, status, checked_at) VALUES (?, ?, ?, datetime('now'))",
            ("fly", "cachetest", "healthy"),
        )
    resp = client.post(f"/api/projects/{pid}/refresh")
    data = resp.json()
    assert data["changed"] is True
    assert data["new_status"] == "deployed"
    # Verify it persisted
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["status"] == "deployed"


def test_refresh_does_not_demote():
    """A deployed project stays deployed even with no evidence."""
    r = client.post("/api/projects", json={"name": "NoDemote", "status": "deployed"})
    pid = r.json()["id"]
    resp = client.post(f"/api/projects/{pid}/refresh")
    data = resp.json()
    assert data["changed"] is False
    assert data["new_status"] == "deployed"


def test_refresh_404():
    resp = client.post("/api/projects/99999/refresh")
    assert resp.status_code == 404


# ── search ──────────────────────────────────────────────────────────────────

def test_search_by_name():
    client.post("/api/projects", json={"name": "Mnemos", "status": "deployed"})
    client.post("/api/projects", json={"name": "Legion", "status": "development"})
    resp = client.get("/api/projects?q=mnemos")
    results = resp.json()
    assert len(results) == 1
    assert results[0]["name"] == "Mnemos"


def test_search_by_notes():
    client.post("/api/projects", json={"name": "Proj1", "status": "research", "notes": "memory corpus search engine"})
    client.post("/api/projects", json={"name": "Proj2", "status": "research", "notes": "desktop client"})
    resp = client.get("/api/projects?q=corpus")
    results = resp.json()
    assert len(results) == 1
    assert results[0]["name"] == "Proj1"


def test_search_by_platform():
    client.post("/api/projects", json={"name": "FlyApp", "status": "deployed", "platform": "fly.io"})
    client.post("/api/projects", json={"name": "RailApp", "status": "deployed", "platform": "railway"})
    resp = client.get("/api/projects?q=railway")
    results = resp.json()
    assert len(results) == 1
    assert results[0]["name"] == "RailApp"


def test_search_case_insensitive():
    client.post("/api/projects", json={"name": "Taisce", "status": "deployed"})
    resp = client.get("/api/projects?q=TAISCE")
    assert len(resp.json()) == 1


def test_search_no_match():
    client.post("/api/projects", json={"name": "Alpha", "status": "research"})
    resp = client.get("/api/projects?q=zzzznothing")
    assert resp.json() == []


def test_search_by_deployment():
    """Search matches deployment service_name or URL from deployments_cache."""
    from db import db
    client.post("/api/projects", json={"name": "Sentinel", "status": "deployed"})
    with db() as conn:
        conn.execute(
            "INSERT INTO deployments_cache (platform, service_name, status, url, checked_at) "
            "VALUES ('fly', 'sentinel', 'healthy', 'https://sentinel.foxxelabs.com', datetime('now'))"
        )
    resp = client.get("/api/projects?q=foxxelabs")
    results = resp.json()
    assert len(results) == 1
    assert results[0]["name"] == "Sentinel"


def test_search_empty_q_returns_all():
    client.post("/api/projects", json={"name": "A", "status": "research"})
    client.post("/api/projects", json={"name": "B", "status": "research"})
    resp = client.get("/api/projects?q=")
    assert len(resp.json()) == 2


def test_search_by_milestone_title():
    """Search matches milestone titles attached to projects."""
    from db import db
    r = client.post("/api/projects", json={"name": "Radharc", "status": "development"})
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/milestones", json={"title": "OAuth 2.1 integration"})
    client.post("/api/projects", json={"name": "Other", "status": "research"})
    resp = client.get("/api/projects?q=OAuth")
    results = resp.json()
    assert len(results) == 1
    assert results[0]["name"] == "Radharc"


def test_search_by_deploy_platform():
    """Search matches deployment platform from deployments_cache."""
    from db import db
    client.post("/api/projects", json={"name": "RailProject", "status": "deployed"})
    with db() as conn:
        conn.execute(
            "INSERT INTO deployments_cache (platform, service_name, status, checked_at) "
            "VALUES ('railway', 'railproject', 'healthy', datetime('now'))"
        )
    resp = client.get("/api/projects?q=railway")
    results = resp.json()
    assert len(results) == 1
    assert results[0]["name"] == "RailProject"

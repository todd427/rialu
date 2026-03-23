"""
tests/test_mnemos.py — Tests for /api/mnemos endpoints and auto-ingest.
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


# ── stats ────────────────────────────────────────────────────────────────────

def test_stats_unavailable():
    """Without MNEMOS_API_KEY, stats returns unavailable."""
    with patch("routers.mnemos.MNEMOS_KEY", ""):
        resp = client.get("/api/mnemos/stats")
    assert resp.status_code == 200
    assert resp.json()["status"] == "unavailable"


def test_stats_proxied():
    """With key, stats proxies to Mnemos API."""
    mock_data = {"total": 55000, "by_source": {"doc": 84}, "by_date": {}, "undated": 100, "timestamp": "2026-03-22"}
    with patch("routers.mnemos._mnemos_get", new_callable=AsyncMock, return_value=mock_data):
        resp = client.get("/api/mnemos/stats")
    assert resp.status_code == 200
    assert resp.json()["total"] == 55000


# ── search ───────────────────────────────────────────────────────────────────

def test_search_no_key():
    """Without key, search returns empty."""
    with patch("routers.mnemos.MNEMOS_KEY", ""):
        resp = client.post("/api/mnemos/search", json={"query": "test"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_search_proxied():
    """Search proxies to Mnemos query endpoint."""
    mock_data = {
        "query": "legion",
        "filter": "nonfiction",
        "count": 2,
        "hits": [
            {"document": "Legion is a swarm", "source": "doc", "score": 0.85, "preview": "Legion..."},
            {"document": "IRC protocol", "source": "claude", "score": 0.72, "preview": "IRC..."},
        ],
    }
    with patch("routers.mnemos._mnemos_post", new_callable=AsyncMock, return_value=mock_data):
        resp = client.post("/api/mnemos/search", json={"query": "legion", "top": 6, "filter": "nonfiction"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert len(data["hits"]) == 2


# ── ingest ───────────────────────────────────────────────────────────────────

def test_ingest_success():
    mock_data = {"ingested": 1, "skipped": 0, "total": 1}
    with patch("routers.mnemos._mnemos_post", new_callable=AsyncMock, return_value=mock_data):
        resp = client.post("/api/mnemos/ingest", json={"title": "Test doc", "text": "Hello world"})
    assert resp.status_code == 200
    assert resp.json()["ingested"] == 1


def test_ingest_unavailable():
    with patch("routers.mnemos.MNEMOS_KEY", ""):
        resp = client.post("/api/mnemos/ingest", json={"title": "Test", "text": "Hi"})
    assert resp.status_code == 502


# ── auto-ingest on session ───────────────────────────────────────────────────

def test_session_triggers_ingest():
    """Creating a session should fire background ingest to Mnemos."""
    # Create a project first
    proj = client.post("/api/projects", json={"name": "IngestTest", "status": "development"}).json()
    with patch("routers.mnemos.ingest_activity", new_callable=AsyncMock) as mock_ingest:
        client.post(f"/api/projects/{proj['id']}/sessions", json={
            "session_type": "code",
            "notes": "worked on tests",
            "duration_minutes": 30,
        })
        # BackgroundTasks runs synchronously in TestClient
        mock_ingest.assert_called_once()
        title_arg = mock_ingest.call_args[0][0]
        assert "IngestTest" in title_arg


# ── auto-ingest on milestone completion ──────────────────────────────────────

def test_milestone_complete_triggers_ingest():
    """Completing a milestone should fire background ingest to Mnemos."""
    proj = client.post("/api/projects", json={"name": "MilestoneTest", "status": "development"}).json()
    ms = client.post(f"/api/projects/{proj['id']}/milestones", json={"title": "Ship v1"}).json()
    with patch("routers.mnemos.ingest_activity", new_callable=AsyncMock) as mock_ingest:
        client.put(f"/api/projects/milestones/{ms['id']}", json={"done": True})
        mock_ingest.assert_called_once()
        text_arg = mock_ingest.call_args[0][1]
        assert "Ship v1" in text_arg


def test_milestone_not_done_no_ingest():
    """Updating a milestone without marking done should not trigger ingest."""
    proj = client.post("/api/projects", json={"name": "NoIngest", "status": "development"}).json()
    ms = client.post(f"/api/projects/{proj['id']}/milestones", json={"title": "Draft"}).json()
    with patch("routers.mnemos.ingest_activity", new_callable=AsyncMock) as mock_ingest:
        client.put(f"/api/projects/milestones/{ms['id']}", json={"title": "Draft v2"})
        mock_ingest.assert_not_called()

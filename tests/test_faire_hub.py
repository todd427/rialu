"""
tests/test_faire_hub.py — Tests for Faire WebSocket hub and agent endpoints.
"""

import json
import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db, db

client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    monkeypatch.setenv("FAIRE_WS_TOKEN", "test-faire-token")
    init_db()


# ── Agents API ──────────────────────────────────────────────────────────────

def test_list_agents_empty():
    resp = client.get("/api/agents")
    assert resp.status_code == 200
    assert resp.json() == []


def test_agent_event_creates_agent():
    """POST /api/agents/{id}/event should upsert the agent and store event."""
    resp = client.post("/api/agents/daisy-agent/event", json={
        "event_type": "heartbeat",
        "payload": {"cpu_pct": 5.0},
    })
    assert resp.status_code == 201
    assert resp.json()["status"] == "ok"

    # Agent should now exist
    agents = client.get("/api/agents").json()
    assert len(agents) == 1
    assert agents[0]["id"] == "daisy-agent"
    assert agents[0]["status"] == "online"


def test_agent_event_with_project():
    with db() as conn:
        conn.execute(
            "INSERT INTO projects (name, slug, status) VALUES ('Legion', 'legion', 'development')"
        )
    resp = client.post("/api/agents/iris-agent/event", json={
        "event_type": "tool_call",
        "project_id": 1,
        "payload": {"tool": "bash", "args": "git status"},
    })
    assert resp.status_code == 201


# ── WebSocket ───────────────────────────────────────────────────────────────

def test_ws_faire_connect_valid_token():
    with client.websocket_connect("/ws/test-faire-token") as ws:
        ws.send_text("ping")
        # Connection stays open — no immediate response expected for pings


def test_ws_faire_connect_invalid_token():
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/wrong-token") as ws:
            ws.receive_text()


# ── Schema ──────────────────────────────────────────────────────────────────

def test_decisions_table_exists():
    with db() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='decisions'"
        ).fetchone()
    assert row is not None


def test_agent_events_table_exists():
    with db() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_events'"
        ).fetchone()
    assert row is not None


def test_agents_table_exists():
    with db() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agents'"
        ).fetchone()
    assert row is not None


def test_projects_faire_columns():
    """Verify the Phase 1 ALTER TABLE columns were added."""
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
    assert "cc_session_id" in cols
    assert "cost_limit_hr" in cols
    assert "auto_approve_rules" in cols

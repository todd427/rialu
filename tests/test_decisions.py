"""
tests/test_decisions.py — Tests for decision queue endpoints (Faire Phase 2).
"""

import json
import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db, db

client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def setup_db():
    init_db()
    # Seed a project for FK references
    with db() as conn:
        conn.execute(
            "INSERT INTO projects (name, slug, status) VALUES ('Legion', 'legion', 'development')"
        )


def _create_decision(project_id=1, trigger_type="cost_threshold", priority=5, timeout_secs=300):
    return client.post("/api/decisions", json={
        "project_id": project_id,
        "trigger_type": trigger_type,
        "priority": priority,
        "timeout_secs": timeout_secs,
        "payload": {
            "summary": "Test decision",
            "project": {"name": "Legion"},
            "current_state": {"workers": 4},
            "proposed_state": {"workers": 8},
        },
    })


# ── Create ──────────────────────────────────────────────────────────────────

def test_create_decision():
    resp = _create_decision()
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    assert data["trigger_type"] == "cost_threshold"
    assert data["project_id"] == 1
    assert data["timeout_secs"] == 300
    assert data["id"] is not None
    assert json.loads(data["payload"])["summary"] == "Test decision"


def test_create_decision_invalid_project():
    """FK constraint rejects invalid project_id."""
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        client.post("/api/decisions", json={
            "project_id": 999,
            "trigger_type": "ai_approval",
            "payload": {"summary": "Should fail"},
        })


# ── List ────────────────────────────────────────────────────────────────────

def test_list_decisions_empty():
    resp = client.get("/api/decisions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_decisions():
    _create_decision()
    _create_decision(trigger_type="deploy_gate")
    resp = client.get("/api/decisions")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_list_decisions_filter_status():
    _create_decision()
    resp = client.get("/api/decisions?status=pending")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.get("/api/decisions?status=approved")
    assert resp.status_code == 200
    assert len(resp.json()) == 0


def test_list_decisions_filter_project():
    _create_decision()
    resp = client.get("/api/decisions?project_id=1")
    assert len(resp.json()) == 1

    resp = client.get("/api/decisions?project_id=999")
    assert len(resp.json()) == 0


# ── Get ─────────────────────────────────────────────────────────────────────

def test_get_decision():
    create_resp = _create_decision()
    decision_id = create_resp.json()["id"]

    resp = client.get(f"/api/decisions/{decision_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == decision_id


def test_get_decision_not_found():
    resp = client.get("/api/decisions/nonexistent-id")
    assert resp.status_code == 404


# ── Respond ─────────────────────────────────────────────────────────────────

def test_respond_approve():
    decision_id = _create_decision().json()["id"]

    resp = client.post(f"/api/decisions/{decision_id}/respond", json={
        "action": "approve",
        "reason": "Looks good",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["responded_by"] == "user"
    assert data["resolved_at"] is not None
    response = json.loads(data["response"])
    assert response["action"] == "approve"
    assert response["reason"] == "Looks good"


def test_respond_reject():
    decision_id = _create_decision().json()["id"]

    resp = client.post(f"/api/decisions/{decision_id}/respond", json={
        "action": "reject",
        "reason": "Over budget",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_respond_defer():
    decision_id = _create_decision().json()["id"]

    resp = client.post(f"/api/decisions/{decision_id}/respond", json={
        "action": "defer",
        "defer_mins": 30,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "deferred"
    assert data["defer_until"] is not None


def test_respond_already_resolved():
    decision_id = _create_decision().json()["id"]
    client.post(f"/api/decisions/{decision_id}/respond", json={"action": "approve"})

    resp = client.post(f"/api/decisions/{decision_id}/respond", json={"action": "reject"})
    assert resp.status_code == 409


def test_respond_not_found():
    resp = client.post("/api/decisions/nonexistent/respond", json={"action": "approve"})
    assert resp.status_code == 404


def test_respond_invalid_action():
    decision_id = _create_decision().json()["id"]
    resp = client.post(f"/api/decisions/{decision_id}/respond", json={"action": "invalid"})
    assert resp.status_code == 400

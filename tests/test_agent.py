"""
tests/test_agent.py — Tests for machine agent endpoints and HMAC auth.
"""

import hashlib
import hmac
import json
import os

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db, db

client = TestClient(app)

AGENT_KEY = "test-secret-key-1234"


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setenv("RIALU_AGENT_KEY", AGENT_KEY)
    init_db()


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(AGENT_KEY.encode(), body, hashlib.sha256).hexdigest()


def _heartbeat_payload(machine="daisy", cpu=12.4, ram=34.1, gpu=0.0):
    return {
        "machine": machine,
        "cpu_pct": cpu,
        "ram_pct": ram,
        "gpu_pct": gpu,
        "processes": [
            {"name": "sceal", "script": "train.py", "pid": 12345, "uptime_s": 3600}
        ],
        "repos": [
            {
                "name": "mnemos",
                "path": "/home/Projects/mnemos",
                "branch": "master",
                "clean": True,
                "ahead": 0,
                "behind": 0,
                "last_commit": "abc1234",
                "last_message": "Fix date index",
            }
        ],
    }


def _post_heartbeat(payload=None, sig=None):
    payload = payload or _heartbeat_payload()
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if sig is not None:
        headers["X-Rialu-Sig"] = sig
    else:
        headers["X-Rialu-Sig"] = _sign(body)
    return client.post("/api/agent/heartbeat", content=body, headers=headers)


# ── HMAC verification ────────────────────────────────────────────────────────

def test_heartbeat_missing_signature():
    body = json.dumps(_heartbeat_payload()).encode()
    resp = client.post("/api/agent/heartbeat", content=body,
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 401
    assert "Missing" in resp.json()["detail"]


def test_heartbeat_malformed_signature():
    resp = _post_heartbeat(sig="bad-signature")
    assert resp.status_code == 401


def test_heartbeat_invalid_signature():
    body = json.dumps(_heartbeat_payload()).encode()
    wrong_sig = "sha256=" + hmac.new(b"wrong-key", body, hashlib.sha256).hexdigest()
    resp = _post_heartbeat(sig=wrong_sig)
    assert resp.status_code == 401
    assert "Invalid" in resp.json()["detail"]


def test_heartbeat_no_agent_key_configured(monkeypatch):
    monkeypatch.setenv("RIALU_AGENT_KEY", "")
    body = json.dumps(_heartbeat_payload()).encode()
    sig = "sha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest()
    resp = _post_heartbeat(sig=sig)
    assert resp.status_code == 500
    assert "not configured" in resp.json()["detail"]


# ── Heartbeat endpoint ───────────────────────────────────────────────────────

def test_heartbeat_accepted():
    resp = _post_heartbeat()
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["machine"] == "daisy"


def test_heartbeat_upserts():
    """Second heartbeat should overwrite the first for the same machine."""
    _post_heartbeat(_heartbeat_payload(cpu=10.0))
    _post_heartbeat(_heartbeat_payload(cpu=55.5))
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM machine_heartbeats WHERE machine_name = 'daisy'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["cpu_pct"] == 55.5


def test_heartbeat_multiple_machines():
    _post_heartbeat(_heartbeat_payload(machine="daisy"))
    _post_heartbeat(_heartbeat_payload(machine="rose"))
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM machine_heartbeats").fetchone()[0]
    assert count == 2


def test_heartbeat_missing_machine():
    payload = {"cpu_pct": 10.0}  # no 'machine' field
    resp = _post_heartbeat(payload)
    assert resp.status_code == 400


def test_heartbeat_stores_processes_and_repos():
    payload = _heartbeat_payload()
    _post_heartbeat(payload)
    with db() as conn:
        row = conn.execute(
            "SELECT processes_json, repos_json FROM machine_heartbeats WHERE machine_name = 'daisy'"
        ).fetchone()
    procs = json.loads(row["processes_json"])
    repos = json.loads(row["repos_json"])
    assert procs[0]["name"] == "sceal"
    assert repos[0]["name"] == "mnemos"


# ── GET /api/machines ────────────────────────────────────────────────────────

def test_machines_empty():
    resp = client.get("/api/machines")
    assert resp.status_code == 200
    assert resp.json() == []


def test_machines_returns_heartbeat_data():
    _post_heartbeat(_heartbeat_payload(machine="daisy"))
    _post_heartbeat(_heartbeat_payload(machine="rose", gpu=None))
    resp = client.get("/api/machines")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    names = {m["machine_name"] for m in data}
    assert names == {"daisy", "rose"}
    daisy = next(m for m in data if m["machine_name"] == "daisy")
    assert "processes" in daisy
    assert "repos" in daisy
    assert "processes_json" not in daisy


# ── POST /api/agent/result ───────────────────────────────────────────────────

def test_agent_result_updates_action():
    # Create an action first
    with db() as conn:
        conn.execute(
            "INSERT INTO agent_actions (machine_name, action_type, payload) VALUES (?, ?, ?)",
            ("daisy", "git_pull", '{"repo":"mnemos"}'),
        )
        action_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    payload = {"action_id": action_id, "status": "success", "result": "Already up to date."}
    body = json.dumps(payload).encode()
    resp = client.post("/api/agent/result", content=body,
                       headers={"Content-Type": "application/json", "X-Rialu-Sig": _sign(body)})
    assert resp.status_code == 200
    with db() as conn:
        row = conn.execute("SELECT status, result FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
    assert row["status"] == "success"
    assert row["result"] == "Already up to date."


def test_agent_result_missing_action_id():
    payload = {"status": "success"}
    body = json.dumps(payload).encode()
    resp = client.post("/api/agent/result", content=body,
                       headers={"Content-Type": "application/json", "X-Rialu-Sig": _sign(body)})
    assert resp.status_code == 400


def test_agent_result_action_not_found():
    payload = {"action_id": 99999, "status": "success"}
    body = json.dumps(payload).encode()
    resp = client.post("/api/agent/result", content=body,
                       headers={"Content-Type": "application/json", "X-Rialu-Sig": _sign(body)})
    assert resp.status_code == 404


# ── POST /api/agent/action (stub) ───────────────────────────────────────────

def test_agent_action_queued():
    resp = client.post("/api/agent/action", json={
        "machine": "daisy",
        "action_type": "git_pull",
        "payload": '{"repo":"mnemos"}',
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "queued"
    assert "action_id" in data


# ── GET /api/machines/claude ─────────────────────────────────────────────────

def test_claude_sessions_empty():
    resp = client.get("/api/machines/claude")
    assert resp.status_code == 200
    assert resp.json() == []


# ── GET /api/machines/status ─────────────────────────────────────────────────

def test_machines_status_empty():
    resp = client.get("/api/machines/status")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


def test_machines_status_with_heartbeat():
    _post_heartbeat(_heartbeat_payload(machine="daisy"))
    resp = client.get("/api/machines/status")
    data = resp.json()
    assert "daisy" in data
    assert data["daisy"]["ws_connected"] is False  # no WS in test
    assert data["daisy"]["last_heartbeat"] is not None


# ── GET /api/machines/{machine}/tmux ─────────────────────────────────────────

def test_tmux_no_agent():
    resp = client.get("/api/machines/daisy/tmux")
    assert resp.status_code == 404


# ── POST /api/machines/{machine}/send ────────────────────────────────────────

def test_send_keys_no_agent():
    resp = client.post("/api/machines/daisy/send", json={
        "pane_id": "main:0.0",
        "keys": "y Enter",
    })
    assert resp.status_code == 404


# ── terminal_sessions table ──────────────────────────────────────────────────

def test_terminal_sessions_table_exists():
    with db() as conn:
        conn.execute("SELECT * FROM terminal_sessions LIMIT 1")

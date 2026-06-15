"""
tests/test_machines.py — Tests for /api/machines endpoints.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db, db
from ws_hub import hub

client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def setup_db():
    init_db()


def _insert_heartbeat(machine="ghostbox"):
    with db() as conn:
        conn.execute(
            """INSERT INTO machine_heartbeats
               (machine_name, cpu_pct, ram_pct, gpu_pct, processes_json, repos_json, received_at)
               VALUES (?, 1.0, 2.0, NULL, '[]', '[]', datetime('now'))""",
            (machine,),
        )


def test_remove_machine_clears_card():
    _insert_heartbeat("ghostbox")
    assert any(m["machine_name"] == "ghostbox" for m in client.get("/api/machines").json())

    resp = client.delete("/api/machines/ghostbox")
    assert resp.status_code == 200
    assert resp.json()["status"] == "removed"

    assert all(m["machine_name"] != "ghostbox" for m in client.get("/api/machines").json())


def test_remove_machine_not_found():
    assert client.delete("/api/machines/nope").status_code == 404


def test_remove_connected_machine_refused(monkeypatch):
    """A live agent must not be removed — its row would just come back."""
    _insert_heartbeat("live")
    monkeypatch.setattr(hub, "is_connected", lambda m: True)

    resp = client.delete("/api/machines/live")
    assert resp.status_code == 409
    assert any(m["machine_name"] == "live" for m in client.get("/api/machines").json())

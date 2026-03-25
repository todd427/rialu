"""
tests/test_export.py — Tests for /api/export CSV endpoints.
"""

import csv
import io
import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db, db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup():
    init_db()


def _parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


# ── projects ─────────────────────────────────────────────────────────────────

def test_export_projects_empty():
    resp = client.get("/api/export/projects.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")


def test_export_projects_with_data():
    client.post("/api/projects", json={"name": "TestProj", "status": "development"})
    resp = client.get("/api/export/projects.csv")
    assert resp.status_code == 200
    rows = _parse_csv(resp.text)
    assert len(rows) == 1
    assert rows[0]["name"] == "TestProj"
    assert rows[0]["status"] == "development"
    assert "id" in rows[0]
    assert "created_at" in rows[0]


# ── worklog ──────────────────────────────────────────────────────────────────

def test_export_worklog_empty():
    resp = client.get("/api/export/worklog.csv")
    assert resp.status_code == 200


def test_export_worklog_with_data():
    proj = client.post("/api/projects", json={"name": "WlProj", "status": "development"}).json()
    with db() as conn:
        conn.execute(
            "INSERT INTO worklog (project_id, date, minutes, session_type, notes, lines_added, lines_removed) VALUES (?, date('now'), 30, 'code', 'test session', 100, 10)",
            (proj["id"],),
        )
    resp = client.get("/api/export/worklog.csv")
    rows = _parse_csv(resp.text)
    assert len(rows) == 1
    assert rows[0]["project"] == "WlProj"
    assert rows[0]["minutes"] == "30"
    assert rows[0]["lines_added"] == "100"


# ── budget ───────────────────────────────────────────────────────────────────

def test_export_budget_empty():
    resp = client.get("/api/export/budget.csv")
    assert resp.status_code == 200


def test_export_budget_with_data():
    client.post("/api/budget", json={
        "platform": "fly.io", "service_name": "mnemos", "cost_gbp": 8.40, "period": "monthly"
    })
    resp = client.get("/api/export/budget.csv")
    rows = _parse_csv(resp.text)
    assert len(rows) == 1
    assert rows[0]["platform"] == "fly.io"
    assert rows[0]["cost_eur"] == "8.4"


# ── usage ────────────────────────────────────────────────────────────────────

def test_export_usage_empty():
    resp = client.get("/api/export/usage.csv")
    assert resp.status_code == 200


def test_export_usage_with_data():
    with db() as conn:
        conn.execute("""
            INSERT INTO anthropic_usage (usage_date, model, api_key_name, input_tokens, output_tokens, cost_usd)
            VALUES ('2026-03-24', 'claude-sonnet-4-6', 'test-key', 50000, 2000, 0.45)
        """)
    resp = client.get("/api/export/usage.csv")
    rows = _parse_csv(resp.text)
    assert len(rows) == 1
    assert rows[0]["model"] == "claude-sonnet-4-6"
    assert rows[0]["input_tokens"] == "50000"


# ── sentinel ─────────────────────────────────────────────────────────────────

def test_export_sentinel_no_key():
    """Without Sentinel env vars, returns empty CSV."""
    import os
    from unittest.mock import patch
    with patch.dict(os.environ, {"SENTINEL_URL": "", "SENTINEL_API_KEY": ""}):
        resp = client.get("/api/export/sentinel.csv")
    assert resp.status_code == 200


# ── content-disposition ──────────────────────────────────────────────────────

def test_csv_content_disposition():
    resp = client.get("/api/export/projects.csv")
    assert "rialu-projects.csv" in resp.headers.get("content-disposition", "")

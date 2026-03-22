"""
tests/test_usage.py — Tests for /api/usage (Anthropic token usage) endpoints.
"""

import io
import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup():
    init_db()


CSV_DATA = """usage_date_utc,model_version,api_key,workspace,usage_type,context_window,usage_input_tokens_no_cache,usage_input_tokens_cache_write_5m,usage_input_tokens_cache_write_1h,usage_input_tokens_cache_read,usage_output_tokens,web_search_count,inference_geo,speed
2026-03-22,claude-sonnet-4-20250514,test-key,Default,standard,≤ 200k,100000,0,0,0,5000,10,not_available,
2026-03-22,claude-haiku-4-5-20251001,test-key,Default,standard,≤ 200k,50000,0,0,0,2000,0,not_available,
2026-03-21,claude-sonnet-4-20250514,other-key,Default,standard,≤ 200k,200000,0,0,0,10000,5,not_available,
"""


def test_import_csv():
    resp = client.post(
        "/api/usage/import",
        files={"file": ("usage.csv", CSV_DATA.encode(), "text/csv")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["rows_imported"] == 3


def test_import_csv_upserts():
    """Importing the same CSV twice shouldn't create duplicates."""
    client.post("/api/usage/import", files={"file": ("u.csv", CSV_DATA.encode(), "text/csv")})
    client.post("/api/usage/import", files={"file": ("u.csv", CSV_DATA.encode(), "text/csv")})
    resp = client.get("/api/usage/daily?days=30")
    # Should have 2 dates, not 4
    assert len(resp.json()) == 2


def test_usage_summary():
    client.post("/api/usage/import", files={"file": ("u.csv", CSV_DATA.encode(), "text/csv")})
    resp = client.get("/api/usage/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["month_input_tokens"] == 350000
    assert data["month_output_tokens"] == 17000
    assert data["month_cost_eur"] > 0
    assert data["month_web_searches"] == 15


def test_usage_by_model():
    client.post("/api/usage/import", files={"file": ("u.csv", CSV_DATA.encode(), "text/csv")})
    resp = client.get("/api/usage/by-model")
    assert resp.status_code == 200
    models = {m["model"] for m in resp.json()}
    assert "claude-sonnet-4-20250514" in models
    assert "claude-haiku-4-5-20251001" in models


def test_usage_by_key():
    client.post("/api/usage/import", files={"file": ("u.csv", CSV_DATA.encode(), "text/csv")})
    resp = client.get("/api/usage/by-key")
    assert resp.status_code == 200
    keys = {k["api_key_name"] for k in resp.json()}
    assert "test-key" in keys
    assert "other-key" in keys


def test_usage_daily():
    client.post("/api/usage/import", files={"file": ("u.csv", CSV_DATA.encode(), "text/csv")})
    resp = client.get("/api/usage/daily?days=30")
    assert resp.status_code == 200
    dates = [d["usage_date"] for d in resp.json()]
    assert "2026-03-22" in dates
    assert "2026-03-21" in dates

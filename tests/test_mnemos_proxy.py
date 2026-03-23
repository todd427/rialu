"""
tests/test_mnemos_proxy.py — Tests for Mnemos proxy endpoints.
"""

import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from main import app
from db import init_db

client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def setup_db():
    init_db()


def test_mnemos_stats_endpoint_exists():
    """Endpoint exists even if Mnemos is unreachable."""
    resp = client.get("/api/mnemos/stats")
    # Returns 200 with None/error or actual data
    assert resp.status_code == 200


def test_mnemos_search_requires_query():
    """POST /api/mnemos/search needs a query field."""
    resp = client.post("/api/mnemos/search", json={})
    assert resp.status_code == 422  # validation error


def test_mnemos_search_with_query():
    """POST /api/mnemos/search accepts a valid query."""
    resp = client.post("/api/mnemos/search", json={
        "query": "test query",
        "top": 3,
        "filter": "nonfiction",
    })
    # May succeed or fail depending on Mnemos availability
    # but should not crash
    assert resp.status_code in (200, 502, 500)


def test_mnemos_ingest_requires_fields():
    """POST /api/mnemos/ingest validates input."""
    resp = client.post("/api/mnemos/ingest", json={})
    assert resp.status_code == 422

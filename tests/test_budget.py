"""
tests/test_budget.py — Integration tests for /api/budget and /api/apis endpoints.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup():
    init_db()


def test_list_budget_empty():
    resp = client.get("/api/budget")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_budget_entry():
    resp = client.post("/api/budget", json={
        "platform": "fly.io",
        "service_name": "mnemos",
        "cost_gbp": 8.40,
        "period": "monthly",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["platform"] == "fly.io"
    assert data["cost_gbp"] == 8.40


def test_budget_summary():
    client.post("/api/budget", json={
        "platform": "railway", "service_name": "anseo",
        "cost_gbp": 5.00, "period": "monthly",
    })
    resp = client.get("/api/budget/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "monthly_platform_gbp" in data
    assert "api_30d_gbp" in data
    assert "total_gbp" in data
    assert data["monthly_platform_gbp"] >= 5.0


def test_update_budget_entry():
    r = client.post("/api/budget", json={
        "platform": "cf", "service_name": "test", "cost_gbp": 0.0, "period": "monthly"
    })
    eid = r.json()["id"]
    resp = client.put(f"/api/budget/{eid}", json={"cost_gbp": 2.50})
    assert resp.status_code == 200
    assert resp.json()["cost_gbp"] == 2.50


def test_delete_budget_entry():
    r = client.post("/api/budget", json={
        "platform": "test", "service_name": "ephemeral", "cost_gbp": 1.0, "period": "monthly"
    })
    eid = r.json()["id"]
    assert client.delete(f"/api/budget/{eid}").status_code == 204


def test_list_apis_empty():
    resp = client.get("/api/apis")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_api_entry():
    resp = client.post("/api/apis", json={
        "name": "Claude API",
        "provider": "Anthropic",
        "billing_model": "per_token",
        "cost_per_unit_gbp": 0.003,
    })
    assert resp.status_code == 201
    assert resp.json()["name"] == "Claude API"


def test_update_api_entry():
    r = client.post("/api/apis", json={
        "name": "Test API", "provider": "Test", "billing_model": "free"
    })
    aid = r.json()["id"]
    resp = client.put(f"/api/apis/{aid}", json={"notes": "Updated"})
    assert resp.status_code == 200
    assert resp.json()["notes"] == "Updated"


def test_delete_api_entry():
    r = client.post("/api/apis", json={
        "name": "Delete Me", "provider": "Nobody", "billing_model": "free"
    })
    aid = r.json()["id"]
    assert client.delete(f"/api/apis/{aid}").status_code == 204

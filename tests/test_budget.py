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
    assert "monthly_platform_eur" in data
    assert "api_30d_eur" in data
    assert "total_eur" in data
    assert data["monthly_platform_eur"] >= 5.0


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


def test_costs_by_project_empty():
    resp = client.get("/api/apis/costs-by-project")
    assert resp.status_code == 200
    assert resp.json() == []


def test_costs_by_project_with_data():
    from db import db
    # Create a project and an API
    proj = client.post("/api/projects", json={
        "name": "TestProj", "status": "development"
    }).json()
    api = client.post("/api/apis", json={
        "name": "Claude", "provider": "Anthropic", "billing_model": "per_token"
    }).json()
    # Insert api_usage with project attribution
    with db() as conn:
        conn.execute("""
            INSERT INTO api_usage (api_id, project_id, tokens_in, tokens_out, call_count, cost_gbp)
            VALUES (?, ?, 1000, 500, 10, 1.50)
        """, (api["id"], proj["id"]))
        # Unattributed usage
        conn.execute("""
            INSERT INTO api_usage (api_id, project_id, tokens_in, tokens_out, call_count, cost_gbp)
            VALUES (?, NULL, 200, 100, 2, 0.30)
        """, (api["id"],))
    resp = client.get("/api/apis/costs-by-project")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    attributed = [d for d in data if d["project_name"] == "TestProj"]
    assert len(attributed) == 1
    assert attributed[0]["cost_eur"] == 1.50
    assert attributed[0]["tokens_in"] == 1000
    unattr = [d for d in data if d["project_name"] == "Unattributed"]
    assert len(unattr) == 1
    assert unattr[0]["cost_eur"] == 0.30

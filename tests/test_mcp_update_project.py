"""
tests/test_mcp_update_project.py — Tests for the update_project MCP tool.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db, db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup():
    init_db()


def _create_project(name="TestProj", status="development"):
    resp = client.post("/api/projects", json={"name": name, "status": status})
    return resp.json()


def test_update_project_status():
    proj = _create_project()
    from mcp_server import update_project
    result = update_project(project_id=proj["id"], status="deployed")
    assert result["status"] == "deployed"
    assert result["id"] == proj["id"]


def test_update_project_multiple_fields():
    proj = _create_project()
    from mcp_server import update_project
    result = update_project(
        project_id=proj["id"],
        status="deployed",
        platform="railway",
        site_url="https://example.com",
    )
    assert result["status"] == "deployed"
    assert result["platform"] == "railway"
    assert result["site_url"] == "https://example.com"


def test_update_project_no_fields_returns_error():
    proj = _create_project()
    from mcp_server import update_project
    result = update_project(project_id=proj["id"])
    assert "error" in result
    assert "No fields" in result["error"]


def test_update_project_not_found_returns_error():
    from mcp_server import update_project
    result = update_project(project_id=99999, status="deployed")
    assert "error" in result
    assert "99999" in result["error"]

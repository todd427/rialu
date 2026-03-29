"""
tests/test_mcp_projects_crud.py — Tests for create_project and get_project MCP tools.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db, db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup():
    init_db()


def test_create_project_basic():
    from mcp_server import create_project
    result = create_project(name="Litir", status="development", platform="fly.io")
    assert result["id"]
    assert result["name"] == "Litir"
    assert result["slug"] == "litir"
    assert result["status"] == "development"
    assert result["platform"] == "fly.io"


def test_create_project_slug_collision():
    from mcp_server import create_project
    first = create_project(name="My App")
    assert first["slug"] == "my-app"
    second = create_project(name="My App")
    assert second["slug"] != first["slug"]
    assert second["slug"].startswith("my-app-")


def test_get_project_exists():
    from mcp_server import create_project, get_project
    created = create_project(name="Anseo", phase="phase-1", notes="test notes")
    fetched = get_project(project_id=created["id"])
    assert fetched["id"] == created["id"]
    assert fetched["name"] == "Anseo"
    assert fetched["phase"] == "phase-1"
    assert fetched["notes"] == "test notes"


def test_get_project_missing():
    from mcp_server import get_project
    result = get_project(project_id=99999)
    assert "error" in result
    assert "99999" in result["error"]

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


def test_list_projects_returns_all_rows():
    """Guard against payload-truncation under-reporting: list_projects must
    return exactly as many rows as there are projects, regardless of size."""
    from mcp_server import create_project, list_projects
    # Seed enough projects, each with a large notes blob, that a full-notes
    # projection would produce a bloated (truncatable) payload.
    fat_notes = "x" * 4000
    for i in range(30):
        create_project(name=f"Project {i}", notes=fat_notes)

    with db() as conn:
        db_count = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]

    listed = list_projects()
    assert len(listed) == db_count


def test_list_projects_omits_notes():
    """The lean projection must not carry the free-text notes column — that
    bloat is what truncated the response and under-reported the count."""
    from mcp_server import create_project, list_projects
    create_project(name="Notted", notes="secret sauce " * 100)
    listed = list_projects()
    assert listed, "expected at least one project"
    for row in listed:
        assert "notes" not in row

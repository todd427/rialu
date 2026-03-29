"""
tests/test_status_sync.py — Tests for automatic project status transitions.
"""

import pytest
from unittest.mock import patch, AsyncMock

from db import init_db, db
from poller import sync_project_status


@pytest.fixture(autouse=True)
def setup():
    init_db()


def _create_project(name, slug, status):
    with db() as conn:
        conn.execute(
            "INSERT INTO projects (name, slug, status) VALUES (?, ?, ?)",
            (name, slug, status),
        )
        return conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()["id"]


def _add_deploy(service_name, status):
    with db() as conn:
        conn.execute(
            """INSERT INTO deployments_cache (platform, service_name, status)
               VALUES ('fly.io', ?, ?)""",
            (service_name, status),
        )


def _add_milestone(project_id, title, done=False):
    with db() as conn:
        conn.execute(
            "INSERT INTO milestones (project_id, title, done) VALUES (?, ?, ?)",
            (project_id, title, int(done)),
        )


def _add_worklog(project_id, lines_added=100):
    with db() as conn:
        conn.execute(
            "INSERT INTO worklog (project_id, date, minutes, session_type, lines_added, lines_removed) VALUES (?, date('now'), 30, 'code', ?, 0)",
            (project_id, lines_added),
        )


def _get_status(slug):
    with db() as conn:
        return conn.execute("SELECT status FROM projects WHERE slug = ?", (slug,)).fetchone()["status"]


def _get_runtime(slug):
    with db() as conn:
        return conn.execute("SELECT runtime FROM projects WHERE slug = ?", (slug,)).fetchone()["runtime"]


@pytest.mark.asyncio
async def test_development_to_deployed():
    """Project in development with healthy deploy → deployed, runtime → running."""
    _create_project("Mnemos", "mnemos", "development")
    _add_deploy("mnemos", "healthy")
    await sync_project_status()
    assert _get_status("mnemos") == "deployed"
    assert _get_runtime("mnemos") == "running"


@pytest.mark.asyncio
async def test_deployed_stays_deployed_with_stopped_deploy():
    """Deployed project with stopped deploy stays deployed, runtime → sleeping."""
    _create_project("Park", "park", "deployed")
    _add_deploy("park-api", "stopped")
    await sync_project_status()
    assert _get_status("park") == "deployed"
    assert _get_runtime("park") == "sleeping"


@pytest.mark.asyncio
async def test_paused_to_deployed():
    """Paused project with healthy deploy → deployed."""
    _create_project("Park", "park", "paused")
    _add_deploy("park-api", "healthy")
    await sync_project_status()
    assert _get_status("park") == "deployed"


@pytest.mark.asyncio
async def test_research_to_development():
    """Research project with code commits → development."""
    pid = _create_project("NewProj", "newproj", "research")
    _add_worklog(pid, lines_added=50)
    await sync_project_status()
    assert _get_status("newproj") == "development"


@pytest.mark.asyncio
async def test_research_stays_without_code():
    """Research project without code stays research."""
    _create_project("Ideas", "ideas", "research")
    await sync_project_status()
    assert _get_status("ideas") == "research"


@pytest.mark.asyncio
async def test_all_milestones_done_ships():
    """Project with all milestones done and no healthy deploy → shipped."""
    pid = _create_project("Done", "done", "development")
    _add_milestone(pid, "Alpha", done=True)
    _add_milestone(pid, "Beta", done=True)
    _add_milestone(pid, "Release", done=True)
    await sync_project_status()
    assert _get_status("done") == "shipped"


@pytest.mark.asyncio
async def test_all_milestones_done_but_deployed_stays():
    """Active deployed service with all milestones done stays deployed."""
    pid = _create_project("Active", "active", "deployed")
    _add_deploy("active", "healthy")
    _add_milestone(pid, "v1", done=True)
    _add_milestone(pid, "v2", done=True)
    _add_milestone(pid, "v3", done=True)
    await sync_project_status()
    assert _get_status("active") == "deployed"


@pytest.mark.asyncio
async def test_partial_milestones_no_ship():
    """Project with some milestones incomplete stays as-is."""
    pid = _create_project("WIP", "wip", "deployed")
    _add_deploy("wip", "healthy")
    _add_milestone(pid, "Done", done=True)
    _add_milestone(pid, "Not done", done=False)
    await sync_project_status()
    assert _get_status("wip") == "deployed"


@pytest.mark.asyncio
async def test_no_milestones_no_ship():
    """Project with zero milestones doesn't auto-ship."""
    _create_project("Bare", "bare", "deployed")
    _add_deploy("bare", "healthy")
    await sync_project_status()
    assert _get_status("bare") == "deployed"


@pytest.mark.asyncio
async def test_too_few_milestones_no_ship():
    """Project with <3 milestones all done doesn't auto-ship."""
    pid = _create_project("Small", "small", "deployed")
    _add_milestone(pid, "Only one", done=True)
    await sync_project_status()
    assert _get_status("small") == "deployed"


@pytest.mark.asyncio
async def test_shipped_stays_shipped():
    """Shipped projects are never changed."""
    _create_project("Legacy", "legacy", "shipped")
    _add_deploy("legacy", "stopped")
    await sync_project_status()
    assert _get_status("legacy") == "shipped"


@pytest.mark.asyncio
async def test_fuzzy_match_service_name():
    """Deploy name 'sentinel-foxxelabs' matches project slug 'sentinel'."""
    _create_project("Sentinel", "sentinel", "development")
    _add_deploy("sentinel-foxxelabs", "healthy")
    await sync_project_status()
    assert _get_status("sentinel") == "deployed"

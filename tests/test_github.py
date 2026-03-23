"""
tests/test_github.py — Tests for /api/github endpoints.
"""

import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from main import app
from db import init_db, db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup():
    init_db()


def _seed_repo(full_name="todd427/test-repo", name="test-repo", html_url="https://github.com/todd427/test-repo"):
    """Insert a repo into the cache."""
    with db() as conn:
        conn.execute("""
            INSERT INTO github_repos (id, full_name, name, description, html_url, language, private, fork, archived, stars, pushed_at)
            VALUES (?, ?, ?, 'A test repo', ?, 'Python', 0, 0, 0, 5, '2026-03-20 10:00:00')
        """, (1, full_name, name, html_url))


def test_list_repos_empty():
    resp = client.get("/api/github/repos")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_repos():
    _seed_repo()
    resp = client.get("/api/github/repos")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["name"] == "test-repo"


def test_untracked_all_when_no_projects():
    _seed_repo()
    resp = client.get("/api/github/untracked")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_untracked_excludes_linked():
    """If a project has the repo_url, it shouldn't appear as untracked."""
    _seed_repo()
    client.post("/api/projects", json={
        "name": "test-repo", "status": "development",
        "repo_url": "https://github.com/todd427/test-repo",
    })
    resp = client.get("/api/github/untracked")
    assert resp.status_code == 200
    assert len(resp.json()) == 0


def test_untracked_excludes_archived():
    """Archived repos should not appear as untracked."""
    with db() as conn:
        conn.execute("""
            INSERT INTO github_repos (id, full_name, name, html_url, private, fork, archived, stars)
            VALUES (2, 'todd427/old', 'old', 'https://github.com/todd427/old', 0, 0, 1, 0)
        """)
    resp = client.get("/api/github/untracked")
    assert resp.status_code == 200
    assert len(resp.json()) == 0


def test_adopt_repo():
    _seed_repo()
    resp = client.post("/api/github/adopt", json={
        "repo_full_name": "todd427/test-repo",
        "status": "development",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "test-repo"
    assert data["repo_url"] == "https://github.com/todd427/test-repo"
    assert data["notes"] == "A test repo"


def test_adopt_repo_not_found():
    resp = client.post("/api/github/adopt", json={
        "repo_full_name": "nonexistent/repo",
    })
    assert resp.status_code == 404


def test_create_repo():
    """Creating a GitHub repo calls the API and creates a project."""
    from unittest.mock import MagicMock
    mock_gh_response = MagicMock()
    mock_gh_response.status_code = 201
    mock_gh_response.json.return_value = {
        "id": 99,
        "full_name": "todd427/new-proj",
        "name": "new-proj",
        "html_url": "https://github.com/todd427/new-proj",
        "description": "New project",
        "private": True,
        "pushed_at": "2026-03-23T00:00:00Z",
        "created_at": "2026-03-23T00:00:00Z",
    }

    with patch("routers.github.GITHUB_TOKEN", "fake-token"):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_gh_response):
            resp = client.post("/api/github/create", json={
                "name": "new-proj",
                "description": "New project",
                "private": True,
                "status": "research",
            })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "new-proj"
    assert data["repo_url"] == "https://github.com/todd427/new-proj"
    assert data["status"] == "research"


def test_create_repo_no_token():
    with patch("routers.github.GITHUB_TOKEN", ""):
        resp = client.post("/api/github/create", json={
            "name": "nope",
        })
    assert resp.status_code == 503

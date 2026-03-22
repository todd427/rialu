"""
tests/test_milestone_review.py — Tests for automated milestone review.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from main import app
from db import init_db, db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup():
    init_db()


def _create_project_with_milestone(name="TestProj", milestone="Build the thing", repo_url=None):
    resp = client.post("/api/projects", json={
        "name": name, "status": "development", "repo_url": repo_url,
    })
    pid = resp.json()["id"]
    resp = client.post(f"/api/projects/{pid}/milestones", json={"title": milestone})
    mid = resp.json()["id"]
    return pid, mid


def test_review_no_github_token(monkeypatch):
    monkeypatch.setenv("GITHUB_PAT", "")
    _create_project_with_milestone()
    resp = client.post("/api/milestones/review")
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_review_no_open_milestones(monkeypatch):
    import routers.milestone_review as mr
    monkeypatch.setattr(mr, "GITHUB_TOKEN", "fake-token")
    resp = client.post("/api/milestones/review")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("reviewed", 0) == 0
    assert data["results"] == []


def test_review_skips_no_repo(monkeypatch):
    import routers.milestone_review as mr
    monkeypatch.setattr(mr, "GITHUB_TOKEN", "fake-token")
    _create_project_with_milestone("NoRepo", "Some milestone", repo_url=None)
    resp = client.post("/api/milestones/review")
    data = resp.json()
    assert data["reviewed"] == 1
    assert data["completed"] == 0
    assert "No GitHub repo URL" in data["results"][0]["evidence"]


def test_review_skips_non_github(monkeypatch):
    import routers.milestone_review as mr
    monkeypatch.setattr(mr, "GITHUB_TOKEN", "fake-token")
    _create_project_with_milestone("NonGH", "Deploy site", repo_url="https://example.com")
    resp = client.post("/api/milestones/review")
    data = resp.json()
    assert data["completed"] == 0
    assert "No GitHub repo URL" in data["results"][0]["evidence"]


def test_review_log_endpoint():
    resp = client.get("/api/milestones/review/log")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_review_logs_decisions(monkeypatch):
    import routers.milestone_review as mr
    monkeypatch.setattr(mr, "GITHUB_TOKEN", "fake-token")
    _create_project_with_milestone("LogTest", "Test milestone", repo_url=None)
    client.post("/api/milestones/review")
    resp = client.get("/api/milestones/review/log")
    logs = resp.json()
    assert len(logs) == 1
    assert logs[0]["action"] == "unchanged"
    assert logs[0]["project_name"] == "LogTest"


def test_keyword_extraction():
    from routers.milestone_review import _extract_keywords
    kw = _extract_keywords("Phase 3: Anthropic token usage tracking (CSV import)")
    assert "anthropic" in kw
    assert "token" in kw
    assert "csv" in kw or "import" in kw

    kw = _extract_keywords("OAuth 2.1 upgrade")
    assert "oauth" in kw

    kw = _extract_keywords("Phase 1: Foundation")
    assert "foundation" in kw

"""
tests/test_commits.py — Tests for commit activity endpoints.
"""

from datetime import date, timedelta

from fastapi.testclient import TestClient

from main import app
from db import init_db, db

client = TestClient(app)


def _seed_project(name="Test Project"):
    r = client.post("/api/projects", json={"name": name, "status": "development"})
    assert r.status_code == 201
    return r.json()["id"]


def _seed_commits(project_id, day_offset=0, notes="[auto-git] abc123 feat: thing | def456 fix: bug", lines_added=100, lines_removed=10):
    """Insert an [auto-git] worklog row for a given day."""
    d = (date.today() - timedelta(days=day_offset)).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO worklog (project_id, date, minutes, session_type, notes, lines_added, lines_removed) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, d, 30, "code", notes, lines_added, lines_removed),
        )


def test_commits_empty():
    init_db()
    pid = _seed_project()
    resp = client.get(f"/api/projects/{pid}/commits")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_commits"] == 0
    assert data["total_days_active"] == 0
    assert data["series"] == []
    assert data["peak_day"] is None


def test_commits_single_day():
    init_db()
    pid = _seed_project()
    _seed_commits(pid, day_offset=0, notes="[auto-git] abc123 feat: new thing", lines_added=150, lines_removed=20)
    resp = client.get(f"/api/projects/{pid}/commits")
    data = resp.json()
    assert data["total_commits"] == 1
    assert data["total_days_active"] == 1
    assert len(data["series"]) == 1
    assert data["series"][0]["commits"] == 1
    assert data["series"][0]["messages"] == ["feat: new thing"]
    assert data["series"][0]["lines_added"] == 150
    assert data["series"][0]["lines_removed"] == 20


def test_commits_multiple_per_day():
    init_db()
    pid = _seed_project()
    _seed_commits(pid, day_offset=0, notes="[auto-git] a1 feat: one | b2 fix: two | c3 docs: three")
    resp = client.get(f"/api/projects/{pid}/commits")
    data = resp.json()
    assert data["total_commits"] == 3
    assert data["total_days_active"] == 1
    assert data["peak_day"]["commits"] == 3


def test_commits_multiple_days():
    init_db()
    pid = _seed_project()
    _seed_commits(pid, day_offset=0, notes="[auto-git] a1 feat: today")
    _seed_commits(pid, day_offset=1, notes="[auto-git] b1 fix: yesterday | b2 docs: readme")
    resp = client.get(f"/api/projects/{pid}/commits")
    data = resp.json()
    assert data["total_commits"] == 3
    assert data["total_days_active"] == 2
    assert data["peak_day"]["commits"] == 2  # yesterday had 2


def test_commits_days_filter():
    init_db()
    pid = _seed_project()
    _seed_commits(pid, day_offset=0, notes="[auto-git] a1 recent")
    _seed_commits(pid, day_offset=40, notes="[auto-git] b1 older")
    # 30-day window should exclude the 40-day-old entry
    resp = client.get(f"/api/projects/{pid}/commits?days=30")
    data = resp.json()
    assert data["total_commits"] == 1
    # 90-day window should include both
    resp = client.get(f"/api/projects/{pid}/commits?days=90")
    data = resp.json()
    assert data["total_commits"] == 2


def test_commits_ignores_non_autogit():
    init_db()
    pid = _seed_project()
    _seed_commits(pid, day_offset=0, notes="[auto-git] a1 real commit")
    # Insert a manual worklog entry (not [auto-git])
    d = date.today().isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO worklog (project_id, date, minutes, session_type, notes) VALUES (?, ?, ?, ?, ?)",
            (pid, d, 60, "code", "Manual work session"),
        )
    resp = client.get(f"/api/projects/{pid}/commits")
    data = resp.json()
    assert data["total_commits"] == 1


def test_commits_404():
    init_db()
    resp = client.get("/api/projects/99999/commits")
    assert resp.status_code == 404


def test_commits_csv():
    init_db()
    pid = _seed_project()
    _seed_commits(pid, day_offset=0, notes="[auto-git] a1 feat: thing | b2 fix: bug")
    resp = client.get(f"/api/projects/{pid}/commits?format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    lines = resp.text.strip().splitlines()
    assert lines[0] == "date,commits,lines_added,lines_removed,messages"
    assert len(lines) == 2  # header + 1 data row


def test_commits_csv_empty():
    init_db()
    pid = _seed_project()
    resp = client.get(f"/api/projects/{pid}/commits?format=csv")
    assert resp.status_code == 200
    assert resp.text == ""


# --- Global endpoint: GET /api/commits ---


def test_global_commits_empty():
    init_db()
    resp = client.get("/api/commits")
    assert resp.status_code == 200
    data = resp.json()
    assert data["series"] == []


def test_global_commits_single_project():
    init_db()
    pid = _seed_project("Alpha")
    _seed_commits(pid, day_offset=0, notes="[auto-git] a1 feat: one | a2 fix: two")
    resp = client.get("/api/commits")
    data = resp.json()
    assert len(data["series"]) == 1
    day = data["series"][0]
    assert day["total_commits"] == 2
    assert len(day["by_project"]) == 1
    assert day["by_project"][0]["name"] == "Alpha"
    assert day["by_project"][0]["commits"] == 2


def test_global_commits_multiple_projects():
    init_db()
    pid_a = _seed_project("Alpha")
    pid_b = _seed_project("Beta")
    _seed_commits(pid_a, day_offset=0, notes="[auto-git] a1 feat: alpha")
    _seed_commits(pid_b, day_offset=0, notes="[auto-git] b1 feat: beta1 | b2 feat: beta2")
    resp = client.get("/api/commits")
    data = resp.json()
    assert len(data["series"]) == 1
    day = data["series"][0]
    assert day["total_commits"] == 3
    # by_project sorted by commits desc — Beta (2) before Alpha (1)
    assert day["by_project"][0]["name"] == "Beta"
    assert day["by_project"][0]["commits"] == 2
    assert day["by_project"][1]["name"] == "Alpha"
    assert day["by_project"][1]["commits"] == 1


def test_global_commits_multiple_days():
    init_db()
    pid = _seed_project("Alpha")
    _seed_commits(pid, day_offset=0, notes="[auto-git] a1 today")
    _seed_commits(pid, day_offset=3, notes="[auto-git] b1 three days ago")
    resp = client.get("/api/commits")
    data = resp.json()
    assert len(data["series"]) == 2
    # Sorted by date ascending
    assert data["series"][0]["date"] < data["series"][1]["date"]


def test_global_commits_days_filter():
    init_db()
    pid = _seed_project("Alpha")
    _seed_commits(pid, day_offset=0, notes="[auto-git] a1 recent")
    _seed_commits(pid, day_offset=50, notes="[auto-git] b1 old")
    resp = client.get("/api/commits?days=30")
    data = resp.json()
    assert len(data["series"]) == 1
    resp = client.get("/api/commits?days=90")
    data = resp.json()
    assert len(data["series"]) == 2


def test_global_commits_csv():
    init_db()
    pid_a = _seed_project("Alpha")
    pid_b = _seed_project("Beta")
    _seed_commits(pid_a, day_offset=0, notes="[auto-git] a1 feat: alpha")
    _seed_commits(pid_b, day_offset=0, notes="[auto-git] b1 feat: beta")
    resp = client.get("/api/commits?format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    lines = resp.text.strip().splitlines()
    assert lines[0] == "date,project,commits"
    assert len(lines) == 3  # header + 2 project rows


def test_global_commits_csv_empty():
    init_db()
    resp = client.get("/api/commits?format=csv")
    assert resp.status_code == 200
    assert resp.text == ""


# --- commits_7d on project list ---


def test_projects_list_commits_7d_zero():
    init_db()
    _seed_project("Empty")
    resp = client.get("/api/projects")
    data = resp.json()
    assert data[0]["commits_7d"] == 0


def test_projects_list_commits_7d_counted():
    init_db()
    pid = _seed_project("Active")
    _seed_commits(pid, day_offset=0, notes="[auto-git] a1 feat: one | a2 fix: two")
    _seed_commits(pid, day_offset=2, notes="[auto-git] b1 docs: three")
    resp = client.get("/api/projects")
    proj = [p for p in resp.json() if p["id"] == pid][0]
    assert proj["commits_7d"] == 3  # 2 + 1, not 2 rows


def test_projects_list_commits_7d_excludes_old():
    init_db()
    pid = _seed_project("Mixed")
    _seed_commits(pid, day_offset=0, notes="[auto-git] a1 recent")
    _seed_commits(pid, day_offset=10, notes="[auto-git] b1 old | b2 also old")
    resp = client.get("/api/projects")
    proj = [p for p in resp.json() if p["id"] == pid][0]
    assert proj["commits_7d"] == 1  # only the recent one

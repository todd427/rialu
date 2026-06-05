"""
tests/test_divergence.py — Tests for the portfolio divergence digest.

Covers the classification rules, persistence, idempotency, and the read
endpoints. Reads only projects/worklog — no external calls to mock.
"""

from datetime import date, timedelta

from fastapi.testclient import TestClient

from main import app
from db import init_db, db

client = TestClient(app)


def _project(name, status="development", notes=None, revisit_trigger=None):
    r = client.post("/api/projects", json={"name": name, "status": status, "notes": notes})
    assert r.status_code == 201
    pid = r.json()["id"]
    if revisit_trigger is not None:
        with db() as conn:
            conn.execute(
                "UPDATE projects SET revisit_trigger = ? WHERE id = ?", (revisit_trigger, pid)
            )
    return pid


def _commits(pid, day_offset=0, notes="[auto-git] abc feat: a | def fix: b"):
    """Insert an [auto-git] worklog row `day_offset` days ago."""
    d = (date.today() - timedelta(days=day_offset)).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO worklog (project_id, date, minutes, session_type, notes) VALUES (?,?,?,?,?)",
            (pid, d, 30, "code", notes),
        )


def _manual(pid, day_offset=0, notes="hand-written session note"):
    d = (date.today() - timedelta(days=day_offset)).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO worklog (project_id, date, minutes, session_type, notes) VALUES (?,?,?,?,?)",
            (pid, d, 45, "code", notes),
        )


def _flag_for(results, pid):
    return next(r["flag"] for r in results if r["project_id"] == pid)


# ── classification rules ──────────────────────────────────────────────────────

def test_stale_active():
    init_db()
    pid = _project("Litir", status="development")  # no worklog at all
    results = client.post("/api/divergence/run").json()["results"]
    assert _flag_for(results, pid) == "stale-active"


def test_healthy_recent_commits():
    init_db()
    pid = _project("Active", status="running")
    _commits(pid, day_offset=2)
    results = client.post("/api/divergence/run").json()["results"]
    assert _flag_for(results, pid) == "healthy"


def test_manual_worklog_in_window_is_healthy_not_stale():
    init_db()
    pid = _project("Hands", status="development")
    _manual(pid, day_offset=3)  # non-auto-git activity, no commits
    results = client.post("/api/divergence/run").json()["results"]
    assert _flag_for(results, pid) == "healthy"


def test_old_commits_outside_window_are_stale():
    init_db()
    pid = _project("Quiet", status="development")
    _commits(pid, day_offset=45)  # outside default 30d window
    results = client.post("/api/divergence/run").json()["results"]
    assert _flag_for(results, pid) == "stale-active"


def test_no_trigger_paused_without_marker():
    init_db()
    pid = _project("Parked", status="paused", notes="some idea I had")
    results = client.post("/api/divergence/run").json()["results"]
    assert _flag_for(results, pid) == "no-trigger"


def test_trigger_marker_in_notes_is_dormant_ok():
    init_db()
    pid = _project("Planned", status="research", notes="trigger: post-viva")
    results = client.post("/api/divergence/run").json()["results"]
    assert _flag_for(results, pid) == "dormant-ok"


def test_revisit_trigger_column_is_dormant_ok():
    init_db()
    pid = _project("ColumnPlan", status="paused", revisit_trigger="after Cló MVP ships")
    results = client.post("/api/divergence/run").json()["results"]
    assert _flag_for(results, pid) == "dormant-ok"


def test_deployed_without_recent_commits_is_dormant_ok():
    init_db()
    pid = _project("Mnemos", status="deployed")  # no commits in 90d
    results = client.post("/api/divergence/run").json()["results"]
    assert _flag_for(results, pid) == "dormant-ok"


def test_deployed_with_recent_commit_is_active_class():
    init_db()
    pid = _project("Sentinel", status="deployed")
    _commits(pid, day_offset=5)  # within window -> active and healthy
    results = client.post("/api/divergence/run").json()["results"]
    assert _flag_for(results, pid) == "healthy"


def test_deployed_commit_in_90d_but_not_window_is_stale():
    init_db()
    pid = _project("Edge", status="deployed")
    _commits(pid, day_offset=60)  # active-class (commit in 90d) but quiet in 30d
    results = client.post("/api/divergence/run").json()["results"]
    assert _flag_for(results, pid) == "stale-active"


def test_shipped_is_dormant_ok():
    init_db()
    pid = _project("Done", status="shipped")
    results = client.post("/api/divergence/run").json()["results"]
    assert _flag_for(results, pid) == "dormant-ok"


# ── commit counting reuses commits.py parsing ─────────────────────────────────

def test_commit_count_parses_pipe_delimited():
    from routers.commits import _parse_commit_count
    notes = "[auto-git] a feat: x | b fix: y | c chore: z"
    assert _parse_commit_count(notes) == 3


# ── persistence + endpoints ───────────────────────────────────────────────────

def test_run_persists_health_and_log():
    init_db()
    pid = _project("Logged", status="development")
    resp = client.post("/api/divergence/run")
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["checked"] == 1
    assert summary["window_days"] == 30
    assert summary["flags"].get("stale-active") == 1

    # projects.health updated
    with db() as conn:
        row = conn.execute("SELECT health, health_checked_at FROM projects WHERE id = ?", (pid,)).fetchone()
    assert row["health"] == "stale-active"
    assert row["health_checked_at"]

    # log row written
    logs = client.get("/api/divergence/log").json()
    assert len(logs) == 1
    assert logs[0]["project_id"] == pid
    assert logs[0]["flag"] == "stale-active"


def test_latest_endpoint():
    init_db()
    pid = _project("LatestProj", status="development")
    client.post("/api/divergence/run")
    latest = client.get("/api/divergence/latest").json()
    entry = next(e for e in latest if e["project_id"] == pid)
    assert entry["flag"] == "stale-active"
    assert entry["project"] == "LatestProj"


def test_idempotency_one_health_two_log_rows():
    init_db()
    pid = _project("Twice", status="development")
    client.post("/api/divergence/run")
    client.post("/api/divergence/run")
    with db() as conn:
        health_count = conn.execute(
            "SELECT COUNT(*) c FROM projects WHERE id = ?", (pid,)
        ).fetchone()["c"]
        log_count = conn.execute(
            "SELECT COUNT(*) c FROM divergence_log WHERE project_id = ?", (pid,)
        ).fetchone()["c"]
    assert health_count == 1  # single project row, single health value
    assert log_count == 2     # two runs -> two log rows


def test_window_days_override():
    init_db()
    pid = _project("Windowed", status="development")
    _commits(pid, day_offset=20)  # inside 30d, outside 10d
    r30 = client.post("/api/divergence/run", params={"window_days": 30}).json()["results"]
    assert _flag_for(r30, pid) == "healthy"
    r10 = client.post("/api/divergence/run", params={"window_days": 10}).json()["results"]
    assert _flag_for(r10, pid) == "stale-active"


def test_health_exposed_on_projects_list():
    init_db()
    pid = _project("Exposed", status="development")
    client.post("/api/divergence/run")
    projects = client.get("/api/projects").json()
    p = next(x for x in projects if x["id"] == pid)
    assert p["health"] == "stale-active"
    assert p["health_detail"]

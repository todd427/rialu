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


def _project(name, status="development", notes=None, revisit_trigger=None, phase=None):
    r = client.post(
        "/api/projects", json={"name": name, "status": status, "notes": notes, "phase": phase}
    )
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
    entry = next(e for e in latest["projects"] if e["project_id"] == pid)
    assert entry["health"] == "stale-active"
    assert entry["name"] == "LatestProj"
    assert entry["health_detail"]


def test_latest_counts_and_deadline():
    init_db()
    _project("A", status="development")          # stale-active
    _project("B", status="paused", notes="idea")  # no-trigger
    c = _project("C", status="running")
    _commits(c, day_offset=2)                      # healthy
    client.post("/api/divergence/run")
    data = client.get("/api/divergence/latest").json()
    assert data["counts"]["stale-active"] == 1
    assert data["counts"]["no-trigger"] == 1
    assert data["counts"]["healthy"] == 1
    # Deadline card removed 2026-08-20 (hardcoded viva date had passed).
    assert "days_to_deadline" not in data
    assert "deadline_label" not in data


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


# ── narrative staleness — the second, orthogonal axis ─────────────────────────
#
# The bug this section exists to prevent: narrative_written_at must measure when
# the PROSE was authored, not when the row was last touched. Anything that moves
# it on an incidental edit makes stale prose look fresh, silently.

def _set_narrative(pid, day_offset):
    """Backdate narrative_written_at so 'unchanged' is provably distinguishable."""
    d = (date.today() - timedelta(days=day_offset)).isoformat()
    with db() as conn:
        conn.execute(
            "UPDATE projects SET narrative_written_at = ? WHERE id = ?", (f"{d} 12:00:00", pid)
        )


def _narrative_at(pid):
    with db() as conn:
        return conn.execute(
            "SELECT narrative_written_at FROM projects WHERE id = ?", (pid,)
        ).fetchone()["narrative_written_at"]


def _many_commits(pid, n, day_offset=1):
    """One [auto-git] worklog row carrying n pipe-delimited commits."""
    _commits(pid, day_offset=day_offset,
             notes="[auto-git] " + " | ".join(f"h{i} feat: change {i}" for i in range(n)))


def _narrative_flag_for(results, pid):
    return next(r["narrative_flag"] for r in results if r["project_id"] == pid)


def test_narrative_written_at_set_on_create_with_prose():
    init_db()
    pid = _project("Rian", phase="phase-1 (scaffold)")
    assert _narrative_at(pid)


def test_narrative_written_at_null_when_created_without_prose():
    init_db()
    pid = _project("Bare")
    assert _narrative_at(pid) is None


def test_phase_change_redates_narrative():
    init_db()
    pid = _project("Rian", phase="phase-1 (scaffold)")
    _set_narrative(pid, 40)
    before = _narrative_at(pid)
    client.put(f"/api/projects/{pid}", json={"phase": "phase-5 (shipped)"})
    assert _narrative_at(pid) != before


def test_notes_change_redates_narrative():
    init_db()
    pid = _project("Uire", notes="manifold empty, 0 scored")
    _set_narrative(pid, 40)
    before = _narrative_at(pid)
    client.put(f"/api/projects/{pid}", json={"notes": "73-paper manifold, publishing nightly"})
    assert _narrative_at(pid) != before


def test_identical_phase_does_not_redate_narrative():
    init_db()
    pid = _project("Same", phase="phase-1")
    _set_narrative(pid, 40)
    before = _narrative_at(pid)
    client.put(f"/api/projects/{pid}", json={"phase": "phase-1"})  # present, unchanged
    assert _narrative_at(pid) == before


def test_unrelated_field_does_not_redate_narrative():
    """The regression that matters: updated_at moves, the narrative date does not."""
    init_db()
    pid = _project("Incidental", phase="phase-1")
    _set_narrative(pid, 40)
    before = _narrative_at(pid)
    client.put(f"/api/projects/{pid}", json={"machine": "daisy", "platform": "fly.io"})
    assert _narrative_at(pid) == before


def test_mcp_update_path_honours_the_same_guard():
    init_db()
    from mcp_server import update_project as mcp_update
    pid = _project("ViaMcp", phase="phase-1")
    _set_narrative(pid, 40)
    before = _narrative_at(pid)

    mcp_update(project_id=pid, status="deployed")          # unrelated field
    assert _narrative_at(pid) == before
    mcp_update(project_id=pid, phase="phase-1")            # present, unchanged
    assert _narrative_at(pid) == before
    mcp_update(project_id=pid, phase="phase-5 (shipped)")  # real change
    assert _narrative_at(pid) != before


def test_narrative_stale_above_threshold():
    init_db()
    pid = _project("Rian", phase="phase-1 (scaffold)")
    _set_narrative(pid, 40)
    _many_commits(pid, 31)  # 31 pipe-delimited commits in one row, dated yesterday
    results = client.post("/api/divergence/run").json()["results"]
    assert _narrative_flag_for(results, pid) == "narrative-stale"


def test_below_threshold_is_not_narrative_stale():
    init_db()
    pid = _project("Fresh", phase="phase-1")
    _set_narrative(pid, 40)
    _many_commits(pid, 4)
    results = client.post("/api/divergence/run").json()["results"]
    assert _narrative_flag_for(results, pid) is None


def test_narrative_stale_is_orthogonal_to_health():
    """Rian's case: committing steadily (healthy) while the phase string lies."""
    init_db()
    pid = _project("Rian", status="running", phase="phase-1 (scaffold)")
    _set_narrative(pid, 40)
    _many_commits(pid, 31)
    results = client.post("/api/divergence/run").json()["results"]
    assert _flag_for(results, pid) == "healthy"
    assert _narrative_flag_for(results, pid) == "narrative-stale"
    with db() as conn:
        row = conn.execute(
            "SELECT health, narrative_health FROM projects WHERE id = ?", (pid,)
        ).fetchone()
    assert row["health"] == "healthy"
    assert row["narrative_health"] == "narrative-stale"


def test_null_narrative_written_at_never_flags():
    """Absence of an authoring date is not staleness."""
    init_db()
    pid = _project("Undated", phase="phase-1")
    with db() as conn:
        conn.execute("UPDATE projects SET narrative_written_at = NULL WHERE id = ?", (pid,))
    _many_commits(pid, 40)
    results = client.post("/api/divergence/run").json()["results"]
    assert _narrative_flag_for(results, pid) is None


def test_project_without_prose_never_flags():
    """No phase and no notes: there is no narrative to be stale."""
    init_db()
    pid = _project("Wordless")
    _set_narrative(pid, 40)  # e.g. a migration backfill from updated_at
    _many_commits(pid, 40)
    results = client.post("/api/divergence/run").json()["results"]
    assert _narrative_flag_for(results, pid) is None


def test_narrative_counting_parses_pipe_delimited_commits():
    """3 commits in one worklog row count as 3, not 1 (reuses commits.py parsing)."""
    init_db()
    pid = _project("Counted", phase="phase-1")
    _set_narrative(pid, 40)
    _many_commits(pid, 3)
    results = client.post("/api/divergence/run").json()["results"]
    assert next(r["commits_since_narrative"] for r in results if r["project_id"] == pid) == 3


def test_commits_before_the_narrative_date_do_not_count():
    init_db()
    pid = _project("Prior", phase="phase-1")
    _set_narrative(pid, 10)
    _many_commits(pid, 20, day_offset=30)  # all landed before the prose was written
    results = client.post("/api/divergence/run").json()["results"]
    assert next(r["commits_since_narrative"] for r in results if r["project_id"] == pid) == 0
    assert _narrative_flag_for(results, pid) is None


def test_narrative_threshold_override():
    init_db()
    pid = _project("Tunable", phase="phase-1")
    _set_narrative(pid, 40)
    _many_commits(pid, 10)  # under the default 15, over an explicit 5
    default = client.post("/api/divergence/run").json()["results"]
    assert _narrative_flag_for(default, pid) is None
    tuned = client.post("/api/divergence/run", params={"narrative_threshold": 5}).json()["results"]
    assert _narrative_flag_for(tuned, pid) == "narrative-stale"


def test_narrative_log_row_does_not_hijack_health_detail():
    """Both axes share divergence_log; the health pill must keep its own detail."""
    init_db()
    pid = _project("Both", status="running", phase="phase-1")
    _set_narrative(pid, 40)
    _many_commits(pid, 31)
    client.post("/api/divergence/run")

    flags = {r["flag"] for r in client.get("/api/divergence/log").json()}
    assert flags == {"healthy", "narrative-stale"}

    entry = next(e for e in client.get("/api/divergence/latest").json()["projects"]
                 if e["project_id"] == pid)
    assert "commits in 30d" in entry["health_detail"]        # not the narrative text
    assert "since phase/notes written" in entry["narrative_detail"]
    assert entry["narrative_health"] == "narrative-stale"
    assert entry["commits_since_narrative"] == 31


def test_commits_since_narrative_on_projects_api():
    init_db()
    pid = _project("Exposed", phase="phase-1")
    _set_narrative(pid, 40)
    _many_commits(pid, 31)
    p = next(x for x in client.get("/api/projects").json() if x["id"] == pid)
    assert p["commits_since_narrative"] == 31


def test_mcp_list_projects_carries_commits_since_narrative():
    """The actual deliverable: one int per row, and the count still doesn't truncate."""
    init_db()
    from mcp_server import list_projects as mcp_list
    pid = _project("Rian", phase="phase-1 (scaffold)")
    _set_narrative(pid, 40)
    _many_commits(pid, 31)
    for i in range(30):
        _project(f"Filler {i}", notes="x" * 4000)

    listed = mcp_list()
    with db() as conn:
        db_count = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
    assert len(listed) == db_count
    assert all("commits_since_narrative" in row for row in listed)
    assert all("notes" not in row for row in listed)
    assert next(r for r in listed if r["id"] == pid)["commits_since_narrative"] == 31


def test_health_exposed_on_projects_list():
    init_db()
    pid = _project("Exposed", status="development")
    client.post("/api/divergence/run")
    projects = client.get("/api/projects").json()
    p = next(x for x in projects if x["id"] == pid)
    assert p["health"] == "stale-active"
    assert p["health_detail"]

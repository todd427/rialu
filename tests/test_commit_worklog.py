"""
tests/test_commit_worklog.py — Tests for auto-git worklog generation.
"""

import pytest
from datetime import datetime, timezone, timedelta

from db import init_db, db
from commit_worklog import (
    process_commits_for_worklog,
    _compute_minutes,
    _map_commits_to_projects,
    _build_notes,
    FIRST_COMMIT_MINUTES,
)


@pytest.fixture(autouse=True)
def setup():
    init_db()


def _create_project(slug="rialu", name="Rialú"):
    with db() as conn:
        conn.execute(
            "INSERT INTO projects (name, slug, status) VALUES (?, ?, 'development')",
            (name, slug),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _make_commits(base_time, gaps_minutes):
    """Create a list of commits with specified gaps between them.
    gaps_minutes is a list of minute offsets from base_time."""
    commits = []
    for i, offset in enumerate(gaps_minutes):
        dt = base_time + timedelta(minutes=offset)
        commits.append({
            "hash": f"abc{i:04d}",
            "message": f"Commit {i}",
            "dt": dt,
        })
    return commits


def _make_repo(name="rialu", commits=None):
    """Create a repo dict as the agent would send it."""
    repo = {
        "name": name,
        "path": f"/home/Projects/{name}",
        "branch": "main",
        "clean": True,
        "ahead": 0,
        "behind": 0,
        "last_commit": "abc0000",
        "last_message": "Latest",
    }
    if commits:
        repo["recent_commits"] = [
            {"hash": c["hash"], "message": c["message"], "timestamp": c["dt"].isoformat()}
            for c in commits
        ]
    return repo


# ── Gap heuristic tests ─────────────────────────────────────────────────────

def test_single_commit():
    base = datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc)
    commits = _make_commits(base, [0])
    assert _compute_minutes(commits) == FIRST_COMMIT_MINUTES


def test_two_commits_close():
    """Two commits 45 min apart = 30 (first) + 45 (gap) = 75 min."""
    base = datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc)
    commits = _make_commits(base, [0, 45])
    assert _compute_minutes(commits) == 75


def test_two_commits_far_apart():
    """Two commits 3 hours apart = 30 + 30 = 60 min (two separate sessions)."""
    base = datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc)
    commits = _make_commits(base, [0, 180])
    assert _compute_minutes(commits) == 60


def test_multiple_commits_mixed_gaps():
    """0, +30, +60, +200, +230 = session1(30+30+30) + session2(30+30) = 150 min."""
    base = datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc)
    commits = _make_commits(base, [0, 30, 60, 200, 230])
    assert _compute_minutes(commits) == 150


def test_no_commits():
    assert _compute_minutes([]) == 0


# ── Notes building ──────────────────────────────────────────────────────────

def test_build_notes():
    base = datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc)
    commits = _make_commits(base, [0, 30])
    notes = _build_notes(commits)
    assert notes.startswith("[auto-git] ")
    assert "abc0000 Commit 0" in notes
    assert "abc0001 Commit 1" in notes
    assert " | " in notes


# ── Full pipeline tests ─────────────────────────────────────────────────────

def test_process_creates_worklog():
    pid = _create_project("rialu")
    base = datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc)
    commits = _make_commits(base, [0, 30, 60])
    repos = [_make_repo("rialu", commits)]

    summaries = process_commits_for_worklog(repos)
    assert len(summaries) == 1
    assert summaries[0]["action"] == "created"
    assert summaries[0]["minutes"] == 90  # 30 + 30 + 30

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM worklog WHERE project_id = ?", (pid,)
        ).fetchone()
    assert row is not None
    assert row["minutes"] == 90
    assert row["session_type"] == "code"
    assert row["notes"].startswith("[auto-git]")


def test_process_idempotent():
    """Running twice with same data doesn't create duplicates."""
    _create_project("rialu")
    base = datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc)
    commits = _make_commits(base, [0, 45])
    repos = [_make_repo("rialu", commits)]

    process_commits_for_worklog(repos)
    summaries = process_commits_for_worklog(repos)
    # Second run should return nothing (no change)
    assert len(summaries) == 0

    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM worklog").fetchone()[0]
    assert count == 1


def test_process_updates_on_new_commits():
    """Adding a commit updates the existing entry."""
    pid = _create_project("rialu")
    base = datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc)

    # First: 2 commits
    commits1 = _make_commits(base, [0, 30])
    process_commits_for_worklog([_make_repo("rialu", commits1)])

    # Second: 3 commits (one more added)
    commits2 = _make_commits(base, [0, 30, 60])
    summaries = process_commits_for_worklog([_make_repo("rialu", commits2)])

    assert len(summaries) == 1
    assert summaries[0]["action"] == "updated"
    assert summaries[0]["minutes"] == 90

    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM worklog WHERE project_id = ?", (pid,)).fetchone()[0]
    assert count == 1  # still one entry, not two


def test_no_matching_project():
    """Commits from unknown repos are silently skipped."""
    base = datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc)
    commits = _make_commits(base, [0, 30])
    repos = [_make_repo("unknown-repo", commits)]

    summaries = process_commits_for_worklog(repos)
    assert summaries == []


def test_no_recent_commits():
    """Repos with no recent_commits are skipped."""
    _create_project("rialu")
    repos = [_make_repo("rialu")]  # no commits
    summaries = process_commits_for_worklog(repos)
    assert summaries == []


def test_manual_worklog_untouched():
    """Auto-git does not interfere with manual worklog entries."""
    pid = _create_project("rialu")
    with db() as conn:
        conn.execute(
            "INSERT INTO worklog (project_id, date, minutes, session_type, notes) VALUES (?, '2026-03-21', 120, 'code', 'Manual entry')",
            (pid,),
        )

    base = datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc)
    commits = _make_commits(base, [0, 30])
    process_commits_for_worklog([_make_repo("rialu", commits)])

    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM worklog WHERE project_id = ? ORDER BY id", (pid,)
        ).fetchall()
    assert len(rows) == 2  # manual + auto-git, both preserved
    assert rows[0]["notes"] == "Manual entry"
    assert rows[1]["notes"].startswith("[auto-git]")

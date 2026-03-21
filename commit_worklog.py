"""
commit_worklog.py — Auto-generate worklog entries from git commit history.

Uses a commit-gap heuristic: if two commits by the same author are less than
2 hours apart, the gap is counted as work time. The first commit in a session
gets a default 30 minutes. One worklog entry per project per day, marked with
[auto-git] prefix for idempotency.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from db import db

log = logging.getLogger("commit_worklog")

# Gap heuristic parameters
MAX_GAP_MINUTES = 120  # gaps > 2h start a new session
FIRST_COMMIT_MINUTES = 30  # default time for first commit in a session


def process_commits_for_worklog(repos: list) -> list:
    """
    Process recent commits from repo heartbeat data into worklog entries.

    Args:
        repos: list of repo dicts from agent heartbeat, each may have
               a "recent_commits" key with list of {hash, message, timestamp}.

    Returns:
        list of summary dicts describing what was created/updated.
    """
    # Collect all commits, keyed by (project_id, date)
    project_commits = _map_commits_to_projects(repos)
    if not project_commits:
        return []

    summaries = []
    for (project_id, date_str), commits in project_commits.items():
        minutes = _compute_minutes(commits)
        notes = _build_notes(commits)
        action = _upsert_worklog(project_id, date_str, minutes, notes)
        if action:
            summaries.append(action)

    return summaries


def _map_commits_to_projects(repos: list) -> dict:
    """Map commits to (project_id, date) groups using repo name -> project slug."""
    # Load project slugs
    with db() as conn:
        rows = conn.execute("SELECT id, slug FROM projects").fetchall()
    slug_to_id = {r["slug"]: r["id"] for r in rows}

    # Group commits by (project_id, date)
    grouped = defaultdict(list)
    for repo in repos:
        commits = repo.get("recent_commits", [])
        if not commits:
            continue
        repo_name = repo.get("name", "")
        # Try exact slug match, then lowercase
        project_id = slug_to_id.get(repo_name) or slug_to_id.get(repo_name.lower())
        if not project_id:
            # Try with hyphens replaced by underscores and vice versa
            alt = repo_name.replace("-", "_")
            project_id = slug_to_id.get(alt)
            if not project_id:
                alt = repo_name.replace("_", "-")
                project_id = slug_to_id.get(alt)
        if not project_id:
            continue  # No matching project, skip silently

        for c in commits:
            ts = c.get("timestamp", "")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts)
                date_str = dt.strftime("%Y-%m-%d")
                grouped[(project_id, date_str)].append({
                    "hash": c.get("hash", ""),
                    "message": c.get("message", ""),
                    "dt": dt,
                })
            except (ValueError, TypeError):
                continue

    return dict(grouped)


def _compute_minutes(commits: list) -> int:
    """Apply commit-gap heuristic to estimate work minutes."""
    if not commits:
        return 0

    # Sort by timestamp
    sorted_commits = sorted(commits, key=lambda c: c["dt"])

    total_minutes = FIRST_COMMIT_MINUTES  # first commit gets default time

    for i in range(1, len(sorted_commits)):
        gap = sorted_commits[i]["dt"] - sorted_commits[i - 1]["dt"]
        gap_minutes = gap.total_seconds() / 60

        if gap_minutes < MAX_GAP_MINUTES:
            total_minutes += int(gap_minutes)
        else:
            # New session — add default time for this commit
            total_minutes += FIRST_COMMIT_MINUTES

    return total_minutes


def _build_notes(commits: list) -> str:
    """Build notes string from commits, prefixed with [auto-git]."""
    sorted_commits = sorted(commits, key=lambda c: c["dt"])
    parts = [f"{c['hash']} {c['message']}" for c in sorted_commits]
    return "[auto-git] " + " | ".join(parts)


def _upsert_worklog(project_id: int, date_str: str, minutes: int, notes: str) -> dict:
    """Create or update a worklog entry. Returns summary dict or None."""
    with db() as conn:
        existing = conn.execute(
            """SELECT id, minutes, notes FROM worklog
               WHERE project_id = ? AND date = ? AND session_type = 'code'
               AND notes LIKE '[auto-git]%'""",
            (project_id, date_str),
        ).fetchone()

        if existing:
            if existing["minutes"] == minutes and existing["notes"] == notes:
                return None  # No change
            conn.execute(
                "UPDATE worklog SET minutes = ?, notes = ? WHERE id = ?",
                (minutes, notes, existing["id"]),
            )
            return {"action": "updated", "project_id": project_id, "date": date_str, "minutes": minutes}
        else:
            conn.execute(
                """INSERT INTO worklog (project_id, date, minutes, session_type, notes)
                   VALUES (?, ?, ?, 'code', ?)""",
                (project_id, date_str, minutes, notes),
            )
            return {"action": "created", "project_id": project_id, "date": date_str, "minutes": minutes}

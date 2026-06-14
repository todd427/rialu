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

# Prefix marking an auto-generated row (must match routers/commits.py parser).
AUTO_GIT_PREFIX = "[auto-git] "


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
        action = _upsert_worklog(project_id, date_str, commits)
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
    return AUTO_GIT_PREFIX + " | ".join(parts)


def _parse_notes_entries(notes: str) -> dict:
    """Parse an [auto-git] notes string into an ordered {hash: 'hash message'} map.

    Mirrors the pipe-delimited 'hash message' format routers/commits.py parses,
    so a merged row stays readable by the same consumer.
    """
    body = notes[len(AUTO_GIT_PREFIX):] if notes.startswith(AUTO_GIT_PREFIX) else notes
    entries = {}
    for chunk in (body.split(" | ") if body else []):
        h = chunk.split(" ", 1)[0]
        if h:
            entries[h] = chunk
    return entries


def _upsert_worklog(project_id: int, date_str: str, incoming_commits: list) -> dict:
    """Create or merge the [auto-git] worklog row for a project-day.

    Multi-machine safe: the row holds the UNION of commits (deduped by hash)
    across every machine that reports the repo, instead of the last reporter
    clobbering it. Minutes is the max of each reporter's gap-heuristic estimate
    — we can't recompute the heuristic across machines without every commit's
    timestamp, and max never shrinks a real session when a machine that is
    behind reports only a partial view. Returns a summary dict, or None when
    this reporter added nothing new.
    """
    incoming = sorted(incoming_commits, key=lambda c: c["dt"])
    incoming_minutes = _compute_minutes(incoming)
    incoming_entries = {
        c["hash"]: f"{c['hash']} {c['message']}" for c in incoming if c.get("hash")
    }

    with db() as conn:
        existing = conn.execute(
            """SELECT id, minutes, notes FROM worklog
               WHERE project_id = ? AND date = ? AND session_type = 'code'
               AND notes LIKE '[auto-git]%'""",
            (project_id, date_str),
        ).fetchone()

        if existing:
            # Union by hash: incoming (timestamp-ordered) first, then any
            # commits a different machine reported earlier that we don't have.
            merged = dict(incoming_entries)
            for h, chunk in _parse_notes_entries(existing["notes"]).items():
                merged.setdefault(h, chunk)
            notes = AUTO_GIT_PREFIX + " | ".join(merged.values())
            minutes = max(existing["minutes"] or 0, incoming_minutes)
            if existing["minutes"] == minutes and existing["notes"] == notes:
                return None  # nothing new from this reporter
            conn.execute(
                "UPDATE worklog SET minutes = ?, notes = ? WHERE id = ?",
                (minutes, notes, existing["id"]),
            )
            return {"action": "updated", "project_id": project_id, "date": date_str, "minutes": minutes}

        notes = AUTO_GIT_PREFIX + " | ".join(incoming_entries.values())
        conn.execute(
            """INSERT INTO worklog (project_id, date, minutes, session_type, notes)
               VALUES (?, ?, ?, 'code', ?)""",
            (project_id, date_str, incoming_minutes, notes),
        )
        return {"action": "created", "project_id": project_id, "date": date_str, "minutes": incoming_minutes}

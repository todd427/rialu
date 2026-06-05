"""
routers/divergence.py — Portfolio divergence digest.

The inverse of routers/milestone_review.py: instead of finding *evidence of
progress* and auto-closing milestones, this finds the *absence of progress*
relative to a project's declared status —

  - stale-active: claims active (development/running) but has gone quiet
  - no-trigger:   parked (research/paused) with no defined way back

Reads ONLY the local `projects` and `worklog` tables. Makes NO external calls
(no git-mcp, no GitHub) — that is what makes the scheduled run reliable. Commit
activity is already materialised in `worklog` by commit_worklog.py.

The core lives in run_divergence() so both the HTTP route and the CLI
(`cli/rialu divergence-run`) share one implementation.
"""

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query

from auth import verify_faire_token
from db import db, row_to_dict
from routers.commits import AUTO_GIT_PREFIX, _parse_commit_count

router = APIRouter(prefix="/api/divergence", tags=["divergence"])
log = logging.getLogger("rialu.divergence")

ACTIVE_STATES = {"development", "running"}
PARKED_STATES = {"research", "paused"}
# deployed is ambiguous — treated as active-class only if it has a commit within
# this many days; otherwise it is a finished service that is quiet by design.
DEPLOYED_ACTIVE_WINDOW = 90

# The one binding deadline through June 2026 (dissertation viva). Phase 1 hardcodes
# it as a constant per the PRD; a later iteration may derive it from milestone
# due_dates. Drives the summary-strip "Deadline" card via /api/divergence/latest.
from datetime import date as _date
VIVA_DEADLINE = _date(2026, 6, 12)
DEADLINE_LABEL = "viva"


def _has_trigger(project: dict) -> bool:
    """True if the project declares a plan to revisit it (so parking is intentional)."""
    if (project.get("revisit_trigger") or "").strip():
        return True
    return "trigger:" in (project.get("notes") or "").lower()


def _classify(project, commits_window, manual_window, commits_recent, last_worklog, window_days):
    """Return (flag, detail) for a single project. Exactly one flag each."""
    status = (project.get("status") or "").lower()

    # status is lifecycle; runtime is operational state (migration 019). Only
    # lifecycle drives staleness. deployed joins the active class only with a
    # recent commit — otherwise it is a shipped service, not a stale one.
    is_active = status in ACTIVE_STATES or (status == "deployed" and commits_recent > 0)

    if is_active:
        if commits_window > 0 or manual_window > 0:
            return "healthy", f"{commits_window} commits in {window_days}d; status={status}"
        tail = f"; last worklog {last_worklog}" if last_worklog else "; no worklog on record"
        return "stale-active", f"0 commits in {window_days}d; status={status}{tail}"

    if status in PARKED_STATES:
        if _has_trigger(project):
            return "dormant-ok", f"parked ({status}) with a revisit trigger"
        return "no-trigger", f"parked ({status}) with no revisit trigger"

    # archived / shipped / deployed-without-recent-commits / anything else:
    # quiet by design, recorded so absence-of-flag stays meaningful.
    return "dormant-ok", f"status={status}; quiet by design"


def run_divergence(window_days: int = 30) -> dict:
    """
    Classify every project into exactly one health flag and persist the result.

    Idempotent and safe to re-run: re-running overwrites projects.health and
    appends a fresh divergence_log row per project. No external calls.
    """
    today = date.today()
    since_window = (today - timedelta(days=window_days - 1)).isoformat()
    since_recent = (today - timedelta(days=DEPLOYED_ACTIVE_WINDOW - 1)).isoformat()
    earliest = min(since_window, since_recent)

    with db() as conn:
        projects = [row_to_dict(r) for r in conn.execute(
            "SELECT id, name, status, runtime, notes, revisit_trigger FROM projects"
        ).fetchall()]

        # All worklog rows back to the earliest date any rule needs, in one pass.
        wl_rows = conn.execute(
            "SELECT project_id, date, notes FROM worklog WHERE date >= ?",
            (earliest,),
        ).fetchall()

        # Most-recent worklog date per project (any type, any age) for detail text.
        last_wl = {
            r["project_id"]: r["last_date"]
            for r in conn.execute(
                "SELECT project_id, MAX(date) AS last_date FROM worklog GROUP BY project_id"
            ).fetchall()
        }

    commits_window: dict[int, int] = {}
    commits_recent: dict[int, int] = {}
    manual_window: dict[int, int] = {}
    for r in wl_rows:
        pid, d, notes = r["project_id"], r["date"], r["notes"] or ""
        if notes.startswith(AUTO_GIT_PREFIX):
            count = _parse_commit_count(notes)
            if d >= since_recent:
                commits_recent[pid] = commits_recent.get(pid, 0) + count
            if d >= since_window:
                commits_window[pid] = commits_window.get(pid, 0) + count
        elif d >= since_window:
            manual_window[pid] = manual_window.get(pid, 0) + 1

    results = []
    for p in projects:
        pid = p["id"]
        flag, detail = _classify(
            p,
            commits_window.get(pid, 0),
            manual_window.get(pid, 0),
            commits_recent.get(pid, 0),
            last_wl.get(pid),
            window_days,
        )
        results.append({"project_id": pid, "project": p["name"], "flag": flag, "detail": detail})

    with db() as conn:
        for r in results:
            conn.execute(
                "UPDATE projects SET health = ?, health_checked_at = datetime('now') WHERE id = ?",
                (r["flag"], r["project_id"]),
            )
            conn.execute(
                """INSERT INTO divergence_log (project_id, project_name, flag, detail, window_days)
                   VALUES (?, ?, ?, ?, ?)""",
                (r["project_id"], r["project"], r["flag"], r["detail"], window_days),
            )

    flag_counts: dict[str, int] = {}
    for r in results:
        flag_counts[r["flag"]] = flag_counts.get(r["flag"], 0) + 1

    return {
        "checked": len(results),
        "flags": flag_counts,
        "window_days": window_days,
        "results": results,
    }


@router.post("/run", dependencies=[Depends(verify_faire_token)])
def run(window_days: int = Query(default=30, ge=1, le=365)):
    """Compute flags for all projects, persist, and append to divergence_log."""
    return run_divergence(window_days=window_days)


@router.get("/latest")
def latest():
    """
    Current flag per project plus an aggregate `counts` block and the days left
    to the binding deadline. Drives both the per-card pills and the Projects-tab
    summary strip.
    """
    with db() as conn:
        proj_rows = conn.execute(
            "SELECT id, name, health, health_checked_at FROM projects ORDER BY name"
        ).fetchall()
        # latest detail per project, for pill tooltips / strip drill-down
        detail_rows = conn.execute(
            """SELECT d.project_id, d.detail
               FROM divergence_log d
               JOIN (SELECT project_id, MAX(id) AS mid FROM divergence_log GROUP BY project_id) m
                 ON d.id = m.mid"""
        ).fetchall()

    details = {r["project_id"]: r["detail"] for r in detail_rows}
    counts: dict[str, int] = {}
    projects = []
    for r in proj_rows:
        flag = r["health"]
        if flag:
            counts[flag] = counts.get(flag, 0) + 1
        projects.append({
            "project_id": r["id"],
            "name": r["name"],
            "health": flag,
            "health_detail": details.get(r["id"]),
            "health_checked_at": r["health_checked_at"],
        })

    return {
        "counts": counts,
        "days_to_deadline": (VIVA_DEADLINE - _date.today()).days,
        "deadline_label": DEADLINE_LABEL,
        "projects": projects,
    }


@router.get("/log")
def divergence_log(limit: int = Query(default=50, ge=1, le=500)):
    """Recent divergence decisions, newest first."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM divergence_log ORDER BY checked_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]

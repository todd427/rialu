"""
routers/divergence.py — Portfolio divergence digest.

The inverse of routers/milestone_review.py: instead of finding *evidence of
progress* and auto-closing milestones, this finds the *absence of progress*
relative to a project's declared status —

  - stale-active: claims active (development/running) but has gone quiet
  - no-trigger:   parked (research/paused) with no defined way back

A second, ORTHOGONAL axis lives here too (docs/cc-brief-narrative-staleness.md):

  - narrative-stale: the code has moved a long way past the free-text phase/notes

The first axis is status-vs-activity; the second is narrative-vs-activity. They
do not compete — a project can be `healthy` on activity and narrative-stale at
the same time, which is the exact case that motivated it, so the flag has its own
column (`narrative_health`) rather than folding into `health`.

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

# Commits since the prose was authored before phase/notes count as misleading.
# A guess, not a derived figure — roughly where Rian's phase string went wrong.
# Tunable per run alongside window_days.
NARRATIVE_THRESHOLD = 15
NARRATIVE_STALE = "narrative-stale"
# The free-text fields whose authorship date narrative_written_at records.
NARRATIVE_FIELDS = ("phase", "notes")


def _has_trigger(project: dict) -> bool:
    """True if the project declares a plan to revisit it (so parking is intentional)."""
    if (project.get("revisit_trigger") or "").strip():
        return True
    return "trigger:" in (project.get("notes") or "").lower()


def narrative_changed(conn, project_id: int, fields: dict) -> bool:
    """
    True if `fields` changes the *value* of phase or notes on this project.

    The presence of the key is not enough. `updated_at` already moves on any edit
    — that is precisely why it cannot measure prose age — so narrative_written_at
    is written only when the stored text actually differs from the incoming text.

    Shared by the HTTP (routers/projects.py) and MCP (mcp_server.py) update paths
    so there is one convention, not two.
    """
    incoming = {k: v for k, v in fields.items() if k in NARRATIVE_FIELDS}
    if not incoming:
        return False
    row = conn.execute(
        f"SELECT {', '.join(NARRATIVE_FIELDS)} FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        return False
    return any(incoming[k] != row[k] for k in incoming)


def commits_since_narrative(conn) -> dict[int, int]:
    """
    {project_id: commits landed since phase/notes were authored}.

    Counted from worklog only (no git-mcp, no GitHub) with the same pipe-delimited
    [auto-git] parsing the rest of the digest uses. Compared on the date part:
    commits must be dated strictly after the authoring *day*, so the commits that
    prompted a prose edit don't immediately count against it.

    Projects with no narrative_written_at are absent from the mapping — absence of
    an authoring date is not staleness.
    """
    rows = conn.execute(
        """
        SELECT w.project_id, w.notes
        FROM worklog w
        JOIN projects p ON p.id = w.project_id
        WHERE p.narrative_written_at IS NOT NULL
          AND w.notes LIKE ?
          AND w.date > substr(p.narrative_written_at, 1, 10)
        """,
        (AUTO_GIT_PREFIX.strip() + "%",),
    ).fetchall()
    counts: dict[int, int] = {}
    for r in rows:
        counts[r["project_id"]] = counts.get(r["project_id"], 0) + _parse_commit_count(r["notes"])
    return counts


def _has_prose(project: dict) -> bool:
    """True if there is any free-text narrative that could go stale."""
    return any((project.get(f) or "").strip() for f in NARRATIVE_FIELDS)


def _classify_narrative(project, commits_since, threshold):
    """
    Second axis: (flag, detail), or (None, None) when there is nothing to say.

    Composes with _classify() rather than replacing it — both can be set on one
    project. Silent when there is no authoring date (absence is not staleness) or
    no prose at all (nothing to be stale).
    """
    written = (project.get("narrative_written_at") or "").strip()
    if not written or not _has_prose(project):
        return None, None
    if commits_since > threshold:
        return NARRATIVE_STALE, (
            f"{commits_since} commits since phase/notes written {written[:10]} "
            f"(threshold {threshold})"
        )
    return None, None


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


def run_divergence(window_days: int = 30, narrative_threshold: int = NARRATIVE_THRESHOLD) -> dict:
    """
    Classify every project into exactly one health flag, plus an independent
    narrative-staleness flag, and persist both.

    Idempotent and safe to re-run: re-running overwrites projects.health /
    projects.narrative_health and appends fresh divergence_log rows. No external
    calls.
    """
    today = date.today()
    since_window = (today - timedelta(days=window_days - 1)).isoformat()
    since_recent = (today - timedelta(days=DEPLOYED_ACTIVE_WINDOW - 1)).isoformat()
    earliest = min(since_window, since_recent)

    with db() as conn:
        projects = [row_to_dict(r) for r in conn.execute(
            "SELECT id, name, status, runtime, phase, notes, revisit_trigger, "
            "narrative_written_at FROM projects"
        ).fetchall()]

        # Second axis, its own query: narrative_written_at can predate any of the
        # windows above, so it cannot ride along on the windowed worklog scan.
        narrative_counts = commits_since_narrative(conn)

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
        since_narrative = narrative_counts.get(pid, 0)
        n_flag, n_detail = _classify_narrative(p, since_narrative, narrative_threshold)
        results.append({
            "project_id": pid,
            "project": p["name"],
            "flag": flag,
            "detail": detail,
            "narrative_flag": n_flag,
            "narrative_detail": n_detail,
            "commits_since_narrative": since_narrative,
        })

    with db() as conn:
        for r in results:
            conn.execute(
                """UPDATE projects
                   SET health = ?, narrative_health = ?, health_checked_at = datetime('now')
                   WHERE id = ?""",
                (r["flag"], r["narrative_flag"], r["project_id"]),
            )
            conn.execute(
                """INSERT INTO divergence_log (project_id, project_name, flag, detail, window_days)
                   VALUES (?, ?, ?, ?, ?)""",
                (r["project_id"], r["project"], r["flag"], r["detail"], window_days),
            )
            # Second row, same shape, only when flagged — keeps the log a record of
            # decisions rather than a per-run cross product.
            if r["narrative_flag"]:
                conn.execute(
                    """INSERT INTO divergence_log (project_id, project_name, flag, detail, window_days)
                       VALUES (?, ?, ?, ?, ?)""",
                    (r["project_id"], r["project"], r["narrative_flag"], r["narrative_detail"], window_days),
                )

    flag_counts: dict[str, int] = {}
    for r in results:
        flag_counts[r["flag"]] = flag_counts.get(r["flag"], 0) + 1

    return {
        "checked": len(results),
        # One health flag per project, so these sum to `checked`. The narrative
        # count is reported separately because it is a second axis, not a share.
        "flags": flag_counts,
        "narrative_stale": sum(1 for r in results if r["narrative_flag"]),
        "window_days": window_days,
        "narrative_threshold": narrative_threshold,
        "results": results,
    }


@router.post("/run", dependencies=[Depends(verify_faire_token)])
def run(
    window_days: int = Query(default=30, ge=1, le=365),
    narrative_threshold: int = Query(default=NARRATIVE_THRESHOLD, ge=1, le=1000),
):
    """Compute flags for all projects, persist, and append to divergence_log."""
    return run_divergence(window_days=window_days, narrative_threshold=narrative_threshold)


@router.get("/latest")
def latest():
    """
    Current flag per project plus an aggregate `counts` block. Drives both the
    per-card pills and the Projects-tab summary strip.

    The hardcoded viva-deadline card (`days_to_deadline`/`deadline_label`) was
    removed 2026-08-20: the date had passed, so the tile rendered a permanently
    red negative countdown.
    """
    with db() as conn:
        proj_rows = conn.execute(
            "SELECT id, name, health, narrative_health, health_checked_at "
            "FROM projects ORDER BY name"
        ).fetchall()
        # latest detail per project, for pill tooltips / strip drill-down.
        # Narrative rows are excluded here: they share the log table but belong to
        # the other axis, and would otherwise hijack the health pill's tooltip.
        detail_rows = conn.execute(
            """SELECT d.project_id, d.detail
               FROM divergence_log d
               JOIN (SELECT project_id, MAX(id) AS mid FROM divergence_log
                     WHERE flag != ? GROUP BY project_id) m
                 ON d.id = m.mid""",
            (NARRATIVE_STALE,),
        ).fetchall()
        narrative_rows = conn.execute(
            """SELECT d.project_id, d.detail
               FROM divergence_log d
               JOIN (SELECT project_id, MAX(id) AS mid FROM divergence_log
                     WHERE flag = ? GROUP BY project_id) m
                 ON d.id = m.mid""",
            (NARRATIVE_STALE,),
        ).fetchall()
        # Live count, not the one from the last run — the flag ages, the number shouldn't.
        narrative_counts = commits_since_narrative(conn)

    details = {r["project_id"]: r["detail"] for r in detail_rows}
    narrative_details = {r["project_id"]: r["detail"] for r in narrative_rows}
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
            "narrative_health": r["narrative_health"],
            "narrative_detail": narrative_details.get(r["id"]) if r["narrative_health"] else None,
            "commits_since_narrative": narrative_counts.get(r["id"], 0),
        })

    return {
        "counts": counts,
        "narrative_stale": sum(1 for p in projects if p["narrative_health"]),
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

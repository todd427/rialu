"""
routers/worklog.py — Work session log: entries + rolling stats.
"""

from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel

from db import db, row_to_dict

router = APIRouter(prefix="/api/worklog", tags=["worklog"])


class WorklogIn(BaseModel):
    project_id: int
    minutes: int
    session_type: str = "code"
    notes: Optional[str] = None
    date: Optional[str] = None  # ISO date string; defaults to today
    lines_added: int = 0
    lines_removed: int = 0


@router.get("/stats")
def worklog_stats():
    with db() as conn:
        this_week = conn.execute(
            """SELECT COALESCE(SUM(minutes), 0) as total
               FROM worklog
               WHERE date >= date('now', '-6 days')"""
        ).fetchone()["total"]

        sessions_7d = conn.execute(
            """SELECT COUNT(*) as cnt FROM worklog
               WHERE date >= date('now', '-6 days')"""
        ).fetchone()["cnt"]

        top_project = conn.execute(
            """SELECT p.name, SUM(w.minutes) as total
               FROM worklog w JOIN projects p ON p.id = w.project_id
               WHERE w.date >= date('now', '-6 days')
               GROUP BY w.project_id ORDER BY total DESC LIMIT 1"""
        ).fetchone()

        loc_week = conn.execute(
            """SELECT COALESCE(SUM(lines_added), 0) as added,
                      COALESCE(SUM(lines_removed), 0) as removed
               FROM worklog WHERE date >= date('now', '-6 days')"""
        ).fetchone()

        streak = conn.execute(
            """
            WITH RECURSIVE dates(d, n) AS (
                SELECT date('now'), 0
                UNION ALL
                SELECT date(d, '-1 day'), n + 1
                FROM dates WHERE n < 30
            )
            SELECT COUNT(*) as streak FROM dates
            WHERE EXISTS (SELECT 1 FROM worklog WHERE date = d)
            AND n <= (
                SELECT MIN(n) - 1 FROM dates
                WHERE NOT EXISTS (SELECT 1 FROM worklog WHERE date = d)
                AND n > 0
            )
            """
        ).fetchone()

    return {
        "minutes_this_week": this_week,
        "sessions_7d": sessions_7d,
        "top_project": dict(top_project) if top_project else None,
        "streak_days": streak["streak"] if streak else 0,
        "lines_added_week": loc_week["added"],
        "lines_removed_week": loc_week["removed"],
    }


@router.get("")
def list_worklog(limit: int = 50):
    with db() as conn:
        rows = conn.execute(
            """SELECT w.*, p.name as project_name, p.status as project_status
               FROM worklog w JOIN projects p ON p.id = w.project_id
               ORDER BY w.date DESC, w.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@router.post("", status_code=201)
def create_entry(w: WorklogIn):
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO worklog (project_id, date, minutes, session_type, notes, lines_added, lines_removed)
               VALUES (?, COALESCE(?, date('now')), ?, ?, ?, ?, ?)""",
            (w.project_id, w.date, w.minutes, w.session_type, w.notes, w.lines_added, w.lines_removed),
        )
        conn.execute(
            "UPDATE projects SET updated_at = datetime('now') WHERE id = ?",
            (w.project_id,),
        )
        row = conn.execute(
            """SELECT w.*, p.name as project_name
               FROM worklog w JOIN projects p ON p.id = w.project_id
               WHERE w.id = ?""",
            (cur.lastrowid,),
        ).fetchone()
    return row_to_dict(row)


@router.delete("/{entry_id}", status_code=204)
def delete_entry(entry_id: int):
    with db() as conn:
        conn.execute("DELETE FROM worklog WHERE id = ?", (entry_id,))

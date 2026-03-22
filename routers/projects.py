"""
routers/projects.py — CRUD for projects, milestones, and sessions.
"""

import re
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import db, row_to_dict

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ── models ───────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-")[:64]


class ProjectIn(BaseModel):
    name: str
    phase: Optional[str] = None
    status: str = "development"
    notes: Optional[str] = None
    repo_url: Optional[str] = None
    machine: Optional[str] = None
    platform: Optional[str] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    phase: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    repo_url: Optional[str] = None
    machine: Optional[str] = None
    platform: Optional[str] = None


class MilestoneIn(BaseModel):
    title: str
    due_date: Optional[str] = None
    sort_order: int = 0


class MilestoneUpdate(BaseModel):
    title: Optional[str] = None
    due_date: Optional[str] = None
    done: Optional[bool] = None
    sort_order: Optional[int] = None


class SessionIn(BaseModel):
    session_type: str = "code"
    notes: Optional[str] = None
    duration_minutes: Optional[int] = None


# ── projects ─────────────────────────────────────────────────────────────────

@router.get("")
def list_projects():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY updated_at DESC"
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@router.post("", status_code=201)
def create_project(p: ProjectIn):
    slug = slugify(p.name)
    with db() as conn:
        # ensure slug uniqueness
        existing = conn.execute(
            "SELECT id FROM projects WHERE slug = ?", (slug,)
        ).fetchone()
        if existing:
            slug = f"{slug}-{existing['id']}"
        cur = conn.execute(
            """INSERT INTO projects (name, slug, phase, status, notes, repo_url, machine, platform)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (p.name, slug, p.phase, p.status, p.notes, p.repo_url, p.machine, p.platform),
        )
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return row_to_dict(row)


@router.get("/{project_id}")
def get_project(project_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Project not found")
    return row_to_dict(row)


@router.put("/{project_id}")
def update_project(project_id: int, p: ProjectUpdate):
    fields = {k: v for k, v in p.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update")
    fields["updated_at"] = "datetime('now')"
    set_clause = ", ".join(
        f"{k} = datetime('now')" if k == "updated_at" else f"{k} = ?"
        for k in fields
    )
    values = [v for k, v in fields.items() if k != "updated_at"]
    values.append(project_id)
    with db() as conn:
        conn.execute(
            f"UPDATE projects SET {set_clause} WHERE id = ?", values
        )
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Project not found")
    return row_to_dict(row)


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: int):
    with db() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


# ── project dashboard ────────────────────────────────────────────────────────

@router.get("/{project_id}/dashboard")
def project_dashboard(project_id: int):
    """At-a-glance stats for a project: LOC, deploy status, recent worklog."""
    with db() as conn:
        proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not proj:
            raise HTTPException(404, "Project not found")

        # LOC this week
        loc = conn.execute(
            """SELECT COALESCE(SUM(lines_added), 0) as added,
                      COALESCE(SUM(lines_removed), 0) as removed
               FROM worklog WHERE project_id = ? AND date >= date('now', '-6 days')""",
            (project_id,),
        ).fetchone()

        # LOC all time
        loc_all = conn.execute(
            """SELECT COALESCE(SUM(lines_added), 0) as added,
                      COALESCE(SUM(lines_removed), 0) as removed
               FROM worklog WHERE project_id = ?""",
            (project_id,),
        ).fetchone()

        # Minutes this week
        mins = conn.execute(
            """SELECT COALESCE(SUM(minutes), 0) as total
               FROM worklog WHERE project_id = ? AND date >= date('now', '-6 days')""",
            (project_id,),
        ).fetchone()["total"]

        # Deploy status — match by project name (lowercase) in deployments_cache
        name = proj["name"].lower()
        deploy = conn.execute(
            """SELECT service_name, status, last_deploy_at, url, platform
               FROM deployments_cache
               WHERE LOWER(service_name) LIKE ? OR LOWER(service_name) LIKE ?
               ORDER BY checked_at DESC LIMIT 1""",
            (f"%{name}%", f"%{proj['slug']}%"),
        ).fetchone()

        # Recent worklog entries
        recent_work = conn.execute(
            """SELECT date, minutes, session_type, lines_added, lines_removed, notes
               FROM worklog WHERE project_id = ?
               ORDER BY date DESC, id DESC LIMIT 5""",
            (project_id,),
        ).fetchall()

    return {
        "loc_week": {"added": loc["added"], "removed": loc["removed"]},
        "loc_total": {"added": loc_all["added"], "removed": loc_all["removed"]},
        "minutes_week": mins,
        "deploy": row_to_dict(deploy) if deploy else None,
        "recent_work": [row_to_dict(r) for r in recent_work],
    }


# ── milestones ───────────────────────────────────────────────────────────────

@router.get("/{project_id}/milestones")
def list_milestones(project_id: int):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM milestones WHERE project_id = ? ORDER BY sort_order, id",
            (project_id,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@router.post("/{project_id}/milestones", status_code=201)
def create_milestone(project_id: int, m: MilestoneIn):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO milestones (project_id, title, due_date, sort_order) VALUES (?, ?, ?, ?)",
            (project_id, m.title, m.due_date, m.sort_order),
        )
        row = conn.execute(
            "SELECT * FROM milestones WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return row_to_dict(row)


@router.put("/milestones/{milestone_id}")
def update_milestone(milestone_id: int, m: MilestoneUpdate):
    fields = {k: v for k, v in m.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update")
    if "done" in fields:
        fields["done"] = 1 if fields["done"] else 0
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [milestone_id]
    with db() as conn:
        conn.execute(
            f"UPDATE milestones SET {set_clause} WHERE id = ?", values
        )
        row = conn.execute(
            "SELECT * FROM milestones WHERE id = ?", (milestone_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Milestone not found")
    return row_to_dict(row)


@router.delete("/milestones/{milestone_id}", status_code=204)
def delete_milestone(milestone_id: int):
    with db() as conn:
        conn.execute("DELETE FROM milestones WHERE id = ?", (milestone_id,))


# ── sessions ──────────────────────────────────────────────────────────────────

@router.post("/{project_id}/sessions", status_code=201)
def create_session(project_id: int, s: SessionIn):
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO sessions (project_id, session_type, notes, duration_minutes)
               VALUES (?, ?, ?, ?)""",
            (project_id, s.session_type, s.notes, s.duration_minutes),
        )
        # Also add to worklog if duration given
        if s.duration_minutes:
            conn.execute(
                """INSERT INTO worklog (project_id, session_type, minutes, notes)
                   VALUES (?, ?, ?, ?)""",
                (project_id, s.session_type, s.duration_minutes, s.notes),
            )
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        # bump project updated_at
        conn.execute(
            "UPDATE projects SET updated_at = datetime('now') WHERE id = ?",
            (project_id,),
        )
    return row_to_dict(row)

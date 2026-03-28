"""
routers/commits.py — Commit activity endpoints.

Derives daily commit counts from worklog rows written by commit_worklog.py.
Each [auto-git] worklog row = one project-day; individual commits are
pipe-delimited in the notes field.
"""

import csv
import io
from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from db import db

router = APIRouter(prefix="/api", tags=["commits"])

AUTO_GIT_PREFIX = "[auto-git] "


def _parse_commit_count(notes: str) -> int:
    """Count individual commits from a pipe-delimited [auto-git] notes string."""
    body = notes.replace(AUTO_GIT_PREFIX, "", 1)
    return len(body.split(" | ")) if body else 0


def _parse_messages(notes: str) -> list[str]:
    """Extract commit messages (without hashes) from [auto-git] notes."""
    body = notes.replace(AUTO_GIT_PREFIX, "", 1)
    messages = []
    for entry in body.split(" | "):
        parts = entry.split(" ", 1)
        messages.append(parts[1] if len(parts) > 1 else parts[0])
    return messages


def _csv_response(rows: list[dict], filename: str) -> StreamingResponse:
    if not rows:
        return StreamingResponse(
            iter([""]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/projects/{project_id}/commits")
def project_commits(
    project_id: int,
    days: int = Query(default=90, ge=1, le=365),
    format: str = Query(default="json", pattern="^(json|csv)$"),
):
    with db() as conn:
        project = conn.execute(
            "SELECT id, name, slug FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not project:
            raise HTTPException(404, "Project not found")

        since = (date.today() - timedelta(days=days - 1)).isoformat()
        rows = conn.execute(
            """
            SELECT date, notes, COALESCE(lines_added, 0) as lines_added, COALESCE(lines_removed, 0) as lines_removed
            FROM worklog
            WHERE project_id = ?
              AND date >= ?
              AND notes LIKE '[auto-git]%'
            ORDER BY date
            """,
            (project_id, since),
        ).fetchall()

    # Aggregate by date (multiple rows per day possible)
    day_map: dict[str, dict] = {}
    for row in rows:
        d = row["date"]
        count = _parse_commit_count(row["notes"])
        messages = _parse_messages(row["notes"])
        if d not in day_map:
            day_map[d] = {"date": d, "commits": 0, "lines_added": 0, "lines_removed": 0, "messages": []}
        day_map[d]["commits"] += count
        day_map[d]["lines_added"] += row["lines_added"]
        day_map[d]["lines_removed"] += row["lines_removed"]
        day_map[d]["messages"].extend(messages)

    series = [day_map[d] for d in sorted(day_map)]
    total_commits = sum(e["commits"] for e in series)
    peak_day = max(series, key=lambda e: e["commits"]) if series else None
    if peak_day:
        peak_day = {"date": peak_day["date"], "commits": peak_day["commits"]}

    if format == "csv":
        csv_rows = [
            {"date": e["date"], "commits": e["commits"], "lines_added": e["lines_added"], "lines_removed": e["lines_removed"], "messages": " | ".join(e["messages"])}
            for e in series
        ]
        slug = project["slug"] or project["name"].lower().replace(" ", "-")
        return _csv_response(csv_rows, f"{slug}-commits.csv")

    return {
        "project_id": project["id"],
        "project_name": project["name"],
        "from": since,
        "to": date.today().isoformat(),
        "total_commits": total_commits,
        "total_days_active": len(series),
        "peak_day": peak_day,
        "series": series,
    }


@router.get("/commits")
def global_commits(
    days: int = Query(default=90, ge=1, le=365),
    format: str = Query(default="json", pattern="^(json|csv)$"),
):
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT w.date, w.project_id, w.notes, p.name as project_name,
                   COALESCE(w.lines_added, 0) as lines_added, COALESCE(w.lines_removed, 0) as lines_removed
            FROM worklog w
            JOIN projects p ON p.id = w.project_id
            WHERE w.date >= ?
              AND w.notes LIKE '[auto-git]%'
            ORDER BY w.date, p.name
            """,
            (since,),
        ).fetchall()

    # Group by date, then by project
    day_map: dict[str, dict] = {}
    for row in rows:
        d = row["date"]
        count = _parse_commit_count(row["notes"])
        if d not in day_map:
            day_map[d] = {"total_commits": 0, "lines_added": 0, "lines_removed": 0, "by_project": {}}
        day_map[d]["total_commits"] += count
        day_map[d]["lines_added"] += row["lines_added"]
        day_map[d]["lines_removed"] += row["lines_removed"]
        pid = row["project_id"]
        if pid not in day_map[d]["by_project"]:
            day_map[d]["by_project"][pid] = {
                "project_id": pid,
                "name": row["project_name"],
                "commits": 0,
            }
        day_map[d]["by_project"][pid]["commits"] += count

    series = []
    for d in sorted(day_map):
        entry = day_map[d]
        series.append({
            "date": d,
            "total_commits": entry["total_commits"],
            "lines_added": entry["lines_added"],
            "lines_removed": entry["lines_removed"],
            "by_project": sorted(
                entry["by_project"].values(),
                key=lambda p: p["commits"],
                reverse=True,
            ),
        })

    if format == "csv":
        csv_rows = []
        for entry in series:
            for proj in entry["by_project"]:
                csv_rows.append({
                    "date": entry["date"],
                    "project": proj["name"],
                    "commits": proj["commits"],
                })
        return _csv_response(csv_rows, "commits.csv")

    return {
        "from": since,
        "to": date.today().isoformat(),
        "series": series,
    }

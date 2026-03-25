"""
routers/export.py — CSV export endpoints for all major data types.

GET /api/export/projects.csv
GET /api/export/worklog.csv
GET /api/export/budget.csv
GET /api/export/usage.csv
GET /api/export/sentinel.csv
"""

import csv
import io

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from db import db, row_to_dict

router = APIRouter(prefix="/api/export", tags=["export"])


def _csv_response(rows: list[dict], filename: str) -> StreamingResponse:
    """Build a StreamingResponse from a list of dicts."""
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


@router.get("/projects.csv")
def export_projects():
    """All projects with metadata."""
    with db() as conn:
        rows = conn.execute("""
            SELECT id, name, slug, phase, status, notes, repo_url, site_url,
                   machine, platform, created_at, updated_at
            FROM projects ORDER BY name
        """).fetchall()
    return _csv_response([row_to_dict(r) for r in rows], "rialu-projects.csv")


@router.get("/worklog.csv")
def export_worklog(days: int = 90):
    """Worklog entries with project names."""
    with db() as conn:
        rows = conn.execute("""
            SELECT w.id, p.name as project, w.date, w.minutes, w.session_type,
                   w.lines_added, w.lines_removed, w.notes
            FROM worklog w
            JOIN projects p ON p.id = w.project_id
            WHERE w.date >= date('now', ? || ' days')
            ORDER BY w.date DESC, w.id DESC
        """, (f"-{days}",)).fetchall()
    return _csv_response([row_to_dict(r) for r in rows], "rialu-worklog.csv")


@router.get("/budget.csv")
def export_budget():
    """Platform costs and API registry."""
    with db() as conn:
        rows = conn.execute("""
            SELECT platform, service_name, cost_gbp as cost_eur, period, active, notes
            FROM budget ORDER BY platform, service_name
        """).fetchall()
    return _csv_response([row_to_dict(r) for r in rows], "rialu-budget.csv")


@router.get("/usage.csv")
def export_usage(days: int = 90):
    """Anthropic token usage."""
    with db() as conn:
        rows = conn.execute("""
            SELECT usage_date, model, api_key_name, input_tokens,
                   cache_write_5m, cache_write_1h, cache_read,
                   output_tokens, web_searches, cost_usd
            FROM anthropic_usage
            WHERE usage_date >= date('now', ? || ' days')
            ORDER BY usage_date DESC, model
        """, (f"-{days}",)).fetchall()
    return _csv_response([row_to_dict(r) for r in rows], "rialu-usage.csv")


@router.get("/sentinel.csv")
def export_sentinel():
    """Proxy Sentinel recent events as CSV."""
    import httpx
    import os
    url = os.environ.get("SENTINEL_URL", "")
    key = os.environ.get("SENTINEL_API_KEY", "")
    if not url or not key:
        return _csv_response([], "rialu-sentinel.csv")
    try:
        r = httpx.get(
            f"{url}/events/recent?hours=720&limit=500",
            headers={"X-Sentinel-Key": key},
            timeout=10,
        )
        events = r.json().get("events", [])
        # Flatten to CSV-friendly rows
        rows = [{
            "timestamp": e.get("timestamp", ""),
            "ip": e.get("ip", ""),
            "method": e.get("method", ""),
            "path": e.get("path", ""),
            "status_code": e.get("status_code", ""),
            "project": e.get("project", ""),
            "threat_score": e.get("threat_score", ""),
            "reported": e.get("reported", ""),
            "user_agent": e.get("user_agent", ""),
        } for e in events]
        return _csv_response(rows, "rialu-sentinel.csv")
    except Exception:
        return _csv_response([], "rialu-sentinel.csv")

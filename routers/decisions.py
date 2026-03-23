"""
routers/decisions.py — Decision queue API (Faire Phase 1: read-only).
"""

from typing import Optional
from fastapi import APIRouter, HTTPException

from db import db, row_to_dict

router = APIRouter(prefix="/api/decisions", tags=["decisions"])


@router.get("")
def list_decisions(status: Optional[str] = None, project_id: Optional[int] = None):
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM decisions{where} ORDER BY created_at DESC", params
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@router.get("/{decision_id}")
def get_decision(decision_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Decision not found")
    return row_to_dict(row)

"""
routers/decisions.py — Decision queue API (Faire Phase 2: create + respond).
"""

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import verify_faire_token
from db import db, row_to_dict

router = APIRouter(
    prefix="/api/decisions",
    tags=["decisions"],
    dependencies=[Depends(verify_faire_token)],
)


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


# ── Create ──────────────────────────────────────────────────────────────────

VALID_TRIGGER_TYPES = {"ai_approval", "deploy_gate", "error_triage", "cost_threshold"}


class DecisionIn(BaseModel):
    project_id: int
    trigger_type: str
    priority: int = 5
    payload: dict
    agent_id: Optional[str] = None
    timeout_secs: int = 300


@router.post("", status_code=201)
async def create_decision(d: DecisionIn):
    if d.trigger_type not in VALID_TRIGGER_TYPES:
        raise HTTPException(400, f"Invalid trigger_type. Must be one of: {VALID_TRIGGER_TYPES}")
    if not 0 <= d.timeout_secs <= 3600:
        raise HTTPException(400, "timeout_secs must be 0-3600")
    if len(json.dumps(d.payload)) > 65536:
        raise HTTPException(400, "payload too large (max 64KB)")
    decision_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload_json = json.dumps(d.payload)

    with db() as conn:
        conn.execute(
            """INSERT INTO decisions (id, project_id, trigger_type, priority, payload,
               agent_id, timeout_secs, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (decision_id, d.project_id, d.trigger_type, d.priority,
             payload_json, d.agent_id, d.timeout_secs, now),
        )
        row = conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()

    result = row_to_dict(row)

    # Broadcast to Faire clients
    from faire_hub import faire_hub
    await faire_hub.broadcast({
        "event": "decision.new",
        "project_id": d.project_id,
        "payload": result,
    })

    return result


# ── Respond ─────────────────────────────────────────────────────────────────

class RespondIn(BaseModel):
    action: str  # approve|reject|defer
    defer_mins: Optional[int] = None
    reason: Optional[str] = None


@router.post("/{decision_id}/respond")
async def respond_decision(decision_id: str, r: RespondIn):
    if r.action not in ("approve", "reject", "defer"):
        raise HTTPException(400, "action must be approve, reject, or defer")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Decision not found")
        if row["status"] != "pending":
            raise HTTPException(409, f"Decision already resolved: {row['status']}")

        if r.action == "defer":
            mins = r.defer_mins or 30
            defer_until = (datetime.now(timezone.utc) + timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                """UPDATE decisions SET status='deferred', defer_until=?,
                   response=?, responded_by='user', resolved_at=? WHERE id=?""",
                (defer_until, json.dumps({"action": "defer", "reason": r.reason}), now, decision_id),
            )
        else:
            status = "approved" if r.action == "approve" else "rejected"
            conn.execute(
                """UPDATE decisions SET status=?, response=?,
                   responded_by='user', resolved_at=? WHERE id=?""",
                (status, json.dumps({"action": r.action, "reason": r.reason}), now, decision_id),
            )

        updated = conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()

    result = row_to_dict(updated)

    # Broadcast resolution to Faire clients
    from faire_hub import faire_hub
    await faire_hub.broadcast({
        "event": "decision.resolved",
        "project_id": result["project_id"],
        "payload": result,
    })

    return result

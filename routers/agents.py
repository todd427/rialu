"""
routers/agents.py — Agent registry and event ingestion (Faire Phase 1).
"""

import hashlib
import hmac
import json
import os
import uuid
from typing import Optional
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel

from db import db, row_to_dict

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _verify_agent_sig(body: bytes, sig: Optional[str]) -> bool:
    secret = os.environ.get("RIALU_AGENT_KEY", "")
    if not secret:
        return True
    if not sig:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


@router.get("")
def list_agents():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM agents ORDER BY last_seen DESC"
        ).fetchall()
    return [row_to_dict(r) for r in rows]


class EventIn(BaseModel):
    event_type: str
    project_id: Optional[int] = None
    payload: Optional[dict] = None


@router.post("/{agent_id}/event", status_code=201)
async def ingest_event(
    agent_id: str,
    event: EventIn,
    request: Request,
    x_rialu_sig: Optional[str] = Header(None),
):
    # HMAC verification (optional — skip if no key configured or no sig sent)
    body = await request.body()
    secret = os.environ.get("RIALU_AGENT_KEY", "")
    if secret and x_rialu_sig:
        if not _verify_agent_sig(body, x_rialu_sig):
            raise HTTPException(403, "Invalid signature")

    event_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload_json = json.dumps(event.payload) if event.payload else None

    with db() as conn:
        # Upsert agent last_seen
        conn.execute(
            """INSERT INTO agents (id, machine, name, status, last_seen)
               VALUES (?, '', ?, 'online', ?)
               ON CONFLICT(id) DO UPDATE SET status='online', last_seen=?""",
            (agent_id, agent_id, now, now),
        )
        conn.execute(
            """INSERT INTO agent_events (id, agent_id, project_id, event_type, payload, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, agent_id, event.project_id, event.event_type, payload_json, now),
        )

    # Broadcast to Faire clients
    from faire_hub import faire_hub
    await faire_hub.broadcast({
        "event": f"agent.{event.event_type}",
        "agent_id": agent_id,
        "project_id": event.project_id,
        "payload": event.payload or {},
    })

    return {"id": event_id, "status": "ok"}


# ── Agent Events (read) ────────────────────────────────────────────────────

@router.get("/events")
def list_agent_events(
    project_id: Optional[int] = None,
    event_type: Optional[str] = None,
    limit: int = 50,
):
    clauses, params = [], []
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM agent_events{where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [row_to_dict(r) for r in rows]


# ── Timeline (aggregated) ──────────────────────────────────────────────────

@router.get("/timeline")
def get_timeline(project_id: Optional[int] = None, limit: int = 50):
    """Aggregated timeline: decisions + agent events + worklog."""
    events = []
    proj_filter = ""
    proj_params: list = []
    if project_id:
        proj_filter = " WHERE project_id = ?"
        proj_params = [project_id]

    with db() as conn:
        # Decisions
        rows = conn.execute(
            f"SELECT id, project_id, trigger_type, status, payload, created_at FROM decisions{proj_filter} ORDER BY created_at DESC LIMIT ?",
            proj_params + [limit],
        ).fetchall()
        for r in rows:
            d = row_to_dict(r)
            payload = json.loads(d["payload"]) if d.get("payload") else {}
            events.append({
                "type": "decision",
                "project_id": d["project_id"],
                "summary": f"{d['trigger_type']}: {payload.get('summary', d['status'])}",
                "status": d["status"],
                "ts": d["created_at"],
            })

        # Agent events (skip heartbeats — too noisy)
        rows = conn.execute(
            f"""SELECT id, agent_id, project_id, event_type, payload, created_at
                FROM agent_events WHERE event_type != 'heartbeat'
                {('AND project_id = ?' if project_id else '')}
                ORDER BY created_at DESC LIMIT ?""",
            (proj_params + [limit]) if project_id else [limit],
        ).fetchall()
        for r in rows:
            d = row_to_dict(r)
            payload = json.loads(d["payload"]) if d.get("payload") else {}
            summary = payload.get("text", payload.get("tool_name", d["event_type"]))
            if len(summary) > 100:
                summary = summary[:100] + "..."
            events.append({
                "type": d["event_type"],
                "project_id": d["project_id"],
                "agent_id": d["agent_id"],
                "summary": summary,
                "ts": d["created_at"],
            })

        # Worklog
        rows = conn.execute(
            f"SELECT project_id, minutes, session_type, date, notes, lines_added, lines_removed FROM worklog{proj_filter} ORDER BY date DESC, id DESC LIMIT ?",
            proj_params + [limit],
        ).fetchall()
        for r in rows:
            d = row_to_dict(r)
            summary = f"{d['session_type']} — {d['minutes']}m"
            if d.get("lines_added"):
                summary += f" (+{d['lines_added']}/-{d.get('lines_removed', 0)} lines)"
            if d.get("notes"):
                summary += f" · {d['notes'][:60]}"
            events.append({
                "type": "worklog",
                "project_id": d["project_id"],
                "summary": summary,
                "ts": d["date"] + "T00:00:00Z",
            })

    # Sort all events by timestamp, newest first
    events.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return events[:limit]

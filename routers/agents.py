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
    body = await request.body()
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

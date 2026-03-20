"""
routers/machines.py — Machine agent endpoints.

Receives heartbeats from rialu-agent daemons running on local machines,
stores them in machine_heartbeats, and exposes machine state to the SPA.
All agent endpoints are authenticated via HMAC-SHA256.
"""

import hashlib
import hmac
import json
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from db import db, row_to_dict

router = APIRouter(prefix="/api", tags=["machines"])


# ── HMAC verification ────────────────────────────────────────────────────────

def _agent_key() -> bytes:
    return os.environ.get("RIALU_AGENT_KEY", "").encode()


async def verify_hmac(request: Request):
    """FastAPI dependency — verify X-Rialu-Sig HMAC-SHA256 header."""
    sig_header = request.headers.get("X-Rialu-Sig", "")
    if not sig_header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Missing or malformed signature")
    body = await request.body()
    key = _agent_key()
    if not key:
        raise HTTPException(status_code=500, detail="RIALU_AGENT_KEY not configured")
    expected = "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig_header):
        raise HTTPException(status_code=401, detail="Invalid signature")
    # Stash parsed body on request state so endpoints don't re-parse
    request.state.body = json.loads(body)


# ── Pydantic models ──────────────────────────────────────────────────────────

class ActionResultIn(BaseModel):
    machine: str
    action_id: int
    status: str  # "success" | "error"
    result: Optional[str] = None


class ActionIn(BaseModel):
    machine: str
    action_type: str
    payload: Optional[str] = None


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/machines")
def list_machines():
    """Return latest heartbeat per machine."""
    with db() as conn:
        rows = conn.execute("""
            SELECT m1.* FROM machine_heartbeats m1
            INNER JOIN (
                SELECT machine_name, MAX(received_at) AS max_at
                FROM machine_heartbeats
                GROUP BY machine_name
            ) m2 ON m1.machine_name = m2.machine_name
                 AND m1.received_at = m2.max_at
            ORDER BY m1.machine_name
        """).fetchall()
    result = []
    for r in rows:
        d = row_to_dict(r)
        d["processes"] = json.loads(d["processes_json"]) if d.get("processes_json") else []
        d["repos"] = json.loads(d["repos_json"]) if d.get("repos_json") else []
        del d["processes_json"]
        del d["repos_json"]
        result.append(d)
    return result


@router.post("/agent/heartbeat", status_code=202, dependencies=[Depends(verify_hmac)])
async def agent_heartbeat(request: Request):
    """Receive heartbeat from rialu-agent, upsert into machine_heartbeats."""
    data = request.state.body
    machine = data.get("machine")
    if not machine:
        raise HTTPException(status_code=400, detail="Missing 'machine' field")

    with db() as conn:
        # Delete previous heartbeat for this machine (one row per machine)
        conn.execute("DELETE FROM machine_heartbeats WHERE machine_name = ?", (machine,))
        conn.execute(
            """INSERT INTO machine_heartbeats
               (machine_name, cpu_pct, ram_pct, gpu_pct, processes_json, repos_json, received_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                machine,
                data.get("cpu_pct"),
                data.get("ram_pct"),
                data.get("gpu_pct"),
                json.dumps(data.get("processes", [])),
                json.dumps(data.get("repos", [])),
            ),
        )
    return {"status": "accepted", "machine": machine}


@router.post("/agent/result", status_code=200, dependencies=[Depends(verify_hmac)])
async def agent_result(request: Request):
    """Receive action result from rialu-agent, update agent_actions row."""
    data = request.state.body
    action_id = data.get("action_id")
    if not action_id:
        raise HTTPException(status_code=400, detail="Missing 'action_id' field")

    with db() as conn:
        row = conn.execute("SELECT id FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Action not found")
        conn.execute(
            "UPDATE agent_actions SET status = ?, result = ? WHERE id = ?",
            (data.get("status", "unknown"), data.get("result"), action_id),
        )
    return {"status": "updated", "action_id": action_id}


@router.post("/agent/action", status_code=201)
def agent_action(payload: ActionIn):
    """Stub — queue an action for a machine agent. Phase 3 will proxy to the agent."""
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO agent_actions (machine_name, action_type, payload) VALUES (?, ?, ?)",
            (payload.machine, payload.action_type, payload.payload),
        )
    return {"status": "queued", "action_id": cur.lastrowid}

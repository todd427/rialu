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
from faire_hub import faire_hub
from ws_hub import hub

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


# ── Heartbeat shape ──────────────────────────────────────────────────────────

def normalise_heartbeat(data: dict) -> dict:
    """Reduce either heartbeat shape to the columns we store.

    Agents upgrade per-machine via firstlight, so the hub sees the old flat
    shape (`cpu_pct`, scalar `gpu_pct`, no `gpus`) and the new one
    (`cpu: {load_pct, temp_c}`, `gpus: [...]`) at the same time for some
    period. Both are accepted here so neither end of a rolling upgrade breaks.

    The legacy scalars are always derived and always stored — Faire's .mcard
    reads cpu_pct/ram_pct/gpu_pct today and must render unchanged.

    Shared by the HTTP route and ws_hub's WebSocket path; two copies of this
    logic would drift the moment one shape gained a field.
    """
    cpu = data.get("cpu") or {}
    gpus = data.get("gpus")

    if gpus is None:
        # Legacy agent. It cannot tell us anything per-die, so `gpus` stays an
        # empty list rather than a fabricated entry — the brief rules out
        # zero-filled placeholders — and the scalar carries what we do know.
        gpu_pct = data.get("gpu_pct")
        gpus = []
    else:
        loads = [g.get("load_pct") for g in gpus if g.get("load_pct") is not None]
        # max, not mean: a monitoring field must not average away a pegged card.
        gpu_pct = max(loads) if loads else None

    return {
        # New shape wins where present; fall back to the flat legacy field.
        "cpu_pct": cpu.get("load_pct", data.get("cpu_pct")),
        "ram_pct": data.get("ram_pct"),
        "gpu_pct": gpu_pct,
        "cpu_temp_c": cpu.get("temp_c"),
        "gpus": gpus,
        "processes": data.get("processes", []),
        "repos": data.get("repos", []),
    }


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
        # `gpus` is a list at every layer — a machine with no card reports [],
        # never null, so consumers never special-case "the GPU".
        d["gpus"] = json.loads(d["gpus_json"]) if d.get("gpus_json") else []
        # Nested shape for new consumers; the flat cpu_pct/ram_pct/gpu_pct keys
        # stay alongside it untouched for Faire.
        d["cpu"] = {"load_pct": d.get("cpu_pct"), "temp_c": d.get("cpu_temp_c")}
        del d["processes_json"]
        del d["repos_json"]
        del d["gpus_json"]
        del d["cpu_temp_c"]   # exposed as cpu.temp_c; one home for the new shape
        result.append(d)
    return result


@router.post("/agent/heartbeat", status_code=202, dependencies=[Depends(verify_hmac)])
async def agent_heartbeat(request: Request):
    """Receive heartbeat from rialu-agent, upsert into machine_heartbeats."""
    data = request.state.body
    machine = data.get("machine")
    if not machine:
        raise HTTPException(status_code=400, detail="Missing 'machine' field")

    hb = normalise_heartbeat(data)
    with db() as conn:
        # Delete previous heartbeat for this machine (one row per machine)
        conn.execute("DELETE FROM machine_heartbeats WHERE machine_name = ?", (machine,))
        conn.execute(
            """INSERT INTO machine_heartbeats
               (machine_name, cpu_pct, ram_pct, gpu_pct, cpu_temp_c,
                processes_json, repos_json, gpus_json, received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                machine,
                hb["cpu_pct"],
                hb["ram_pct"],
                hb["gpu_pct"],
                hb["cpu_temp_c"],
                json.dumps(hb["processes"]),
                json.dumps(hb["repos"]),
                json.dumps(hb["gpus"]),
            ),
        )

    # Agents normally heartbeat over the WebSocket, but this HTTP path is still
    # live — fan out here too, or a viewer silently misses any machine using it.
    from ws_hub import hub
    await hub.broadcast_to_viewers({
        "type": "heartbeat",
        "machine_name": machine,
        "cpu_pct": hb["cpu_pct"],
        "ram_pct": hb["ram_pct"],
        "gpu_pct": hb["gpu_pct"],
        "cpu": {"load_pct": hb["cpu_pct"], "temp_c": hb["cpu_temp_c"]},
        "gpus": hb["gpus"],
        "processes": hb["processes"],
        "repos": hb["repos"],
    })
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


@router.post("/agent/action", status_code=201, dependencies=[Depends(verify_hmac)])
async def agent_action(request: Request, payload: ActionIn):
    """Queue an action and forward to agent via WebSocket if connected."""
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO agent_actions (machine_name, action_type, payload) VALUES (?, ?, ?)",
            (payload.machine, payload.action_type, payload.payload),
        )
        action_id = cur.lastrowid

    # Try to send via WebSocket
    sent = await hub.send_to_agent(payload.machine, {
        "type": "action",
        "action_id": action_id,
        "action_type": payload.action_type,
        "payload": payload.payload,
    })
    return {
        "status": "sent" if sent else "queued",
        "action_id": action_id,
    }


# ── tmux / Claude Code / send-keys ──────────────────────────────────────────

@router.get("/machines/{machine}/tmux")
async def get_tmux(machine: str):
    """Get tmux sessions/panes for a machine via the WebSocket agent."""
    sessions = await hub.request_tmux_list(machine)
    if sessions is None:
        raise HTTPException(status_code=404, detail=f"Machine '{machine}' not connected")
    return sessions


@router.get("/machines/claude")
def get_claude_sessions():
    """Get all Claude Code sessions across all machines."""
    return hub.get_claude_sessions()


class SendKeysIn(BaseModel):
    pane_id: str
    keys: str


@router.post("/machines/{machine}/send")
async def send_keys(machine: str, payload: SendKeysIn):
    """Inject keystrokes into a tmux pane on a machine."""
    sent = await hub.send_to_agent(machine, {
        "type": "send_keys",
        "pane_id": payload.pane_id,
        "keys": payload.keys,
    })
    if not sent:
        raise HTTPException(status_code=404, detail=f"Machine '{machine}' not connected")
    return {"status": "sent"}


class CcStartIn(BaseModel):
    slug: str
    path: str = ""


class CcStopIn(BaseModel):
    target: str = ""
    slug: str = ""


@router.post("/machines/{machine}/cc-session/start")
async def cc_session_start_remote(machine: str, payload: CcStartIn):
    """Launch (or reuse) a Claude Code tmux session on a remote machine."""
    try:
        res = await hub.run_agent_action(
            machine, "start_cc_session",
            {"slug": payload.slug, "path": payload.path},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    if res.get("status") != "success":
        raise HTTPException(status_code=500, detail=res.get("result", "start failed"))
    return {"status": "started", "target": res.get("result", "")}


@router.post("/machines/{machine}/cc-session/stop")
async def cc_session_stop_remote(machine: str, payload: CcStopIn):
    """Kill a Claude Code tmux session on a remote machine."""
    try:
        res = await hub.run_agent_action(
            machine, "stop_cc_session",
            {"target": payload.target, "slug": payload.slug},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    if res.get("status") != "success":
        raise HTTPException(status_code=500, detail=res.get("result", "stop failed"))
    return {"status": "stopped", "result": res.get("result", "")}


class InitRepoIn(BaseModel):
    name: str
    description: str = ""


@router.post("/machines/{machine}/init-repo")
async def init_repo_remote(machine: str, payload: InitRepoIn):
    """Scaffold a new git repo on a remote machine via its agent."""
    try:
        res = await hub.run_agent_action(
            machine, "init_repo",
            {"name": payload.name, "description": payload.description},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    if res.get("status") != "success":
        raise HTTPException(status_code=500, detail=res.get("result", "init failed"))
    return {"status": "created", "path": res.get("result", "")}


@router.get("/machines/status")
def machines_status():
    """Quick status: which machines are connected via WebSocket."""
    connected = hub.connected_machines()
    with db() as conn:
        rows = conn.execute("""
            SELECT machine_name, received_at FROM machine_heartbeats
            ORDER BY machine_name
        """).fetchall()
    machines = {}
    for r in rows:
        machines[r["machine_name"]] = {
            "last_heartbeat": r["received_at"],
            "ws_connected": r["machine_name"] in connected,
        }
    for m in connected:
        if m not in machines:
            machines[m] = {"last_heartbeat": None, "ws_connected": True}
    return machines


@router.delete("/machines/{machine}")
def remove_machine(machine: str):
    """Remove a machine's stored heartbeat — clears a retired/ghost card.

    Refuses if the agent is currently WS-connected, since a live machine would
    just re-create the row on its next heartbeat.
    """
    if hub.is_connected(machine):
        raise HTTPException(status_code=409, detail=f"Machine '{machine}' is currently connected")
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM machine_heartbeats WHERE machine_name = ?", (machine,)
        )
        deleted = cur.rowcount
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Machine '{machine}' not found")
    return {"status": "removed", "machine": machine}

"""
routers/machines.py — Stub for Phase 2 (rialu-agent).

Phase 1: returns empty data so the Machines tab renders cleanly.
Phase 2: agent heartbeat receiver, action proxy, repo state.
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api", tags=["machines"])


@router.get("/machines")
def list_machines():
    """Phase 2: returns live heartbeat data from rialu-agent daemons."""
    return []


@router.post("/agent/heartbeat", status_code=202)
def agent_heartbeat(payload: dict):
    """Phase 2: receives 30s heartbeat from rialu-agent on each machine."""
    return {"status": "accepted"}


@router.post("/agent/action")
def agent_action(payload: dict):
    """Phase 2: proxy action (git pull, restart process) to named machine."""
    return {"status": "phase2_not_implemented"}

"""
routers/deployments.py — Serve cached cloud service status and deploy history.
"""

from fastapi import APIRouter
from db import db, row_to_dict
from poller import run_all_now

router = APIRouter(prefix="/api/deployments", tags=["deployments"])


@router.get("")
def list_deployments():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM deployments_cache ORDER BY platform, service_name"
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@router.get("/history")
def deploy_history(limit: int = 20):
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM deploy_history
               ORDER BY deployed_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@router.post("/refresh")
async def refresh_deployments():
    await run_all_now()
    return {"status": "ok", "message": "Poll triggered"}

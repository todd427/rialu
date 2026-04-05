"""
routers/sentinel.py — Sentinel threat intelligence dashboard.

Proxies the Sentinel API to show threat stats, recent events,
top offenders, and blocklist status on rialu.ie. Also proxies
the admin settings endpoints so Rialú is the single control plane.
"""

import os

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/sentinel", tags=["sentinel"])

SENTINEL_URL = os.environ.get("SENTINEL_URL", "https://sentinel-foxxelabs.fly.dev")
SENTINEL_KEY = os.environ.get("SENTINEL_API_KEY", "")


async def _sentinel_get(path: str) -> dict | list | None:
    if not SENTINEL_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{SENTINEL_URL}{path}",
                headers={"X-Sentinel-Key": SENTINEL_KEY},
            )
            if r.status_code == 200:
                return r.json()
            return None
    except Exception:
        return None


async def _sentinel_patch(path: str, payload: dict) -> dict | None:
    if not SENTINEL_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.patch(
                f"{SENTINEL_URL}{path}",
                json=payload,
                headers={"X-Sentinel-Key": SENTINEL_KEY},
            )
            if r.status_code == 200:
                return r.json()
            if r.status_code == 422:
                raise HTTPException(status_code=422, detail=r.json().get("detail"))
            return None
    except HTTPException:
        raise
    except Exception:
        return None


# ── Read-only dashboard endpoints ────────────────────────────────────────

@router.get("/overview")
async def overview():
    """Combined overview: stats + blocklist count + top offenders."""
    stats = await _sentinel_get("/stats")
    blocklist = await _sentinel_get("/blocklist")
    return {
        "stats": stats,
        "blocklist_count": blocklist["count"] if blocklist else 0,
        "blocklist_ips": blocklist["ips"][:20] if blocklist else [],
    }


@router.get("/events")
async def recent_events(hours: int = 24, limit: int = 50):
    """Proxy recent individual events from Sentinel."""
    return await _sentinel_get(f"/events/recent?hours={hours}&limit={limit}") or {"events": [], "count": 0}


@router.get("/ip/{ip}")
async def ip_detail(ip: str):
    """Full IP detail from Sentinel."""
    return await _sentinel_get(f"/ip/{ip}")


@router.get("/stats")
async def stats():
    return await _sentinel_get("/stats") or {}


@router.get("/blocklist")
async def blocklist():
    return await _sentinel_get("/blocklist") or {"count": 0, "ips": []}


# ── Admin settings endpoints ─────────────────────────────────────────────

class SettingsPatch(BaseModel):
    settings: dict[str, str]


@router.get("/settings")
async def get_settings():
    """Proxy GET /admin/settings from Sentinel."""
    result = await _sentinel_get("/admin/settings")
    if result is None:
        raise HTTPException(status_code=502, detail="Sentinel unavailable or not configured")
    return result


@router.patch("/settings")
async def update_settings(data: SettingsPatch):
    """Proxy PATCH /admin/settings to Sentinel."""
    result = await _sentinel_patch("/admin/settings", {"settings": data.settings})
    if result is None:
        raise HTTPException(status_code=502, detail="Sentinel unavailable or not configured")
    return result

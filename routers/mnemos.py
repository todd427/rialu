"""
routers/mnemos.py — Mnemos memory system integration.

Proxies the Mnemos HTTP API to show memory stats, search, and ingest
work sessions / milestones from Rialú.
"""

import os
import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/mnemos", tags=["mnemos"])
log = logging.getLogger("rialu.mnemos")

MNEMOS_URL = os.environ.get("MNEMOS_URL", "https://mnemos.foxxelabs.ie")
MNEMOS_KEY = os.environ.get("MNEMOS_API_KEY", "")


async def _mnemos_get(path: str) -> dict | None:
    if not MNEMOS_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{MNEMOS_URL}{path}",
                headers={"X-API-Key": MNEMOS_KEY},
            )
            if r.status_code == 200:
                return r.json()
            return None
    except Exception:
        return None


async def _mnemos_post(path: str, json: dict) -> dict | None:
    if not MNEMOS_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{MNEMOS_URL}{path}",
                headers={"X-API-Key": MNEMOS_KEY},
                json=json,
            )
            if r.status_code == 200:
                return r.json()
            return None
    except Exception:
        return None


# ── stats ────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def stats():
    """Mnemos collection statistics."""
    data = await _mnemos_get("/api/stats")
    if data is None:
        return {"status": "unavailable", "total": 0}
    return data


# ── search ───────────────────────────────────────────────────────────────────

class SearchIn(BaseModel):
    query: str
    top: int = 6
    filter: str = "nonfiction"


@router.post("/search")
async def search(s: SearchIn):
    """Search Mnemos memory."""
    data = await _mnemos_post("/api/query", {
        "query": s.query,
        "top": s.top,
        "type_filter": s.filter,
    })
    if data is None:
        return {"query": s.query, "count": 0, "hits": []}
    return data


# ── ingest ───────────────────────────────────────────────────────────────────

class IngestIn(BaseModel):
    title: str
    text: str
    tag: str = "doc"
    source: str = "rialu"


@router.post("/ingest")
async def ingest(doc: IngestIn):
    """Ingest a document into Mnemos."""
    data = await _mnemos_post("/api/ingest", {
        "documents": [{
            "id": f"rialu-{doc.title.lower().replace(' ', '-')[:60]}",
            "text": doc.text,
            "metadata": {
                "title": doc.title,
                "source": doc.source,
                "sft_tag": doc.tag,
            },
        }],
    })
    if data is None:
        raise HTTPException(502, "Mnemos unavailable")
    return data


# ── fire-and-forget ingest helper (used by other routers) ────────────────────

async def ingest_activity(title: str, text: str):
    """Fire-and-forget ingest of a work activity into Mnemos.
    Called from session/milestone handlers. Never raises."""
    if not MNEMOS_KEY:
        return
    try:
        await _mnemos_post("/api/ingest", {
            "documents": [{
                "id": f"rialu-activity-{title.lower().replace(' ', '-')[:60]}",
                "text": text,
                "metadata": {
                    "title": title,
                    "source": "rialu",
                    "sft_tag": "notes",
                },
            }],
        })
    except Exception:
        log.warning("Mnemos ingest failed for: %s", title)

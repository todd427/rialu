"""
routers/spend.py — Receiver for Suim per-project spend rollups.

Suim ("ps/top for Claude usage across the fleet") feeds ground-truth per-project
Claude spend *up* to Rialú as windowed rollups; services then enforce against the
one authoritative number. Suim informs, Rialú/services enforce — Suim never blocks.

Wire contract is pinned by Suim (docs/suim-spend-rollup-receiver-prd.md §2). This
plane owns storage + policy:

  - POST /api/spend          upsert a rollup on rollup_key (idempotent — §4)
  - GET  /api/spend/summary  recent $/hr per project vs projects.cost_limit_hr (§10)
  - GET  /api/spend/recent   latest rollups, newest first (observability)

Two invariants from the contract:
  - **Upsert on rollup_key.** Suim re-sends the identical key after a lost ack; the
    re-send must be a no-op, never a double-count.
  - **Accept, don't reject.** An unknown or NULL project_id is stored as-is and
    reconciled later — never 4xx a valid-shaped rollup for an unknown slug.

Complements (does not replace) anthropic_usage: that table is account-wide by API
key from Console CSV; this is per-project ground truth from Suim.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from auth import verify_faire_token
from db import db, row_to_dict

router = APIRouter(prefix="/api/spend", tags=["spend"])
log = logging.getLogger("rialu.spend")


class SpendRollupIn(BaseModel):
    rollup_key: str
    project_id: Optional[str] = None  # projects.slug, or None (unresolved bucket)
    window_start: str                 # UTC ISO-8601, half-open [start, end)
    window_end: str
    cost_usd: float                   # Suim's native unit (USD); stored verbatim
    input_tokens: int
    output_tokens: int


def _broadcast_spend(data: dict) -> None:
    """Schedule a Faire hub broadcast for a spend update (best-effort)."""
    from faire_hub import faire_hub
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(faire_hub.broadcast({"event": "spend.update", "payload": data}))
    except RuntimeError:
        pass  # no running loop (e.g. sync test client) — skip


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    """Parse a UTC ISO-8601 timestamp; assume UTC if naive. None on bad input."""
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@router.post("", dependencies=[Depends(verify_faire_token)])
def receive_spend(rollup: SpendRollupIn):
    """
    Upsert one Suim rollup on rollup_key. Any 2xx is the ack Suim drains on; a
    duplicate key overwrites in place, so a re-send after a lost ack never
    double-counts (§4). Unknown/NULL project_id is accepted, not rejected (§2).
    """
    with db() as conn:
        conn.execute(
            """INSERT INTO project_spend
                 (rollup_key, project_id, window_start, window_end,
                  cost_usd, input_tokens, output_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(rollup_key) DO UPDATE SET
                 project_id    = excluded.project_id,
                 window_start  = excluded.window_start,
                 window_end    = excluded.window_end,
                 cost_usd      = excluded.cost_usd,
                 input_tokens  = excluded.input_tokens,
                 output_tokens = excluded.output_tokens,
                 received_at   = datetime('now')""",
            (rollup.rollup_key, rollup.project_id, rollup.window_start,
             rollup.window_end, rollup.cost_usd, rollup.input_tokens,
             rollup.output_tokens),
        )
    _broadcast_spend(rollup.model_dump())
    return {"ok": True, "rollup_key": rollup.rollup_key}


@router.get("/summary")
def summary(lookback_hours: int = Query(default=24, ge=1, le=720)):
    """
    Recent $/hr per project, compared to projects.cost_limit_hr (the policy number
    services read; computing the breach is in scope, blocking is not — §10).

    spend_usd_per_hr = sum(cost_usd) / sum(window-hours) across rollups whose
    window_end falls within the lookback. Dividing by covered hours (not wall-clock)
    yields the actual average rate during active windows. Unknown/NULL project_id
    is reported too (cost_limit_hr=None, over_budget=False) so unattributed spend
    stays visible.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    with db() as conn:
        rows = conn.execute(
            "SELECT project_id, window_start, window_end, cost_usd FROM project_spend"
        ).fetchall()
        limits = {
            r["slug"]: r["cost_limit_hr"]
            for r in conn.execute("SELECT slug, cost_limit_hr FROM projects").fetchall()
        }

    agg: dict[Optional[str], list[float]] = {}  # project_id -> [cost_sum, hours_sum]
    for r in rows:
        end = _parse_dt(r["window_end"])
        if end is None or end < cutoff:
            continue
        start = _parse_dt(r["window_start"])
        hours = (end - start).total_seconds() / 3600 if start else 0.0
        if hours <= 0:
            continue
        slot = agg.setdefault(r["project_id"], [0.0, 0.0])
        slot[0] += r["cost_usd"]
        slot[1] += hours

    out = []
    for pid, (cost, hours) in agg.items():
        rate = cost / hours if hours else 0.0
        limit = limits.get(pid)  # None for unknown/NULL project_id
        out.append({
            "project_id": pid,
            "spend_usd_per_hr": round(rate, 6),
            "cost_limit_hr": limit,
            "over_budget": limit is not None and rate > limit,
        })
    out.sort(key=lambda x: x["spend_usd_per_hr"], reverse=True)
    return {"lookback_hours": lookback_hours, "projects": out}


@router.get("/recent")
def recent(limit: int = Query(default=50, ge=1, le=500)):
    """Latest rollups, newest first — observability for the drain backlog."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM project_spend ORDER BY received_at DESC, rollup_key DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]

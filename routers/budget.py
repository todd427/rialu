"""
routers/budget.py — Platform costs, API registry, and usage tracking.
"""

from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import db, row_to_dict
from poller import poll_fly_billing

router = APIRouter(tags=["budget"])


# ── models ───────────────────────────────────────────────────────────────────

class BudgetIn(BaseModel):
    platform: str
    service_name: str
    cost_gbp: float = 0.0
    period: str = "monthly"
    active: bool = True
    notes: Optional[str] = None


class BudgetUpdate(BaseModel):
    platform: Optional[str] = None
    service_name: Optional[str] = None
    cost_gbp: Optional[float] = None
    period: Optional[str] = None
    active: Optional[bool] = None
    notes: Optional[str] = None


class ApiRegistryIn(BaseModel):
    name: str
    provider: str
    auth_key_ref: Optional[str] = None
    billing_model: str = "per_token"
    cost_unit: Optional[str] = None
    cost_per_unit_gbp: float = 0.0
    billing_url: Optional[str] = None
    usage_api_endpoint: Optional[str] = None
    notes: Optional[str] = None
    active: bool = True


class ApiRegistryUpdate(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    billing_model: Optional[str] = None
    cost_per_unit_gbp: Optional[float] = None
    notes: Optional[str] = None
    active: Optional[bool] = None


class ApiProjectMapIn(BaseModel):
    api_id: int
    project_id: int
    usage_description: Optional[str] = None
    is_primary: bool = False


# ── budget ────────────────────────────────────────────────────────────────────

@router.get("/api/budget")
def list_budget():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM budget ORDER BY platform, service_name"
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@router.get("/api/budget/summary")
def budget_summary():
    with db() as conn:
        monthly = conn.execute(
            """SELECT COALESCE(SUM(cost_gbp), 0) as total
               FROM budget WHERE active = 1 AND period = 'monthly'"""
        ).fetchone()["total"]
        annual = conn.execute(
            """SELECT COALESCE(SUM(cost_gbp / 12), 0) as total
               FROM budget WHERE active = 1 AND period = 'annual'"""
        ).fetchone()["total"]
        api_30d = conn.execute(
            """SELECT COALESCE(SUM(cost_gbp), 0) as total
               FROM api_usage
               WHERE recorded_at >= datetime('now', '-30 days')"""
        ).fetchone()["total"]
    return {
        "monthly_platform_eur": round(monthly + annual, 2),
        "api_30d_eur": round(api_30d, 2),
        "total_eur": round(monthly + annual + api_30d, 2),
    }


@router.post("/api/budget/refresh")
async def refresh_budget():
    await poll_fly_billing()
    return {"status": "ok", "message": "Billing refreshed"}


@router.post("/api/budget", status_code=201)
def create_budget(b: BudgetIn):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO budget (platform, service_name, cost_gbp, period, active, notes) VALUES (?,?,?,?,?,?)",
            (b.platform, b.service_name, b.cost_gbp, b.period, int(b.active), b.notes),
        )
        row = conn.execute("SELECT * FROM budget WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@router.put("/api/budget/{entry_id}")
def update_budget(entry_id: int, b: BudgetUpdate):
    fields = {k: v for k, v in b.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update")
    if "active" in fields:
        fields["active"] = 1 if fields["active"] else 0
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE budget SET {set_clause} WHERE id = ?", list(fields.values()) + [entry_id])
        row = conn.execute("SELECT * FROM budget WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    return row_to_dict(row)


@router.delete("/api/budget/{entry_id}", status_code=204)
def delete_budget(entry_id: int):
    with db() as conn:
        conn.execute("DELETE FROM budget WHERE id = ?", (entry_id,))


# ── api registry ──────────────────────────────────────────────────────────────

@router.get("/api/apis")
def list_apis():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM api_registry ORDER BY provider, name"
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@router.post("/api/apis", status_code=201)
def create_api(a: ApiRegistryIn):
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO api_registry
               (name, provider, auth_key_ref, billing_model, cost_unit,
                cost_per_unit_gbp, billing_url, usage_api_endpoint, notes, active)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (a.name, a.provider, a.auth_key_ref, a.billing_model, a.cost_unit,
             a.cost_per_unit_gbp, a.billing_url, a.usage_api_endpoint, a.notes, int(a.active)),
        )
        row = conn.execute("SELECT * FROM api_registry WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@router.put("/api/apis/{api_id}")
def update_api(api_id: int, a: ApiRegistryUpdate):
    fields = {k: v for k, v in a.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update")
    if "active" in fields:
        fields["active"] = 1 if fields["active"] else 0
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE api_registry SET {set_clause} WHERE id = ?", list(fields.values()) + [api_id])
        row = conn.execute("SELECT * FROM api_registry WHERE id = ?", (api_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    return row_to_dict(row)


@router.delete("/api/apis/{api_id}", status_code=204)
def delete_api(api_id: int):
    with db() as conn:
        conn.execute("DELETE FROM api_registry WHERE id = ?", (api_id,))


# ── api ↔ project map ─────────────────────────────────────────────────────────

@router.get("/api/apis/{api_id}/projects")
def api_projects(api_id: int):
    with db() as conn:
        rows = conn.execute(
            """SELECT m.*, p.name as project_name
               FROM api_project_map m JOIN projects p ON p.id = m.project_id
               WHERE m.api_id = ?""",
            (api_id,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@router.get("/api/apis/costs-by-project")
def api_costs_by_project(days: int = 30):
    """Aggregate api_usage costs grouped by project, last N days."""
    with db() as conn:
        rows = conn.execute("""
            SELECT p.id as project_id, p.name as project_name,
                   COALESCE(SUM(u.cost_gbp), 0) as cost_eur,
                   COALESCE(SUM(u.tokens_in), 0) as tokens_in,
                   COALESCE(SUM(u.tokens_out), 0) as tokens_out,
                   COALESCE(SUM(u.call_count), 0) as calls
            FROM api_usage u
            JOIN projects p ON p.id = u.project_id
            WHERE u.recorded_at >= datetime('now', ? || ' days')
            GROUP BY p.id
            ORDER BY cost_eur DESC
        """, (f"-{days}",)).fetchall()
        unattr = conn.execute("""
            SELECT COALESCE(SUM(cost_gbp), 0) as cost_eur,
                   COALESCE(SUM(tokens_in), 0) as tokens_in,
                   COALESCE(SUM(tokens_out), 0) as tokens_out,
                   COALESCE(SUM(call_count), 0) as calls
            FROM api_usage
            WHERE project_id IS NULL
              AND recorded_at >= datetime('now', ? || ' days')
        """, (f"-{days}",)).fetchone()
    result = [row_to_dict(r) for r in rows]
    ua = row_to_dict(unattr)
    if ua["cost_eur"] > 0 or ua["calls"] > 0:
        result.append({
            "project_id": None,
            "project_name": "Unattributed",
            "cost_eur": ua["cost_eur"],
            "tokens_in": ua["tokens_in"],
            "tokens_out": ua["tokens_out"],
            "calls": ua["calls"],
        })
    return result


@router.post("/api/apis/map", status_code=201)
def map_api_project(m: ApiProjectMapIn):
    with db() as conn:
        cur = conn.execute(
            """INSERT OR REPLACE INTO api_project_map
               (api_id, project_id, usage_description, is_primary)
               VALUES (?,?,?,?)""",
            (m.api_id, m.project_id, m.usage_description, int(m.is_primary)),
        )
        row = conn.execute(
            "SELECT * FROM api_project_map WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return row_to_dict(row)

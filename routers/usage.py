"""
routers/usage.py — Anthropic API token usage tracking.

Import usage from Anthropic Console CSV exports, view summaries by day/model/key.
CSV is downloaded from: console.anthropic.com → Usage → Export CSV
"""

import csv
import io

from fastapi import APIRouter, UploadFile, File
from db import db, row_to_dict

router = APIRouter(prefix="/api/usage", tags=["usage"])

# Anthropic pricing (USD per million tokens, March 2026)
PRICING = {
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-sonnet-4-20250514":   {"input": 3.00,  "output": 15.00, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00,  "cache_write": 1.00,  "cache_read": 0.08},
}
WEB_SEARCH_COST = 0.01  # $0.01 per search
DEFAULT_PRICING = {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30}
USD_TO_EUR = 0.92


def _estimate_cost(row: dict) -> float:
    """Estimate cost in USD for a single usage row."""
    model = row.get("model", "")
    pricing = PRICING.get(model, DEFAULT_PRICING)
    cost = 0.0
    cost += (row.get("input_tokens", 0) / 1_000_000) * pricing["input"]
    cost += (row.get("output_tokens", 0) / 1_000_000) * pricing["output"]
    cost += (row.get("cache_write_5m", 0) / 1_000_000) * pricing["cache_write"]
    cost += (row.get("cache_write_1h", 0) / 1_000_000) * pricing["cache_write"]
    cost += (row.get("cache_read", 0) / 1_000_000) * pricing["cache_read"]
    cost += row.get("web_searches", 0) * WEB_SEARCH_COST
    return round(cost * USD_TO_EUR, 4)


@router.post("/import")
async def import_csv(file: UploadFile = File(...)):
    """Import Anthropic usage CSV. Upserts by (date, model, api_key_name)."""
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    imported = 0
    with db() as conn:
        for row in reader:
            date = row.get("usage_date_utc", "").strip()
            model = row.get("model_version", "").strip()
            key_name = row.get("api_key", "").strip()
            if not date or not model:
                continue

            input_tokens = int(row.get("usage_input_tokens_no_cache", 0) or 0)
            cache_w5 = int(row.get("usage_input_tokens_cache_write_5m", 0) or 0)
            cache_w1 = int(row.get("usage_input_tokens_cache_write_1h", 0) or 0)
            cache_r = int(row.get("usage_input_tokens_cache_read", 0) or 0)
            output_tokens = int(row.get("usage_output_tokens", 0) or 0)
            web_searches = int(row.get("web_search_count", 0) or 0)

            data = {
                "model": model, "input_tokens": input_tokens,
                "cache_write_5m": cache_w5, "cache_write_1h": cache_w1,
                "cache_read": cache_r, "output_tokens": output_tokens,
                "web_searches": web_searches,
            }
            cost = _estimate_cost(data)

            conn.execute("""
                INSERT INTO anthropic_usage
                    (usage_date, model, api_key_name, input_tokens, cache_write_5m,
                     cache_write_1h, cache_read, output_tokens, web_searches, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(usage_date, model, api_key_name) DO UPDATE SET
                    input_tokens=excluded.input_tokens,
                    cache_write_5m=excluded.cache_write_5m,
                    cache_write_1h=excluded.cache_write_1h,
                    cache_read=excluded.cache_read,
                    output_tokens=excluded.output_tokens,
                    web_searches=excluded.web_searches,
                    cost_usd=excluded.cost_usd,
                    imported_at=datetime('now')
            """, (date, model, key_name, input_tokens, cache_w5, cache_w1,
                  cache_r, output_tokens, web_searches, cost))
            imported += 1

    return {"status": "ok", "rows_imported": imported}


@router.get("/summary")
def usage_summary():
    """Totals for current month and last 30 days."""
    with db() as conn:
        month = conn.execute("""
            SELECT COALESCE(SUM(input_tokens + cache_write_5m + cache_write_1h + cache_read), 0) as total_input,
                   COALESCE(SUM(output_tokens), 0) as total_output,
                   COALESCE(SUM(cost_usd), 0) as total_cost_usd,
                   COALESCE(SUM(web_searches), 0) as total_searches
            FROM anthropic_usage
            WHERE usage_date >= date('now', 'start of month')
        """).fetchone()
        prev = conn.execute("""
            SELECT COALESCE(SUM(cost_usd), 0) as total_cost_usd
            FROM anthropic_usage
            WHERE usage_date >= date('now', '-30 days')
              AND usage_date < date('now', 'start of month')
        """).fetchone()
    return {
        "month_input_tokens": month["total_input"],
        "month_output_tokens": month["total_output"],
        "month_cost_eur": round(month["total_cost_usd"], 2),
        "month_web_searches": month["total_searches"],
        "prev_month_cost_eur": round(prev["total_cost_usd"], 2),
    }


@router.get("/daily")
def usage_daily(days: int = 30):
    """Daily usage breakdown."""
    with db() as conn:
        rows = conn.execute("""
            SELECT usage_date,
                   SUM(input_tokens + cache_write_5m + cache_write_1h + cache_read) as input_total,
                   SUM(output_tokens) as output_total,
                   SUM(cost_usd) as cost_usd,
                   SUM(web_searches) as web_searches
            FROM anthropic_usage
            WHERE usage_date >= date('now', ? || ' days')
            GROUP BY usage_date
            ORDER BY usage_date DESC
        """, (f"-{days}",)).fetchall()
    return [row_to_dict(r) for r in rows]


@router.get("/by-model")
def usage_by_model(days: int = 30):
    """Usage breakdown by model."""
    with db() as conn:
        rows = conn.execute("""
            SELECT model,
                   SUM(input_tokens + cache_write_5m + cache_write_1h + cache_read) as input_total,
                   SUM(output_tokens) as output_total,
                   SUM(cost_usd) as cost_usd
            FROM anthropic_usage
            WHERE usage_date >= date('now', ? || ' days')
            GROUP BY model
            ORDER BY cost_usd DESC
        """, (f"-{days}",)).fetchall()
    return [row_to_dict(r) for r in rows]


@router.get("/by-key")
def usage_by_key(days: int = 30):
    """Usage breakdown by API key."""
    with db() as conn:
        rows = conn.execute("""
            SELECT api_key_name,
                   SUM(input_tokens + cache_write_5m + cache_write_1h + cache_read) as input_total,
                   SUM(output_tokens) as output_total,
                   SUM(cost_usd) as cost_usd
            FROM anthropic_usage
            WHERE usage_date >= date('now', ? || ' days')
            GROUP BY api_key_name
            ORDER BY cost_usd DESC
        """, (f"-{days}",)).fetchall()
    return [row_to_dict(r) for r in rows]

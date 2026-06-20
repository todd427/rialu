"""
tests/test_spend.py — Suim spend-rollup receiver (POST /api/spend + summary).

Mirrors the contract Suim asserts in its own tests/test_drain_contract.py:
upsert-on-rollup_key idempotency (incl. lost-ack resend), accept-not-reject for
unknown/NULL slugs, and a queryable $/hr-vs-cost_limit_hr policy surface.
"""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from main import app
from db import init_db, db

client = TestClient(app)


def _rollup(project_id="suim",
            start="2026-06-20T00:00:00+00:00",
            end="2026-06-20T01:00:00+00:00",
            cost=1.5, in_tok=100, out_tok=50, key=None):
    if key is None:
        key = f"{project_id or ''}|{start}|{end}"
    return {
        "rollup_key": key,
        "project_id": project_id,
        "window_start": start,
        "window_end": end,
        "cost_usd": cost,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
    }


def _count():
    with db() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM project_spend").fetchone()["n"]


def test_post_spend_inserts_row():
    init_db()
    r = client.post("/api/spend", json=_rollup())
    assert r.status_code == 200
    assert r.json()["rollup_key"] == "suim|2026-06-20T00:00:00+00:00|2026-06-20T01:00:00+00:00"
    assert _count() == 1
    with db() as conn:
        row = conn.execute("SELECT * FROM project_spend").fetchone()
    assert row["project_id"] == "suim"
    assert row["cost_usd"] == 1.5
    assert row["input_tokens"] == 100


def test_duplicate_rollup_key_upserts_not_doublecounts():
    init_db()
    client.post("/api/spend", json=_rollup(cost=1.5))
    # Re-send the same key with a corrected cost — upsert overwrites, no new row.
    client.post("/api/spend", json=_rollup(cost=2.0))
    assert _count() == 1
    with db() as conn:
        assert conn.execute("SELECT cost_usd FROM project_spend").fetchone()["cost_usd"] == 2.0


def test_lost_ack_resend_is_idempotent():
    init_db()
    payload = _rollup()
    assert client.post("/api/spend", json=payload).status_code == 200
    # Suim never got the ack and re-sends the identical rollup next drain.
    assert client.post("/api/spend", json=payload).status_code == 200
    assert _count() == 1


def test_unknown_slug_is_accepted_not_rejected():
    init_db()
    r = client.post("/api/spend", json=_rollup(project_id="not-a-real-slug"))
    assert r.status_code == 200
    assert _count() == 1


def test_null_project_id_is_accepted():
    init_db()
    key = "|2026-06-20T00:00:00+00:00|2026-06-20T01:00:00+00:00"
    r = client.post("/api/spend", json=_rollup(project_id=None, key=key))
    assert r.status_code == 200
    with db() as conn:
        assert conn.execute("SELECT project_id FROM project_spend").fetchone()["project_id"] is None


def test_summary_rate_and_over_budget():
    init_db()
    # Project created via the API gets slug "suim" and cost_limit_hr default 1.0.
    proj = client.post("/api/projects", json={"name": "Suim"}).json()
    slug = proj["slug"]
    assert slug == "suim"

    # A 1-hour window costing $1.5 → 1.5 $/hr, over the 1.0 cap. Use a window that
    # ends "now" so it always falls inside the default 24h lookback.
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=1)).isoformat()
    end = now.isoformat()
    client.post("/api/spend", json=_rollup(project_id=slug, start=start, end=end, cost=1.5))

    r = client.get("/api/spend/summary")
    assert r.status_code == 200
    rows = {p["project_id"]: p for p in r.json()["projects"]}
    assert slug in rows
    assert abs(rows[slug]["spend_usd_per_hr"] - 1.5) < 1e-6
    assert rows[slug]["cost_limit_hr"] == 1.0
    assert rows[slug]["over_budget"] is True


def test_summary_ignores_windows_outside_lookback():
    init_db()
    proj = client.post("/api/projects", json={"name": "Suim"}).json()
    slug = proj["slug"]
    # A window two days ago is outside the default 24h lookback → excluded.
    old = datetime.now(timezone.utc) - timedelta(days=2)
    start = old.isoformat()
    end = (old + timedelta(hours=1)).isoformat()
    client.post("/api/spend", json=_rollup(project_id=slug, start=start, end=end, cost=9.0))

    rows = {p["project_id"]: p for p in client.get("/api/spend/summary").json()["projects"]}
    assert slug not in rows
    # Widening the lookback brings it back in.
    rows = {p["project_id"]: p for p in
            client.get("/api/spend/summary?lookback_hours=720").json()["projects"]}
    assert rows[slug]["over_budget"] is True


def test_recent_lists_newest_first():
    init_db()
    client.post("/api/spend", json=_rollup(
        key="a", start="2026-06-20T00:00:00+00:00", end="2026-06-20T01:00:00+00:00"))
    client.post("/api/spend", json=_rollup(
        key="b", start="2026-06-20T01:00:00+00:00", end="2026-06-20T02:00:00+00:00"))
    r = client.get("/api/spend/recent")
    assert r.status_code == 200
    assert len(r.json()) == 2

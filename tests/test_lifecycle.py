"""
tests/test_lifecycle.py — Rian lifecycle sync (docs/rian-lifecycle-sync-prd.md §8).

Covers the acceptance list: monotone upsert in both directions, idempotent
re-send, unknown repo_urls counted not rejected, the GitHub fallback filling
only NULLs, the census in one call, and a manual started_at surviving both syncs.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db, db
from routers import lifecycle as lc

client = TestClient(app)

REPO = "https://github.com/todd427/aigne"


@pytest.fixture(autouse=True)
def setup():
    init_db()


def _project(name="Aigne", repo_url=REPO, status="development"):
    return client.post("/api/projects", json={
        "name": name, "status": status, "repo_url": repo_url,
    }).json()


def _batch(items, run_at="2026-09-17T14:00:00+00:00"):
    return client.post("/api/lifecycle", json={
        "source": "rian", "run_at": run_at, "items": items,
    })


def _row(pid):
    with db() as conn:
        return dict(conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone())


# ── join key ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("https://github.com/todd427/aigne", "https://github.com/todd427/aigne/"),
    ("https://github.com/todd427/aigne", "https://github.com/todd427/aigne.git"),
    ("https://GitHub.com/todd427/aigne", "https://github.com/todd427/aigne"),
    ("https://github.com/todd427/aigne", "https://github.com/todd427/aigne.git/"),
])
def test_normalise_repo_url_matches_variants(a, b):
    assert lc.normalise_repo_url(a) == lc.normalise_repo_url(b)


def test_normalise_repo_url_keeps_distinct_repos_apart():
    assert lc.normalise_repo_url(REPO) != lc.normalise_repo_url("https://github.com/todd427/rialu")
    assert lc.normalise_repo_url("") is None
    assert lc.normalise_repo_url(None) is None


# ── monotone upsert ──────────────────────────────────────────────────────────

def test_first_batch_sets_dates_and_derives_started_at():
    p = _project()
    resp = _batch([{"repo_url": REPO, "first_seen": "2026-06-20T09:12:00+00:00",
                    "last_seen": "2026-09-13T19:40:00+00:00", "mentions": 412}])
    assert resp.status_code == 200
    assert resp.json() == {"matched": 1, "unmatched": 0, "unmatched_urls": []}
    row = _row(p["id"])
    assert row["first_seen"] == "2026-06-20T09:12:00+00:00"
    assert row["last_seen"] == "2026-09-13T19:40:00+00:00"
    assert row["started_at"] == "2026-06-20T09:12:00+00:00"
    assert row["started_at_source"] == "rian"


def test_resend_is_a_noop():
    p = _project()
    item = {"repo_url": REPO, "first_seen": "2026-06-20T09:12:00+00:00",
            "last_seen": "2026-09-13T19:40:00+00:00", "mentions": 412}
    _batch([item])
    before = _row(p["id"])
    _batch([item])
    assert _row(p["id"]) == before


def test_earlier_first_seen_moves_it_earlier():
    p = _project()
    _batch([{"repo_url": REPO, "first_seen": "2026-06-20T00:00:00+00:00"}])
    _batch([{"repo_url": REPO, "first_seen": "2026-02-01T00:00:00+00:00"}])
    row = _row(p["id"])
    assert row["first_seen"] == "2026-02-01T00:00:00+00:00"
    assert row["started_at"] == "2026-02-01T00:00:00+00:00"


def test_later_first_seen_does_not_move_it():
    p = _project()
    _batch([{"repo_url": REPO, "first_seen": "2026-02-01T00:00:00+00:00"}])
    _batch([{"repo_url": REPO, "first_seen": "2026-08-01T00:00:00+00:00"}])
    assert _row(p["id"])["first_seen"] == "2026-02-01T00:00:00+00:00"


def test_last_seen_moves_later_only():
    p = _project()
    _batch([{"repo_url": REPO, "last_seen": "2026-09-01T00:00:00+00:00"}])
    _batch([{"repo_url": REPO, "last_seen": "2026-07-01T00:00:00+00:00"}])
    assert _row(p["id"])["last_seen"] == "2026-09-01T00:00:00+00:00"
    _batch([{"repo_url": REPO, "last_seen": "2026-09-30T00:00:00+00:00"}])
    assert _row(p["id"])["last_seen"] == "2026-09-30T00:00:00+00:00"


def test_partial_item_leaves_the_other_date_alone():
    p = _project()
    _batch([{"repo_url": REPO, "first_seen": "2026-03-01T00:00:00+00:00",
             "last_seen": "2026-09-01T00:00:00+00:00"}])
    _batch([{"repo_url": REPO, "first_seen": "2026-02-01T00:00:00+00:00"}])
    row = _row(p["id"])
    assert row["first_seen"] == "2026-02-01T00:00:00+00:00"
    assert row["last_seen"] == "2026-09-01T00:00:00+00:00"


def test_url_variants_still_match_the_project():
    p = _project(repo_url="https://github.com/todd427/aigne")
    resp = _batch([{"repo_url": "https://GitHub.com/todd427/aigne.git/",
                    "first_seen": "2026-05-05T00:00:00+00:00"}])
    assert resp.json()["matched"] == 1
    assert _row(p["id"])["first_seen"] == "2026-05-05T00:00:00+00:00"


# ── accept, don't reject ─────────────────────────────────────────────────────

def test_unknown_repo_is_counted_not_rejected():
    _project()
    resp = _batch([
        {"repo_url": REPO, "first_seen": "2026-06-01T00:00:00+00:00"},
        {"repo_url": "https://github.com/todd427/ghost", "first_seen": "2026-06-01T00:00:00+00:00"},
    ])
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"matched": 1, "unmatched": 1,
                    "unmatched_urls": ["https://github.com/todd427/ghost"]}


def test_empty_batch_is_accepted():
    resp = _batch([])
    assert resp.status_code == 200
    assert resp.json()["matched"] == 0


def test_run_is_audited_with_payload():
    _project()
    _batch([{"repo_url": REPO, "first_seen": "2026-06-01T00:00:00+00:00", "mentions": 412}])
    with db() as conn:
        run = dict(conn.execute("SELECT * FROM lifecycle_runs").fetchone())
    assert run["source"] == "rian"
    assert run["run_at"] == "2026-09-17T14:00:00+00:00"
    assert run["matched"] == 1 and run["unmatched"] == 0
    assert "412" in run["payload"]        # raw JSON as received


# ── GitHub fallback (§3.3) ───────────────────────────────────────────────────

def _fill(dates: dict):
    async def fetcher(repo):
        return dates.get(repo)
    return asyncio.run(lc.fill_from_github(fetcher=fetcher))


def test_github_fills_only_null_started_at():
    rian = _project("Aigne", REPO)
    bare = _project("Rialu", "https://github.com/todd427/rialu")
    _batch([{"repo_url": REPO, "first_seen": "2026-06-20T00:00:00+00:00"}])

    result = _fill({"todd427/aigne": "2026-01-01T00:00:00Z",
                    "todd427/rialu": "2026-03-19T00:00:00Z"})
    assert result["filled"] == 1
    # the Rian date is untouched — first mention beats `gh repo create` (§4)
    assert _row(rian["id"])["started_at"] == "2026-06-20T00:00:00+00:00"
    assert _row(rian["id"])["started_at_source"] == "rian"
    assert _row(bare["id"])["started_at"] == "2026-03-19T00:00:00Z"
    assert _row(bare["id"])["started_at_source"] == "github"


def test_github_fill_is_idempotent():
    p = _project("Rialu", "https://github.com/todd427/rialu")
    dates = {"todd427/rialu": "2026-03-19T00:00:00Z"}
    assert _fill(dates)["filled"] == 1
    assert _fill(dates)["filled"] == 0
    assert _row(p["id"])["started_at"] == "2026-03-19T00:00:00Z"


def test_rian_batch_overwrites_a_github_date():
    p = _project()
    _fill({"todd427/aigne": "2026-05-01T00:00:00Z"})
    assert _row(p["id"])["started_at_source"] == "github"
    _batch([{"repo_url": REPO, "first_seen": "2026-02-02T00:00:00+00:00"}])
    row = _row(p["id"])
    assert row["started_at"] == "2026-02-02T00:00:00+00:00"
    assert row["started_at_source"] == "rian"


def test_github_skips_non_github_and_missing_dates():
    _project("Other", "https://gitlab.com/todd427/other")
    _project("NoDate", "https://github.com/todd427/nodate")
    result = _fill({})
    assert result["filled"] == 0 and result["skipped"] == 2


def test_github_endpoint_runs(monkeypatch):
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    resp = client.post("/api/lifecycle/github")
    assert resp.status_code == 200
    assert resp.json()["filled"] == 0


# ── manual override (§4) ─────────────────────────────────────────────────────

def test_manual_started_at_survives_both_syncs():
    p = _project()
    client.put(f"/api/projects/{p['id']}", json={"started_at": "2025-11-11T00:00:00+00:00"})
    assert _row(p["id"])["started_at_source"] == "manual"

    _batch([{"repo_url": REPO, "first_seen": "2026-06-20T00:00:00+00:00"}])
    row = _row(p["id"])
    assert row["started_at"] == "2025-11-11T00:00:00+00:00"
    assert row["started_at_source"] == "manual"
    assert row["first_seen"] == "2026-06-20T00:00:00+00:00"   # raw date still recorded

    _fill({"todd427/aigne": "2026-01-01T00:00:00Z"})
    assert _row(p["id"])["started_at"] == "2025-11-11T00:00:00+00:00"


# ── census (§3.4) ────────────────────────────────────────────────────────────

def test_census_answers_the_question_in_one_call():
    a = _project("A", "https://github.com/todd427/a", status="deployed")
    b = _project("B", "https://github.com/todd427/b", status="shipped")
    c = _project("C", "https://github.com/todd427/c", status="development")
    _project("D", "https://github.com/todd427/d")            # no started_at at all
    _batch([
        {"repo_url": "https://github.com/todd427/a", "first_seen": "2026-02-01T00:00:00+00:00"},
        {"repo_url": "https://github.com/todd427/b", "first_seen": "2026-03-01T00:00:00+00:00"},
        {"repo_url": "https://github.com/todd427/c", "first_seen": "2025-12-01T00:00:00+00:00"},
    ])
    data = client.get("/api/projects/census?year=2026").json()
    assert data["started"] == 2                              # c started in 2025
    assert data["by_status"] == {"deployed": 1, "shipped": 1}
    assert data["in_production"] == 2
    assert data["no_start_date"] == 1                        # d, reported not folded in
    assert data["total_projects"] == 4
    assert data["by_source"] == {"rian": 2}

    all_years = client.get("/api/projects/census").json()
    assert all_years["started"] == 3 and all_years["year"] is None


def test_census_route_not_shadowed_by_project_id():
    """/census must be declared before /{project_id} or it 422s as a bad int."""
    assert client.get("/api/projects/census").status_code == 200


def test_census_counts_all_production_statuses():
    for i, st in enumerate(("deployed", "live", "running", "shipped", "research")):
        _project(f"P{i}", f"https://github.com/todd427/p{i}", status=st)
        _batch([{"repo_url": f"https://github.com/todd427/p{i}",
                 "first_seen": "2026-04-01T00:00:00+00:00"}])
    data = client.get("/api/projects/census?year=2026").json()
    assert data["started"] == 5 and data["in_production"] == 4


# ── MCP surfaces ─────────────────────────────────────────────────────────────

def test_mcp_census_matches_endpoint():
    from mcp_server import project_census
    _project("A", "https://github.com/todd427/a", status="deployed")
    _batch([{"repo_url": "https://github.com/todd427/a", "first_seen": "2026-02-01T00:00:00+00:00"}])
    assert project_census(2026) == client.get("/api/projects/census?year=2026").json()


def test_list_projects_projection_gains_two_fields_only():
    from mcp_server import list_projects
    p = _project()
    _batch([{"repo_url": REPO, "first_seen": "2026-06-20T00:00:00+00:00",
             "last_seen": "2026-09-13T00:00:00+00:00"}])
    row = list_projects()[0]
    assert row["started_at"] == "2026-06-20T00:00:00+00:00"
    assert row["started_at_source"] == "rian"
    # first_seen/last_seen stay out of the lean shape; full record has them
    assert "first_seen" not in row and "last_seen" not in row
    from mcp_server import get_project
    assert get_project(p["id"])["last_seen"] == "2026-09-13T00:00:00+00:00"


def test_list_projects_still_returns_every_row():
    for i in range(99):
        _project(f"P{i}", f"https://github.com/todd427/p{i}")
    from mcp_server import list_projects
    assert len(list_projects()) == 99

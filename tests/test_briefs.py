"""
tests/test_briefs.py — Fleet-wide outstanding-briefs scan (docs/cc-brief-outstanding-briefs.md §6).

The fixture repo carries the seven files the brief names: one per status,
one with no status line, one with the legacy "Ready for implementation"
phrasing. A fake fetcher stands in for GitHub so the real parse → store →
endpoint → MCP path runs end to end.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db, db
from routers import briefs as br

client = TestClient(app)
NOW = datetime.now(timezone.utc)   # fixture ages are relative to the real clock, like the store


@pytest.fixture(autouse=True)
def setup():
    init_db()


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _doc(title, status_line, authored="**Authored:** 17 Sep 2026"):
    return f"# {title}\n\n**For:** a CC session.\n{status_line}\n{authored}\n\n---\n\n## 1. Problem\n\nbody\n"


# path → (text, days_ago)
FIXTURE = {
    "docs/cc-brief-alpha.md":        (_doc("Alpha", "**Status:** not-started"), 30),
    "docs/cc-brief-beta.md":         (_doc("Beta", "**Status:** ready"), 20),
    "docs/gamma-prd.md":             (_doc("Gamma", "**Status:** in-progress"), 10),
    "docs/delta-handoff.md":         (_doc("Delta", "**Status:** parked"), 5),
    "briefs/epsilon.md":             (_doc("Epsilon", "**Status:** done"), 2),
    "briefs/zeta.md":                (_doc("Zeta <span class='x'>tag</span>", "**Type:** brief", authored=""), 40),
    "docs/eta-prd.md":               (_doc("Eta", "**Status:** Ready for implementation"), 1),
    # not briefs — must be ignored by the path filter
    "docs/architecture.md":          (_doc("Arch", "**Status:** not-started"), 99),
    "docs/sub/cc-brief-nested.md":   (_doc("Nested", "**Status:** not-started"), 99),
    "README.md":                     (_doc("Readme", "**Status:** not-started"), 99),
}


def _files(fixture=FIXTURE):
    return [
        {"path": p, "text": t, "last_commit": _iso(d)}
        for p, (t, d) in fixture.items() if br.is_brief_path(p)
    ]


def _seed_project(name="rialu", repo_url="https://github.com/todd427/rialu"):
    client.post("/api/projects", json={"name": name, "status": "development", "repo_url": repo_url})


def _scan(files_by_repo: dict):
    async def fetcher(repo):
        return files_by_repo.get(repo)
    return asyncio.run(br.run_briefs_scan(fetcher=fetcher))


# ── parsing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("not-started", "not-started"), ("Ready", "ready"), ("in-progress", "in-progress"),
    ("parked", "parked"), ("Done", "done"), ("  READY  ", "ready"),
    ("Not started", "not-started"), ("In progress", "in-progress"),
    ("Ready for implementation", "ready"), ("build-ready", "ready"),
    ("Implemented", "done"), ("Shipped", "done"),
    ("Not started. Rian side is a small follow-on (§7), not yet written.", "not-started"),
    ("in-progress (rialu session, 18 Sep)", "in-progress"),
    ("draft", "unknown"), ("", "unknown"), (None, "unknown"), ("ready-ish", "unknown"),
])
def test_normalise_status(raw, want):
    assert br.normalise_status(raw) == want


def test_parse_brief_title_strips_span_tags_and_reads_authored():
    meta = br.parse_brief(FIXTURE["briefs/zeta.md"][0])
    assert meta["title"] == "Zeta tag"
    assert meta["status"] == "unknown"
    assert meta["authored"] is None
    meta = br.parse_brief(FIXTURE["docs/cc-brief-alpha.md"][0])
    assert meta == {"title": "Alpha", "status": "not-started", "authored": "17 Sep 2026"}


def test_parse_brief_status_only_counts_in_first_30_lines():
    text = "# Late\n" + "\n" * 35 + "**Status:** ready\n"
    assert br.parse_brief(text)["status"] == "unknown"


def test_parse_brief_tolerates_bold_variants():
    assert br.parse_brief("# T\n**Status**: done\n")["status"] == "done"
    assert br.parse_brief("# T\n**Authored** 1 Jan 2026\n")["authored"] == "1 Jan 2026"


@pytest.mark.parametrize("path,want", [
    ("docs/cc-brief-x.md", True), ("docs/foo-prd.md", True), ("docs/PRD-foo.md", True),
    ("docs/x-handoff.md", True), ("briefs/anything.md", True),
    ("docs/architecture.md", False), ("docs/sub/cc-brief-x.md", False),
    ("briefs/sub/x.md", False), ("cc-brief-x.md", False), ("docs/cc-brief-x.txt", False),
])
def test_is_brief_path(path, want):
    assert br.is_brief_path(path) is want


def test_repo_full_name():
    assert br.repo_full_name("https://github.com/todd427/rialu") == "todd427/rialu"
    assert br.repo_full_name("https://github.com/todd427/rialu.git/") == "todd427/rialu"
    assert br.repo_full_name("https://gitlab.com/todd427/rialu") is None
    assert br.repo_full_name("") is None
    assert br.repo_full_name(None) is None


# ── scan → store → read ──────────────────────────────────────────────────────

def test_scan_by_status_counts_exactly():
    _seed_project()
    result = _scan({"todd427/rialu": _files()})
    assert result == {"repos": 1, "briefs": 7, "failed": []}

    data = client.get("/api/briefs?status=all&limit=50").json()
    assert data["by_status"] == {
        "not-started": 1, "ready": 2, "in-progress": 1, "parked": 1, "done": 1, "unknown": 1,
    }
    assert data["count_open"] == 5
    by_path = {r["path"]: r for r in data["shown"]}
    assert by_path["briefs/zeta.md"]["status"] == "unknown"        # no status line
    assert by_path["docs/eta-prd.md"]["status"] == "ready"         # legacy phrasing
    assert by_path["docs/cc-brief-alpha.md"]["age_days"] == 30
    assert by_path["docs/cc-brief-alpha.md"]["authored"] == "17 Sep 2026"
    assert "docs/architecture.md" not in by_path


def test_open_is_default_and_sorted_oldest_first():
    _seed_project()
    _scan({"todd427/rialu": _files()})
    data = client.get("/api/briefs").json()
    assert data["count_open"] == 5
    assert [r["path"] for r in data["shown"]] == [
        "briefs/zeta.md",              # 40d, unknown
        "docs/cc-brief-alpha.md",      # 30d
        "docs/cc-brief-beta.md",       # 20d
        "docs/gamma-prd.md",           # 10d
        "docs/eta-prd.md",             # 1d
    ]
    statuses = {r["status"] for r in data["shown"]}
    assert "parked" not in statuses and "done" not in statuses


def test_limit_respected_and_capped():
    _seed_project()
    _scan({"todd427/rialu": _files()})
    data = client.get("/api/briefs?limit=2").json()
    assert len(data["shown"]) == 2
    assert data["count_open"] == 5           # whole-set counts survive the cap
    assert client.get("/api/briefs?limit=51").status_code == 422
    assert client.get("/api/briefs?limit=0").status_code == 422
    assert client.get("/api/briefs?status=bogus").status_code == 422


def test_single_status_filter():
    _seed_project()
    _scan({"todd427/rialu": _files()})
    data = client.get("/api/briefs?status=ready").json()
    assert sorted(r["path"] for r in data["shown"]) == ["docs/cc-brief-beta.md", "docs/eta-prd.md"]


def test_deleted_brief_vanishes_on_rescan():
    _seed_project()
    _scan({"todd427/rialu": _files()})
    assert client.get("/api/briefs?status=all&limit=50").json()["count"] == 7

    smaller = {k: v for k, v in FIXTURE.items() if k != "docs/cc-brief-alpha.md"}
    _scan({"todd427/rialu": _files(smaller)})
    paths = [r["path"] for r in client.get("/api/briefs?status=all&limit=50").json()["shown"]]
    assert "docs/cc-brief-alpha.md" not in paths and len(paths) == 6


def test_failed_repo_keeps_previous_rows():
    _seed_project()
    _scan({"todd427/rialu": _files()})
    result = _scan({})                        # fetcher returns None → unreadable
    assert result["failed"] == ["todd427/rialu"]
    assert client.get("/api/briefs?status=all&limit=50").json()["count"] == 7


def test_empty_repo_clears_rows():
    _seed_project()
    _scan({"todd427/rialu": _files()})
    _scan({"todd427/rialu": []})              # readable, no briefs
    assert client.get("/api/briefs?status=all").json()["count"] == 0


def test_two_projects_same_repo_scanned_once_and_non_github_skipped():
    _seed_project("a", "https://github.com/todd427/rialu")
    _seed_project("b", "https://github.com/todd427/rialu.git")
    _seed_project("c", "https://gitlab.com/todd427/other")
    calls = []
    async def fetcher(repo):
        calls.append(repo)
        return _files()
    asyncio.run(br.run_briefs_scan(fetcher=fetcher))
    assert calls == ["todd427/rialu"]


def test_scan_skips_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    _seed_project()
    result = asyncio.run(br.run_briefs_scan())
    assert result["skipped"] == "GITHUB_PAT not set"


def test_scan_endpoint_runs(monkeypatch):
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    resp = client.post("/api/briefs/scan")
    assert resp.status_code == 200
    assert resp.json()["repos"] == 0


# ── MCP tool ─────────────────────────────────────────────────────────────────

def test_mcp_briefs_tool_matches_endpoint():
    from mcp_server import briefs
    _seed_project()
    _scan({"todd427/rialu": _files()})
    via_mcp = briefs(status="open", limit=3)
    via_http = client.get("/api/briefs?status=open&limit=3").json()
    assert via_mcp == via_http
    assert len(via_mcp["shown"]) == 3
    assert set(via_mcp["shown"][0]) == {"repo", "path", "title", "status", "authored", "age_days", "last_commit"}
    assert briefs(status="bogus")["error"]
    assert len(briefs(limit=500)["shown"]) <= br.MAX_LIMIT

"""
routers/lifecycle.py — Receiver for Rian project-lifecycle batches.

`projects.created_at` is when a project was *registered*, and the register only
began in March 2026, so it cannot answer "how many projects did we start this
year" — every backfilled row says 2026. Rian derives a real lifecycle from the
chat exports (first mention, last mention, salience) and git-joins each project
to its repo; GitHub's repo `created_at` covers whatever never appeared in chat.

Decided 2026-09-17 (PRD §1): **no MCP on Rian.** Two servers answering "what is
the state of project X" from different data with different freshness is the
divergence problem restated. Rian writes in, Rialú serves the answer.

  - POST /api/lifecycle         a Rian batch, upserted monotonically (§2)
  - POST /api/lifecycle/github  fill started_at from GitHub where still NULL (§3.3)

Two invariants from the wire contract:
  - **Monotone by construction.** Keep the earliest first_seen and the latest
    last_seen ever received, so a partial or repeated batch can never move a
    date the wrong way and a re-send is a no-op.
  - **Accept, don't reject.** An item whose repo_url matches no project is
    counted and returned as `unmatched`, never 4xx'd — those URLs are the
    ghost-repo signal Rian logs.
"""

import json
import logging
import os
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import verify_faire_token
from db import db, row_to_dict

router = APIRouter(prefix="/api/lifecycle", tags=["lifecycle"])
log = logging.getLogger("rialu.lifecycle")

GITHUB_API = "https://api.github.com"

# started_at_source values. 'manual' is set by a human through update_project and
# is never overwritten by either sync (§4).
SOURCE_RIAN = "rian"
SOURCE_GITHUB = "github"
SOURCE_MANUAL = "manual"


class LifecycleItem(BaseModel):
    repo_url: str
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    mentions: Optional[int] = None      # informational; stored in the payload only


class LifecycleBatch(BaseModel):
    source: str = SOURCE_RIAN
    run_at: Optional[str] = None
    items: list[LifecycleItem] = []


def normalise_repo_url(url: Optional[str]) -> Optional[str]:
    """
    The join key, applied to both sides: lowercase host, no trailing slash, no
    `.git`. Rian's git-join and Rialú's register disagree on all three.
    """
    if not url or not url.strip():
        return None
    parts = urlsplit(url.strip())
    if not parts.netloc:                       # bare 'owner/repo' or similar
        return url.strip().rstrip("/").removesuffix(".git").lower() or None
    path = parts.path.rstrip("/").removesuffix(".git")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", "")) or None


def _earliest(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """Monotone floor. ISO-8601 UTC strings compare correctly as text."""
    return min([x for x in (a, b) if x], default=None)


def _latest(a: Optional[str], b: Optional[str]) -> Optional[str]:
    return max([x for x in (a, b) if x], default=None)


def derive_started_at(row: dict) -> tuple[Optional[str], Optional[str]]:
    """
    §4, decided and not for revisiting: first_seen when Rian has one, else the
    GitHub repo date, else NULL — a project starts when it is first thought
    about, not when `gh repo create` runs. A manual override wins over both.
    """
    if row.get("started_at_source") == SOURCE_MANUAL and row.get("started_at"):
        return row["started_at"], SOURCE_MANUAL
    if row.get("first_seen"):
        return row["first_seen"], SOURCE_RIAN
    if row.get("started_at") and row.get("started_at_source") == SOURCE_GITHUB:
        return row["started_at"], SOURCE_GITHUB
    return None, None


def _apply_item(conn, row: dict, item: LifecycleItem) -> None:
    """Monotone upsert of one matched project, then recompute started_at."""
    first = _earliest(row.get("first_seen"), item.first_seen)
    last = _latest(row.get("last_seen"), item.last_seen)
    merged = {**row, "first_seen": first, "last_seen": last}
    started, source = derive_started_at(merged)
    conn.execute(
        """UPDATE projects
              SET first_seen = ?, last_seen = ?, started_at = ?, started_at_source = ?
            WHERE id = ?""",
        (first, last, started, source, row["id"]),
    )


@router.post("", dependencies=[Depends(verify_faire_token)])
def receive_lifecycle(batch: LifecycleBatch):
    """
    Upsert one Rian batch. Any 2xx is the ack; a re-send with the same items is
    a no-op because every write is monotone (§2). Unknown repo_urls come back in
    `unmatched_urls` rather than failing the batch.
    """
    with db() as conn:
        index: dict[str, dict] = {}
        for r in conn.execute(
            "SELECT * FROM projects WHERE repo_url IS NOT NULL AND repo_url != ''"
        ).fetchall():
            key = normalise_repo_url(r["repo_url"])
            # First registration of a URL wins, so a duplicate repo_url row can
            # never make the match order depend on SQLite's row order.
            if key and key not in index:
                index[key] = row_to_dict(r)

        matched, unmatched_urls, touched = 0, [], []
        for item in batch.items:
            row = index.get(normalise_repo_url(item.repo_url) or "")
            if row is None:
                unmatched_urls.append(item.repo_url)
                continue
            _apply_item(conn, row, item)
            matched += 1
            touched.append(row["id"])

        conn.execute(
            """INSERT INTO lifecycle_runs (source, run_at, matched, unmatched, payload)
               VALUES (?, ?, ?, ?, ?)""",
            (batch.source, batch.run_at or "", matched, len(unmatched_urls),
             json.dumps(batch.model_dump(), default=str)),
        )
        rows = {
            r["id"]: row_to_dict(r)
            for r in conn.execute(
                f"SELECT * FROM projects WHERE id IN ({','.join('?' * len(touched))})", touched
            ).fetchall()
        } if touched else {}

    from routers.projects import _broadcast_project
    for pid in touched:
        _broadcast_project(pid, rows.get(pid, {"id": pid}))

    if unmatched_urls:
        log.info("[lifecycle] %d unmatched repo_url(s): %s", len(unmatched_urls),
                 ", ".join(unmatched_urls[:5]))
    return {"matched": matched, "unmatched": len(unmatched_urls), "unmatched_urls": unmatched_urls}


# ── GitHub fallback (§3.3) ───────────────────────────────────────────────────

async def fill_from_github(fetcher=None) -> dict:
    """
    Fill started_at from the repo's GitHub creation date, **only where it is
    still NULL**. Idempotent, and it never overwrites a Rian or manual date —
    first mention beats `gh repo create` whenever both exist (§4).

    `fetcher` maps 'owner/name' → an ISO creation date (or None); tests pass a
    stub. The default reads github_repos, which the 6h repos poller caches, and
    falls back to the API for anything not yet cached.
    """
    from routers.briefs import repo_full_name

    with db() as conn:
        rows = [row_to_dict(r) for r in conn.execute(
            """SELECT id, repo_url FROM projects
                WHERE started_at IS NULL AND repo_url IS NOT NULL AND repo_url != ''"""
        ).fetchall()]
        cached = {
            r["full_name"]: r["created_at"]
            for r in conn.execute(
                "SELECT full_name, created_at FROM github_repos WHERE created_at IS NOT NULL"
            ).fetchall()
        }

    client = None
    if fetcher is None:
        token = os.environ.get("GITHUB_PAT", "")
        client = httpx.AsyncClient(
            timeout=15,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"},
        ) if token else None

        async def fetcher(repo: str):
            if repo in cached:
                return cached[repo]
            if client is None:
                return None
            resp = await client.get(f"{GITHUB_API}/repos/{repo}")
            return resp.json().get("created_at") if resp.status_code == 200 else None

    filled, skipped = 0, 0
    try:
        for row in rows:
            repo = repo_full_name(row["repo_url"])
            if not repo:
                skipped += 1
                continue
            try:
                created = await fetcher(repo)
            except Exception as exc:
                log.warning("[lifecycle] %s: %s", repo, exc)
                created = None
            if not created:
                skipped += 1
                continue
            with db() as conn:
                # The NULL guard is repeated in SQL, not just in the SELECT above:
                # a Rian batch landing mid-run must win, not be clobbered by it.
                cur = conn.execute(
                    """UPDATE projects SET started_at = ?, started_at_source = ?
                        WHERE id = ? AND started_at IS NULL""",
                    (created, SOURCE_GITHUB, row["id"]),
                )
                filled += cur.rowcount
    finally:
        if client is not None:
            await client.aclose()
    log.info("[lifecycle] github fill: %d filled, %d skipped", filled, skipped)
    return {"filled": filled, "skipped": skipped, "candidates": len(rows)}


@router.post("/github", dependencies=[Depends(verify_faire_token)])
async def lifecycle_github():
    """Fill started_at from GitHub for projects that still have none."""
    return await fill_from_github()


# ── Census (§3.4) ────────────────────────────────────────────────────────────

# What counts as "in production" for the census. Deliberately wider than
# `deployed` alone: the register has grown three other terminal-ish states and
# the question being asked ("how many are live?") means all four.
IN_PRODUCTION = ("deployed", "live", "running", "shipped")


def run_census(year: Optional[int] = None) -> dict:
    """
    Answer "how many projects did we start this year, and how many are
    deployed" in one call — the question that prompted the PRD.

    `started` counts projects whose derived started_at falls in `year`.
    `no_start_date` is reported alongside, never folded in: with the register
    backfilled in March 2026 a bare count would quietly present a floor as an
    answer. Omit `year` for the whole portfolio.
    """
    where, params = "started_at IS NOT NULL", []
    if year is not None:
        where += " AND substr(started_at, 1, 4) = ?"
        params = [str(year)]
    with db() as conn:
        rows = [row_to_dict(r) for r in conn.execute(
            f"SELECT status, started_at_source FROM projects WHERE {where}", params
        ).fetchall()]
        no_start = conn.execute(
            "SELECT COUNT(*) AS n FROM projects WHERE started_at IS NULL"
        ).fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]

    by_status: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_source[r["started_at_source"] or "unknown"] = by_source.get(r["started_at_source"] or "unknown", 0) + 1
    return {
        "year": year,
        "started": len(rows),
        "by_status": by_status,
        "in_production": sum(n for s, n in by_status.items() if s in IN_PRODUCTION),
        "by_source": by_source,
        "no_start_date": no_start,
        "total_projects": total,
    }

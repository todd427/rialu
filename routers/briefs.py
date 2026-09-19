"""
routers/briefs.py — Fleet-wide outstanding-briefs scan.

Briefs and PRDs get written into docs/ and briefs/ across the fleet and then
nothing reads them; the only record that one exists and is unbuilt is a
`**Status:**` line inside the file. Rialú is the one component with a handle
on every registered repo, so it scans; Timire shows the result in the morning
standup (timire/briefs/standup-outstanding-briefs.md). Spec:
docs/cc-brief-outstanding-briefs.md.

  - GET  /api/briefs?status=open&limit=5   the list, oldest first, capped
  - POST /api/briefs/scan                  rescan every registered repo now

The scan reaches repos through the GitHub REST API (trees → contents →
commits), the same way the LOC poller does — Rialú never clones. The brief
wrote it into the divergence pass, but that pass is deliberately local-only so
a scheduled run cannot be sunk by an outage upstream; the scan is its own 6h
poller instead. A transient failure on a repo leaves that repo's rows as they
were rather than wiping them — better a stale list than an empty one.

The status vocabulary is fixed here and used fleet-wide (§2). Anything that
does not match is `unknown`, never guessed: an unknown is a brief that needs
a status line, which is the point of reporting it.
"""

import fnmatch
import logging
import os
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter, Depends, Query

from auth import verify_faire_token
from db import db, row_to_dict

router = APIRouter(prefix="/api/briefs", tags=["briefs"])
log = logging.getLogger("rialu.briefs")

GITHUB_API = "https://api.github.com"

# ── Status vocabulary (§2) ───────────────────────────────────────────────────

STATUSES = ("not-started", "ready", "in-progress", "parked", "done")
OPEN_STATUSES = ("not-started", "ready", "in-progress", "unknown")

# Phrasings already in the fleet before the vocabulary was fixed. Keys are
# lower-cased, whitespace-collapsed. Anything not here and not in STATUSES
# reports as 'unknown'.
_LEGACY = {
    "not started": "not-started",
    "ready for implementation": "ready",
    "build-ready": "ready",
    "build ready": "ready",
    "implemented": "done",
    "shipped": "done",
}

# Which files count as briefs. Directory is exact (no recursion — `*` in
# fnmatch would otherwise match a `/`); the basename is matched case-blind.
BRIEF_GLOBS = ("docs/*prd*.md", "docs/cc-brief-*.md", "docs/*-handoff.md", "briefs/*.md")

HEAD_LINES = 30  # the status line must sit in the first 30 lines

_STATUS_RE = re.compile(r"^\s*\*\*status:?\*\*:?\s*(.+?)\s*$", re.IGNORECASE)
_AUTHORED_RE = re.compile(r"^\s*\*\*authored:?\*\*:?\s*(.+?)\s*$", re.IGNORECASE)
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_TAG_RE = re.compile(r"<[^>]+>")


def is_brief_path(path: str) -> bool:
    """True if a repo-relative path matches one of BRIEF_GLOBS."""
    directory, _, name = path.rpartition("/")
    for pattern in BRIEF_GLOBS:
        pdir, _, pname = pattern.rpartition("/")
        if directory == pdir and fnmatch.fnmatchcase(name.lower(), pname):
            return True
    return False


def normalise_status(raw: Optional[str]) -> str:
    """
    Map a raw `**Status:**` value onto the vocabulary, or 'unknown'.

    Only the head of the value counts — up to the first sentence-ending or
    clause punctuation — so `Not started. Rian side is a follow-on (§7)`
    reads as not-started. Case, surrounding whitespace, and the legacy
    phrasings are tolerated; nothing else is.
    """
    if not raw:
        return "unknown"
    head = re.split(r"[.;,(—–|]", raw, maxsplit=1)[0]
    head = " ".join(head.strip().strip("*_`").lower().split())
    if head in STATUSES:
        return head
    if head.replace(" ", "-") in STATUSES:
        return head.replace(" ", "-")
    return _LEGACY.get(head, "unknown")


def parse_brief(text: str) -> dict:
    """
    Pull title, status and authored out of a brief's text.

    Title is the first `# ` heading with any HTML (span tags) stripped. Status
    and authored come from the first HEAD_LINES lines only.
    """
    title = None
    status_raw = None
    authored = None
    for i, line in enumerate(text.splitlines()):
        if title is None:
            m = _TITLE_RE.match(line)
            if m:
                title = _TAG_RE.sub("", m.group(1)).strip() or None
        if i < HEAD_LINES:
            if status_raw is None:
                m = _STATUS_RE.match(line)
                if m:
                    status_raw = m.group(1)
            if authored is None:
                m = _AUTHORED_RE.match(line)
                if m:
                    authored = m.group(1).strip()
        if title is not None and status_raw is not None and authored is not None:
            break
    return {"title": title, "status": normalise_status(status_raw), "authored": authored}


# ── Storage ──────────────────────────────────────────────────────────────────

def _age_days(last_commit: Optional[str], now: datetime) -> int:
    if not last_commit:
        return 0
    try:
        dt = datetime.fromisoformat(last_commit.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (now - dt).days)


def store_repo_briefs(conn, repo: str, files: list[dict], now: Optional[datetime] = None) -> int:
    """
    Full replace of one repo's rows. `files` is a list of
    {path, text, last_commit} — everything the fetcher found — so a brief
    deleted upstream vanishes here. Returns the number of rows written.
    """
    now = now or datetime.now(timezone.utc)
    scanned_at = now.isoformat(timespec="seconds")
    conn.execute("DELETE FROM briefs WHERE repo = ?", (repo,))
    for f in files:
        meta = parse_brief(f.get("text") or "")
        conn.execute(
            """INSERT INTO briefs (repo, path, title, status, authored, age_days, last_commit, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (repo, f["path"], meta["title"], meta["status"], meta["authored"],
             _age_days(f.get("last_commit"), now), f.get("last_commit"), scanned_at),
        )
    return len(files)


# ── Fetching ─────────────────────────────────────────────────────────────────

# A fetcher takes "owner/name" and returns the brief files in that repo as
# [{path, text, last_commit}], or None if the repo could not be read (which
# leaves its existing rows alone). Tests substitute a fixture fetcher; the
# poller uses the GitHub one below.
Fetcher = Callable[[str], Awaitable[Optional[list[dict]]]]


def repo_full_name(repo_url: Optional[str]) -> Optional[str]:
    """'https://github.com/todd427/rialu.git/' → 'todd427/rialu'; None if not GitHub."""
    if not repo_url or "github.com" not in repo_url:
        return None
    parts = repo_url.rstrip("/").split("/")
    if len(parts) < 2 or not parts[-2] or not parts[-1]:
        return None
    return f"{parts[-2]}/{parts[-1].removesuffix('.git')}"


async def fetch_github_briefs(client: httpx.AsyncClient, repo: str) -> Optional[list[dict]]:
    """
    Resolve the default branch, list its tree, then per brief a raw read and
    the last commit touching it. The branch lookup is not optional: asking the
    trees API for `HEAD` is unreliable — on git-mcp it served a 7-file tree
    from an early commit while `master` had 28. A 404 on the repo or the tree
    is a missing or empty repo — zero briefs, which is a real answer. Anything
    else is a failure: return None and keep the old rows.
    """
    meta = await client.get(f"{GITHUB_API}/repos/{repo}")
    if meta.status_code == 404:
        return []
    if meta.status_code != 200:
        log.warning(f"[briefs] {repo}: repo {meta.status_code}")
        return None
    branch = meta.json().get("default_branch") or "main"
    tree = await client.get(f"{GITHUB_API}/repos/{repo}/git/trees/{branch}", params={"recursive": "1"})
    if tree.status_code == 404:
        return []
    if tree.status_code != 200:
        log.warning(f"[briefs] {repo}: tree {tree.status_code}")
        return None
    body = tree.json()
    if body.get("truncated"):
        log.warning(f"[briefs] {repo}: tree listing truncated — briefs may be missed")
    paths = [e["path"] for e in body.get("tree", []) if e.get("type") == "blob" and is_brief_path(e["path"])]

    files = []
    for path in paths:
        raw = await client.get(
            f"{GITHUB_API}/repos/{repo}/contents/{path}",
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if raw.status_code != 200:
            log.warning(f"[briefs] {repo}/{path}: contents {raw.status_code}")
            return None
        last_commit = None
        commits = await client.get(f"{GITHUB_API}/repos/{repo}/commits", params={"path": path, "per_page": 1})
        if commits.status_code == 200 and commits.json():
            last_commit = (commits.json()[0].get("commit", {}).get("committer") or {}).get("date")
        files.append({"path": path, "text": raw.text, "last_commit": last_commit})
    return files


async def run_briefs_scan(fetcher: Optional[Fetcher] = None) -> dict:
    """
    Scan every registered GitHub repo. Returns {repos, briefs, failed}.
    Without a fetcher, uses GitHub with GITHUB_PAT — and skips quietly if the
    token is not set, like every other poller.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT repo_url FROM projects WHERE repo_url IS NOT NULL AND repo_url != ''"
        ).fetchall()
    repos = sorted({r for r in (repo_full_name(row["repo_url"]) for row in rows) if r})

    client = None
    if fetcher is None:
        token = os.environ.get("GITHUB_PAT", "")
        if not token:
            log.debug("GITHUB_PAT not set — skipping briefs scan")
            return {"repos": 0, "briefs": 0, "failed": [], "skipped": "GITHUB_PAT not set"}
        client = httpx.AsyncClient(
            timeout=15,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"},
        )

        async def fetcher(repo: str):
            return await fetch_github_briefs(client, repo)

    scanned = written = 0
    failed = []
    try:
        for repo in repos:
            try:
                files = await fetcher(repo)
            except Exception as exc:
                log.warning(f"[briefs] {repo}: {exc}")
                files = None
            if files is None:
                failed.append(repo)
                continue
            with db() as conn:
                written += store_repo_briefs(conn, repo, files)
            scanned += 1
    finally:
        if client is not None:
            await client.aclose()
    log.info(f"[briefs] scanned {scanned} repos, {written} briefs, {len(failed)} failed")
    return {"repos": scanned, "briefs": written, "failed": failed}


# ── Reading ──────────────────────────────────────────────────────────────────

MAX_LIMIT = 50


def list_briefs(status: str = "open", limit: int = 5) -> dict:
    """
    The one projection, shared by the route, the MCP tool and the dashboard.

    Oldest first (age_days desc, then repo, then path) — age is the sort key
    and nothing is coloured or thresholded, so the reader is not trained to
    ignore red. `shown` is the first `limit` rows; `count_open` and `by_status`
    describe the whole open set so the caller can say "and N more".
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    if status == "open":
        wanted = OPEN_STATUSES
    elif status == "all":
        wanted = STATUSES + ("unknown",)
    else:
        wanted = (status,)
    marks = ",".join("?" * len(wanted))
    with db() as conn:
        rows = conn.execute(
            f"""SELECT repo, path, title, status, authored, age_days, last_commit
                FROM briefs WHERE status IN ({marks})
                ORDER BY age_days DESC, repo, path""",
            wanted,
        ).fetchall()
        counts = conn.execute(
            "SELECT status, COUNT(*) AS n FROM briefs GROUP BY status"
        ).fetchall()
    by_status = {r["status"]: r["n"] for r in counts}
    return {
        "count_open": sum(by_status.get(s, 0) for s in OPEN_STATUSES),
        "count": len(rows),
        "shown": [row_to_dict(r) for r in rows[:limit]],
        "by_status": by_status,
    }


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("")
def get_briefs(
    status: str = Query(default="open", pattern="^(open|all|not-started|ready|in-progress|parked|done|unknown)$"),
    limit: int = Query(default=5, ge=1, le=MAX_LIMIT),
):
    """Outstanding briefs, oldest first. The cap is enforced here so no caller can dump forty rows."""
    return list_briefs(status=status, limit=limit)


@router.post("/scan", dependencies=[Depends(verify_faire_token)])
async def scan_now():
    """Rescan every registered GitHub repo now (the 6h poller does the same)."""
    return await run_briefs_scan()

"""
routers/github.py — GitHub repo discovery, adoption, and creation.

Lists cached repos, identifies untracked ones, and allows adopting or
creating repos as Rialú projects.
"""

import os
import re
import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from db import db, row_to_dict

router = APIRouter(prefix="/api/github", tags=["github"])
log = logging.getLogger("rialu.github")

GITHUB_TOKEN = os.environ.get("GITHUB_PAT", "")
GITHUB_API = "https://api.github.com"
GITHUB_USER = os.environ.get("GITHUB_USER", "todd427")


def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-")[:64]


# ── list repos ───────────────────────────────────────────────────────────────

@router.get("/repos")
def list_repos():
    """All cached GitHub repos."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM github_repos ORDER BY pushed_at DESC"
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@router.get("/untracked")
def untracked_repos():
    """Repos not linked to any project (by repo_url match)."""
    with db() as conn:
        rows = conn.execute("""
            SELECT gr.* FROM github_repos gr
            WHERE gr.archived = 0
              AND NOT EXISTS (
                SELECT 1 FROM projects p
                WHERE p.repo_url = gr.html_url
                   OR p.repo_url = gr.html_url || '.git'
                   OR gr.html_url LIKE '%/' || p.slug
              )
            ORDER BY gr.pushed_at DESC
        """).fetchall()
    return [row_to_dict(r) for r in rows]


# ── adopt a repo as a project ───────────────────────────────────────────────

class AdoptIn(BaseModel):
    repo_full_name: str
    status: str = "development"
    machine: Optional[str] = None
    platform: Optional[str] = None
    notes: Optional[str] = None


@router.post("/adopt", status_code=201)
def adopt_repo(a: AdoptIn):
    """Create a Rialú project from an existing GitHub repo."""
    with db() as conn:
        repo = conn.execute(
            "SELECT * FROM github_repos WHERE full_name = ?", (a.repo_full_name,)
        ).fetchone()
        if not repo:
            raise HTTPException(404, "Repo not found in cache — wait for next poll or refresh")

        slug = _slugify(repo["name"])
        # Check slug uniqueness
        existing = conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
        if existing:
            slug = f"{slug}-gh"

        desc = repo["description"] or ""
        notes = a.notes or desc

        cur = conn.execute(
            """INSERT INTO projects (name, slug, status, repo_url, machine, platform, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (repo["name"], slug, a.status, repo["html_url"], a.machine, a.platform, notes),
        )
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


# ── create a new repo + project ──────────────────────────────────────────────

class CreateRepoIn(BaseModel):
    name: str
    description: Optional[str] = None
    private: bool = True
    status: str = "development"
    machine: Optional[str] = None
    platform: Optional[str] = None


@router.post("/create", status_code=201)
async def create_repo(c: CreateRepoIn):
    """Create a new GitHub repo and a matching Rialú project."""
    if not GITHUB_TOKEN:
        raise HTTPException(503, "GITHUB_PAT not configured")

    # Create repo on GitHub
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{GITHUB_API}/user/repos",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={
                "name": c.name,
                "description": c.description or "",
                "private": c.private,
                "auto_init": True,
            },
        )
        if resp.status_code == 422:
            raise HTTPException(409, "Repo already exists on GitHub")
        if resp.status_code not in (200, 201):
            raise HTTPException(502, f"GitHub API error: {resp.status_code}")
        gh_repo = resp.json()

    # Create project in Rialú
    slug = _slugify(c.name)
    with db() as conn:
        existing = conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
        if existing:
            slug = f"{slug}-new"

        cur = conn.execute(
            """INSERT INTO projects (name, slug, status, repo_url, machine, platform, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (c.name, slug, c.status, gh_repo["html_url"], c.machine, c.platform, c.description),
        )

        # Also cache the repo
        conn.execute("""
            INSERT OR REPLACE INTO github_repos
                (id, full_name, name, description, html_url, language,
                 private, fork, archived, stars, pushed_at, created_at, checked_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?, 0, 0, 0, ?, ?, datetime('now'))
        """, (
            gh_repo["id"], gh_repo["full_name"], gh_repo["name"],
            gh_repo.get("description"), gh_repo["html_url"],
            int(gh_repo.get("private", True)),
            (gh_repo.get("pushed_at") or "")[:19].replace("T", " ") or None,
            (gh_repo.get("created_at") or "")[:19].replace("T", " ") or None,
        ))

        row = conn.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone()

    return row_to_dict(row)


# ── manual refresh ───────────────────────────────────────────────────────────

@router.post("/refresh")
async def refresh_repos():
    """Manually trigger a GitHub repo cache refresh."""
    from poller import poll_github_repos
    await poll_github_repos()
    return {"status": "ok"}

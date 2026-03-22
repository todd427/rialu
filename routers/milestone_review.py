"""
routers/milestone_review.py — Automated milestone verification.

Checks open milestones against GitHub repos: commit messages, file trees,
and deploy status. Auto-marks milestones as done when evidence is strong.
Logs every decision for transparency.
"""

import os
import re
import logging

import httpx
from fastapi import APIRouter

from db import db, row_to_dict

router = APIRouter(prefix="/api/milestones", tags=["milestones"])
log = logging.getLogger("rialu.milestone_review")

GITHUB_TOKEN = os.environ.get("GITHUB_PAT", "")
GITHUB_API = "https://api.github.com"


def _extract_keywords(title: str) -> list[str]:
    """Extract searchable keywords from a milestone title."""
    # Remove phase prefix like "Phase 3: "
    title = re.sub(r"^Phase \d+[:\s—–-]+", "", title, flags=re.IGNORECASE)
    # Remove parenthetical hints
    inner = re.findall(r"\(([^)]+)\)", title)
    title_clean = re.sub(r"\([^)]*\)", "", title)

    # Split on common delimiters
    words = re.split(r"[\s,/+&—–-]+", title_clean.strip())
    # Filter out stopwords and short words
    stopwords = {
        "the", "a", "an", "and", "or", "for", "to", "in", "on", "of", "is",
        "with", "from", "via", "per", "all", "new", "add", "tab", "view",
        "layer", "auto", "upgrade", "integration",
    }
    keywords = [w.lower().strip("()[]") for w in words if len(w) > 2 and w.lower() not in stopwords]

    # Add inner parenthetical terms as keywords too
    for phrase in inner:
        keywords.extend(w.lower() for w in re.split(r"[\s,/+]+", phrase) if len(w) > 2)

    # Deduplicate while preserving order
    seen = set()
    result = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result


def _keywords_to_search_terms(keywords: list[str]) -> list[str]:
    """Build search terms from keywords — pairs and singles."""
    terms = []
    # Try pairs first (more specific)
    if len(keywords) >= 2:
        terms.append(" ".join(keywords[:3]))
    # Individual meaningful keywords
    for k in keywords:
        if len(k) > 3:
            terms.append(k)
    return terms[:5]  # Limit API calls


async def _check_milestone(
    client: httpx.AsyncClient,
    milestone: dict,
    project: dict,
    deploy_status: str | None,
) -> dict:
    """
    Check a single milestone against its project's GitHub repo.
    Returns {action: "completed"|"unchanged", evidence: str}
    """
    title = milestone["title"]
    keywords = _extract_keywords(title)
    if not keywords:
        return {"action": "unchanged", "evidence": "Could not extract keywords"}

    repo_url = project.get("repo_url") or ""
    if "github.com" not in repo_url:
        return {"action": "unchanged", "evidence": "No GitHub repo URL"}

    parts = repo_url.rstrip("/").split("/")
    owner, repo = parts[-2], parts[-1].replace(".git", "")
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    evidence_pieces = []
    confidence = 0

    search_terms = _keywords_to_search_terms(keywords)

    # 1. Search commit messages (last 90 days)
    for term in search_terms[:3]:
        try:
            r = await client.get(
                f"{GITHUB_API}/search/commits",
                params={"q": f"{term} repo:{owner}/{repo}", "per_page": 5},
                headers={**headers, "Accept": "application/vnd.github.cloak-preview+json"},
            )
            if r.status_code == 200:
                items = r.json().get("items", [])
                if items:
                    msgs = [i["commit"]["message"].split("\n")[0][:80] for i in items[:3]]
                    evidence_pieces.append(f"Commits matching '{term}': {msgs}")
                    confidence += min(len(items), 3) * 15
        except Exception:
            pass

    # 2. Search code in repo
    for term in search_terms[:2]:
        try:
            r = await client.get(
                f"{GITHUB_API}/search/code",
                params={"q": f"{term} repo:{owner}/{repo}", "per_page": 5},
                headers=headers,
            )
            if r.status_code == 200:
                items = r.json().get("items", [])
                if items:
                    files = [i["path"] for i in items[:5]]
                    evidence_pieces.append(f"Code matching '{term}': {files}")
                    confidence += min(len(items), 3) * 10
        except Exception:
            pass

    # 3. Check deploy status for deploy-related milestones
    deploy_words = {"deploy", "deployed", "launch", "live", "ship", "shipped", "release"}
    if any(w in title.lower() for w in deploy_words):
        if deploy_status == "healthy":
            evidence_pieces.append(f"Deploy status: healthy")
            confidence += 30
        elif deploy_status in ("error", "stopped"):
            confidence -= 20

    # 4. Boost for very specific file matches
    specific_patterns = {
        "oauth": ["oauth_provider.py", "oauth"],
        "csv": ["csv", "import"],
        "kanban": ["kanban"],
        "timeline": ["timeline"],
        "export": ["export"],
        "sentinel": ["sentinel"],
        "mnemos": ["mnemos"],
        "key vault": ["key_vault", "shamir"],
        "fts5": ["fts5", "fts"],
        "websocket": ["ws_hub", "websocket"],
        "terminal": ["terminal", "xterm"],
    }
    title_lower = title.lower()
    for pattern, file_terms in specific_patterns.items():
        if pattern in title_lower:
            for ft in file_terms:
                try:
                    r = await client.get(
                        f"{GITHUB_API}/search/code",
                        params={"q": f"filename:{ft} repo:{owner}/{repo}", "per_page": 3},
                        headers=headers,
                    )
                    if r.status_code == 200 and r.json().get("total_count", 0) > 0:
                        files = [i["path"] for i in r.json()["items"][:3]]
                        evidence_pieces.append(f"File match '{ft}': {files}")
                        confidence += 20
                        break
                except Exception:
                    pass

    # Decision
    if confidence >= 50:
        evidence_str = " | ".join(evidence_pieces[:4])
        return {"action": "completed", "evidence": f"confidence={confidence}. {evidence_str}"}
    elif evidence_pieces:
        evidence_str = " | ".join(evidence_pieces[:3])
        return {"action": "unchanged", "evidence": f"confidence={confidence} (below threshold). {evidence_str}"}
    else:
        return {"action": "unchanged", "evidence": f"No evidence found for keywords: {keywords}"}


@router.post("/review")
async def review_milestones():
    """
    Review all open milestones across all projects.
    Auto-marks milestones as done when GitHub evidence is strong (confidence >= 50).
    Returns a summary of all decisions.
    """
    if not GITHUB_TOKEN:
        return {"error": "GITHUB_PAT not set", "results": []}

    # Gather open milestones with their projects
    with db() as conn:
        milestones = conn.execute("""
            SELECT m.id, m.title, m.project_id, p.name as project_name, p.repo_url, p.slug
            FROM milestones m
            JOIN projects p ON p.id = m.project_id
            WHERE m.done = 0
            ORDER BY p.name, m.sort_order
        """).fetchall()

        # Get deploy status per project
        deploys = {}
        for row in conn.execute("SELECT service_name, status FROM deployments_cache").fetchall():
            deploys[row["service_name"].lower()] = row["status"]

    milestones = [row_to_dict(m) for m in milestones]
    if not milestones:
        return {"message": "No open milestones to review", "results": []}

    results = []
    completed = 0

    async with httpx.AsyncClient(timeout=10) as client:
        for ms in milestones:
            project = {
                "name": ms["project_name"],
                "repo_url": ms.get("repo_url", ""),
                "slug": ms.get("slug", ""),
            }
            # Find deploy status for this project
            name_lower = ms["project_name"].lower()
            deploy_status = None
            for svc, status in deploys.items():
                if name_lower in svc or ms.get("slug", "") in svc:
                    deploy_status = status
                    break

            result = await _check_milestone(client, ms, project, deploy_status)
            result["milestone_id"] = ms["id"]
            result["project"] = ms["project_name"]
            result["title"] = ms["title"]

            if result["action"] == "completed":
                # Mark as done
                with db() as conn:
                    conn.execute("UPDATE milestones SET done = 1 WHERE id = ?", (ms["id"],))
                    conn.execute(
                        """INSERT INTO milestone_review_log
                           (milestone_id, project_name, milestone_title, action, evidence)
                           VALUES (?, ?, ?, ?, ?)""",
                        (ms["id"], ms["project_name"], ms["title"],
                         "auto-completed", result["evidence"]),
                    )
                completed += 1
            else:
                # Log the skip too
                with db() as conn:
                    conn.execute(
                        """INSERT INTO milestone_review_log
                           (milestone_id, project_name, milestone_title, action, evidence)
                           VALUES (?, ?, ?, ?, ?)""",
                        (ms["id"], ms["project_name"], ms["title"],
                         "unchanged", result["evidence"]),
                    )

            results.append(result)

    return {
        "reviewed": len(results),
        "completed": completed,
        "unchanged": len(results) - completed,
        "results": results,
    }


@router.get("/review/log")
def review_log(limit: int = 50):
    """Recent milestone review decisions."""
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM milestone_review_log
               ORDER BY reviewed_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]

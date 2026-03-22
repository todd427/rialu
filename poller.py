"""
poller.py — APScheduler background jobs for cloud platform polling.

Phase 1 pollers:
  - Fly.io GraphQL API     (every 60s)
  - Railway GraphQL API    (every 60s)

Phase 3 will add: Cloudflare Pages, GitHub, Anthropic usage.

All credentials come from environment variables. Pollers are no-ops
if credentials are absent — they log a warning and skip.
"""

import json
import logging
import os
from datetime import datetime, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db import db

log = logging.getLogger("rialu.poller")

scheduler = AsyncIOScheduler()

# ── helpers ─────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _upsert_service(
    conn,
    platform: str,
    service_name: str,
    status: str,
    url: str = None,
    last_deploy_at: str = None,
    commit_hash: str = None,
    commit_message: str = None,
    duration_s: int = None,
) -> None:
    conn.execute(
        """
        INSERT INTO deployments_cache
            (platform, service_name, status, url, last_deploy_at,
             last_commit_hash, last_commit_message, deploy_duration_s, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(service_name) DO UPDATE SET
            platform            = excluded.platform,
            status              = excluded.status,
            url                 = COALESCE(excluded.url, deployments_cache.url),
            last_deploy_at      = COALESCE(excluded.last_deploy_at, deployments_cache.last_deploy_at),
            last_commit_hash    = COALESCE(excluded.last_commit_hash, deployments_cache.last_commit_hash),
            last_commit_message = COALESCE(excluded.last_commit_message, deployments_cache.last_commit_message),
            deploy_duration_s   = COALESCE(excluded.deploy_duration_s, deployments_cache.deploy_duration_s),
            checked_at          = excluded.checked_at
        """,
        (platform, service_name, status, url, last_deploy_at,
         commit_hash, commit_message, duration_s, _now()),
    )


# ── Fly.io ───────────────────────────────────────────────────────────────────

FLY_TOKEN = os.environ.get("FLY_API_TOKEN", "")
FLY_GQL   = "https://api.fly.io/graphql"

FLY_QUERY = """
{
  apps(first: 50) {
    nodes {
      name
      status
      hostname
      currentRelease {
        createdAt
        status
        version
        description
      }
    }
  }
}
"""


async def poll_flyio() -> None:
    if not FLY_TOKEN:
        log.debug("FLY_API_TOKEN not set — skipping Fly.io poll")
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                FLY_GQL,
                headers={"Authorization": f"Bearer {FLY_TOKEN}"},
                json={"query": FLY_QUERY},
            )
            resp.raise_for_status()
            data = resp.json()
        apps = (data.get("data") or {}).get("apps", {}).get("nodes", [])
        with db() as conn:
            for app in apps:
                name   = app.get("name", "")
                status = app.get("status", "unknown").lower()
                url    = f"https://{app.get('hostname', '')}" if app.get("hostname") else None
                rel    = app.get("currentRelease") or {}
                deploy_at = rel.get("createdAt", "")[:19].replace("T", " ") if rel.get("createdAt") else None
                # Map Fly statuses to our vocabulary
                if status in ("running", "deployed"):
                    status = "healthy"
                elif status in ("suspended", "stopped"):
                    status = "stopped"
                elif status == "pending":
                    status = "deploying"
                _upsert_service(
                    conn, "fly.io", name, status,
                    url=url, last_deploy_at=deploy_at,
                    commit_message=rel.get("description"),
                )
        log.info(f"[fly.io] polled {len(apps)} apps")
    except Exception as exc:
        log.warning(f"[fly.io] poll failed: {exc}")


# ── Railway ──────────────────────────────────────────────────────────────────

RAILWAY_TOKEN = os.environ.get("RAILWAY_API_TOKEN", "")
RAILWAY_GQL   = "https://backboard.railway.app/graphql/v2"

RAILWAY_QUERY = """
{
  projects {
    edges {
      node {
        id
        name
        services {
          edges {
            node {
              id
              name
              deployments(first: 1) {
                edges {
                  node {
                    id
                    status
                    createdAt
                    url
                    meta
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


async def poll_railway() -> None:
    if not RAILWAY_TOKEN:
        log.debug("RAILWAY_API_TOKEN not set — skipping Railway poll")
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                RAILWAY_GQL,
                headers={"Authorization": f"Bearer {RAILWAY_TOKEN}"},
                json={"query": RAILWAY_QUERY},
            )
            resp.raise_for_status()
            data = resp.json()

        api_data = data.get("data") or {}
        projects_data = api_data.get("projects")
        if projects_data is None:
            errors = data.get("errors", [])
            msg = errors[0].get("message", "unknown") if errors else "no data"
            log.warning(f"[railway] API returned no data: {msg}")
            return
        projects = projects_data.get("edges", [])
        with db() as conn:
            count = 0
            for p_edge in projects:
                project = p_edge.get("node", {})
                for s_edge in project.get("services", {}).get("edges", []):
                    svc = s_edge.get("node", {})
                    svc_name = svc.get("name", "")
                    deploys  = svc.get("deployments", {}).get("edges", [])
                    if deploys:
                        dep    = deploys[0].get("node", {})
                        status = dep.get("status", "UNKNOWN").lower()
                        url    = dep.get("url")
                        deploy_at = (dep.get("createdAt") or "")[:19].replace("T", " ") or None
                        meta   = dep.get("meta") or {}
                        if isinstance(meta, str):
                            try:
                                meta = json.loads(meta)
                            except Exception:
                                meta = {}
                        commit_msg = meta.get("commitMessage") or meta.get("description")
                        commit_hash = meta.get("commitHash", "")[:7] if meta.get("commitHash") else None
                        # Map Railway statuses
                        if status in ("success", "complete"):
                            status = "healthy"
                        elif status in ("failed", "crashed"):
                            status = "error"
                        elif status in ("deploying", "building"):
                            status = "deploying"
                        _upsert_service(
                            conn, "railway", svc_name, status,
                            url=url, last_deploy_at=deploy_at,
                            commit_hash=commit_hash, commit_message=commit_msg,
                        )
                    else:
                        _upsert_service(conn, "railway", svc_name, "unknown")
                    count += 1
        log.info(f"[railway] polled {count} services")
    except Exception as exc:
        log.warning(f"[railway] poll failed: {exc}")


# ── Fly.io billing ──────────────────────────────────────────────────────────

FLY_MACHINES_API = "https://api.machines.dev/v1"

# Fly.io pricing (USD/month, March 2026)
FLY_PRICING = {
    # shared-cpu-1x per 256MB
    "shared": {"cpu_per": 1.94, "mem_per_256mb": 0.64},
    # performance/dedicated
    "performance": {"cpu_per": 29.00, "mem_per_256mb": 2.30},
}
FLY_VOLUME_PER_GB = 0.15  # USD/GB/month
USD_TO_EUR = 0.92  # approximate


async def poll_fly_billing() -> None:
    """Estimate per-app Fly.io costs from machine specs and volumes."""
    if not FLY_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Get all apps
            resp = await client.post(
                FLY_GQL,
                headers={"Authorization": f"Bearer {FLY_TOKEN}"},
                json={"query": "{ apps { nodes { name status } } }"},
            )
            resp.raise_for_status()
            apps = resp.json().get("data", {}).get("apps", {}).get("nodes", [])

            with db() as conn:
                for app in apps:
                    app_name = app["name"]
                    if app.get("status") in ("suspended", "dead"):
                        # Suspended apps only cost for volumes
                        _upsert_budget(conn, "fly.io", app_name, 0, "monthly",
                                       notes="suspended — volume costs only")
                        continue

                    # Get machines for this app
                    try:
                        mr = await client.get(
                            f"{FLY_MACHINES_API}/apps/{app_name}/machines",
                            headers={"Authorization": f"Bearer {FLY_TOKEN}"},
                        )
                        if mr.status_code != 200:
                            continue
                        machines = mr.json()
                    except Exception:
                        continue

                    monthly_usd = 0.0
                    for m in machines:
                        guest = m.get("config", {}).get("guest", {})
                        cpu_kind = guest.get("cpu_kind", "shared")
                        cpus = guest.get("cpus", 1)
                        mem_mb = guest.get("memory_mb", 256)

                        pricing = FLY_PRICING.get(cpu_kind, FLY_PRICING["shared"])
                        monthly_usd += cpus * pricing["cpu_per"]
                        monthly_usd += (mem_mb / 256) * pricing["mem_per_256mb"]

                    # Get volumes
                    try:
                        vr = await client.get(
                            f"{FLY_MACHINES_API}/apps/{app_name}/volumes",
                            headers={"Authorization": f"Bearer {FLY_TOKEN}"},
                        )
                        if vr.status_code == 200:
                            volumes = vr.json()
                            for v in volumes:
                                size_gb = v.get("size_gb", 0)
                                monthly_usd += size_gb * FLY_VOLUME_PER_GB
                    except Exception:
                        pass

                    cost_gbp = round(monthly_usd * USD_TO_EUR, 2)
                    machine_desc = f"{len(machines)} machine(s)" if machines else "no machines"
                    _upsert_budget(conn, "fly.io", app_name, cost_gbp, "monthly",
                                   notes=f"estimated: {machine_desc}, ${monthly_usd:.2f}/mo")

        log.info("[fly.io] billing estimated for %d apps", len(apps))
    except Exception as exc:
        log.warning("[fly.io] billing poll failed: %s", exc)


def _upsert_budget(conn, platform, service_name, cost_gbp, period, notes=None):
    """Upsert a budget row by platform + service_name."""
    existing = conn.execute(
        "SELECT id FROM budget WHERE platform = ? AND service_name = ?",
        (platform, service_name),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE budget SET cost_gbp = ?, notes = ?, active = 1 WHERE id = ?",
            (cost_gbp, notes, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO budget (platform, service_name, cost_gbp, period, active, notes) VALUES (?, ?, ?, ?, 1, ?)",
            (platform, service_name, cost_gbp, period, notes),
        )


# ── GitHub LOC ────────────────────────────────────────────────────────────────

GITHUB_TOKEN = os.environ.get("GITHUB_PAT", "")
GITHUB_USER  = os.environ.get("GITHUB_USER", "todd427")
GITHUB_API   = "https://api.github.com"


async def poll_github_loc() -> None:
    """
    Fetch commit stats from GitHub for all projects with a repo_url.
    Upserts worklog entries with lines_added/lines_removed per day per project.
    Only looks at the last 7 days to stay within API limits.
    """
    if not GITHUB_TOKEN:
        log.debug("GITHUB_PAT not set — skipping GitHub LOC poll")
        return
    try:
        with db() as conn:
            projects = conn.execute(
                "SELECT id, name, repo_url FROM projects WHERE repo_url IS NOT NULL AND repo_url != ''"
            ).fetchall()

        if not projects:
            return

        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        }

        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            for proj in projects:
                repo_url = proj["repo_url"]
                # Extract owner/repo from URL
                # Handles https://github.com/owner/repo or github.com/owner/repo
                parts = repo_url.rstrip("/").split("/")
                if len(parts) < 2:
                    continue
                owner, repo = parts[-2], parts[-1]
                repo = repo.replace(".git", "")

                try:
                    # Get commits for last 7 days by this user
                    since = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
                    resp = await client.get(
                        f"{GITHUB_API}/repos/{owner}/{repo}/commits",
                        params={"author": GITHUB_USER, "since": since, "per_page": 100},
                    )
                    if resp.status_code != 200:
                        continue
                    commits = resp.json()

                    # Group commits by date and sum stats
                    daily_stats = {}
                    for c in commits:
                        commit_date = (c.get("commit", {}).get("author", {}).get("date") or "")[:10]
                        if not commit_date:
                            continue

                        # Fetch individual commit for stats (additions/deletions)
                        cr = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}/commits/{c['sha']}")
                        if cr.status_code != 200:
                            continue
                        stats = cr.json().get("stats", {})
                        if commit_date not in daily_stats:
                            daily_stats[commit_date] = {"added": 0, "removed": 0}
                        daily_stats[commit_date]["added"] += stats.get("additions", 0)
                        daily_stats[commit_date]["removed"] += stats.get("deletions", 0)

                    # Upsert into worklog
                    with db() as conn:
                        for date_str, stats in daily_stats.items():
                            existing = conn.execute(
                                """SELECT id FROM worklog
                                   WHERE project_id = ? AND date = ? AND session_type = 'code'""",
                                (proj["id"], date_str),
                            ).fetchone()
                            if existing:
                                conn.execute(
                                    """UPDATE worklog SET lines_added = ?, lines_removed = ?
                                       WHERE id = ?""",
                                    (stats["added"], stats["removed"], existing["id"]),
                                )
                            else:
                                conn.execute(
                                    """INSERT INTO worklog (project_id, date, minutes, session_type, notes, lines_added, lines_removed)
                                       VALUES (?, ?, 0, 'code', 'auto: git LOC', ?, ?)""",
                                    (proj["id"], date_str, stats["added"], stats["removed"]),
                                )

                except Exception as exc:
                    log.warning(f"[github-loc] {proj['name']}: {exc}")
                    continue

        log.info(f"[github-loc] polled {len(projects)} projects")
    except Exception as exc:
        log.warning(f"[github-loc] poll failed: {exc}")


# ── scheduler setup ──────────────────────────────────────────────────────────

def setup_scheduler() -> AsyncIOScheduler:
    scheduler.add_job(poll_flyio,       "interval", seconds=60,   id="fly",         replace_existing=True)
    scheduler.add_job(poll_railway,     "interval", seconds=60,   id="railway",     replace_existing=True)
    scheduler.add_job(poll_fly_billing, "interval", seconds=3600, id="fly_billing", replace_existing=True)
    scheduler.add_job(poll_github_loc,  "interval", seconds=21600, id="github_loc", replace_existing=True)
    return scheduler


async def run_all_now() -> None:
    """Trigger all pollers immediately — used by the /refresh endpoint."""
    await poll_flyio()
    await poll_railway()
    await poll_fly_billing()

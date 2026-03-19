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
query($appNames: [String!]!) {
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
                json={"query": FLY_QUERY, "variables": {"appNames": []}},
            )
            resp.raise_for_status()
            data = resp.json()
        apps = data.get("data", {}).get("apps", {}).get("nodes", [])
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
query {
  me {
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

        projects = (
            data.get("data", {})
                .get("me", {})
                .get("projects", {})
                .get("edges", [])
        )
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


# ── scheduler setup ──────────────────────────────────────────────────────────

def setup_scheduler() -> AsyncIOScheduler:
    scheduler.add_job(poll_flyio,   "interval", seconds=60,  id="fly",     replace_existing=True)
    scheduler.add_job(poll_railway, "interval", seconds=60,  id="railway", replace_existing=True)
    return scheduler


async def run_all_now() -> None:
    """Trigger all pollers immediately — used by the /refresh endpoint."""
    await poll_flyio()
    await poll_railway()

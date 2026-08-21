"""
main.py — Rialú FastAPI application.

Startup sequence:
  1. init_db()           — run SQLite migrations
  2. scheduler.start()   — begin background pollers (skipped in test mode)
  3. serve routes        — API + static SPA

Auth: Cloudflare Access injects Cf-Access-Authenticated-User-Email.
      The app trusts that header; no auth code needed here.
      In local dev (no CF Access), auth is bypassed.

MCP: FastMCP mounted at /mcp — Bearer token via RIALU_MCP_KEY env var.
"""

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount

from db import init_db
from poller import setup_scheduler
from routers import projects, worklog, deployments, budget, machines, mcp_status, usage, sentinel, milestone_review, mnemos, github, export, decisions, agents, commits, divergence, spend
from ws_hub import hub
from faire_hub import faire_hub
import mcp_server as _mcp

TEST_MODE = os.environ.get("RIALU_TEST") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not TEST_MODE:
        sched = setup_scheduler()
        sched.start()
    # MCP session manager must run for the full lifespan
    session_mgr = _mcp.mcp.session_manager
    async with session_mgr.run():
        yield
    if not TEST_MODE:
        from poller import scheduler
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="Rialú",
    description="Personal command centre — rialu.ie",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

# ── force canonical hostname ──────────────────────────────────────────────────

from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import RedirectResponse


import ipaddress
import secrets

# Cloudflare's published edge ranges (www.cloudflare.com/ips-v4 + ips-v6),
# snapshotted 2026-08-21. Refresh if Cloudflare publishes new ranges — a stale
# list fails closed (legitimate traffic 403s), never open.
CLOUDFLARE_IP_RANGES = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

_CF_NETS = tuple(ipaddress.ip_network(r) for r in CLOUDFLARE_IP_RANGES)


def _via_cloudflare(request) -> bool:
    """True if this request actually traversed Cloudflare.

    Checks the peer address, NOT a header. The Host header and Cf-* headers are
    all attacker-controlled when someone connects straight to the Fly origin —
    which is exactly how Access got bypassed on 2026-08-21: a request carrying
    `Host: rialu.ie` sent directly to the Fly IP satisfied the old hostname
    check and was served in full. Fly sets Fly-Client-IP to the real peer.
    """
    raw = request.headers.get("fly-client-ip") or (
        request.client.host if request.client else "")
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False          # unparseable — fail closed
    return any(ip in net for net in _CF_NETS)


def _has_valid_bearer(request) -> bool:
    """True if the request carries a correct Bearer token.

    Mirrors auth.py's verify_faire_token comparison. Returns False when no
    token is configured — in that case the caller has proved nothing, so the
    Cloudflare check must decide.
    """
    expected = os.environ.get("FAIRE_WS_TOKEN", "")
    if not expected:
        return False
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return False
    return secrets.compare_digest(header[7:], expected)


class CanonicalHostMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        host = request.headers.get("host", "")
        path = request.url.path

        if TEST_MODE:
            return await call_next(request)

        # Health check — always allowed (Fly internal monitoring)
        if path == "/api/health":
            return await call_next(request)

        # WebSocket — token-authenticated at application layer (faire_hub validates FAIRE_WS_TOKEN)
        if path.startswith("/ws/"):
            return await call_next(request)

        # MCP + OAuth endpoints — self-authenticating via OAuth 2.1
        if path.startswith("/mcp") or path.startswith("/.well-known") or path in ("/authorize", "/token", "/register", "/revoke"):
            return await call_next(request)

        # Everything else must come through rialu.ie (Cloudflare Access)
        # Faire, agents, and browsers all go through CF with appropriate auth
        if host and "rialu.ie" not in host:
            return JSONResponse({"detail": "Use rialu.ie"}, status_code=421)

        # ...and must genuinely have passed through Cloudflare. The hostname
        # check above proves nothing on its own: Host is set by the caller, so
        # a direct request to the Fly origin carrying `Host: rialu.ie` cleared
        # it and got the full app, bypassing Access entirely. Verify the peer.
        #
        # A valid Bearer token is authentication in its own right, so it is
        # allowed to reach the origin directly — the guard exists to stop
        # UNauthenticated access, not to force everything through Cloudflare.
        # scripts/divergence_selfcall.py depends on this: the weekly scheduled
        # machine POSTs to the public Fly edge (it cannot run in-process — the
        # data volume is single-attach) and never traverses Cloudflare.
        if not _via_cloudflare(request) and not _has_valid_bearer(request):
            return JSONResponse(
                {"detail": "Direct origin access is not permitted; use rialu.ie"},
                status_code=403,
            )

        return await call_next(request)


app.add_middleware(CanonicalHostMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://tauri.localhost", "https://tauri.localhost", "tauri://localhost"],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|tauri\.localhost)(:\d+)?$",
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ── routers ──────────────────────────────────────────────────────────────────

app.include_router(projects.router)
app.include_router(worklog.router)
app.include_router(deployments.router)
app.include_router(budget.router)
app.include_router(machines.router)
app.include_router(mcp_status.router)
app.include_router(usage.router)
app.include_router(sentinel.router)
app.include_router(milestone_review.router)
app.include_router(mnemos.router)
app.include_router(github.router)
app.include_router(export.router)
app.include_router(decisions.router)
app.include_router(agents.router)
app.include_router(commits.router)
app.include_router(divergence.router)
app.include_router(spend.router)


# ── WebSocket routes ─────────────────────────────────────────────────────────

@app.websocket("/ws/agent")
async def ws_agent(websocket: WebSocket):
    """Persistent agent connection — heartbeats, terminal bridging, tmux."""
    await hub.handle_agent(websocket)


@app.websocket("/ws/terminal/{machine}")
async def ws_terminal(websocket: WebSocket, machine: str):
    """Browser terminal — opens a shell on the named machine."""
    await hub.handle_browser_terminal(websocket, machine)


@app.websocket("/ws/pane/{machine}/{pane_id:path}")
async def ws_pane(websocket: WebSocket, machine: str, pane_id: str):
    """Browser pane attachment — streams an existing tmux pane."""
    await hub.handle_browser_terminal(websocket, machine, pane_id=pane_id)


@app.websocket("/ws/viewer")
async def ws_viewer(websocket: WebSocket):
    """Read-only telemetry feed for Teas — snapshot on connect, then push.

    Must stay registered ahead of the /ws/{token} catch-all below, which would
    otherwise swallow it and hand the socket to the Faire hub.
    """
    await hub.handle_viewer(websocket)


@app.websocket("/ws/{token}")
async def ws_faire(websocket: WebSocket, token: str):
    """Faire desktop client — broadcast hub for project/decision events."""
    if not await faire_hub.connect(websocket, token):
        return
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        faire_hub.disconnect(websocket)


# ── health ───────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "app": "rialu"}


@app.post("/api/test-broadcast")
async def test_broadcast():
    """Debug endpoint — send a test event to all Faire WS clients."""
    clients = len(faire_hub.clients)
    await faire_hub.broadcast({
        "event": "project.update",
        "project_id": None,
        "payload": {"test": True, "message": "hello from test-broadcast"},
    })
    return {"clients": clients, "sent": True}


# ── SPA ──────────────────────────────────────────────────────────────────────

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_INDEX_HTML = os.path.join(STATIC_DIR, "index.html") if os.path.isdir(STATIC_DIR) else None

if _INDEX_HTML:
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=FileResponse)
    def index():
        return FileResponse(_INDEX_HTML)

# ── MCP — mount LAST at root; catches /.well-known, /authorize, /token, /mcp ─

app.router.routes.append(Mount("/", app=_mcp.get_asgi_app()))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        reload=True,
    )

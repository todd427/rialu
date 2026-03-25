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
from routers import projects, worklog, deployments, budget, machines, keys, mcp_status, usage, sentinel, milestone_review, mnemos, github, export, decisions, agents
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


class CanonicalHostMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        host = request.headers.get("host", "")
        path = request.url.path

        if TEST_MODE:
            return await call_next(request)

        # Health check — always allowed (Fly internal monitoring)
        if path == "/api/health":
            return await call_next(request)

        # API, WS, MCP + OAuth endpoints — allowed on any host (Faire desktop, agents, MCP)
        if path.startswith("/api/") or path.startswith("/ws/") or path.startswith("/mcp") or path.startswith("/.well-known") or path in ("/authorize", "/token", "/register", "/revoke"):
            return await call_next(request)

        # Tauri/localhost origins — allowed via CORS (Faire desktop)
        origin = request.headers.get("origin", "")
        if origin and ("localhost" in origin or "tauri" in origin):
            return await call_next(request)

        # Everything else must come through rialu.ie (Cloudflare Access)
        if host and "rialu.ie" not in host:
            return JSONResponse({"detail": "Use rialu.ie"}, status_code=421)

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
app.include_router(keys.router)
app.include_router(mcp_status.router)
app.include_router(usage.router)
app.include_router(sentinel.router)
app.include_router(milestone_review.router)
app.include_router(mnemos.router)
app.include_router(github.router)
app.include_router(export.router)
app.include_router(decisions.router)
app.include_router(agents.router)


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


# ── MCP — mount at root; streamable_http_app() registers /mcp internally ─────
# OAuth endpoints (/.well-known, /authorize, /token, /register) also at root.

app.router.routes.append(Mount("/", app=_mcp.get_asgi_app()))

# ── SPA catch-all (must be AFTER MCP mount) ──────────────────────────────────

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_INDEX_HTML = os.path.join(STATIC_DIR, "index.html") if os.path.isdir(STATIC_DIR) else None

if _INDEX_HTML:
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=FileResponse)
    def index():
        return FileResponse(_INDEX_HTML)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        reload=True,
    )

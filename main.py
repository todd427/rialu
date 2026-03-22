"""
main.py — Rialú FastAPI application.

Startup sequence:
  1. init_db()           — run SQLite migrations
  2. scheduler.start()   — begin background pollers (skipped in test mode)
  3. serve routes        — API + static SPA

Auth: Cloudflare Access injects Cf-Access-Authenticated-User-Email.
      The app trusts that header; no auth code needed here.
      In local dev (no CF Access), auth is bypassed.
"""

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from db import init_db
from poller import setup_scheduler
from routers import projects, worklog, deployments, budget, machines, keys, mcp_status, usage, sentinel, milestone_review
from ws_hub import hub

TEST_MODE = os.environ.get("RIALU_TEST") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not TEST_MODE:
        sched = setup_scheduler()
        sched.start()
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
        # Allow agent WebSocket connections on any hostname
        if request.url.path.startswith("/ws/agent") or request.url.path.startswith("/api/agent/"):
            return await call_next(request)
        if host and "rialu.ie" not in host and not TEST_MODE:
            return RedirectResponse(f"https://rialu.ie{request.url.path}", status_code=301)
        return await call_next(request)


app.add_middleware(CanonicalHostMiddleware)

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


# ── health ───────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "app": "rialu"}


# ── SPA catch-all ─────────────────────────────────────────────────────────────

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=FileResponse)
    def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    @app.get("/{full_path:path}", response_class=FileResponse)
    def spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        reload=True,
    )

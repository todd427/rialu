"""
main.py — Rialú FastAPI application.

Startup sequence:
  1. init_db()           — run SQLite migrations
  2. scheduler.start()   — begin background pollers
  3. serve routes        — API + static SPA

Auth: Cloudflare Access injects Cf-Access-Authenticated-User-Email.
      The app trusts that header; no auth code needed here.
      In local dev (no CF Access), auth is bypassed.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from db import init_db
from poller import setup_scheduler
from routers import projects, worklog, deployments, budget, machines


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    sched = setup_scheduler()
    sched.start()
    yield
    sched.shutdown(wait=False)


app = FastAPI(
    title="Rialú",
    description="Personal command centre — rialu.ie",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

# ── routers ──────────────────────────────────────────────────────────────────

app.include_router(projects.router)
app.include_router(worklog.router)
app.include_router(deployments.router)
app.include_router(budget.router)
app.include_router(machines.router)


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
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), reload=True)

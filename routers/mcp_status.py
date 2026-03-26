"""
routers/mcp_status.py — MCP connector health checker.

Checks each registered MCP server's:
  1. /health endpoint (basic reachability)
  2. /.well-known/oauth-authorization-server (OAuth discovery)
  3. POST /mcp with initialize (full MCP protocol handshake)
"""

import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

MCP_SERVERS = [
    {
        "name": "Rialú",
        "url": "https://rialu.fly.dev",
        "mcp_path": "/mcp",
        "platform": "fly.io",
        "description": "DevOps command centre — vault + projects",
    },
    {
        "name": "Sentinel",
        "url": "https://sentinel-foxxelabs.fly.dev",
        "mcp_path": "/mcp",
        "platform": "fly.io",
        "description": "IP threat intelligence",
    },
    {
        "name": "Mnemos",
        "url": "https://mnemos.foxxelabs.ie",
        "mcp_path": "/mcp",
        "platform": "fly.io",
        "description": "Personal memory system",
    },
    {
        "name": "git-mcp",
        "url": "https://git-mcp-foxxelabs.fly.dev",
        "mcp_path": "/mcp",
        "platform": "fly.io",
        "description": "Git operations",
    },
    {
        "name": "Flyer",
        "url": "https://fly-mcp-foxxelabs.fly.dev",
        "mcp_path": "/mcp",
        "platform": "fly.io",
        "description": "Fly.io management",
    },
    {
        "name": "Eric",
        "url": "https://mark-foxxelabs.fly.dev",
        "mcp_path": "/mcp",
        "platform": "fly.io",
        "description": "M. Eric Ting — marketing intelligence agent",
    },
]


async def _check_server(server: dict) -> dict:
    base = server["url"]
    result = {
        "name": server["name"],
        "url": base,
        "platform": server["platform"],
        "description": server["description"],
        "health": "unknown",
        "oauth": "unknown",
        "mcp": "unknown",
        "tools": [],
        "tool_count": 0,
        "latency_ms": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        # 1. Health check
        try:
            t0 = time.monotonic()
            r = await client.get(f"{base}/health")
            result["latency_ms"] = round((time.monotonic() - t0) * 1000)
            result["health"] = "ok" if r.status_code == 200 else f"http {r.status_code}"
        except Exception as e:
            result["health"] = "down"
            result["error"] = str(e)
            return result

        # 2. OAuth discovery
        try:
            r = await client.get(f"{base}/.well-known/oauth-authorization-server")
            if r.status_code == 200:
                meta = r.json()
                has_required = all(
                    k in meta for k in ("authorization_endpoint", "token_endpoint")
                )
                result["oauth"] = "ok" if has_required else "incomplete"
            else:
                result["oauth"] = f"http {r.status_code}"
        except Exception:
            result["oauth"] = "error"

        # 3. MCP protocol check (initialize only — no auth needed to see if it responds)
        mcp_path = server.get("mcp_path", "/mcp")
        try:
            r = await client.post(
                f"{base}{mcp_path}",
                json={
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "rialu-checker", "version": "0.1"},
                    },
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            if r.status_code == 401:
                result["mcp"] = "auth-required"
            elif r.status_code == 200:
                result["mcp"] = "ok"
                # Try to extract server info from SSE response
                for line in r.text.strip().split("\n"):
                    if line.startswith("data:"):
                        import json
                        try:
                            data = json.loads(line[5:].strip())
                            info = data.get("result", {}).get("serverInfo", {})
                            if info:
                                result["mcp_server"] = info.get("name", "")
                                result["mcp_version"] = info.get("version", "")
                            caps = data.get("result", {}).get("capabilities", {})
                            if caps.get("tools"):
                                result["mcp"] = "ok"
                        except json.JSONDecodeError:
                            pass
            else:
                result["mcp"] = f"http {r.status_code}"
        except Exception as e:
            result["mcp"] = "error"
            if not result["error"]:
                result["error"] = str(e)

    return result


@router.get("")
async def mcp_status():
    """Check all MCP connectors and return their status."""
    import asyncio
    results = await asyncio.gather(
        *[_check_server(s) for s in MCP_SERVERS],
        return_exceptions=True,
    )
    checked = []
    for r in results:
        if isinstance(r, Exception):
            checked.append({"name": "?", "health": "error", "error": str(r)})
        else:
            checked.append(r)
    return checked


@router.get("/servers")
def list_servers():
    """Return the list of configured MCP servers."""
    return MCP_SERVERS

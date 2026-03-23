"""
faire_hub.py — Broadcast WebSocket hub for Faire desktop clients.

Simple pub/sub fan-out: all connected Faire clients receive every event.
Auth via token in the URL path, validated against FAIRE_WS_TOKEN env var.

Event envelope:
  { "event": "project.update|decision.new|agent.heartbeat",
    "project_id": str|null, "agent_id": str|null,
    "payload": {}, "ts": "ISO datetime" }
"""

import json
import logging
import os
from datetime import datetime, timezone

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("faire_hub")


class FaireHub:
    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket, token: str) -> bool:
        expected = os.environ.get("FAIRE_WS_TOKEN", "")
        if not expected or token != expected:
            await ws.close(code=4003, reason="Invalid token")
            return False
        await ws.accept()
        self.clients.add(ws)
        log.info("Faire client connected (%d total)", len(self.clients))
        return True

    def disconnect(self, ws: WebSocket):
        self.clients.discard(ws)
        log.info("Faire client disconnected (%d total)", len(self.clients))

    async def broadcast(self, envelope: dict):
        if not self.clients:
            return
        envelope.setdefault("ts", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        envelope.setdefault("project_id", None)
        envelope.setdefault("agent_id", None)
        msg = json.dumps(envelope)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


faire_hub = FaireHub()

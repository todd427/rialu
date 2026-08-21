"""
ws_hub.py — WebSocket hub for agent ↔ browser bridging.

Agents (rialu-agent on each machine) maintain a persistent WebSocket
connection to the hub. Browsers connect to the hub to open terminals,
view tmux panes, and send keystrokes. The hub bridges the two.

Message protocol (JSON envelope):
{
    "type": "heartbeat | tmux_list | terminal_open | terminal_data |
             terminal_close | terminal_resize | pane_attach | pane_data |
             pane_detach | send_keys | claude_status | action",
    "machine": "daisy",
    "channel": "uuid",        # for terminal/pane routing
    "payload": ...,            # type-specific data
    "ts": 1742428800
}
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from db import db

log = logging.getLogger("ws_hub")


def _agent_key() -> bytes:
    return os.environ.get("RIALU_AGENT_KEY", "").encode()


class AgentHub:
    """Manages agent and browser WebSocket connections."""

    def __init__(self):
        # machine_name -> WebSocket
        self.agents: dict[str, WebSocket] = {}
        # channel_id -> browser WebSocket
        self.browser_channels: dict[str, WebSocket] = {}
        # channel_id -> machine_name
        self.channel_machines: dict[str, str] = {}
        # machine_name -> latest tmux data
        self.tmux_cache: dict[str, list] = {}
        # machine_name -> latest claude status
        self.claude_cache: dict[str, list] = {}
        # action_id -> Future, for request/response agent actions
        self.pending_actions: dict[str, asyncio.Future] = {}
        # Read-only viewer sockets (Teas). A set, not a dict: viewers are
        # anonymous fan-out targets with no routing key and no reply path.
        self.viewers: set[WebSocket] = set()

    def is_connected(self, machine: str) -> bool:
        return machine in self.agents

    def connected_machines(self) -> list[str]:
        return list(self.agents.keys())

    # ── Agent connection ─────────────────────────────────────────────────

    async def handle_agent(self, ws: WebSocket):
        """Handle a persistent agent WebSocket connection."""
        await ws.accept()

        # First message must be auth
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
            auth = json.loads(raw)
        except (asyncio.TimeoutError, json.JSONDecodeError):
            await ws.close(code=4001, reason="Auth timeout or invalid JSON")
            return

        machine = auth.get("machine", "")
        if not self._verify_agent_auth(auth):
            await ws.close(code=4003, reason="Auth failed")
            return

        # Register agent
        old_ws = self.agents.get(machine)
        self.agents[machine] = ws
        log.info("Agent connected: %s", machine)

        if old_ws:
            try:
                await old_ws.close(code=1000, reason="Replaced by new connection")
            except Exception:
                pass

        try:
            while True:
                raw = await ws.receive_text()
                data = json.loads(raw)
                await self._handle_agent_message(machine, data)
        except WebSocketDisconnect:
            log.info("Agent disconnected: %s", machine)
        except Exception:
            log.exception("Agent error: %s", machine)
        finally:
            if self.agents.get(machine) is ws:
                del self.agents[machine]
            # Close any browser channels for this machine
            await self._cleanup_machine_channels(machine)

    def _verify_agent_auth(self, auth: dict) -> bool:
        """Verify HMAC auth from agent."""
        key = _agent_key()
        if not key:
            log.error("RIALU_AGENT_KEY not configured")
            return False
        sig = auth.get("sig", "")
        machine = auth.get("machine", "")
        ts = str(auth.get("ts", ""))
        if not sig.startswith("sha256=") or not machine:
            return False
        msg = f"{machine}:{ts}".encode()
        expected = "sha256=" + hmac.new(key, msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    async def _handle_agent_message(self, machine: str, data: dict):
        """Route an incoming message from an agent."""
        msg_type = data.get("type", "")

        if msg_type == "heartbeat":
            await self._store_heartbeat(machine, data)
            # Auto-generate worklog from git commits
            repos = data.get("repos", [])
            if any(r.get("recent_commits") for r in repos):
                try:
                    from commit_worklog import process_commits_for_worklog
                    summaries = process_commits_for_worklog(repos)
                    for s in summaries:
                        log.info("Worklog %s: project=%s date=%s min=%d",
                                 s["action"], s["project_id"], s["date"], s["minutes"])
                except Exception:
                    log.exception("Error processing commits for worklog")
            # Process API scan results
            api_scan = data.get("api_scan", {})
            if api_scan:
                try:
                    self._store_api_scan(api_scan)
                except Exception:
                    log.exception("Error storing API scan results")

        elif msg_type == "tmux_list":
            self.tmux_cache[machine] = data.get("sessions", [])

        elif msg_type == "claude_status":
            sessions = data.get("sessions", [])
            prev_sessions = self.claude_cache.get(machine, [])
            self.claude_cache[machine] = sessions

            # Broadcast to Faire desktop clients
            from faire_hub import faire_hub
            await faire_hub.broadcast({
                "event": "agent.claude_status",
                "agent_id": machine,
                "payload": {"machine": machine, "sessions": sessions},
            })

            # Auto-create decision when CC session starts waiting
            await self._check_cc_waiting(machine, sessions, prev_sessions)

        elif msg_type in ("terminal_data", "pane_data"):
            channel = data.get("channel", "")
            browser_ws = self.browser_channels.get(channel)
            if browser_ws:
                try:
                    await browser_ws.send_text(json.dumps({
                        "type": msg_type,
                        "data": data.get("data", ""),
                    }))
                except Exception:
                    pass

        elif msg_type == "action_result":
            # Resolve a pending run_agent_action() call by its action_id.
            aid = data.get("action_id")
            fut = self.pending_actions.get(aid)
            if fut and not fut.done():
                fut.set_result(data)

        elif msg_type == "terminal_closed":
            channel = data.get("channel", "")
            browser_ws = self.browser_channels.pop(channel, None)
            self.channel_machines.pop(channel, None)
            if browser_ws:
                try:
                    await browser_ws.send_text(json.dumps({"type": "terminal_closed"}))
                except Exception:
                    pass

    async def _store_heartbeat(self, machine: str, data: dict):
        """Upsert heartbeat data into DB and broadcast to viewers and Faire."""
        from routers.machines import normalise_heartbeat
        hb = normalise_heartbeat(data)

        payload = {
            "machine_name": machine,
            # Legacy scalars — load-bearing for Faire's .mcard.
            "cpu_pct": hb["cpu_pct"],
            "ram_pct": hb["ram_pct"],
            "gpu_pct": hb["gpu_pct"],
            # New per-die shape for Teas.
            "cpu": {"load_pct": hb["cpu_pct"], "temp_c": hb["cpu_temp_c"]},
            "gpus": hb["gpus"],
            "processes": hb["processes"],
            "repos": hb["repos"],
        }

        # Fan out to viewers FIRST. A viewer socket that waits on SQLite has
        # thrown away the latency the adaptive heartbeat interval bought.
        await self.broadcast_to_viewers({"type": "heartbeat", **payload})

        with db() as conn:
            conn.execute(
                "DELETE FROM machine_heartbeats WHERE machine_name = ?",
                (machine,),
            )
            conn.execute(
                """INSERT INTO machine_heartbeats
                   (machine_name, cpu_pct, ram_pct, gpu_pct, cpu_temp_c,
                    processes_json, repos_json, gpus_json, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (
                    machine,
                    hb["cpu_pct"],
                    hb["ram_pct"],
                    hb["gpu_pct"],
                    hb["cpu_temp_c"],
                    json.dumps(hb["processes"]),
                    json.dumps(hb["repos"]),
                    json.dumps(hb["gpus"]),
                ),
            )
        # Broadcast to Faire desktop clients
        from faire_hub import faire_hub
        await faire_hub.broadcast({
            "event": "agent.heartbeat",
            "agent_id": machine,
            "payload": payload,
        })

    async def _check_cc_waiting(self, machine: str, sessions: list, prev_sessions: list):
        """Create a decision when a CC session starts waiting for user input."""
        # Track which sessions were already waiting to avoid duplicate decisions
        prev_waiting = {s.get("pane_id") for s in prev_sessions if s.get("claude_state") == "waiting"}

        for s in sessions:
            if s.get("claude_state") == "waiting" and s.get("pane_id") not in prev_waiting:
                prompt = s.get("waiting_prompt", "Claude Code is waiting")
                pane_id = s.get("pane_id", "unknown")
                last_lines = s.get("last_lines", [])
                context = "\n".join(last_lines[-5:]) if last_lines else ""

                log.info("CC waiting detected on %s pane %s: %s", machine, pane_id, prompt)

                # Create a decision
                try:
                    from db import db
                    decision_id = str(uuid.uuid4())
                    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    payload = json.dumps({
                        "summary": f"Claude Code on {machine} is waiting: {prompt}",
                        "project": {"name": machine, "type": "cc-session"},
                        "agent": {"id": machine, "name": f"cc-{pane_id}", "machine": machine},
                        "current_state": {"pane_id": pane_id, "prompt": prompt, "context": context},
                        "proposed_state": {"action": "respond to Claude Code"},
                    })
                    with db() as conn:
                        conn.execute(
                            """INSERT INTO decisions (id, project_id, trigger_type, priority, payload, timeout_secs, created_at)
                               VALUES (?, (SELECT id FROM projects WHERE LOWER(machine) = ? LIMIT 1), 'ai_approval', 2, ?, 600, ?)""",
                            (decision_id, machine, payload, now),
                        )

                    # Broadcast decision.new
                    from faire_hub import faire_hub
                    await faire_hub.broadcast({
                        "event": "decision.new",
                        "agent_id": machine,
                        "payload": {
                            "id": decision_id,
                            "trigger_type": "ai_approval",
                            "priority": 2,
                            "status": "pending",
                            "payload": payload,
                            "timeout_secs": 600,
                            "created_at": now,
                        },
                    })
                except Exception:
                    log.exception("Failed to create CC waiting decision")

    def _store_api_scan(self, api_scan: dict):
        """Store API scan results in api_registry and api_project_map."""
        with db() as conn:
            # Load project slugs
            projects = conn.execute("SELECT id, slug FROM projects").fetchall()
            slug_to_id = {r["slug"]: r["id"] for r in projects}

            for repo_name, apis in api_scan.items():
                project_id = slug_to_id.get(repo_name) or slug_to_id.get(repo_name.lower())
                if not project_id:
                    continue

                for api_info in apis:
                    api_name = api_info.get("api", "")
                    provider = api_info.get("provider", "")
                    detected_file = api_info.get("file", "")

                    # Ensure api_registry entry exists
                    existing_api = conn.execute(
                        "SELECT id FROM api_registry WHERE name = ?", (api_name,)
                    ).fetchone()
                    if existing_api:
                        api_id = existing_api["id"]
                    else:
                        conn.execute(
                            "INSERT INTO api_registry (name, provider, billing_model, notes, active) VALUES (?, ?, 'unknown', 'auto-discovered by code scanner', 1)",
                            (api_name, provider),
                        )
                        api_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                    # Ensure api_project_map entry exists
                    existing_map = conn.execute(
                        "SELECT id FROM api_project_map WHERE api_id = ? AND project_id = ?",
                        (api_id, project_id),
                    ).fetchone()
                    if not existing_map:
                        conn.execute(
                            "INSERT INTO api_project_map (api_id, project_id, usage_description, is_primary) VALUES (?, ?, ?, 0)",
                            (api_id, project_id, f"detected in {detected_file}"),
                        )

    async def _cleanup_machine_channels(self, machine: str):
        """Close all browser channels for a disconnected machine."""
        channels_to_close = [
            ch for ch, m in self.channel_machines.items() if m == machine
        ]
        for ch in channels_to_close:
            ws = self.browser_channels.pop(ch, None)
            self.channel_machines.pop(ch, None)
            if ws:
                try:
                    await ws.send_text(json.dumps({
                        "type": "terminal_closed",
                        "reason": "Agent disconnected",
                    }))
                except Exception:
                    pass

    # ── Viewer socket (read-only) ────────────────────────────────────────

    async def broadcast_to_viewers(self, msg: dict):
        """Fan a message out to every connected viewer.

        Never raises: a viewer that has gone away must not interrupt the
        heartbeat path that called us.
        """
        if not self.viewers:
            return
        raw = json.dumps(msg)
        dead = []
        for ws in list(self.viewers):
            try:
                await ws.send_text(raw)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.viewers.discard(ws)

    def _viewer_snapshot(self) -> dict:
        """Current state of every machine, for a viewer that just connected.

        Without this a widget opening mid-session shows nothing until the next
        heartbeat — up to 30s of blank gauges on an idle fleet.
        """
        from routers.machines import list_machines
        return {"type": "snapshot", "machines": list_machines()}

    async def handle_viewer(self, ws: WebSocket):
        """Handle a read-only viewer WebSocket (Teas).

        Same HMAC scheme as the agent socket — deliberately reusing
        _verify_agent_auth rather than inventing a second auth path.

        A viewer is strictly a sink. It may send pings to keep the connection
        alive; every other inbound message type is refused and the socket is
        closed. A viewer must never be able to reach an agent, so there is no
        branch here that forwards anything — the capability simply does not
        exist on this path.
        """
        await ws.accept()

        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
            auth = json.loads(raw)
        except (asyncio.TimeoutError, json.JSONDecodeError):
            await ws.close(code=4001, reason="Auth timeout or invalid JSON")
            return

        if not self._verify_agent_auth(auth):
            await ws.close(code=4003, reason="Auth failed")
            return

        self.viewers.add(ws)
        log.info("Viewer connected (%d total)", len(self.viewers))

        try:
            await ws.send_text(json.dumps(self._viewer_snapshot()))
            while True:
                data = json.loads(await ws.receive_text())
                if data.get("type") != "ping":
                    # Not merely ignored — refused. An inbound type we do not
                    # recognise on a read-only socket is a client doing
                    # something it should not, and it ends the connection.
                    log.warning("Viewer sent disallowed message type: %r",
                                data.get("type"))
                    await ws.close(code=4400, reason="Viewer sockets are read-only")
                    return
                await ws.send_text(json.dumps({"type": "pong"}))
        except WebSocketDisconnect:
            log.info("Viewer disconnected")
        except Exception:
            log.exception("Viewer error")
        finally:
            self.viewers.discard(ws)

    # ── Browser terminal ─────────────────────────────────────────────────

    async def handle_browser_terminal(self, ws: WebSocket, machine: str,
                                       pane_id: Optional[str] = None):
        """Handle a browser terminal WebSocket — bridge to agent.

        SECURITY: this route hands out a live shell on the named machine. It
        used to have no authentication of its own, relying entirely on
        Cloudflare Access sitting in front of rialu.ie. That assumption broke
        on 2026-08-21: the DNS record was not proxied, so Access was out of the
        request path and the socket served an unauthenticated root shell to the
        public internet. Even with Access restored, the Fly origin IPs stay
        publicly reachable and bypass it entirely.

        So the socket now authenticates itself, with the same HMAC handshake
        the agent and viewer sockets use. An attacker reaching the origin
        directly cannot forge it without RIALU_AGENT_KEY. Never reintroduce a
        dependency on an upstream proxy for this route.
        """
        if machine not in self.agents:
            await ws.close(code=4004, reason=f"Machine '{machine}' not connected")
            return

        # Cloudflare Access injects a SIGNED JWT on the upgrade request, which
        # a browser cannot set itself and a direct-to-origin caller cannot
        # forge. That is what lets the SPA open a terminal without shipping a
        # shared secret to the page. Checked BEFORE accept() so an
        # unauthenticated caller never reaches the handshake.
        import cf_access
        who = cf_access.identity(ws.headers)

        await ws.accept()

        if not who:
            # No verified Access identity — fall back to the HMAC handshake
            # used by the agent socket, for clients that hold RIALU_AGENT_KEY.
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
                auth = json.loads(raw)
            except (asyncio.TimeoutError, json.JSONDecodeError):
                await ws.close(code=4001, reason="Auth timeout or invalid JSON")
                return
            if not self._verify_agent_auth(auth):
                log.warning("Terminal auth failed for machine=%s", machine)
                await ws.close(code=4003, reason="Auth failed")
                return
            who = auth.get("machine", "hmac-client")

        log.info("Terminal authorised for %s on machine=%s", who, machine)
        channel = str(uuid.uuid4())
        self.browser_channels[channel] = ws
        self.channel_machines[channel] = machine

        # Record the terminal session
        with db() as conn:
            conn.execute(
                "INSERT INTO terminal_sessions (machine_name, channel_id, pane_id) VALUES (?, ?, ?)",
                (machine, channel, pane_id),
            )

        # Tell agent to open terminal or attach to pane
        agent_ws = self.agents[machine]
        if pane_id:
            await agent_ws.send_text(json.dumps({
                "type": "pane_attach",
                "channel": channel,
                "pane_id": pane_id,
            }))
        else:
            await agent_ws.send_text(json.dumps({
                "type": "terminal_open",
                "channel": channel,
            }))

        try:
            while True:
                raw = await ws.receive_text()
                data = json.loads(raw)

                if data.get("type") == "resize":
                    await agent_ws.send_text(json.dumps({
                        "type": "terminal_resize",
                        "channel": channel,
                        "cols": data.get("cols", 80),
                        "rows": data.get("rows", 24),
                    }))
                elif pane_id:
                    # tmux pane channel: input must go to the pane via send-keys.
                    # (A pane channel has no PTY, so terminal_data would be dropped.)
                    await agent_ws.send_text(json.dumps({
                        "type": "send_keys",
                        "pane_id": pane_id,
                        "keys": data.get("data", ""),
                    }))
                else:
                    # PTY terminal channel: forward input to the pty.
                    await agent_ws.send_text(json.dumps({
                        "type": "terminal_data",
                        "channel": channel,
                        "data": data.get("data", ""),
                    }))
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("Browser terminal error: %s/%s", machine, channel)
        finally:
            self.browser_channels.pop(channel, None)
            self.channel_machines.pop(channel, None)
            # Tell agent to close
            try:
                close_type = "pane_detach" if pane_id else "terminal_close"
                await agent_ws.send_text(json.dumps({
                    "type": close_type,
                    "channel": channel,
                }))
            except Exception:
                pass
            # Record close
            with db() as conn:
                conn.execute(
                    "UPDATE terminal_sessions SET closed_at = datetime('now') WHERE channel_id = ?",
                    (channel,),
                )

    # ── Send to agent ────────────────────────────────────────────────────

    async def send_to_agent(self, machine: str, message: dict) -> bool:
        """Send a message to a connected agent. Returns False if not connected."""
        ws = self.agents.get(machine)
        if not ws:
            return False
        try:
            await ws.send_text(json.dumps(message))
            return True
        except Exception:
            return False

    async def run_agent_action(self, machine: str, action_type: str,
                               payload: dict, timeout: float = 35.0) -> dict:
        """Send an action to a machine's agent and await its action_result.

        Returns {"status", "result"}. Raises if the agent is not connected or
        does not reply in time.
        """
        if machine not in self.agents:
            raise RuntimeError(f"agent '{machine}' not connected")
        action_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending_actions[action_id] = fut
        try:
            sent = await self.send_to_agent(machine, {
                "type": "action",
                "action_type": action_type,
                "payload": json.dumps(payload),
                "action_id": action_id,
            })
            if not sent:
                raise RuntimeError(f"failed to send action to '{machine}'")
            result = await asyncio.wait_for(fut, timeout=timeout)
            return {"status": result.get("status", "error"),
                    "result": result.get("result", "")}
        finally:
            self.pending_actions.pop(action_id, None)

    async def request_tmux_list(self, machine: str) -> Optional[list]:
        """Request tmux list from agent and return cached result."""
        if machine not in self.agents:
            return None
        await self.send_to_agent(machine, {"type": "tmux_list"})
        # Give agent a moment to respond
        await asyncio.sleep(0.5)
        return self.tmux_cache.get(machine, [])

    def get_claude_sessions(self) -> list:
        """Get all Claude Code sessions across all machines."""
        result = []
        for machine, sessions in self.claude_cache.items():
            for s in sessions:
                s["machine"] = machine
                result.append(s)
        return result


# Singleton
hub = AgentHub()

"""
cc_wrapper.py — Claude Code stream-json wrapper for rialu-agent.

Spawns `claude --output-format stream-json --verbose` as a subprocess,
parses the newline-delimited JSON output, and emits events to Rialú
via POST /api/agents/{agent_id}/event.

Usage:
    python3 cc_wrapper.py --project-id 1 --cwd /home/Projects/legion --prompt "fix the bug in main.py"

Or import and use programmatically:
    from cc_wrapper import CCSession
    session = CCSession(agent_id="daisy", project_id=1, rialu_base="https://rialu.fly.dev")
    await session.run(prompt="fix the bug", cwd="/home/Projects/legion")
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone


class CCSession:
    def __init__(
        self,
        agent_id: str = "daisy",
        project_id: int = 1,
        rialu_base: str = "https://rialu.ie",
        agent_key: str = "",
        require_approval_for: list[str] | None = None,
        auto_approve_rules: list[dict] | None = None,
    ):
        self.agent_id = agent_id
        self.project_id = project_id
        self.rialu_base = rialu_base.rstrip("/")
        self.agent_key = agent_key or os.environ.get("RIALU_AGENT_KEY", "")
        self.require_approval_for = require_approval_for or []
        self.auto_approve_rules = auto_approve_rules or []
        self.process = None
        self.session_id = None
        self.total_cost = 0.0

    def _sign(self, body: bytes) -> dict:
        """Generate HMAC-SHA256 signature header."""
        if not self.agent_key:
            return {}
        sig = "sha256=" + hmac.new(
            self.agent_key.encode(), body, hashlib.sha256
        ).hexdigest()
        return {"X-Rialu-Sig": sig}

    async def _emit(self, event_type: str, payload: dict):
        """POST an event to Rialú."""
        import httpx

        body = json.dumps({
            "event_type": event_type,
            "project_id": self.project_id,
            "payload": payload,
        }).encode()
        headers = {"Content-Type": "application/json", **self._sign(body)}

        # Add CF Access headers if configured
        cf_id = os.environ.get("CF_ACCESS_CLIENT_ID", "")
        cf_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")
        if cf_id and cf_secret:
            headers["CF-Access-Client-Id"] = cf_id
            headers["CF-Access-Client-Secret"] = cf_secret

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self.rialu_base}/api/agents/{self.agent_id}/event",
                    content=body,
                    headers=headers,
                )
                if resp.status_code != 201:
                    print(f"[cc_wrapper] emit {event_type}: HTTP {resp.status_code} {resp.text[:100]}", file=sys.stderr)
                else:
                    print(f"[cc_wrapper] emit {event_type} ok", file=sys.stderr)
        except Exception as e:
            print(f"[cc_wrapper] emit error: {e}", file=sys.stderr)

    async def _check_approval(self, tool_name: str, tool_args: dict) -> bool:
        """Check auto-approve rules, or create a decision and wait for response."""
        # Check auto-approve rules first
        for rule in self.auto_approve_rules:
            if rule.get("tool") == tool_name:
                match_pattern = rule.get("match", "")
                args_str = json.dumps(tool_args)
                import re
                if re.search(match_pattern, args_str):
                    if rule.get("action") == "approve":
                        print(f"[cc_wrapper] auto-approved: {tool_name}", file=sys.stderr)
                        return True

        # Create a decision
        import httpx

        decision_payload = json.dumps({
            "project_id": self.project_id,
            "trigger_type": "ai_approval",
            "priority": 3,
            "timeout_secs": 300,
            "payload": {
                "summary": f"Claude Code wants to use tool: {tool_name}",
                "project": {"id": str(self.project_id), "name": f"Project #{self.project_id}"},
                "agent": {"id": self.agent_id, "name": self.agent_id, "machine": self.agent_id},
                "current_state": {"tool_name": tool_name, "tool_args": tool_args},
                "proposed_state": {"action": "execute tool", "risk_level": "medium"},
            },
        }).encode()

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self.rialu_base}/api/decisions",
                    content=decision_payload,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code != 201:
                    print(f"[cc_wrapper] decision create failed: {resp.status_code}", file=sys.stderr)
                    return True  # fail open

                decision = resp.json()
                decision_id = decision["id"]
                print(f"[cc_wrapper] decision created: {decision_id}, waiting...", file=sys.stderr)

                # Poll for resolution
                for _ in range(300):  # 5 minutes max
                    await asyncio.sleep(1)
                    check = await client.get(
                        f"{self.rialu_base}/api/decisions/{decision_id}"
                    )
                    if check.status_code == 200:
                        d = check.json()
                        if d["status"] != "pending":
                            approved = d["status"] == "approved"
                            print(f"[cc_wrapper] decision {d['status']}: {decision_id}", file=sys.stderr)
                            return approved

                print(f"[cc_wrapper] decision timed out: {decision_id}", file=sys.stderr)
                return False

        except Exception as e:
            print(f"[cc_wrapper] decision error: {e}", file=sys.stderr)
            return True  # fail open

    def _needs_approval(self, tool_name: str) -> bool:
        """Check if this tool requires approval."""
        return tool_name in self.require_approval_for

    async def _handle_line(self, line: str):
        """Parse a stream-json line and emit the appropriate event."""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type", "")

        if msg_type == "system" and data.get("subtype") == "init":
            self.session_id = data.get("session_id")
            await self._emit("cc_init", {
                "session_id": self.session_id,
                "model": data.get("model"),
                "tools": data.get("tools", []),
            })

        elif msg_type == "assistant":
            message = data.get("message", {})
            content = message.get("content", [])
            usage = message.get("usage", {})

            for block in content:
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text.strip():
                        await self._emit("cc_text", {
                            "text": text,
                            "session_id": self.session_id,
                        })

                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    await self._emit("cc_tool_call", {
                        "tool_name": tool_name,
                        "tool_args": tool_input,
                        "tool_use_id": block.get("id"),
                        "session_id": self.session_id,
                    })

        elif msg_type == "result":
            cost = data.get("total_cost_usd", 0)
            self.total_cost = cost
            await self._emit("cc_cost_update", {
                "total_cost_usd": cost,
                "duration_ms": data.get("duration_ms"),
                "num_turns": data.get("num_turns"),
                "session_id": self.session_id,
                "result": data.get("result", "")[:500],
            })

    async def run(self, prompt: str, cwd: str = "."):
        """Spawn claude and process its stream-json output."""
        # Build command — include MCP config if available
        mcp_config = os.path.expanduser("~/.rialu/dream-mcp.json")
        cmd = ["claude", "--output-format", "stream-json", "--verbose"]
        if os.path.exists(mcp_config):
            cmd.extend(["--mcp-config", mcp_config])
        cmd.extend(["-p", prompt])
        print(f"[cc_wrapper] spawning: {' '.join(cmd)}", file=sys.stderr)
        print(f"[cc_wrapper] cwd: {cwd}", file=sys.stderr)

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

        try:
            async for line_bytes in self.process.stdout:
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                await self._handle_line(line)
        except Exception as e:
            await self._emit("cc_error", {"error": str(e)})
        finally:
            await self.process.wait()
            print(f"[cc_wrapper] done. cost=${self.total_cost:.4f}", file=sys.stderr)


async def main():
    parser = argparse.ArgumentParser(description="Claude Code stream-json wrapper")
    parser.add_argument("--agent-id", default="daisy")
    parser.add_argument("--project-id", type=int, default=1)
    parser.add_argument("--rialu-base", default="https://rialu.fly.dev")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--require-approval-for", nargs="*", default=[])
    args = parser.parse_args()

    session = CCSession(
        agent_id=args.agent_id,
        project_id=args.project_id,
        rialu_base=args.rialu_base,
        require_approval_for=args.require_approval_for,
    )
    await session.run(prompt=args.prompt, cwd=args.cwd)


if __name__ == "__main__":
    asyncio.run(main())

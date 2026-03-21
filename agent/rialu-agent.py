#!/usr/bin/env python3
"""
rialu-agent — machine agent daemon for Rialú.

Maintains a persistent WebSocket connection to the hub. Sends heartbeats,
enumerates tmux sessions, detects Claude Code prompts, and spawns pty
shells for browser terminals.

Env vars (from /etc/rialu-agent.env):
  RIALU_HUB_URL         — e.g. https://rialu.fly.dev (http scheme, WS derived)
  RIALU_AGENT_KEY       — shared HMAC secret
  RIALU_MACHINE_NAME    — e.g. daisy
  RIALU_HEARTBEAT_INTERVAL — seconds between heartbeats (default 30)
"""

import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import os
import pty
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path

import psutil

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [rialu-agent] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rialu-agent")

# ── Config ───────────────────────────────────────────────────────────────────

HUB_URL = os.environ.get("RIALU_HUB_URL", "https://rialu.fly.dev").rstrip("/")
AGENT_KEY = os.environ.get("RIALU_AGENT_KEY", "")
MACHINE_NAME = os.environ.get("RIALU_MACHINE_NAME", "unknown")
HEARTBEAT_INTERVAL = int(os.environ.get("RIALU_HEARTBEAT_INTERVAL", "30"))
TMUX_POLL_INTERVAL = 5  # seconds between tmux scans
CLAUDE_POLL_INTERVAL = 3  # seconds between Claude Code checks


def ws_url() -> str:
    """Derive WebSocket URL from HTTP URL."""
    url = HUB_URL.replace("https://", "wss://").replace("http://", "ws://")
    return url + "/ws/agent"


def load_config() -> dict:
    """Load config. Search order: alongside this script, /etc/, home dir."""
    script_dir = Path(__file__).parent
    for path in [
        script_dir / "rialu-agent.json",
        Path("/etc/rialu-agent.json"),
        Path.home() / ".rialu-agent.json",
    ]:
        if path.exists():
            try:
                with open(path) as f:
                    log.info("Config loaded from %s", path)
                    return json.load(f)
            except Exception as e:
                log.warning("Failed to load config from %s: %s", path, e)
    return {}


CONFIG = load_config()
KNOWN_PROJECTS = CONFIG.get("projects", [])
REPO_DIRS = [Path(p) for p in CONFIG.get("repo_dirs", [])]
GIT_AUTHOR = CONFIG.get("git_author", "todd")
COMMIT_LOOKBACK_HOURS = CONFIG.get("commit_lookback_hours", 24)

# Claude Code pause patterns
CLAUDE_PATTERNS = [
    "Do you want to proceed?",
    "Press Enter to confirm",
    "Press Escape to cancel",
    "Continue? [Y/n]",
    "Allow",
    "? (y/n)",
    "approve or deny",
]


# ── Auth ─────────────────────────────────────────────────────────────────────

def make_auth_message() -> dict:
    ts = int(time.time())
    msg = f"{MACHINE_NAME}:{ts}".encode()
    sig = "sha256=" + hmac.new(AGENT_KEY.encode(), msg, hashlib.sha256).hexdigest()
    return {"type": "auth", "machine": MACHINE_NAME, "ts": ts, "sig": sig}


# ── Resource collection ──────────────────────────────────────────────────────

def get_cpu_pct() -> float:
    return psutil.cpu_percent(interval=1)


def get_ram_pct() -> float:
    return psutil.virtual_memory().percent


def get_gpu_pct():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return float(result.stdout.strip().split("\n")[0])
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


# ── Process filtering ────────────────────────────────────────────────────────

def get_filtered_processes() -> list:
    if not KNOWN_PROJECTS:
        return []
    lower_projects = {p.lower() for p in KNOWN_PROJECTS}
    result = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = proc.info
            name = (info["name"] or "").lower()
            cmdline = " ".join(info["cmdline"] or []).lower()
            matched_project = None
            for proj in lower_projects:
                if proj in name or proj in cmdline:
                    matched_project = proj
                    break
            if matched_project:
                uptime_s = int(time.time() - (info["create_time"] or time.time()))
                script = ""
                cmdparts = info["cmdline"] or []
                for part in cmdparts:
                    if part.endswith((".py", ".sh", ".js")):
                        script = os.path.basename(part)
                        break
                result.append({
                    "name": matched_project,
                    "script": script,
                    "pid": info["pid"],
                    "uptime_s": uptime_s,
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return result


# ── Git repo scanning ────────────────────────────────────────────────────────

def _run_git(repo_path: Path, args: list) -> str:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(repo_path),
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _get_recent_commits(repo_path: Path) -> list:
    """Get commits from the last COMMIT_LOOKBACK_HOURS hours by GIT_AUTHOR."""
    raw = _run_git(repo_path, [
        "log",
        f"--since={COMMIT_LOOKBACK_HOURS} hours ago",
        f"--author={GIT_AUTHOR}",
        "--format=%H|%s|%aI",
        "--no-merges",
    ])
    if not raw:
        return []
    commits = []
    for line in raw.strip().split("\n"):
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({
                "hash": parts[0][:7],
                "message": parts[1],
                "timestamp": parts[2],
            })
    return commits


def scan_repos() -> list:
    if not REPO_DIRS:
        return []
    repos = []
    entries = []
    for d in REPO_DIRS:
        if not d.is_dir():
            continue
        entries.extend(sorted(d.iterdir()))
    for entry in entries:
        git_dir = entry / ".git"
        if not entry.is_dir() or not git_dir.exists():
            continue
        branch = _run_git(entry, ["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown"
        porcelain = _run_git(entry, ["status", "--porcelain"])
        clean = porcelain == ""
        log_line = _run_git(entry, ["log", "-1", "--format=%H %s"])
        last_commit = ""
        last_message = ""
        if log_line:
            parts = log_line.split(" ", 1)
            last_commit = parts[0][:7] if parts else ""
            last_message = parts[1] if len(parts) > 1 else ""
        ahead = 0
        behind = 0
        tracking = _run_git(entry, ["rev-parse", "--abbrev-ref", "@{upstream}"])
        if tracking:
            ab = _run_git(entry, ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
            if ab:
                parts = ab.split()
                if len(parts) == 2:
                    ahead = int(parts[0])
                    behind = int(parts[1])
        repos.append({
            "name": entry.name,
            "path": str(entry),
            "branch": branch,
            "clean": clean,
            "ahead": ahead,
            "behind": behind,
            "last_commit": last_commit,
            "last_message": last_message,
            "recent_commits": _get_recent_commits(entry),
        })
    return repos


# ── tmux enumeration ─────────────────────────────────────────────────────────

def _run_cmd(args: list, timeout: int = 5) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def enumerate_tmux() -> list:
    """List all tmux sessions and their panes."""
    sessions_raw = _run_cmd([
        "tmux", "list-sessions", "-F",
        "#{session_name}\t#{session_windows}\t#{session_attached}"
    ])
    if not sessions_raw:
        return []

    sessions = []
    for line in sessions_raw.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        sname, wins, attached = parts[0], int(parts[1]), int(parts[2])

        # Get panes for this session
        panes_raw = _run_cmd([
            "tmux", "list-panes", "-s", "-t", sname, "-F",
            "#{window_index}\t#{pane_index}\t#{pane_pid}\t#{pane_current_command}\t#{pane_width}\t#{pane_height}"
        ])
        panes = []
        if panes_raw:
            for pline in panes_raw.strip().split("\n"):
                pp = pline.split("\t")
                if len(pp) < 6:
                    continue
                pane_id = f"{sname}:{pp[0]}.{pp[1]}"
                panes.append({
                    "pane_id": pane_id,
                    "window": int(pp[0]),
                    "pane": int(pp[1]),
                    "pid": int(pp[2]),
                    "command": pp[3],
                    "width": int(pp[4]),
                    "height": int(pp[5]),
                })
        sessions.append({
            "name": sname,
            "windows": wins,
            "attached": attached > 0,
            "panes": panes,
        })
    return sessions


# ── Claude Code detection ────────────────────────────────────────────────────

def detect_claude_sessions(tmux_sessions: list) -> list:
    """Check tmux panes for Claude Code activity."""
    claude_sessions = []
    for session in tmux_sessions:
        for pane in session.get("panes", []):
            # Check if the command is claude
            cmd = pane.get("command", "").lower()
            is_claude = "claude" in cmd

            if not is_claude:
                # Check pane content for claude signatures
                content = _run_cmd([
                    "tmux", "capture-pane", "-t", pane["pane_id"],
                    "-p", "-S", "-30"
                ])
                if content and ("claude" in content.lower() or "anthropic" in content.lower()):
                    is_claude = True

            if is_claude:
                # Get last lines to check for waiting state
                last_lines = _run_cmd([
                    "tmux", "capture-pane", "-t", pane["pane_id"],
                    "-p", "-S", "-20"
                ])
                lines = last_lines.strip().split("\n") if last_lines else []

                # Check for waiting patterns
                claude_state = "running"
                waiting_prompt = None
                last_20 = "\n".join(lines[-20:]) if lines else ""
                for pattern in CLAUDE_PATTERNS:
                    if pattern.lower() in last_20.lower():
                        claude_state = "waiting"
                        waiting_prompt = pattern
                        break

                claude_sessions.append({
                    "pane_id": pane["pane_id"],
                    "session": session["name"],
                    "window": pane["window"],
                    "pane": pane["pane"],
                    "pid": pane["pid"],
                    "is_claude": True,
                    "claude_state": claude_state,
                    "last_lines": lines[-10:] if lines else [],
                    "waiting_prompt": waiting_prompt,
                })
    return claude_sessions


# ── Terminal (pty) management ────────────────────────────────────────────────

# channel_id -> {"fd": master_fd, "pid": child_pid, "task": asyncio.Task}
terminals: dict = {}


async def open_terminal(ws, channel: str):
    """Spawn a bash shell with a pty, pipe output to WebSocket."""
    master_fd, slave_fd = pty.openpty()
    pid = os.fork()
    if pid == 0:
        # Child process
        os.setsid()
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        os.close(master_fd)
        os.close(slave_fd)
        os.environ["TERM"] = "xterm-256color"
        os.execvp("/bin/bash", ["/bin/bash", "--login"])
    else:
        # Parent
        os.close(slave_fd)
        os.set_blocking(master_fd, False)
        task = asyncio.create_task(pty_reader(ws, channel, master_fd, pid))
        terminals[channel] = {"fd": master_fd, "pid": pid, "task": task}
        log.info("Terminal opened: channel=%s pid=%d", channel, pid)


async def pty_reader(ws, channel: str, fd: int, pid: int):
    """Read from pty and send to hub via WebSocket."""
    loop = asyncio.get_event_loop()
    try:
        while True:
            # Use run_in_executor for blocking read
            try:
                data = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: os.read(fd, 4096)),
                    timeout=0.5,
                )
                if not data:
                    break
                await ws.send(json.dumps({
                    "type": "terminal_data",
                    "channel": channel,
                    "data": data.decode("utf-8", errors="replace"),
                }))
            except asyncio.TimeoutError:
                # Check if process is still alive
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    break
            except OSError:
                break
    except asyncio.CancelledError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        terminals.pop(channel, None)
        try:
            await ws.send(json.dumps({
                "type": "terminal_closed",
                "channel": channel,
            }))
        except Exception:
            pass
        log.info("Terminal closed: channel=%s", channel)


def write_terminal(channel: str, data: str):
    """Write data to a terminal's pty."""
    t = terminals.get(channel)
    if t:
        try:
            os.write(t["fd"], data.encode("utf-8"))
        except OSError:
            pass


def resize_terminal(channel: str, cols: int, rows: int):
    """Resize a terminal's pty."""
    t = terminals.get(channel)
    if t:
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(t["fd"], termios.TIOCSWINSZ, winsize)
        except OSError:
            pass


def close_terminal(channel: str):
    """Close a terminal."""
    t = terminals.pop(channel, None)
    if t:
        t["task"].cancel()


# ── tmux pane streaming ─────────────────────────────────────────────────────

# channel_id -> {"pane_id": str, "task": asyncio.Task}
pane_streams: dict = {}


async def attach_pane(ws, channel: str, pane_id: str):
    """Stream tmux pane output to hub."""
    task = asyncio.create_task(pane_streamer(ws, channel, pane_id))
    pane_streams[channel] = {"pane_id": pane_id, "task": task}
    log.info("Pane attached: channel=%s pane=%s", channel, pane_id)


async def pane_streamer(ws, channel: str, pane_id: str):
    """Continuously capture pane content and send diffs."""
    last_content = ""
    try:
        while True:
            content = _run_cmd(["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-100"])
            if content != last_content:
                await ws.send(json.dumps({
                    "type": "pane_data",
                    "channel": channel,
                    "data": content,
                }))
                last_content = content
            await asyncio.sleep(0.3)
    except asyncio.CancelledError:
        pass
    finally:
        pane_streams.pop(channel, None)
        log.info("Pane detached: channel=%s", channel)


def detach_pane(channel: str):
    s = pane_streams.pop(channel, None)
    if s:
        s["task"].cancel()


def send_tmux_keys(pane_id: str, keys: str):
    """Send keystrokes to a tmux pane."""
    _run_cmd(["tmux", "send-keys", "-t", pane_id, keys])


# ── Main loop ────────────────────────────────────────────────────────────────

async def heartbeat_loop(ws):
    """Send heartbeat with system stats every HEARTBEAT_INTERVAL seconds."""
    while True:
        try:
            payload = {
                "type": "heartbeat",
                "machine": MACHINE_NAME,
                "cpu_pct": get_cpu_pct(),
                "ram_pct": get_ram_pct(),
                "gpu_pct": get_gpu_pct(),
                "processes": get_filtered_processes(),
                "repos": scan_repos(),
            }
            await ws.send(json.dumps(payload))
            log.info("Heartbeat sent — cpu=%.1f%% ram=%.1f%% repos=%d",
                     payload["cpu_pct"], payload["ram_pct"], len(payload["repos"]))
        except Exception:
            log.exception("Heartbeat error")
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def tmux_monitor_loop(ws):
    """Periodically enumerate tmux sessions and send to hub."""
    while True:
        try:
            sessions = enumerate_tmux()
            await ws.send(json.dumps({
                "type": "tmux_list",
                "machine": MACHINE_NAME,
                "sessions": sessions,
            }))

            # Claude Code detection
            claude_sessions = detect_claude_sessions(sessions)
            if claude_sessions:
                await ws.send(json.dumps({
                    "type": "claude_status",
                    "machine": MACHINE_NAME,
                    "sessions": claude_sessions,
                }))
        except Exception:
            log.exception("tmux monitor error")
        await asyncio.sleep(TMUX_POLL_INTERVAL)


async def receive_loop(ws):
    """Handle incoming messages from hub."""
    async for raw in ws:
        try:
            data = json.loads(raw)
            msg_type = data.get("type", "")

            if msg_type == "terminal_open":
                await open_terminal(ws, data["channel"])

            elif msg_type == "terminal_data":
                write_terminal(data["channel"], data.get("data", ""))

            elif msg_type == "terminal_close":
                close_terminal(data["channel"])

            elif msg_type == "terminal_resize":
                resize_terminal(data["channel"], data.get("cols", 80), data.get("rows", 24))

            elif msg_type == "pane_attach":
                await attach_pane(ws, data["channel"], data["pane_id"])

            elif msg_type == "pane_detach":
                detach_pane(data["channel"])

            elif msg_type == "send_keys":
                send_tmux_keys(data["pane_id"], data["keys"])

            elif msg_type == "tmux_list":
                sessions = enumerate_tmux()
                await ws.send(json.dumps({
                    "type": "tmux_list",
                    "machine": MACHINE_NAME,
                    "sessions": sessions,
                }))

            elif msg_type == "action":
                await handle_action(ws, data)

        except Exception:
            log.exception("Error handling message: %s", raw[:200])


async def handle_action(ws, data: dict):
    """Execute an action (git pull, etc.) and send result back."""
    action_type = data.get("action_type", "")
    payload = data.get("payload", "")
    action_id = data.get("action_id")
    result = "unknown"
    status = "error"

    try:
        if action_type == "git_pull":
            p = json.loads(payload) if payload else {}
            repo_path = p.get("path", "")
            if repo_path and Path(repo_path).is_dir():
                output = _run_cmd(["git", "-C", repo_path, "pull"], timeout=30)
                result = output or "No output"
                status = "success"
            else:
                result = f"Invalid repo path: {repo_path}"

        elif action_type == "process_kill":
            p = json.loads(payload) if payload else {}
            pid = p.get("pid")
            if pid:
                os.kill(int(pid), signal.SIGTERM)
                result = f"SIGTERM sent to {pid}"
                status = "success"

        else:
            result = f"Unknown action type: {action_type}"

    except Exception as e:
        result = str(e)

    if action_id:
        await ws.send(json.dumps({
            "type": "action_result",
            "action_id": action_id,
            "status": status,
            "result": result,
        }))


async def run():
    """Main connection loop with auto-reconnect."""
    import websockets

    url = ws_url()
    log.info("rialu-agent starting — machine=%s hub=%s", MACHINE_NAME, url)

    if not AGENT_KEY:
        log.error("RIALU_AGENT_KEY not set — exiting")
        return

    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=10,
                max_size=2**20,
            ) as ws:
                # Authenticate
                auth = make_auth_message()
                await ws.send(json.dumps(auth))
                log.info("Connected and authenticated")

                # Run all loops concurrently
                tasks = [
                    asyncio.create_task(heartbeat_loop(ws)),
                    asyncio.create_task(tmux_monitor_loop(ws)),
                    asyncio.create_task(receive_loop(ws)),
                ]

                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_EXCEPTION,
                )
                for t in pending:
                    t.cancel()
                # Re-raise if a task failed
                for t in done:
                    if t.exception():
                        raise t.exception()

        except Exception as e:
            log.warning("Connection lost (%s), reconnecting in 5s...", e)
            # Clean up any open terminals/panes
            for ch in list(terminals.keys()):
                close_terminal(ch)
            for ch in list(pane_streams.keys()):
                detach_pane(ch)
            await asyncio.sleep(5)


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()

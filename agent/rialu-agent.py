#!/usr/bin/env python3
"""
rialu-agent — lightweight daemon that heartbeats to the Rialú hub.

Every 30 seconds, collects:
  - CPU / RAM / GPU utilisation
  - Filtered process list (only known project names)
  - Git repo states from the projects directory

Signs the payload with HMAC-SHA256 and POSTs to the hub.

Env vars (from /etc/rialu-agent.env or shell):
  RIALU_HUB_URL       — e.g. https://rialu.ie
  RIALU_AGENT_KEY     — shared HMAC secret
  RIALU_MACHINE_NAME  — e.g. daisy
"""

import hashlib
import hmac
import json
import logging
import os
import subprocess
import time
from pathlib import Path

import psutil
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [rialu-agent] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rialu-agent")

# ── Config ───────────────────────────────────────────────────────────────────

HUB_URL = os.environ.get("RIALU_HUB_URL", "https://rialu.ie").rstrip("/")
AGENT_KEY = os.environ.get("RIALU_AGENT_KEY", "")
MACHINE_NAME = os.environ.get("RIALU_MACHINE_NAME", "unknown")
HEARTBEAT_INTERVAL = int(os.environ.get("RIALU_HEARTBEAT_INTERVAL", "30"))

def load_config() -> dict:
    """Load config from ~/.rialu-agent.json or /etc/rialu-agent.json."""
    for path in [Path.home() / ".rialu-agent.json", Path("/etc/rialu-agent.json")]:
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception as e:
                log.warning("Failed to load config from %s: %s", path, e)
    return {}


CONFIG = load_config()
# List of project names to match against running processes
KNOWN_PROJECTS = CONFIG.get("projects", [])
# Directories to scan for git repos — explicit list from config
REPO_DIRS = [Path(p) for p in CONFIG.get("repo_dirs", [])]


# ── Resource collection ──────────────────────────────────────────────────────

def get_cpu_pct() -> float:
    return psutil.cpu_percent(interval=1)


def get_ram_pct() -> float:
    return psutil.virtual_memory().percent


def get_gpu_pct():
    """Query nvidia-smi for GPU utilisation. Returns None if unavailable."""
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
    """Return processes whose name matches a known project name."""
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
                # Try to extract script name from cmdline
                script = ""
                cmdparts = info["cmdline"] or []
                for part in cmdparts:
                    if part.endswith(".py") or part.endswith(".sh") or part.endswith(".js"):
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
    """Run a git command in a repo, return stdout or empty string on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(repo_path),
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def scan_repos() -> list:
    """Scan configured repo_dirs for git repos and return their state."""
    if not REPO_DIRS:
        log.warning("No repo_dirs configured in agent config — skipping repo scan")
        return []
    repos = []
    entries = []
    for d in REPO_DIRS:
        if not d.is_dir():
            log.warning("repo_dirs path not found: %s", d)
            continue
        entries.extend(sorted(d.iterdir()))
    for entry in entries:
        git_dir = entry / ".git"
        if not entry.is_dir() or not git_dir.exists():
            continue
        name = entry.name

        # Branch
        branch = _run_git(entry, ["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown"

        # Clean/dirty
        porcelain = _run_git(entry, ["status", "--porcelain"])
        clean = porcelain == ""

        # Last commit
        log_line = _run_git(entry, ["log", "-1", "--format=%H %s"])
        last_commit = ""
        last_message = ""
        if log_line:
            parts = log_line.split(" ", 1)
            last_commit = parts[0][:7] if parts else ""
            last_message = parts[1] if len(parts) > 1 else ""

        # Ahead/behind — needs a remote tracking branch
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
            "name": name,
            "path": str(entry),
            "branch": branch,
            "clean": clean,
            "ahead": ahead,
            "behind": behind,
            "last_commit": last_commit,
            "last_message": last_message,
        })
    return repos


# ── Heartbeat ────────────────────────────────────────────────────────────────

def build_heartbeat() -> dict:
    return {
        "machine": MACHINE_NAME,
        "cpu_pct": get_cpu_pct(),
        "ram_pct": get_ram_pct(),
        "gpu_pct": get_gpu_pct(),
        "processes": get_filtered_processes(),
        "repos": scan_repos(),
    }


def sign_payload(body: bytes) -> str:
    return "sha256=" + hmac.new(AGENT_KEY.encode(), body, hashlib.sha256).hexdigest()


def send_heartbeat():
    payload = build_heartbeat()
    body = json.dumps(payload).encode()
    sig = sign_payload(body)
    url = f"{HUB_URL}/api/agent/heartbeat"
    try:
        resp = requests.post(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Rialu-Sig": sig,
            },
            timeout=10,
        )
        if resp.status_code == 202:
            log.info("Heartbeat sent (%s) — cpu=%.1f%% ram=%.1f%% repos=%d",
                     MACHINE_NAME, payload["cpu_pct"], payload["ram_pct"], len(payload["repos"]))
        else:
            log.warning("Hub returned %d: %s", resp.status_code, resp.text[:200])
    except requests.RequestException as e:
        log.warning("Heartbeat failed: %s", e)


# ── Main loop ────────────────────────────────────────────────────────────────

def main():
    log.info("rialu-agent starting — machine=%s hub=%s interval=%ds",
             MACHINE_NAME, HUB_URL, HEARTBEAT_INTERVAL)
    if not AGENT_KEY:
        log.error("RIALU_AGENT_KEY not set — exiting")
        return
    if not KNOWN_PROJECTS:
        log.warning("No projects configured — process filtering disabled")

    while True:
        try:
            send_heartbeat()
        except Exception:
            log.exception("Unexpected error in heartbeat loop")
        time.sleep(HEARTBEAT_INTERVAL)


if __name__ == "__main__":
    main()

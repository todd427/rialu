"""
tests/test_pty_terminal.py — Regression tests for the agent's pty terminal.

Guards the bug that made the Machines-tab terminal unusable: the pty master fd
is non-blocking, so a read with nothing buffered raises BlockingIOError, and
BlockingIOError subclasses OSError. The old reader loop caught it under a bare
`except OSError: break`, so the session tore itself down on its very first read
— before bash had written its prompt. The socket connected, then immediately
returned terminal_closed.

These drive the real open_terminal/write_terminal/close_terminal against a fake
hub WebSocket, spawning actual bash processes.
"""

import asyncio
import importlib.util
import json
import os
import sys
import types

import pytest

AGENT_PATH = os.path.join(os.path.dirname(__file__), "..", "agent", "rialu-agent.py")


@pytest.fixture(scope="module")
def agent():
    """Load rialu-agent.py — hyphenated, so it needs an explicit spec.

    psutil is a module-level import used only by the heartbeat path, which
    none of these tests exercise. Stub it so the suite runs in the app venv
    (which has no psutil) rather than skipping the regression guard entirely.
    """
    injected = "psutil" not in sys.modules
    if injected:
        sys.modules["psutil"] = types.ModuleType("psutil")
    try:
        spec = importlib.util.spec_from_file_location("rialu_agent", AGENT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        if injected:
            sys.modules.pop("psutil", None)


class FakeWS:
    """Stands in for the agent's hub WebSocket, recording what it is sent."""

    def __init__(self):
        self.msgs = []

    async def send(self, raw):
        self.msgs.append(json.loads(raw))

    def text(self):
        return "".join(
            m.get("data", "") for m in self.msgs if m["type"] == "terminal_data"
        )

    def closed(self):
        return any(m["type"] == "terminal_closed" for m in self.msgs)


async def _until(pred, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.asyncio
async def test_session_survives_and_streams(agent):
    """The regression: the shell must outlive its first read and stay open."""
    ws = FakeWS()
    await agent.open_terminal(ws, "t-survives")
    try:
        assert await _until(lambda: len(ws.text()) > 0), "no prompt from bash"

        # The old code closed within milliseconds of spawning.
        await asyncio.sleep(1.5)
        assert not ws.closed(), "session closed itself before any input"

        agent.write_terminal("t-survives", "echo round_trip_ok\n")
        assert await _until(lambda: "round_trip_ok" in ws.text())
    finally:
        agent.close_terminal("t-survives")


@pytest.mark.asyncio
async def test_resize_applies(agent):
    ws = FakeWS()
    await agent.open_terminal(ws, "t-resize")
    try:
        await _until(lambda: len(ws.text()) > 0)
        agent.resize_terminal("t-resize", 120, 40)
        agent.write_terminal("t-resize", "tput cols\n")
        assert await _until(lambda: "120" in ws.text())
    finally:
        agent.close_terminal("t-resize")


@pytest.mark.asyncio
async def test_burst_arrives_in_order(agent):
    """One pump per terminal, so a flood must not interleave or drop chunks."""
    ws = FakeWS()
    await agent.open_terminal(ws, "t-burst")
    try:
        await _until(lambda: len(ws.text()) > 0)
        agent.write_terminal("t-burst", "seq 1 5000\n")
        assert await _until(lambda: "\n5000" in ws.text(), timeout=20)

        # Whole lines only — the echoed command line would otherwise contribute
        # stray 1 and 5000 tokens and fake a disorder.
        lines = [l.strip() for l in ws.text().replace("\r", "\n").split("\n")]
        assert [int(l) for l in lines if l.isdigit()] == list(range(1, 5001))
    finally:
        agent.close_terminal("t-burst")


@pytest.mark.asyncio
async def test_shell_exit_closes_session(agent):
    """Dropping our slave fd means the master reports EOF when bash exits."""
    ws = FakeWS()
    await agent.open_terminal(ws, "t-exit")
    await _until(lambda: len(ws.text()) > 0)
    agent.write_terminal("t-exit", "exit\n")
    assert await _until(lambda: ws.closed()), "shell exit not detected"
    assert "t-exit" not in agent.terminals, "terminal entry leaked"


@pytest.mark.asyncio
async def test_close_terminal_reports_and_cleans_up(agent):
    ws = FakeWS()
    await agent.open_terminal(ws, "t-close")
    await _until(lambda: len(ws.text()) > 0)
    agent.close_terminal("t-close")
    assert await _until(lambda: ws.closed())
    assert "t-close" not in agent.terminals, "terminal entry leaked"

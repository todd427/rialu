"""
tests/test_teas_telemetry.py — Per-die telemetry and the viewer socket.

Covers docs/cc-brief-teas-per-die-telemetry.md: the payload carries one entry
per GPU (lily has two, rose has none), CPU temperature is optional (the WSL2
hosts report none), the legacy scalars Faire renders keep working through a
rolling fleet upgrade, and /ws/viewer is strictly read-only.
"""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from main import app
from db import init_db, db

client = TestClient(app)

AGENT_KEY = "test-secret-key-1234"


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setenv("RIALU_AGENT_KEY", AGENT_KEY)
    init_db()


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(AGENT_KEY.encode(), body, hashlib.sha256).hexdigest()


def _post(payload):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/agent/heartbeat",
        content=body,
        headers={"Content-Type": "application/json", "X-Rialu-Sig": _sign(body)},
    )


def _gpu(i, load, temp, name="RTX 4090"):
    return {"i": i, "name": name, "load_pct": load, "temp_c": temp,
            "mem_used_mb": 1024, "mem_total_mb": 24564}


def _machine(name):
    return next(m for m in client.get("/api/machines").json()
                if m["machine_name"] == name)


# ── Payload shape ────────────────────────────────────────────────────────────

def test_no_gpu_machine_returns_empty_list_not_null():
    """rose has no GPU. Absent means [], never null — consumers iterate."""
    assert _post({"machine": "rose", "cpu": {"load_pct": 10.0, "temp_c": 44.0},
                  "ram_pct": 30.0, "gpus": []}).status_code == 202
    m = _machine("rose")
    assert m["gpus"] == []
    assert m["gpus"] is not None


def test_two_gpus_round_trip_in_index_order():
    """lily has two cards; the old [0] indexing made the second invisible."""
    assert _post({"machine": "lily", "cpu": {"load_pct": 55.0, "temp_c": 62.0},
                  "ram_pct": 48.2,
                  "gpus": [_gpu(0, 82.0, 74.0), _gpu(1, 63.0, 66.0)]}).status_code == 202
    gpus = _machine("lily")["gpus"]
    assert len(gpus) == 2
    assert [g["i"] for g in gpus] == [0, 1]
    assert [g["load_pct"] for g in gpus] == [82.0, 63.0]
    assert [g["temp_c"] for g in gpus] == [74.0, 66.0]


def test_null_cpu_temp_accepted_and_returned():
    """WSL2 hosts expose no sensor. That must not fail the heartbeat."""
    assert _post({"machine": "lava", "cpu": {"load_pct": 5.0, "temp_c": None},
                  "ram_pct": 20.0, "gpus": []}).status_code == 202
    m = _machine("lava")
    assert m["cpu"]["temp_c"] is None
    assert m["cpu"]["load_pct"] == 5.0


# ── Back-compat: Faire must render unchanged ─────────────────────────────────

def test_gpu_pct_is_max_not_mean():
    """A pegged card must not be averaged away by its idle neighbour."""
    _post({"machine": "lily", "cpu": {"load_pct": 1.0, "temp_c": 40.0},
           "ram_pct": 10.0, "gpus": [_gpu(0, 100.0, 80.0), _gpu(1, 0.0, 40.0)]})
    m = _machine("lily")
    assert m["gpu_pct"] == 100.0, "must be max(100, 0), not mean(50)"


def test_gpu_pct_null_with_no_gpus():
    _post({"machine": "rose", "cpu": {"load_pct": 1.0, "temp_c": 40.0},
           "ram_pct": 10.0, "gpus": []})
    assert _machine("rose")["gpu_pct"] is None


def test_legacy_heartbeat_still_accepted():
    """The fleet upgrades per-machine, so the old flat shape keeps arriving."""
    assert _post({"machine": "iris", "cpu_pct": 22.0, "ram_pct": 41.0,
                  "gpu_pct": 77.0, "processes": [], "repos": []}).status_code == 202
    m = _machine("iris")
    assert m["cpu_pct"] == 22.0
    assert m["gpu_pct"] == 77.0
    # Nothing per-die is known, so gpus stays empty rather than fabricated.
    assert m["gpus"] == []
    assert m["cpu"]["temp_c"] is None


def test_response_carries_both_shapes_at_once():
    _post({"machine": "daisy", "cpu": {"load_pct": 33.0, "temp_c": 51.0},
           "ram_pct": 44.0, "gpus": [_gpu(0, 12.0, 55.0)]})
    m = _machine("daisy")
    # Legacy scalars for Faire...
    assert m["cpu_pct"] == 33.0 and m["ram_pct"] == 44.0 and m["gpu_pct"] == 12.0
    # ...and the new shape for Teas, in the same response.
    assert m["cpu"] == {"load_pct": 33.0, "temp_c": 51.0}
    assert m["gpus"][0]["name"] == "RTX 4090"


# ── Viewer socket ────────────────────────────────────────────────────────────

def _viewer_auth(machine="teas-viewer"):
    ts = int(time.time())
    msg = f"{machine}:{ts}".encode()
    return {"type": "auth", "machine": machine, "ts": ts,
            "sig": "sha256=" + hmac.new(AGENT_KEY.encode(), msg,
                                        hashlib.sha256).hexdigest()}


def test_viewer_gets_snapshot_on_connect():
    """A widget opening mid-session paints immediately, not at the next beat."""
    _post({"machine": "daisy", "cpu": {"load_pct": 9.0, "temp_c": 50.0},
           "ram_pct": 11.0, "gpus": [_gpu(0, 5.0, 45.0)]})
    with client.websocket_connect("/ws/viewer") as ws:
        ws.send_json(_viewer_auth())
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        daisy = next(m for m in snap["machines"] if m["machine_name"] == "daisy")
        assert daisy["gpus"][0]["temp_c"] == 45.0


def test_viewer_receives_pushed_heartbeat():
    with client.websocket_connect("/ws/viewer") as ws:
        ws.send_json(_viewer_auth())
        assert ws.receive_json()["type"] == "snapshot"

        _post({"machine": "lily", "cpu": {"load_pct": 70.0, "temp_c": 78.0},
               "ram_pct": 50.0, "gpus": [_gpu(0, 91.0, 83.0), _gpu(1, 12.0, 55.0)]})

        beat = ws.receive_json()
        assert beat["type"] == "heartbeat"
        assert beat["machine_name"] == "lily"
        assert len(beat["gpus"]) == 2
        assert beat["gpu_pct"] == 91.0          # max, for Faire
        assert beat["cpu"]["temp_c"] == 78.0


def test_viewer_rejects_bad_auth():
    with client.websocket_connect("/ws/viewer") as ws:
        ws.send_json({"type": "auth", "machine": "teas", "ts": 1, "sig": "sha256=bad"})
        with pytest.raises(Exception):
            ws.receive_json()


def test_viewer_ping_is_allowed():
    with client.websocket_connect("/ws/viewer") as ws:
        ws.send_json(_viewer_auth())
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_viewer_cannot_reach_an_agent():
    """Security: a viewer is a sink. An action message must be refused.

    There is deliberately no branch on the viewer path that forwards anything
    to an agent, so this asserts both that the socket closes and that no agent
    action was queued as a side effect.
    """
    with db() as conn:
        before = conn.execute("SELECT COUNT(*) c FROM agent_actions").fetchone()["c"]

    with client.websocket_connect("/ws/viewer") as ws:
        ws.send_json(_viewer_auth())
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"type": "action", "machine": "daisy",
                      "action_type": "run_command", "payload": {"cmd": "rm -rf /"}})
        with pytest.raises(Exception):
            ws.receive_json()

    with db() as conn:
        after = conn.execute("SELECT COUNT(*) c FROM agent_actions").fetchone()["c"]
    assert after == before, "a viewer must never be able to queue an agent action"


# ── Agent-side: adaptive pacing and per-die parsing ──────────────────────────

import importlib.util
import os
import sys
import types


@pytest.fixture(scope="module")
def agent():
    """Load rialu-agent.py — hyphenated, so it needs an explicit spec.

    psutil is stubbed where absent (the app venv has none) so these run in the
    normal suite instead of skipping; nothing here touches the heartbeat path
    that uses it.
    """
    injected = "psutil" not in sys.modules
    if injected:
        sys.modules["psutil"] = types.ModuleType("psutil")
    try:
        path = os.path.join(os.path.dirname(__file__), "..", "agent", "rialu-agent.py")
        spec = importlib.util.spec_from_file_location("rialu_agent_teas", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        if injected:
            sys.modules.pop("psutil", None)


def test_crossing_warn_selects_fast_interval(agent):
    p = agent.HeartbeatPacer(warn_c=75, fast=2, slow=30)
    assert p.interval(60.0, 0.0) == 30
    assert p.interval(75.0, 10.0) == 2, "at the threshold counts as hot"
    assert p.hot


def test_hysteresis_holds_fast_just_below_warn(agent):
    """warn-4 must NOT restore the slow rate — that is the flap guard."""
    p = agent.HeartbeatPacer(warn_c=75, fast=2, slow=30)
    p.interval(80.0, 0.0)
    assert p.interval(71.0, 10.0) == 2
    assert p.interval(71.0, 999.0) == 2, "inside the band, cooldown never starts"


def test_cooldown_restores_slow_interval(agent):
    """Clear of the band, the slow rate returns only after the full cooldown."""
    p = agent.HeartbeatPacer(warn_c=75, fast=2, slow=30, cooldown=60)
    p.interval(80.0, 0.0)
    assert p.interval(69.0, 10.0) == 2, "cooldown starts, still fast"
    assert p.interval(69.0, 60.0) == 2, "50s elapsed — not yet"
    assert p.interval(69.0, 71.0) == 30, "61s elapsed — restored"
    assert not p.hot


def test_no_thermal_data_stays_slow(agent):
    """WSL2 reports nothing; there is no reason to escalate the rate."""
    p = agent.HeartbeatPacer(warn_c=75, fast=2, slow=30)
    assert p.interval(None, 0.0) == 30


def test_get_gpus_parses_every_card(agent, monkeypatch):
    """Two cards in, two cards out — the [0] bug is what this guards."""
    out = ("0, NVIDIA RTX 4090, 82, 74, 14220, 24564\n"
           "1, NVIDIA RTX 4090, 63, 66, 2110, 24564\n")
    monkeypatch.setattr(agent.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=out))
    gpus = agent.get_gpus()
    assert [g["i"] for g in gpus] == [0, 1]
    assert gpus[1]["mem_used_mb"] == 2110
    assert agent.gpu_pct_from(gpus) == 82.0


def test_get_gpus_empty_without_nvidia_smi(agent, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(agent.subprocess, "run", boom)
    assert agent.get_gpus() == []
    assert agent.gpu_pct_from([]) is None


def test_hottest_die_spans_cpu_and_gpus(agent):
    assert agent.hottest_die_c(51.0, [{"temp_c": 83.0}, {"temp_c": 60.0}]) == 83.0
    assert agent.hottest_die_c(None, [{"temp_c": 60.0}]) == 60.0
    assert agent.hottest_die_c(None, []) is None

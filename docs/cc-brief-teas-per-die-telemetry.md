# CC Brief — Rialú: per-die telemetry and adaptive heartbeat (for Teas)

**Project:** Rialú
**Date:** 20 August 2026
**Requested by:** the Teas design session — three viewer surfaces designed against a heartbeat that cannot feed them
**Status:** Implemented 21 Aug 2026 — see `tests/test_teas_telemetry.py` (19 tests)
**Consumer:** `teas` — Tauri desktop viewer (separate repo, not built yet)

---

## <span style="color:#1D9E75">Context</span>

`agent/rialu-agent.py` sends `cpu_pct`, `ram_pct`, and a single scalar `gpu_pct` every 30 seconds. `routers/machines.py` stores those four columns and `/api/machines` returns the latest row per machine. That shape has served Faire's three-gauge `.mcard` well and needs to change in two specific ways before a temperature monitor can exist on top of it.

**No temperatures.** Not one field in the payload. The agent already shells out to `nvidia-smi --query-gpu=utilization.gpu`; `temperature.gpu` is a comma away in the same query — `hosts/common/.local/bin/loads` in firstlight already asks for both in a single call. CPU temperature needs `psutil.sensors_temperatures()`, which is already an installed dependency.

**One GPU per machine, assumed structurally.** `gpu_pct` is a scalar column, and `get_gpu_pct()` explicitly discards everything after the first line:

```python
return float(result.stdout.strip().split("\n")[0])
```

Lily has two cards. Today the second one is invisible fleet-wide, and no amount of frontend work can recover it.

Fleet as of this brief: daisy 🌼, iris 🪻, rose 🌹, lava 🌋, lily 🪷 — glyphs per `hosts/common/.claude/statusline.sh` in firstlight. Rose has no GPU; lily has two. Any shape that assumes exactly one GPU per machine is wrong at both ends of that list.

---

## <span style="color:#1D9E75">Part 1 — The heartbeat interval is the real problem</span>

This is the part to get right, and it is not obvious from the payload shape.

Teas alerts when a die runs hot: warning at a dwell of 30 s, critical at 60 s. At a fixed 30-second heartbeat, a 30-second dwell is measured from **one or two samples**. Worse, the alert can fire up to 30 seconds after the temperature crossed, and de-escalation is equally blind — the window is quantised to a granularity coarser than the thing it measures.

Dropping `RIALU_HEARTBEAT_INTERVAL` to 2 s fleet-wide is the wrong fix: five machines × 30× the traffic, permanently, to catch an event that happens a few times a week. Note also that `get_cpu_pct()` calls `psutil.cpu_percent(interval=1)`, which **blocks for a full second** — at a 2-second interval the agent would spend half its life inside that call.

**Adaptive interval.** The agent already knows its own temperatures; let it decide its own rate.

| Condition | Interval |
|---|---|
| All dies below `TEAS_WARN_C` (default 75) | 30 s — unchanged |
| Any die at or above `TEAS_WARN_C` | 2 s |
| Returned below `TEAS_WARN_C − 5` | back to 30 s after a 60 s cooldown |

The hysteresis margin and cooldown exist so a die sitting at exactly 75 does not oscillate between rates. The fast rate costs nothing when nothing is wrong, which is nearly always — and when something *is* wrong, the fleet is already reporting at the resolution the alert ladder was designed for.

Switch `get_cpu_pct()` to non-blocking `psutil.cpu_percent(interval=None)` (first call returns 0.0; prime it once at startup) so the fast path is not dominated by a blocking sample.

~~`# TODO:` confirm whether the WS hub or Cloudflare imposes a message-rate ceiling that 2 s × 5 machines would approach.~~ **Resolved 21 Aug 2026.** No ceiling is approached. The hot path is 2 s × 5 machines = **2.5 msg/s aggregate**. `pane_streamer` in the same agent sends every **0.3 s** (`agent/rialu-agent.py:824`) — 3.3 msg/s from a *single* machine over the same hub and the same Cloudflare tunnel. One attached tmux pane already sustains more than the entire fleet will at the fast rate.

---

## <span style="color:#1D9E75">Part 2 — Payload shape</span>

`gpus` is a **list**. One entry per card, in `nvidia-smi` index order. Absent GPUs mean an empty list, never a null or a zero-filled entry.

```json
{
  "type": "heartbeat",
  "machine": "lily",
  "cpu": { "load_pct": 55.0, "temp_c": 62 },
  "ram_pct": 48.2,
  "gpus": [
    { "i": 0, "name": "RTX 4090", "load_pct": 82.0, "temp_c": 74,
      "mem_used_mb": 14220, "mem_total_mb": 24564 },
    { "i": 1, "name": "RTX 4090", "load_pct": 63.0, "temp_c": 66,
      "mem_used_mb": 2110,  "mem_total_mb": 24564 }
  ],
  "processes": [],
  "repos": []
}
```

Collect it in **one** `nvidia-smi` invocation, mirroring the query `loads` already uses:

```
nvidia-smi --query-gpu=index,name,utilization.gpu,temperature.gpu,memory.used,memory.total \
           --format=csv,noheader,nounits
```

Iterate every line. Do not `[0]`.

CPU temperature via `psutil.sensors_temperatures()`. The sensor key differs by platform — `k10temp` on the AMD boxes, `coretemp` on Intel, and WSL2 hosts may expose nothing at all. Prefer a `Tctl`/`Package id 0` label where present, fall back to the first sensor's `current`, and emit `null` when there is none. **A machine that cannot report a CPU temperature must still send a valid heartbeat** — lava and rose run WSL2 and are the likely null cases.

### <span style="color:#0F6E56">Backwards compatibility is not optional</span>

Faire's `.mcard` reads `cpu_pct`, `ram_pct`, `gpu_pct` from `/api/machines` today. **Keep emitting all three**, with `gpu_pct` = the maximum load across `gpus` (max, not mean: a monitoring field should not average away a pegged card). New consumers read `cpu` and `gpus`; Faire keeps working untouched. Deprecate the scalars only once Faire has migrated, in a separate change.

---

## <span style="color:#1D9E75">Part 3 — Storage</span>

Append to `MIGRATIONS` in `db.py`, next sequential numbers, idempotent and duplicate-column-tolerant per the existing pattern:

```sql
-- 0NN — per-die telemetry
ALTER TABLE machine_heartbeats ADD COLUMN cpu_temp_c REAL;
ALTER TABLE machine_heartbeats ADD COLUMN gpus_json TEXT;
```

`gpus_json` follows the `processes_json` / `repos_json` convention already in `agent_heartbeat()` — serialise on write, parse into `gpus` on read, delete the raw key from the dict before returning. Do not normalise GPUs into their own table: `machine_heartbeats` holds exactly one row per machine (the endpoint deletes before inserting), so there is no history to model and a join buys nothing.

**Do not store history.** Teas keeps its own in-memory ring buffer for sparklines and dwell tracking. The moment this table accumulates rows, the delete-then-insert contract in `agent_heartbeat()` breaks and every existing query needs a `MAX(received_at)` subquery it currently gets for free.

---

## <span style="color:#1D9E75">Part 4 — Viewer WebSocket</span>

Teas needs push, not polling: five widgets polling `/api/machines` on their own timers is five HTTP clients doing at 30 s what one socket does instantly, and it discards the whole point of Part 1.

Add a **read-only** viewer socket to `ws_hub.py`:

- `GET /ws/viewer` — authenticated by the same HMAC scheme as the agent socket (`make_auth_message()` shape: machine name, timestamp, signature). Reuse it; do not invent a second auth path.
- On connect, send the current state of every machine — one `snapshot` message — so a widget opening mid-session paints immediately rather than waiting for the next heartbeat.
- Thereafter, fan out each agent heartbeat to all connected viewers as it arrives. **Fan out on receipt, before or in parallel with the DB write** — a viewer socket that waits on SQLite has thrown away the latency the adaptive interval bought.
- Viewers send nothing but a periodic ping. Reject any other inbound message type; a viewer must never be able to reach an agent.

`# TODO:` decide whether Rialú's own SPA (`static/index.html`) should consume this socket too, replacing its machine polling. **Deferred, not done** — out of scope per this brief. The socket is in place and the SPA still polls `/api/machines`; switching it is a self-contained follow-up.

---

## <span style="color:#1D9E75">Part 5 — Tests</span>

Extend `tests/test_machines.py` and `tests/test_agent.py`:

- Heartbeat with `gpus: []` (rose) stores and returns an empty list, not null.
- Heartbeat with two GPUs (lily) round-trips both, in index order.
- `cpu.temp_c: null` (WSL2) is accepted; the row stores and the endpoint returns it.
- `gpu_pct` back-compat field equals the **max** GPU load, not the mean, and equals 0 or null with no GPUs — assert the exact value Faire will render.
- A legacy-shaped heartbeat (flat `gpu_pct`, no `gpus`) is still accepted — the fleet will not upgrade atomically.
- `/api/machines` returns both the legacy scalars and the new `cpu`/`gpus` keys in one response.
- Adaptive interval: crossing `TEAS_WARN_C` selects the fast interval; dropping to `warn − 4` does **not** immediately restore the slow one (hysteresis); `warn − 6` restores it after cooldown.
- Viewer socket receives a snapshot on connect and a heartbeat on agent push.
- A viewer socket sending an `action` message is rejected and does not reach any agent. **This is the security-relevant test in the set.**

---

## <span style="color:#1D9E75">Non-goals</span>

- **No alerting logic server-side.** Thresholds, dwell, hysteresis, mute, and escalation all live in Teas. The hub reports; the viewer decides. Two places computing "is lava too hot" will disagree, and the one on the user's screen is the one that matters.
- **No thresholds in the DB.** They are per-user preference in the viewer's `~/.config/teas/`, not fleet state. The only server-side threshold is `TEAS_WARN_C`, which governs the agent's own reporting rate and nothing else.
- **No new Rialú tab.** Teas is a separate app; Faire and the Rialú SPA embed the same component later, reading the same endpoint.
- **No history table, no time-series store.** See Part 3.
- **No Féith dependency.** Build against the public hub with a `TEAS_HUB_URL` override. Mesh-direct polling is a later config change, not a rewrite — and per `feith/DESIGN.md` the mesh membership table does not include lily, who is the display most likely to host other units' widgets.
- **No changes to the action, tmux, terminal, or Claude-Code paths.** This brief touches the heartbeat and adds one read-only socket.

---

## <span style="color:#1D9E75">Acceptance criteria</span>

- [x] Migrations appended to `db.py`, idempotent on restart.
- [x] Agent collects per-die GPU temperature and load in a single `nvidia-smi` call; every card reported, not just index 0.
- [x] Agent reports CPU temperature where available, `null` where not, without failing the heartbeat.
- [x] Adaptive heartbeat interval implemented with hysteresis and cooldown; `get_cpu_pct()` no longer blocks for a second.
- [x] `gpu_pct` / `cpu_pct` / `ram_pct` still emitted and still correct — **Faire renders unchanged against the new agent**.
- [x] `/api/machines` returns `cpu` and `gpus` alongside the legacy scalars.
- [x] `/ws/viewer` authenticates by HMAC, sends a snapshot on connect, fans out heartbeats on receipt, and refuses all inbound message types.
- [x] Tests above passing; existing suite still green.

---

## <span style="color:#1D9E75">Notes for CC</span>

- **Read first:** `agent/rialu-agent.py` (`get_gpu_pct`, `heartbeat_loop`), `routers/machines.py` (`agent_heartbeat`, `list_machines`), `ws_hub.py` (agent socket + auth), `db.py` (migration pattern), and `hosts/common/.local/bin/loads` in the **firstlight** repo — it already runs the exact `nvidia-smi` query this brief needs, multi-GPU loop included. Copy its query, not its formatting.
- **The interval, not the payload, is the hard part.** Adding fields is mechanical. If the heartbeat still arrives every 30 s when a card is at 91 °C, the viewer's alert ladder is decorative and this brief has failed.
- **`gpus` is a list at every layer** — agent, JSON, DB column, endpoint, and viewer. Every place that special-cases "the GPU" is a bug against lily.
- **Faire must not break.** It is Phase 4 on daisy and in daily use. The back-compat scalars are load-bearing, and the test asserting `gpu_pct == max(...)` is what proves it.
- Rolling upgrade: agents update per-machine via firstlight, so the hub will see old and new payload shapes simultaneously for some period. Accept both.

# Brief — `load_1m` on the heartbeat

**Status:** done

Teas's `--list` shows each machine's 1-minute load average after its dies:

    🪷 lily    cpu   76°C  28%  gpu0  46°C  12%  gpu1  39°C   1%  load  3.23

Teas is a viewer and collects nothing (see `CLAUDE.md`), so the number must come from
`rialu-agent` and through the hub.

## Asked of rialu-agent / Rialú

- Agent: add top-level `load_1m` (float) to the heartbeat — `os.getloadavg()[0]`, the
  same figure `/proc/loadavg` and `uptime` report. A run-queue count, **not** a percent.
- Hub: pass it through unchanged on `/ws/viewer`, snapshot and push, beside `ram_pct`.
  Keep the legacy scalars as they are for Faire.
- Null where the platform has none. Do not send `0.0` for "unknown".

## Already done in Teas

`Heartbeat.load_1m` is `#[serde(default)] Option<f32>`, so old agents still parse. An
absent value renders `—`, never `0.00`.

## Acceptance

- `teas --list` shows a real figure for every online machine, matching `uptime` there.
- Rose's offline row is unchanged; lava (WSL2) reports a load though it has no CPU temp.

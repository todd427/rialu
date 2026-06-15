# Adding a machine to the rialu fleet

Runbook for onboarding a new box (e.g. **Lava**, **Rose**) as a `rialu-agent`.
This is the same recipe used for Daisy and Iris.

> **Adding a machine is entirely local to that box.** No hub, Cloudflare, or
> Fly changes are needed — the shared agent key + CF Access service token
> already work for the whole fleet, and the auto-git worklog merges commits by
> hash across machines, so shared repos don't clobber each other.

**Per machine: ~5 min. Only two values differ between machines — the machine
name and the project list.**

## Prerequisites on the target box

- User `todd`, with `sudo` (the systemd / `/etc` steps need it)
- Repo at `/home/Projects/rialu` — `git clone https://github.com/todd427/rialu.git` if absent
- `python3` + `venv`, `systemd`, `git`, and network reach to `rialu.ie`
- The systemd unit hardcodes `User=todd`, `/home/todd/agentEnv`, and
  `/home/Projects/rialu/agent` — keep those paths or edit `rialu-agent.service`

## Secrets (same values for every machine — from Taisce)

| Taisce key | env var |
|---|---|
| `rialu-agent-key` | `RIALU_AGENT_KEY` |
| `cf-access-client-id` | `CF_ACCESS_CLIENT_ID` |
| `cf-access-client-secret` | `CF_ACCESS_CLIENT_SECRET` |

## Steps

1. **`agent/rialu-agent.env`** (gitignored, `chmod 600`) — identical to other
   machines except `RIALU_MACHINE_NAME`:

   ```
   RIALU_HUB_URL=https://rialu.ie
   RIALU_AGENT_KEY=<rialu-agent-key>
   RIALU_MACHINE_NAME=lava        # or rose
   CF_ACCESS_CLIENT_ID=<cf-access-client-id>
   CF_ACCESS_CLIENT_SECRET=<cf-access-client-secret>
   ```

   The Cloudflare Access service token is required to reach
   `wss://rialu.ie/ws/agent` through Cloudflare Access.

2. **`~/.rialu-agent.json`** — `projects` is process-labeling only (cosmetic
   since the worklog merge); `repo_dirs` is what gets scanned for git state:

   ```json
   {
     "projects": ["<projects with repos/processes on this box>"],
     "repo_dirs": ["/home/Projects"],
     "git_author": "Todd McCaffrey",
     "commit_lookback_hours": 24
   }
   ```

3. **venv + deps:**

   ```bash
   python3 -m venv ~/agentEnv
   ~/agentEnv/bin/pip install -r /home/Projects/rialu/agent/requirements.txt
   ```

4. **Install + start (sudo):**

   ```bash
   sudo cp /home/Projects/rialu/agent/rialu-agent.env /etc/rialu-agent.env
   sudo chmod 600 /etc/rialu-agent.env
   sudo cp /home/Projects/rialu/agent/rialu-agent.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now rialu-agent
   ```

5. **Verify:**

   ```bash
   journalctl -u rialu-agent -n 10 --no-pager
   ```

   Look for `machine=<name>`, then **`Connected and authenticated`** and a
   `Heartbeat sent — ... repos=N` line.

`setup-agent.sh` automates steps 3–4, but it copies `rialu-agent.example.json`
over `~/.rialu-agent.json`, so tailor the config **after** running it (or do the
manual steps above). The script is machine-agnostic.

## Lava vs Rose — the only differences

| | Lava | Rose |
|---|---|---|
| `RIALU_MACHINE_NAME` | `lava` | `rose` |
| `projects` list | projects present on Lava | projects present on Rose |

Everything else (hub URL, agent key, CF token, unit) is identical.

## Notes

- The agent reports **all** git repos under `/home/Projects`, not just the
  `projects` list. Shared repos (e.g. `rialu`, `mnemos`) **union by hash** with
  the other machines' worklog rows instead of clobbering, so overlap is fine.
- A `git config` identity on the box is only needed if you'll *commit* from it —
  the agent itself never commits.
- Removing a retired machine: once it stops heartbeating for 5 min it shows as a
  dimmed "last seen" card on the Machines tab; the **Remove** button
  (`DELETE /api/machines/{name}`, refused while still WS-connected) clears it.

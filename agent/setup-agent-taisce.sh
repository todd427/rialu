#!/usr/bin/env bash
# setup-agent-taisce.sh — bootstrap rialu-agent on a new machine, pulling
# secrets from taisce's service reveal endpoint instead of hand-pasting them.
#
# One-time prerequisite (run on the taisce box, e.g. from Daisy):
#   fly ssh console -a taisce -C "python service_token_cli.py create <machine> \
#     --can-read 'rialu-agent-key,cf-access-client-id,cf-access-client-secret'"
# Store the printed token on the new machine as ~/.taisce-service.token
# (chmod 600), or export TAISCE_SERVICE_TOKEN, or let this script prompt.
#
# Usage:  ./setup-agent-taisce.sh [machine-name]
#   machine-name defaults to lowercase hostname.
#
# Idempotent: safe to re-run. Never clobbers an existing ~/.rialu-agent.json.

set -euo pipefail

TAISCE_URL="${TAISCE_URL:-https://taisce.fly.dev}"
MACHINE_NAME="${1:-$(hostname | tr '[:upper:]' '[:lower:]')}"
AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE=/etc/rialu-agent.env
UNIT_SRC="$AGENT_DIR/rialu-agent.service"
UNIT_DST=/etc/systemd/system/rialu-agent.service
CFG="$HOME/.rialu-agent.json"

# ── service token: env var → token file → prompt ─────────────────────────────
TOKEN="${TAISCE_SERVICE_TOKEN:-}"
if [[ -z "$TOKEN" && -r "$HOME/.taisce-service.token" ]]; then
  TOKEN="$(<"$HOME/.taisce-service.token")"
fi
if [[ -z "$TOKEN" ]]; then
  read -rsp "taisce service token for ${MACHINE_NAME}: " TOKEN; echo
fi
[[ -n "$TOKEN" ]] || { echo "No taisce service token — aborting." >&2; exit 1; }

# ── reveal helper (POST /api/service/reveal-key, audited caller-side) ────────
reveal() {
  local name="$1" resp
  if ! resp=$(curl -fsS -X POST "$TAISCE_URL/api/service/reveal-key" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"name\":\"$name\"}"); then
    echo "reveal failed for '$name' — check token scope (--can-read) and taisce reachability." >&2
    exit 1
  fi
  python3 -c 'import json,sys; print(json.load(sys.stdin)["value"])' <<<"$resp"
}

echo "Machine name : $MACHINE_NAME"
echo "Fetching secrets from $TAISCE_URL ..."
RIALU_AGENT_KEY="$(reveal rialu-agent-key)"
CF_ACCESS_CLIENT_ID="$(reveal cf-access-client-id)"
CF_ACCESS_CLIENT_SECRET="$(reveal cf-access-client-secret)"

# ── /etc/rialu-agent.env (root:root 600; no plaintext copy left in the repo) ─
echo "Writing $ENV_FILE ..."
sudo install -m 600 -o root -g root /dev/null "$ENV_FILE"
sudo tee "$ENV_FILE" >/dev/null <<EOF
RIALU_HUB_URL=https://rialu.ie
RIALU_AGENT_KEY=$RIALU_AGENT_KEY
RIALU_MACHINE_NAME=$MACHINE_NAME
CF_ACCESS_CLIENT_ID=$CF_ACCESS_CLIENT_ID
CF_ACCESS_CLIENT_SECRET=$CF_ACCESS_CLIENT_SECRET
EOF

# ── ~/.rialu-agent.json — create if absent, never clobber ────────────────────
if [[ ! -e "$CFG" ]]; then
  cat > "$CFG" <<EOF
{
  "projects": [],
  "repo_dirs": ["$(dirname "$(dirname "$AGENT_DIR")")"],
  "git_author": "Todd McCaffrey",
  "commit_lookback_hours": 24
}
EOF
  chmod 600 "$CFG"
  echo "Wrote $CFG — add this machine's projects to the list (cosmetic labels only)."
else
  echo "$CFG exists — leaving it alone."
fi

# ── venv + deps ───────────────────────────────────────────────────────────────
if [[ ! -d "$HOME/agentEnv" ]]; then
  python3 -m venv "$HOME/agentEnv"
fi
"$HOME/agentEnv/bin/pip" install -q -r "$AGENT_DIR/requirements.txt"

# ── systemd unit, with paths rewritten to wherever this repo actually lives ──
echo "Installing $UNIT_DST ..."
sed "s|/home/Projects/rialu/agent|$AGENT_DIR|g" "$UNIT_SRC" | sudo tee "$UNIT_DST" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now rialu-agent

echo "Waiting for first heartbeat ..."
sleep 5
journalctl -u rialu-agent -n 10 --no-pager
echo
echo "Want: machine=$MACHINE_NAME → 'Connected and authenticated' → 'Heartbeat sent — ... repos=N'"

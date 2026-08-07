#!/usr/bin/env bash
# setup-from-taisce.sh — onboard this box as a rialu-agent, pulling the
# fleet secrets from Taisce via a scoped service token.
#
# Usage (run as todd, NOT with sudo — the script sudos where needed):
#   TAISCE_SERVICE_TOKEN=<token> ./setup-from-taisce.sh [machine-name]
#
# machine-name defaults to the lowercased short hostname.
#
# One-time prerequisite (from any box with flyctl):
#   fly ssh console -a taisce -C "python service_token_cli.py create fleet \
#     --can-read 'rialu-agent-key,cf-access-client-id,cf-access-client-secret'"
# The token is printed once. It is the ONE secret you hand-carry to a new
# machine; everything else arrives through the audited reveal endpoint.
#
# Idempotent: safe to re-run. Never clobbers an existing ~/.rialu-agent.json.

set -euo pipefail
umask 077

if [[ ${EUID} -eq 0 ]]; then
  echo "run as todd, not root — the systemd unit hardcodes User=todd paths" >&2
  exit 1
fi

MACHINE_NAME="${1:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
TAISCE_URL="${TAISCE_URL:-https://taisce.fly.dev}"
REPO_DIR="/home/Projects/rialu"
: "${TAISCE_SERVICE_TOKEN:?set TAISCE_SERVICE_TOKEN (scoped taisce service token)}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 1; }; }
need curl; need git; need python3; need systemctl

reveal() {
  # POST /api/service/reveal-key {"name": ...} -> value
  curl -fsS -X POST "${TAISCE_URL}/api/service/reveal-key" \
    -H "Authorization: Bearer ${TAISCE_SERVICE_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$1\"}" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["value"])'
}

echo "== rialu-agent setup: machine=${MACHINE_NAME}"

# 1. Repo
if [[ ! -d "${REPO_DIR}/.git" ]]; then
  mkdir -p /home/Projects
  git clone https://github.com/todd427/rialu.git "${REPO_DIR}"
fi

# 2. Secrets — straight from taisce into /etc, never parked in the repo dir
RIALU_AGENT_KEY="$(reveal rialu-agent-key)"
CF_ACCESS_CLIENT_ID="$(reveal cf-access-client-id)"
CF_ACCESS_CLIENT_SECRET="$(reveal cf-access-client-secret)"

sudo install -m 600 /dev/null /etc/rialu-agent.env
sudo tee /etc/rialu-agent.env >/dev/null <<EOF
RIALU_HUB_URL=https://rialu.ie
RIALU_AGENT_KEY=${RIALU_AGENT_KEY}
RIALU_MACHINE_NAME=${MACHINE_NAME}
CF_ACCESS_CLIENT_ID=${CF_ACCESS_CLIENT_ID}
CF_ACCESS_CLIENT_SECRET=${CF_ACCESS_CLIENT_SECRET}
EOF

# 3. Agent config — never clobber an existing tailored one
CFG="${HOME}/.rialu-agent.json"
if [[ ! -f "${CFG}" ]]; then
  cat > "${CFG}" <<EOF
{
  "projects": [],
  "repo_dirs": ["/home/Projects"],
  "git_author": "Todd McCaffrey",
  "commit_lookback_hours": 24
}
EOF
  echo "wrote ${CFG} — edit 'projects' to label what runs on ${MACHINE_NAME}"
fi

# 4. venv + deps
[[ -d "${HOME}/agentEnv" ]] || python3 -m venv "${HOME}/agentEnv"
"${HOME}/agentEnv/bin/pip" install -q -r "${REPO_DIR}/agent/requirements.txt"

# 5. systemd
sudo cp "${REPO_DIR}/agent/rialu-agent.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rialu-agent
sudo systemctl restart rialu-agent   # re-runs pick up a rewritten env

# 6. Verify — wait up to 30s for auth
echo "== waiting for agent to authenticate"
for _ in $(seq 1 30); do
  if sudo journalctl -u rialu-agent --since "1 min ago" --no-pager 2>/dev/null \
     | grep -q "Connected and authenticated"; then
    sudo journalctl -u rialu-agent -n 10 --no-pager
    echo "== OK: ${MACHINE_NAME} is heartbeating — check the Machines tab"
    exit 0
  fi
  sleep 1
done

echo "== agent did not authenticate within 30s; last log lines:" >&2
sudo journalctl -u rialu-agent -n 20 --no-pager >&2
exit 1

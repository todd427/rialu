#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$HOME/agentEnv"

echo "==> Installing agent env file"
sudo cp "$SCRIPT_DIR/rialu-agent.env" /etc/rialu-agent.env
sudo chmod 600 /etc/rialu-agent.env

echo "==> Installing agent config"
cp "$SCRIPT_DIR/rialu-agent.example.json" "$HOME/.rialu-agent.json"
echo "    Edit ~/.rialu-agent.json to adjust project list if needed"

echo "==> Setting up venv at $VENV"
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet psutil requests

echo "==> Installing systemd service"
sudo cp "$SCRIPT_DIR/rialu-agent.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rialu-agent

echo "==> Done. Checking status:"
sudo systemctl status rialu-agent --no-pager

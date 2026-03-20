#!/usr/bin/env bash
set -euo pipefail
sudo cp /home/Projects/rialu/agent/rialu-agent.env /etc/rialu-agent.env
sudo chmod 600 /etc/rialu-agent.env
sudo cp /home/Projects/rialu/agent/rialu-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart rialu-agent
echo "==> Status:"
sudo systemctl status rialu-agent --no-pager

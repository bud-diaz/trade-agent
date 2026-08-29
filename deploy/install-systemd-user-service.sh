#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$HOME/.config/systemd/user"
cp deploy/trade-agent.service "$HOME/.config/systemd/user/trade-agent.service"
systemctl --user daemon-reload
systemctl --user enable trade-agent.service
systemctl --user restart trade-agent.service
systemctl --user status trade-agent.service --no-pager

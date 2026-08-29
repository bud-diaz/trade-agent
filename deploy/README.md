# trade-agent systemd deployment

This service runs `live_loop.py` as a user systemd service and restarts it on crash.

## Install

```bash
cd /home/bud/trade-agent
./deploy/install-systemd-user-service.sh
```

## Operate

```bash
systemctl --user start trade-agent.service
systemctl --user stop trade-agent.service
systemctl --user restart trade-agent.service
systemctl --user status trade-agent.service --no-pager
journalctl --user -u trade-agent.service -f
```

## Verify unit syntax

```bash
systemctl --user daemon-reload
systemd-analyze --user verify deploy/trade-agent.service
```

## Modes

Keep `.env.local` in simulated mode unless intentionally testing broker paper execution:

```env
EXECUTION_MODE=simulated
```

Paper broker mode requires broker credentials and still should use paper accounts first:

```env
EXECUTION_MODE=paper_broker
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true
DISCORD_WEBHOOK_URL=...
```

Do not switch to `EXECUTION_MODE=live` until paper broker mode has run cleanly for multiple sessions and reconciliation has proven it does not duplicate orders after restarts.

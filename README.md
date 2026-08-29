# trade-agent

An automated trading agent, built backtest-first. The backtester and live loop call the same strategy and risk-gate code paths — nothing is forked between them, because that is how a backtest ends up lying to you.

The default mode is **simulated**. Real broker execution is opt-in and protected by explicit safety gates.

## Overview

Signals flow in one direction, and every order has to clear the risk gate:

```text
data source ──▶ SQLite ──▶ strategy ──▶ risk gate ──▶ execution ──▶ reconciliation ──▶ portfolio
(yfinance,     (price_     (signal)    (approve/     (sim or      (broker state)   (fills)
 ccxt,          history)                reject)       broker)
 Alpaca)                                  │                         │
                                          └────────▶ SQLite audit trail
                                                     (signals, risk_evaluations,
                                                      orders, snapshots, state)
```

The strategy never touches execution directly. It emits a `Signal`; the risk gate returns a `RiskDecision`; only approved signals become simulated fills or broker order intents. In broker execution mode, local portfolio state is updated from broker-confirmed filled quantities, not from optimistic submit calls.

Two entry points share the core pipeline:

- **`run_backtest.py`** — replays historical bars, writes CSV/plot output.
- **`live_loop.py`** — polls live prices, evaluates new closed bars, and either simulates fills or submits broker orders depending on `EXECUTION_MODE`.

## Modules

| File | What it does |
| --- | --- |
| `engine.py` | Bar-by-bar backtest loop. Defines the `Signal` and `RiskDecision` contracts, feeds the strategy history up to the current bar only, and fills approved orders against the **next** bar's open. |
| `strategy_ma_rsi.py` | Moving-average crossover with RSI filter. `make_strategy(params, equity_lookup)` returns a strategy function. |
| `risk_gate.py` | Hard limits between signal and order: position size, order value, cash buffer, price sanity, data freshness, open positions, daily-loss kill switch, daily trade count, market-hours checks, correlated exposure caps, and per-symbol cooldown after repeated rejections. |
| `portfolio.py` | Virtual portfolio state — cash, positions, realized/unrealized P&L, equity curve, trade log with per-sell realized P&L. |
| `fills.py` | Simulated execution. Applies adverse slippage and fees. |
| `metrics.py` | Total return, CAGR, max drawdown, Sharpe, Sortino, win rate, average win/loss, trade counts. |
| `datasources.py` | Pluggable OHLCV sources normalized to UTC epoch seconds: `YFinanceDataSource`, `CcxtDataSource`, and `AlpacaDataSource` via `alpaca-py`. |
| `db.py` | SQLite schema and persistence. WAL mode for live loop + dashboard. `init_db()` is idempotent and migrates existing databases. |
| `orders.py` | SQLite order repository helpers for idempotent local order records and broker status updates. |
| `brokers.py` | Broker-neutral `OrderIntent`, `BrokerOrderState`, and `BrokerClient` protocol. |
| `alpaca_broker.py` | Alpaca stock order submission/reconciliation client. |
| `ccxt_broker.py` | ccxt crypto order submission/reconciliation client. Paper broker mode requires ccxt sandbox mode. |
| `broker_factory.py` | Routes stock intents to Alpaca and crypto intents to ccxt, with safety guards. |
| `reconcile.py` | Polls broker order state, updates SQLite, and applies confirmed fill deltas to portfolios. |
| `alerts.py` | Discord webhook client and alert helpers for startup, shutdown, fills, errors, and summaries. |
| `summaries.py` | Daily trading summary generation and once-per-day Discord dispatch marker. |
| `live_state.py` | Shared state between loop and dashboard: reconstructs portfolios from confirmed filled quantities, tracks last processed bar, and syncs halt/daily counters. |
| `live_loop.py` | Long-running/single-pass live loop. Default simulated mode needs no broker credentials. |
| `dashboard.py` | Streamlit UI: positions, equity, recent signals, trade blotter, price history, Emergency Halt / Reset Kill Switch. |
| `run_backtest.py` | Runnable backtest over historical data for configured symbols. |
| `report.py` | Printed summary plus CSV/plot output under `backtest_output/`. |
| `deploy/` | systemd user service template, installer, and runbook. |
| `docs/plans/2026-08-28-real-broker-execution.md` | Implementation plan for broker execution work. |

## Design rules

- **No lookahead.** The strategy only ever sees `price_data.iloc[:i+1]`.
- **No filling on the signal bar in backtests.** Backtest orders fill at the next bar's open.
- **Slippage and fees are applied in simulated execution.**
- **Broker state is authoritative for broker execution.** A submitted order does not change local portfolio state until the broker reports filled quantity.
- **Order IDs are deterministic.** The live loop uses stable `client_order_id` values so crash/retry paths can reconcile instead of blindly duplicating.
- **Default mode is safe.** `EXECUTION_MODE=simulated` requires no broker keys.
- **Paper broker mode is guarded.** Stock orders force Alpaca paper mode; crypto broker execution requires `CCXT_SANDBOX=true`.
- **Live mode is deliberately annoying.** It requires `LIVE_TRADING_CONFIRM=I_UNDERSTAND_THIS_TRADES_REAL_MONEY`.
- **The kill switch latches.** Once a daily-loss halt trips, it stays tripped in `system_state` until manually reset.
- **Secrets stay out of git.** Put real keys only in `.env.local`.

## Installation

Requires Python 3.11+.

```bash
git clone https://github.com/bud-diaz/trade-agent.git
cd trade-agent

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt -r requirements-dev.txt
pip install matplotlib  # optional: equity-curve PNGs
```

Create local env only if needed:

```bash
cp .env.local.example .env.local
```

Never commit real keys — `.env.local`, `*.db`, and `backtest_output/` are gitignored.

## Verify the install

The pytest suite is fully offline, so run it first:

```bash
python -m pytest -q
```

Run the synthetic-data scripts if desired:

```bash
python smoke_test.py
python test_strategy_and_risk.py
```

Run the live loop once in safe simulated mode:

```bash
EXECUTION_MODE=simulated python live_loop.py --once
```

This should exit without broker credentials.

## Running it

### Backtest on historical data

```bash
python run_backtest.py
```

Fetches historical bars, stores them in `trade_agent.db`, runs them through the strategy and risk gate, and writes output under `backtest_output/`. Re-running is idempotent because `price_history` dedupes on `UNIQUE(symbol, timestamp, source)`.

### Live loop

```bash
python live_loop.py --once     # one pass over configured symbols, then exit
python live_loop.py            # continuous, 5-minute poll, Ctrl-C to stop
```

With no env changes, this is simulated only. A failed fetch for one symbol is logged and the loop continues with the others.

### Dashboard

```bash
streamlit run dashboard.py
```

Reads the same SQLite database while the loop is running. The Emergency Halt button writes to `system_state`; the loop picks it up at the top of its next cycle and stops trading until reset.

## Configuration

Core tuning lives in plain dicts/dataclasses:

| What | Where |
| --- | --- |
| Strategy parameters | `DEFAULT_PARAMS` in `strategy_ma_rsi.py` |
| Risk limits | `RiskConfig` in `risk_gate.py` |
| Symbols, sources, starting cash | `SYMBOLS` / `STARTING_CASH_PER_SYMBOL` in `live_state.py` |
| Runtime broker/alert mode | `.env.local` via `config.py` |

Poll interval, slippage, and simulated fees are module constants at the top of `live_loop.py`.

## Running your own backtest

```python
import pandas as pd
from engine import BacktestEngine
from strategy_ma_rsi import make_strategy
from risk_gate import RiskGate, RiskConfig

# One symbol per engine instance. Required columns:
# timestamp (unix epoch seconds, UTC), open, high, low, close, volume, symbol
df = pd.read_csv("my_prices.csv")

risk_gate = RiskGate(RiskConfig(
    max_position_size_pct=0.10,
    max_order_value_usd=5_000,
    min_cash_buffer_pct=0.10,
    max_open_positions=5,
    max_daily_loss_pct=0.05,
))

engine = BacktestEngine(
    price_data=df,
    strategy_fn=make_strategy(params={"position_size_pct": 0.05}),
    risk_gate_fn=risk_gate.evaluate,
    starting_cash=10_000,
    slippage_bps=5,
    fee_bps=10,
    min_history_bars=35,
)

result = engine.run()
print(result.summary())
print(result.rejected_signals)
```

`BacktestEngine` runs one symbol per instance. The live loop handles configured symbols separately but links their portfolios for correlated exposure checks.

## Live execution configuration

Create `.env.local` from `.env.local.example` and keep the default safe mode unless you are intentionally testing broker execution:

```env
EXECUTION_MODE=simulated
```

### Paper broker mode

Paper broker mode routes stocks through Alpaca paper trading. Crypto broker execution is disabled unless ccxt sandbox/testnet mode is explicitly enabled, because many ccxt exchanges use real accounts by default.

```env
EXECUTION_MODE=paper_broker
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_PAPER=true
CRYPTO_EXCHANGE_ID=coinbaseexchange
CCXT_API_KEY=
CCXT_SECRET=
CCXT_PASSWORD=
CCXT_SANDBOX=true
DISCORD_WEBHOOK_URL=
ALLOW_EXTENDED_HOURS=false
MAX_CORRELATED_EXPOSURE_PCT=0.35
MARKET_TIMEZONE=America/New_York
```

Safety behavior in `paper_broker`:

- `ALPACA_PAPER=false` is rejected.
- `CCXT_SANDBOX=false` is rejected for crypto broker execution.
- Stock execution enforces regular market hours unless `ALLOW_EXTENDED_HOURS` is set.
- Broker fills are reconciled before being applied locally.

### Live mode

Live mode can place real-money orders. Do not use it until paper broker mode has run cleanly through restarts and reconciliation checks.

```env
EXECUTION_MODE=live
LIVE_TRADING_CONFIRM=I_UNDERSTAND_THIS_TRADES_REAL_MONEY
ALPACA_PAPER=false
CCXT_SANDBOX=false
```

If the confirmation string is missing or wrong, startup fails fast.

## Alerts and summaries

Set `DISCORD_WEBHOOK_URL` to enable Discord webhook alerts. The agent sends:

- startup alerts;
- shutdown alerts;
- fill alerts;
- per-symbol execution errors;
- one daily summary after 21:00 UTC, guarded by `system_state.last_daily_summary_date` so restarts do not spam duplicate summaries.

If `DISCORD_WEBHOOK_URL` is empty, alert calls are no-ops.

## systemd deployment

The service template lives at `deploy/trade-agent.service` and runs:

```bash
/home/bud/trade-agent/.venv/bin/python /home/bud/trade-agent/live_loop.py
```

Install as a user service:

```bash
cd /home/bud/trade-agent
./deploy/install-systemd-user-service.sh
```

Operate it with:

```bash
systemctl --user start trade-agent.service
systemctl --user stop trade-agent.service
systemctl --user restart trade-agent.service
systemctl --user status trade-agent.service --no-pager
journalctl --user -u trade-agent.service -f
```

Verify unit syntax:

```bash
systemd-analyze --user verify deploy/trade-agent.service
```

## Status

Built:

- [x] Backtest engine with next-bar fills and slippage/fee simulation
- [x] MA crossover + RSI strategy
- [x] Risk gate with kill switch and per-symbol cooldown
- [x] Portfolio accounting and performance metrics
- [x] Historical/live data layer with yfinance, ccxt, and Alpaca source
- [x] SQLite persistence and restart-safe live state
- [x] Broker execution interface
- [x] Alpaca stock broker client
- [x] ccxt crypto broker client with sandbox guard for paper mode
- [x] Broker-side order reconciliation
- [x] Discord webhook alerts
- [x] Daily summaries
- [x] Streamlit dashboard
- [x] systemd user service deployment files
- [x] Market-hours and correlated exposure checks

Not done / intentionally deferred:

- [ ] Verified paper broker run with real Alpaca/ccxt credentials
- [ ] Verified live-mode run with small capital
- [ ] Options, bracket orders, trailing stops, margin, shorting, futures, and multi-leg orders
- [ ] Dynamic statistical correlation calculation
- [ ] Holiday/early-close market calendar beyond the current weekday/session check

Nothing here proves an edge. The test suite proves plumbing and guardrails; paper broker mode still needs real credential validation before live capital.

# trade-agent

An automated trading agent, built backtest-first. The backtester and the live
paper-trading loop call the *same* strategy, risk gate, and fill simulation
code — nothing is forked between them, because that is how a backtest ends up
lying to you.

The system currently runs end to end on real market data, in paper mode only.
No broker order is ever placed and no broker credentials are used.

## Overview

Signals flow in one direction, and every order has to clear the risk gate:

```
data source ──▶ SQLite ──▶ strategy ──▶ risk gate ──▶ fill simulation ──▶ portfolio
(yfinance,     (price_     (signal)    (approve/       (slippage+fees)   (cash/positions)
 ccxt)          history)                reject)               │
                                           │                  ▼
                                           └──────────▶ SQLite audit trail
                                                        (signals, risk_evaluations,
                                                         orders, portfolio_snapshots)
                                                                  │
                                                                  ▼
                                                        Streamlit dashboard
```

The strategy never touches execution directly. It emits a `Signal`; the risk
gate turns that into a `RiskDecision`; only approved signals become fills.
Every signal is persisted — including the ones the gate rejects, along with the
per-rule results that rejected them — so every decision has an audit trail.

Two entry points share that pipeline:

- **`run_backtest.py`** — replays years of historical bars as fast as the CPU
  allows, writes CSV/plot output.
- **`live_loop.py`** — polls live prices on an interval, simulates fills against
  the latest bar, and persists state so it can be restarted without losing
  position.

### Modules

| File | What it does |
| --- | --- |
| `engine.py` | Bar-by-bar backtest loop. Defines the `Signal` and `RiskDecision` contracts, feeds the strategy history up to the current bar only, and fills approved orders against the **next** bar's open. |
| `strategy_ma_rsi.py` | Moving-average crossover with an RSI filter. `make_strategy(params, equity_lookup)` returns a `strategy_fn`; parameters come from a config dict so they can be swept without editing the file. |
| `risk_gate.py` | Hard limits between signal and order: position size, order value, cash buffer, price sanity, data freshness, open positions, daily-loss kill switch, daily trade count, and per-symbol cooldown after repeated rejections. Each rule is independent and logs its own result. |
| `portfolio.py` | Virtual portfolio state — cash, positions, realized/unrealized P&L, equity curve, trade log with per-sell realized P&L. |
| `fills.py` | Simulated execution. Applies slippage (always adverse) and fees in basis points. |
| `metrics.py` | Total return, CAGR, max drawdown, Sharpe, Sortino, win rate, average win/loss, trade counts. |
| `datasources.py` | Pluggable OHLCV sources behind one interface, all normalized to int unix-epoch **seconds** UTC: `YFinanceDataSource` (stocks, retries with backoff), `CcxtDataSource` (crypto via Coinbase Exchange, paginated), `AlpacaDataSource` (stub — needs API keys). |
| `db.py` | SQLite schema and persistence: `price_history`, `signals`, `risk_evaluations`, `orders`, `portfolio_snapshots`, `system_state`. WAL mode so the dashboard can read while the loop writes. `init_db()` is idempotent and migrates existing databases. |
| `live_state.py` | Shared state between the loop and the dashboard: rebuilds portfolios by replaying filled orders, tracks the last-processed bar, and owns the halt/daily-counter round trip so both processes agree on what "halted" means. |
| `live_loop.py` | The live paper-trading loop. Polls, detects new closed bars, evaluates, simulates fills, persists everything. Handles SIGINT/SIGTERM cleanly. |
| `dashboard.py` | Streamlit UI: positions, equity, recent signals, trade blotter, price history, plus Emergency Halt / Reset Kill Switch buttons. Read-only against trading logic. |
| `run_backtest.py` | Runnable backtest over real historical data for AAPL and BTC/USD. |
| `report.py` | Printed summary plus `equity_curve.csv` / `trade_log.csv` (and a plot when matplotlib is available) under `backtest_output/`. |
| `smoke_test.py`, `test_strategy_and_risk.py` | Synthetic-data scripts that print a backtest summary. Run directly, not via pytest. |
| `test_datasources.py`, `test_db.py`, `test_live_state.py`, `test_risk_gate.py` | pytest suite. All network calls mocked — runs fully offline. |
| `Trade agent notes.md` | Design notes: architecture, risk-rule rationale, schema, intended build order. |

### Design rules the code holds to

- **No lookahead.** The strategy only ever sees `price_data.iloc[:i+1]`.
- **No filling on the signal bar.** Backtest orders fill at the next bar's open,
  since you cannot trade at the price that triggered the signal.
- **Slippage and fees always applied**, and always against you.
- **The risk gate is not tunable at runtime.** Changing a limit means editing
  `RiskConfig` and restarting — the strategy engine cannot influence it.
- **The kill switch latches.** Once a daily-loss halt trips it stays tripped,
  persisted in `system_state`, until manually reset. It does not clear overnight.
- **Restarts don't double-fire.** The loop only acts on *closed* bars newer than
  the last one recorded in `signals.bar_timestamp`, and rebuilds positions by
  replaying filled orders.

## Installation

Requires Python 3.11+.

```bash
git clone https://github.com/bud-diaz/trade-agent.git
cd trade-agent

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt          # runtime
pip install -r requirements-dev.txt      # runtime + pytest
pip install matplotlib                   # optional: equity-curve PNGs
```

No credentials are needed. yfinance and the ccxt/Coinbase public endpoints are
unauthenticated. `.env.local.example` exists only as a placeholder for the
not-yet-implemented Alpaca source:

```bash
cp .env.local.example .env.local         # optional; .env.local is gitignored
```

Never commit real keys — `.env.local`, `*.db`, and `backtest_output/` are all
gitignored.

### Verify the install

The test suite is fully offline, so run it first:

```bash
pytest
```

22 tests should pass. Then the two synthetic-data scripts, which exercise the
engine end to end without touching the network:

```bash
python smoke_test.py                 # plumbing only: ~28 fills, 0 rejections
python test_strategy_and_risk.py     # real strategy + real gate, with rejections
```

Seeing rejected signals in the second one is the point — it means the gate is
doing its job.

## Running it

### Backtest on real historical data

```bash
python run_backtest.py
```

Fetches two years of daily bars for AAPL (yfinance) and BTC/USD
(ccxt/Coinbase), stores them in `trade_agent.db`, runs both through the
strategy and risk gate, and writes `backtest_output/<symbol>/equity_curve.csv`
and `trade_log.csv`. Re-running is idempotent — `price_history` dedupes on
`UNIQUE(symbol, timestamp, source)`, so only genuinely new bars are inserted.

Requires outbound network access to Yahoo Finance and `api.exchange.coinbase.com`.
Both sources fail loudly rather than returning a partial dataset that would
quietly produce a wrong backtest.

### Live paper trading

```bash
python live_loop.py --once     # one pass over both symbols, then exit
python live_loop.py            # continuous, 5-minute poll, Ctrl-C to stop
```

Simulated fills only. A failed fetch for one symbol is logged and the loop
continues with the other rather than dying.

### Dashboard

```bash
streamlit run dashboard.py
```

Reads the same SQLite database while the loop is running. The Emergency Halt
button writes to `system_state`; the loop picks it up at the top of its next
cycle and stops trading until the kill switch is reset.

## Configuration

Three places to tune, all plain dicts/dataclasses:

| What | Where |
| --- | --- |
| Strategy parameters (MA periods, RSI thresholds, position size) | `DEFAULT_PARAMS` in `strategy_ma_rsi.py` |
| Risk limits | `RiskConfig` in `risk_gate.py` |
| Symbols, sources, starting cash | `SYMBOLS` / `STARTING_CASH_PER_SYMBOL` in `live_state.py` |

Poll interval, slippage, and fees are module constants at the top of
`live_loop.py`.

**One deliberate difference between backtest and live risk config:**
`data_freshness` and `price_sanity` are live-feed *health* checks — they catch a
stale feed or a corrupted tick. Neither failure mode exists when replaying clean
historical data, and their live defaults would reject nearly every signal in a
backtest by mistaking normal multi-week price drift for a bad tick. So
`run_backtest.py` relaxes those two, and only those two. Every portfolio-risk
rule — position size, order value cap, cash buffer, open positions, daily-loss
kill switch, trade count, cooldown — stays at its real default in both modes.

## Writing your own backtest

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

`BacktestEngine` runs one symbol per instance — for multi-symbol tests, run one
engine per symbol and merge the equity curves. That restriction is deliberate:
it keeps lookahead bugs easy to spot.

## Status and roadmap

Built:

- [x] Backtest engine with next-bar fills and slippage/fee simulation
- [x] MA crossover + RSI strategy
- [x] Risk gate with latching kill switch and per-symbol cooldown
- [x] Portfolio accounting and performance metrics
- [x] Data layer — yfinance (stocks) and ccxt/Coinbase (crypto), normalized
- [x] SQLite persistence with the full six-table schema and audit trail
- [x] Live paper-trading loop, restart-safe and idempotent per bar
- [x] Streamlit dashboard with manual halt controls
- [x] Offline pytest suite

Not built yet:

- [ ] Real broker execution — Alpaca for stocks, ccxt for crypto, with
      broker-side order-state reconciliation
- [ ] `AlpacaDataSource` — currently a stub; needs API keys and `alpaca-py`
- [ ] Discord webhook alerting for fills, errors, and daily summaries
- [ ] Deployment as a systemd service with restart-on-crash
- [ ] Correlation/exposure cap and market-hours checks (rules 9 and 10 in the notes)

The intended order from here is paper trading for weeks, not days → alerting and
hardening → small live capital, with the kill switch deliberately triggered once
to confirm it actually halts. See `Trade agent notes.md` for the full plan.

Nothing here has traded real money.

# trade-agent

An automated trading agent, built backtest-first. The engine, the strategy, and
the risk gate are the same code paths the live system will call — nothing is
forked between backtest and live, because that is how a backtest ends up lying
to you.

Today the repo runs entirely offline against synthetic price data. There is no
broker integration and no live money path yet.

## Overview

Signals flow in one direction, and every order has to clear the risk gate:

```
price data ──▶ strategy ──▶ risk gate ──▶ fill simulation ──▶ portfolio
                (signal)   (approve/reject)   (slippage+fees)   (cash/positions)
```

The strategy never touches execution directly. It emits a `Signal`; the risk
gate turns that into a `RiskDecision`; only approved signals become fills.
Rejections are logged with the rule that killed them, so every decision has an
audit trail.

### Modules

| File | What it does |
| --- | --- |
| `engine.py` | Bar-by-bar backtest loop. Defines the `Signal` and `RiskDecision` contracts, feeds the strategy history up to the current bar only, and fills approved orders against the **next** bar's open. |
| `strategy_ma_rsi.py` | Moving-average crossover with an RSI filter. `make_strategy(params, equity_lookup)` returns a `strategy_fn`; parameters come from a config dict so they can be swept without editing the file. |
| `risk_gate.py` | Hard limits between signal and order: position size, order value, cash buffer, price sanity, data freshness, open positions, daily loss kill switch, daily trade count, and per-symbol cooldown after repeated rejections. Each rule is independent and logs its own result. |
| `portfolio.py` | Virtual portfolio state — cash, positions, realized/unrealized P&L, equity curve, trade log. Mirrors the intended `portfolio_snapshots` schema so backtest state can be compared against live state later. |
| `fills.py` | Simulated execution. Applies slippage (always adverse) and fees in basis points, so a strategy that only works at zero cost is visible as such. |
| `metrics.py` | Total return, CAGR, max drawdown, Sharpe, Sortino, trade counts, computed from the equity curve. |
| `smoke_test.py` | End-to-end plumbing check: synthetic random-walk prices, a trivial buy/sell-on-a-timer strategy, a permissive gate. |
| `test_strategy_and_risk.py` | The real strategy against the real risk gate, on trending synthetic data so crossovers actually fire. |
| `Trade agent notes.md` | Design notes: full system architecture, risk-rule rationale, SQLite schema, and intended build order. |

### Design rules the code holds to

- **No lookahead.** The strategy only ever sees `price_data.iloc[:i+1]`.
- **No filling on the signal bar.** Orders fill at the next bar's open, since
  you cannot trade at the price that triggered the signal.
- **Slippage and fees always applied**, and always against you.
- **The risk gate is not tunable at runtime.** Changing a limit means editing
  `RiskConfig` and restarting — the strategy engine cannot influence it.
- **The kill switch latches.** Once a daily-loss halt trips, it needs a manual
  `reset_halt()`; it does not clear itself overnight.

## Installation

Requires Python 3.11+. Dependencies are `pandas` and `numpy`.

```bash
git clone https://github.com/bud-diaz/trade-agent.git
cd trade-agent

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install pandas numpy
```

### Verify the install

Run the plumbing check first — synthetic data, trivial strategy, no real logic:

```bash
python smoke_test.py
```

You should get a backtest summary, ~28 fills, and 0 rejected signals.

Then run the real strategy against the real risk gate:

```bash
python test_strategy_and_risk.py
```

This one prints the summary plus a sample of rejected signals and which rule
rejected each. Seeing rejections here is the point — it means the gate is doing
its job.

## Running your own backtest

```python
import pandas as pd
from engine import BacktestEngine
from strategy_ma_rsi import make_strategy
from risk_gate import RiskGate, RiskConfig

# One symbol per engine instance. Required columns:
# timestamp (unix epoch), open, high, low, close, volume, symbol
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

Strategy parameters (`DEFAULT_PARAMS` in `strategy_ma_rsi.py`) and risk limits
(`RiskConfig` in `risk_gate.py`) are the two places to tune. Both are plain
dataclasses/dicts, so parameter sweeps don't require touching logic.

## Status and roadmap

Built:

- [x] Backtest engine with next-bar fills and slippage/fee simulation
- [x] MA crossover + RSI strategy
- [x] Risk gate with kill switch and per-symbol cooldown
- [x] Portfolio accounting and performance metrics

Not built yet:

- [ ] Data layer — live and historical feeds (Alpaca for stocks, ccxt for
      crypto), persisted to SQLite
- [ ] Execution layer — broker API orders with idempotent order-state tracking
- [ ] Logging and alerting — every signal to SQLite, Discord webhook for fills,
      errors, and daily summaries
- [ ] Deployment as a long-running service that re-checks risk state on restart

The intended order is data layer → paper trading → hardened risk gate and
logging → small live capital, with the kill switch deliberately triggered once
to confirm it actually halts. See `Trade agent notes.md` for the full plan.

Nothing here has traded real money. Backtest results on synthetic data prove
the plumbing, not an edge.

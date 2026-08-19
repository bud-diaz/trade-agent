"""
Runs the real MA/RSI strategy against the real risk gate, on synthetic data
with an actual trend (not pure random walk) so crossovers have something to
catch. Proves the two new modules integrate cleanly with engine.py.
"""

import numpy as np
import pandas as pd

from engine import BacktestEngine
from strategy_ma_rsi import make_strategy
from risk_gate import RiskGate, RiskConfig

np.random.seed(7)
n_bars = 400
timestamps = [1700000000 + i * 3600 for i in range(n_bars)]

# trend + noise, so MA crossovers actually happen a few times
trend = np.linspace(0, 15, n_bars) + 5 * np.sin(np.linspace(0, 6 * np.pi, n_bars))
noise = np.cumsum(np.random.randn(n_bars) * 0.3)
price = 100 + trend + noise
price = np.maximum(price, 1)

df = pd.DataFrame({
    "timestamp": timestamps,
    "open": price,
    "high": price * 1.003,
    "low": price * 0.997,
    "close": price,
    "volume": np.random.randint(1000, 5000, n_bars),
    "symbol": "TEST",
})

STARTING_CASH = 10_000

# equity_lookup closure needs access to the live portfolio, which doesn't
# exist until the engine is constructed — use a mutable holder.
portfolio_ref = {}

def equity_lookup():
    if "engine" not in portfolio_ref:
        return STARTING_CASH
    return portfolio_ref["engine"].portfolio.total_equity(
        {"TEST": portfolio_ref["engine"].price_data.iloc[-1]["close"]}
    )

strategy_fn = make_strategy(params={"position_size_pct": 0.05}, equity_lookup=equity_lookup)

risk_gate = RiskGate(RiskConfig(
    max_position_size_pct=0.10,
    max_order_value_usd=5_000,
    min_cash_buffer_pct=0.10,
    max_open_positions=5,
    max_daily_loss_pct=0.05,
    max_daily_trade_count=50,
))

engine = BacktestEngine(
    price_data=df,
    strategy_fn=strategy_fn,
    risk_gate_fn=risk_gate.evaluate,
    starting_cash=STARTING_CASH,
    slippage_bps=5,
    fee_bps=10,
    min_history_bars=35,
)
portfolio_ref["engine"] = engine

result = engine.run()

print("=== MA Crossover + RSI Strategy Backtest ===\n")
for k, v in result.summary().items():
    print(f"{k}: {v}")

print(f"\nTotal fills: {len(result.portfolio.trade_log)}")
print(f"Rejected signals: {len(result.rejected_signals)}")

if result.rejected_signals:
    print("\nSample rejections:")
    for r in result.rejected_signals[:5]:
        failed_rules = [x["rule_name"] for x in r["rule_results"] if not x["passed"]]
        print(f"  {r['action']} @ {r['timestamp']} — failed: {failed_rules}")

print(f"\nKill switch tripped: {risk_gate.trading_halted}")
if risk_gate.trading_halted:
    print(f"Halt reason: {risk_gate.halt_reason}")

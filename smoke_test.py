"""
Sanity check: synthetic price data + a trivial strategy + a permissive risk
gate, run through the full engine. Not a real strategy — just proves the
plumbing works end to end before real logic gets plugged in.
"""

import numpy as np
import pandas as pd

from engine import BacktestEngine, Signal, RiskDecision

np.random.seed(42)
n_bars = 300
timestamps = [1700000000 + i * 3600 for i in range(n_bars)]  # hourly bars
price = 100 + np.cumsum(np.random.randn(n_bars) * 0.5)
price = np.maximum(price, 1)  # keep positive

df = pd.DataFrame({
    "timestamp": timestamps,
    "open": price,
    "high": price * 1.002,
    "low": price * 0.998,
    "close": price,
    "volume": np.random.randint(1000, 5000, n_bars),
    "symbol": "TEST",
})


def dumb_strategy(history: pd.DataFrame) -> Signal:
    """Buy every 20 bars, sell every 20 bars offset by 10. Pure plumbing test."""
    i = len(history) - 1
    if i % 20 == 0:
        return Signal(symbol="TEST", action="buy", confidence=0.5, suggested_qty=5, inputs={})
    if i % 20 == 10:
        return Signal(symbol="TEST", action="sell", confidence=0.5, suggested_qty=5, inputs={})
    return Signal(symbol="TEST", action="hold", confidence=0.0, suggested_qty=0, inputs={})


def permissive_risk_gate(signal: Signal, portfolio, current_prices) -> RiskDecision:
    """No real limits — just proving the interface works."""
    if signal.action == "sell":
        held = portfolio.positions.get(signal.symbol)
        if held is None or held.qty < signal.suggested_qty:
            return RiskDecision(approved=False, rule_results=[
                {"rule_name": "sufficient_holdings", "passed": False, "detail": "not enough shares to sell"}
            ])
    return RiskDecision(approved=True, rule_results=[{"rule_name": "noop", "passed": True, "detail": ""}])


engine = BacktestEngine(
    price_data=df,
    strategy_fn=dumb_strategy,
    risk_gate_fn=permissive_risk_gate,
    starting_cash=10_000,
    slippage_bps=5,
    fee_bps=10,
    min_history_bars=20,
)

result = engine.run()
print("=== Backtest Summary ===")
for k, v in result.summary().items():
    print(f"{k}: {v}")

print(f"\nTrades executed: {len(result.portfolio.trade_log)}")
print(f"Signals rejected: {len(result.rejected_signals)}")
print(f"Final cash: {result.portfolio.cash:.2f}")
print(f"Open positions: {result.portfolio.positions}")

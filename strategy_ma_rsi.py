"""
Strategy: Moving Average Crossover + RSI filter.

Logic:
- Fast MA crosses above Slow MA -> bullish signal
- Fast MA crosses below Slow MA -> bearish signal
- RSI filters out signals in overbought/oversold extremes to avoid buying
  into an already-overextended move or selling into a washed-out one.

This is intentionally simple. Two well-understood indicators, clear crossover
logic, no black box. The point of starting here is that when it loses money,
you'll know exactly why.

Params are a config dict, not hardcoded, so the backtester can be re-run
across parameter sweeps later without touching this file.
"""

from dataclasses import dataclass
import pandas as pd
import numpy as np

from engine import Signal


DEFAULT_PARAMS = {
    "fast_ma_period": 10,
    "slow_ma_period": 30,
    "rsi_period": 14,
    "rsi_overbought": 70,
    "rsi_oversold": 30,
    "position_size_pct": 0.05,   # 5% of equity per trade, actual sizing enforced by risk gate
}


def compute_rsi(closes: pd.Series, period: int) -> pd.Series:
    delta = closes.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    # Wilder's smoothing (standard RSI method)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(50)  # neutral when undefined (e.g. avg_loss == 0)
    return rsi


def make_strategy(params: dict = None, equity_lookup=None):
    """
    Returns a strategy_fn compatible with BacktestEngine's strategy_fn interface:
    (history: pd.DataFrame) -> Signal

    equity_lookup: optional callable that returns current portfolio equity,
    used to size suggested_qty as position_size_pct of equity. If not
    provided, suggested_qty is left as a placeholder the risk gate must size.
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    min_bars_needed = max(p["slow_ma_period"], p["rsi_period"]) + 2

    # internal state to detect crossovers (need previous bar's relationship)
    state = {"prev_fast": None, "prev_slow": None}

    def strategy_fn(history: pd.DataFrame) -> Signal:
        symbol = history.iloc[-1]["symbol"]

        if len(history) < min_bars_needed:
            return Signal(symbol=symbol, action="hold", confidence=0.0, suggested_qty=0, inputs={})

        closes = history["close"]
        fast_ma = closes.rolling(p["fast_ma_period"]).mean()
        slow_ma = closes.rolling(p["slow_ma_period"]).mean()
        rsi = compute_rsi(closes, p["rsi_period"])

        current_fast = fast_ma.iloc[-1]
        current_slow = slow_ma.iloc[-1]
        current_rsi = rsi.iloc[-1]
        current_price = closes.iloc[-1]

        prev_fast = fast_ma.iloc[-2]
        prev_slow = slow_ma.iloc[-2]

        inputs = {
            "fast_ma": round(float(current_fast), 4),
            "slow_ma": round(float(current_slow), 4),
            "rsi": round(float(current_rsi), 2),
            "price": round(float(current_price), 4),
        }

        crossed_up = prev_fast <= prev_slow and current_fast > current_slow
        crossed_down = prev_fast >= prev_slow and current_fast < current_slow

        action = "hold"
        confidence = 0.0

        if crossed_up and current_rsi < p["rsi_overbought"]:
            # Bullish crossover, and not already overbought
            action = "buy"
            # confidence scales with how far RSI is from overbought ceiling
            confidence = min(1.0, (p["rsi_overbought"] - current_rsi) / p["rsi_overbought"])

        elif crossed_down and current_rsi > p["rsi_oversold"]:
            # Bearish crossover, and not already oversold
            action = "sell"
            confidence = min(1.0, (current_rsi - p["rsi_oversold"]) / (100 - p["rsi_oversold"]))

        suggested_qty = 0
        if action != "hold":
            if equity_lookup is not None:
                target_value = equity_lookup() * p["position_size_pct"]
                suggested_qty = round(target_value / current_price, 6) if current_price > 0 else 0
            else:
                # Placeholder qty — risk gate / caller must size this properly.
                # Kept nonzero so downstream code doesn't silently drop the signal.
                suggested_qty = 1

        return Signal(
            symbol=symbol,
            action=action,
            confidence=round(confidence, 4),
            suggested_qty=suggested_qty,
            inputs=inputs,
        )

    return strategy_fn

"""
Bar-by-bar backtest engine.

Design intent: this loop reuses the SAME strategy and risk-gate functions the
live system will call. Never fork logic between backtest and live — that's how
a backtest ends up lying to you.

Usage:
    from engine import BacktestEngine
    from portfolio import Portfolio

    engine = BacktestEngine(
        price_data=df,                 # pandas DataFrame, one symbol at a time
        strategy_fn=my_strategy.generate_signal,
        risk_gate_fn=my_risk_gate.evaluate,
        starting_cash=10_000,
    )
    result = engine.run()
    result.summary()
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional
import pandas as pd

from portfolio import Portfolio, Fill
from fills import simulate_fill


@dataclass
class Signal:
    symbol: str
    action: str            # 'buy' | 'sell' | 'hold'
    confidence: float
    suggested_qty: float
    inputs: dict


@dataclass
class RiskDecision:
    approved: bool
    rule_results: list[dict]  # [{rule_name, passed, detail}, ...]


class BacktestResult:
    def __init__(self, portfolio: Portfolio, rejected_signals: list[dict]):
        self.portfolio = portfolio
        self.rejected_signals = rejected_signals

    def equity_series(self) -> pd.Series:
        ts, eq = zip(*self.portfolio.equity_curve) if self.portfolio.equity_curve else ([], [])
        return pd.Series(eq, index=pd.to_datetime(list(ts), unit="s", utc=True))

    def summary(self) -> dict:
        from metrics import compute_metrics
        return compute_metrics(self)


class BacktestEngine:
    def __init__(
        self,
        price_data: pd.DataFrame,       # must have columns: timestamp, open, high, low, close, volume, symbol
        strategy_fn: Callable,          # (history_df_up_to_now) -> Signal
        risk_gate_fn: Callable,         # (Signal, Portfolio, current_prices) -> RiskDecision
        starting_cash: float = 10_000,
        slippage_bps: float = 5,
        fee_bps: float = 10,
        min_history_bars: int = 20,     # don't generate signals until strategy has enough context
        asset_type: str = "stock",
    ):
        self.price_data = price_data.sort_values("timestamp").reset_index(drop=True)
        self.strategy_fn = strategy_fn
        self.risk_gate_fn = risk_gate_fn
        self.portfolio = Portfolio(starting_cash=starting_cash)
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        self.min_history_bars = min_history_bars
        self.asset_type = asset_type
        self.rejected_signals: list[dict] = []

        required_cols = {"timestamp", "open", "high", "low", "close", "volume", "symbol"}
        missing = required_cols - set(self.price_data.columns)
        if missing:
            raise ValueError(f"price_data missing required columns: {missing}")

    def run(self) -> BacktestResult:
        symbols = self.price_data["symbol"].unique()
        if len(symbols) > 1:
            raise ValueError(
                "This engine runs one symbol per instance. "
                "For multi-symbol backtests, run one engine per symbol and merge equity curves, "
                "or extend this loop to iterate a symbol dimension — deliberately not done here "
                "to keep single-symbol lookahead bugs easy to spot."
            )

        n = len(self.price_data)

        for i in range(n):
            current_row = self.price_data.iloc[i]
            # CRITICAL: strategy only sees bars up to and including `i`.
            # No .iloc[i+1:] should ever be reachable from strategy_fn.
            history = self.price_data.iloc[: i + 1]

            if i + 1 < self.min_history_bars:
                self._mark_to_market(current_row)
                continue

            signal = self.strategy_fn(history)

            if signal is not None and signal.action != "hold":
                self._process_signal(signal, current_row, history)

            self._mark_to_market(current_row)

        return BacktestResult(self.portfolio, self.rejected_signals)

    # ------------------------------------------------------------------

    def _process_signal(self, signal: Signal, current_row: pd.Series, history: pd.DataFrame) -> None:
        current_prices = {signal.symbol: current_row["close"]}

        decision = self.risk_gate_fn(signal, self.portfolio, current_prices)

        if not decision.approved:
            self.rejected_signals.append({
                "timestamp": int(current_row["timestamp"]),
                "symbol": signal.symbol,
                "action": signal.action,
                "rule_results": decision.rule_results,
            })
            return

        # Fill simulated against the NEXT bar's open, not this bar's close —
        # you can't actually trade at the price the signal fired on.
        next_open = self._next_open(current_row)
        if next_open is None:
            return  # no next bar (end of data) — can't fill, drop it

        fill = simulate_fill(
            symbol=signal.symbol,
            side=signal.action,
            qty=signal.suggested_qty,
            reference_price=next_open,
            slippage_bps=self.slippage_bps,
            fee_bps=self.fee_bps,
            timestamp=int(current_row["timestamp"]),
        )

        if signal.action == "buy" and not self.portfolio.can_afford(fill.qty, fill.price, fill.fee):
            self.rejected_signals.append({
                "timestamp": int(current_row["timestamp"]),
                "symbol": signal.symbol,
                "action": signal.action,
                "rule_results": [{"rule_name": "insufficient_cash", "passed": False, "detail": "backtest guard"}],
            })
            return

        self.portfolio.apply_fill(fill, asset_type=self.asset_type)

    def _next_open(self, current_row: pd.Series) -> Optional[float]:
        idx = self.price_data.index[self.price_data["timestamp"] == current_row["timestamp"]][0]
        if idx + 1 >= len(self.price_data):
            return None
        return float(self.price_data.iloc[idx + 1]["open"])

    def _mark_to_market(self, current_row: pd.Series) -> None:
        symbol = current_row["symbol"]
        day_key = datetime.fromtimestamp(current_row["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
        self.portfolio.mark_to_market(
            timestamp=int(current_row["timestamp"]),
            current_prices={symbol: current_row["close"]},
            day_key=day_key,
        )

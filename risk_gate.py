"""
Risk Gate: hard-coded checks a signal must pass before becoming an order.

Design principle: every rule is a pure function (signal, portfolio, context) -> RuleResult.
Rules do not know about each other. The gate runs all of them, logs every result,
and approves only if ALL pass. Nothing here is tunable by the strategy engine at
runtime — changing a limit means editing RiskConfig and restarting the service.

This module is imported by BOTH the backtester and the live trading loop.
Never fork this logic between the two.
"""

from dataclasses import dataclass, field
from typing import Optional
import time

from engine import Signal, RiskDecision
from exposure import CORRELATION_BUCKETS, projected_bucket_exposure_pct
from market_hours import is_market_open


@dataclass
class RiskConfig:
    # per-trade
    max_position_size_pct: float = 0.10       # no single position > 10% of equity
    max_order_value_usd: float = 2_000.0        # hard dollar cap per order, sanity backstop
    min_cash_buffer_pct: float = 0.15           # never let free cash drop below 15% of equity
    max_price_deviation_pct: float = 0.05       # reject if price jumped >5% from last known good
    max_data_staleness_seconds: int = 300        # reject if last price update older than 5 min
    enforce_market_hours: bool = False           # live loop enables this for broker execution
    allow_extended_hours: bool = False
    market_timezone: str = "America/New_York"

    # portfolio-level
    max_open_positions: int = 8
    max_daily_loss_pct: float = 0.03             # kill switch: halt if daily loss exceeds 3% of equity
    max_daily_trade_count: int = 20
    max_correlated_exposure_pct: float = 0.35
    correlation_buckets: dict[str, set[str]] = field(default_factory=lambda: {k: set(v) for k, v in CORRELATION_BUCKETS.items()})

    # operational
    rejection_cooldown_count: int = 3            # N consecutive rejections -> blacklist symbol temporarily
    rejection_cooldown_seconds: int = 3600


@dataclass
class RuleResult:
    rule_name: str
    passed: bool
    detail: str = ""


class RiskGate:
    def __init__(self, config: RiskConfig = None):
        self.config = config or RiskConfig()

        # mutable operational state — in live trading this should be persisted
        # to the system_state table so it survives restarts.
        self.trading_halted = False
        self.halt_reason: Optional[str] = None
        self.daily_trade_count = 0
        self.last_known_prices: dict[str, float] = {}
        self.last_price_update_ts: dict[str, float] = {}
        self.consecutive_rejections: dict[str, int] = {}
        self.blacklisted_until: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public entrypoint — matches the risk_gate_fn interface engine.py expects
    # ------------------------------------------------------------------

    def evaluate(self, signal: Signal, portfolio, current_prices: dict[str, float],
                 now_ts: Optional[float] = None) -> RiskDecision:
        if now_ts is None:
            # Backtests replay years of history in under a second of real
            # wall-clock time. If time-based rules (data freshness, the
            # rejection cooldown/blacklist) fell back to time.time(), a
            # symbol blacklisted early in a backtest would never see its
            # cooldown expire before the run finishes, silently freezing it
            # out for the rest of the backtest. Fall back to the portfolio's
            # last mark-to-market timestamp (simulated "now") when available;
            # only use real wall-clock time if there's no simulated time to
            # anchor to yet (e.g. live trading, or the very first bar).
            if portfolio.equity_curve:
                now_ts = portfolio.equity_curve[-1][0]
            else:
                now_ts = time.time()
        results: list[RuleResult] = []

        # Kill switch check first — if halted, nothing else matters
        if self.trading_halted:
            results.append(RuleResult("trading_halted", False, self.halt_reason or "manual halt"))
            return self._finalize(signal, results, now_ts)

        if signal.symbol in self.blacklisted_until:
            if now_ts < self.blacklisted_until[signal.symbol]:
                results.append(RuleResult(
                    "symbol_cooldown", False,
                    f"{signal.symbol} blacklisted until {self.blacklisted_until[signal.symbol]:.0f}"
                ))
                return self._finalize(signal, results, now_ts)
            else:
                del self.blacklisted_until[signal.symbol]
                self.consecutive_rejections[signal.symbol] = 0

        results.append(self._check_data_freshness(signal, now_ts))
        results.append(self._check_price_sanity(signal, current_prices, now_ts))
        results.append(self._check_sufficient_holdings(signal, portfolio))
        results.append(self._check_max_position_size(signal, portfolio, current_prices))
        results.append(self._check_max_order_value(signal, current_prices))
        results.append(self._check_min_cash_buffer(signal, portfolio, current_prices))
        results.append(self._check_max_open_positions(signal, portfolio))
        results.append(self._check_daily_loss(portfolio, current_prices))
        results.append(self._check_daily_trade_count())
        results.append(self._check_market_hours(signal, now_ts))
        results.append(self._check_correlated_exposure(signal, portfolio, current_prices))

        return self._finalize(signal, results, now_ts)

    # ------------------------------------------------------------------
    # Individual rule checks — each one independently testable
    # ------------------------------------------------------------------

    def _check_data_freshness(self, signal: Signal, now_ts: float) -> RuleResult:
        last_ts = self.last_price_update_ts.get(signal.symbol)
        if last_ts is None:
            # No prior data point recorded yet — allow, but this should be
            # rare outside of startup/backtest context.
            return RuleResult("data_freshness", True, "no prior timestamp on record")
        age = now_ts - last_ts
        if age > self.config.max_data_staleness_seconds:
            return RuleResult("data_freshness", False, f"data is {age:.0f}s old, max {self.config.max_data_staleness_seconds}s")
        return RuleResult("data_freshness", True, f"data is {age:.0f}s old")

    def _check_price_sanity(self, signal: Signal, current_prices: dict[str, float], now_ts: float) -> RuleResult:
        price = current_prices.get(signal.symbol)
        if price is None:
            return RuleResult("price_sanity", False, "no current price available")

        last_known = self.last_known_prices.get(signal.symbol)
        if last_known is not None and last_known > 0:
            deviation = abs(price - last_known) / last_known
            if deviation > self.config.max_price_deviation_pct:
                return RuleResult(
                    "price_sanity", False,
                    f"price moved {deviation:.1%} from last known {last_known}, max {self.config.max_price_deviation_pct:.1%}"
                )

        # update tracked price regardless of pass/fail on other rules
        self.last_known_prices[signal.symbol] = price
        self.last_price_update_ts[signal.symbol] = now_ts
        return RuleResult("price_sanity", True, f"price {price} within tolerance")

    def _check_sufficient_holdings(self, signal: Signal, portfolio) -> RuleResult:
        if signal.action != "sell":
            return RuleResult("sufficient_holdings", True, "not a sell, skipped")

        held = portfolio.positions.get(signal.symbol)
        held_qty = held.qty if held else 0.0

        if held_qty < signal.suggested_qty:
            return RuleResult(
                "sufficient_holdings", False,
                f"holds {held_qty}, signal wants to sell {signal.suggested_qty}"
            )
        return RuleResult("sufficient_holdings", True, f"holds {held_qty}")

    def _check_max_position_size(self, signal: Signal, portfolio, current_prices: dict[str, float]) -> RuleResult:
        if signal.action != "buy":
            return RuleResult("max_position_size", True, "not a buy, skipped")

        price = current_prices.get(signal.symbol, 0)
        equity = portfolio.total_equity(current_prices)
        if equity <= 0:
            return RuleResult("max_position_size", False, "zero or negative equity")

        existing_value = 0.0
        if signal.symbol in portfolio.positions:
            existing_value = portfolio.positions[signal.symbol].market_value(price)

        new_value = existing_value + (signal.suggested_qty * price)
        pct_of_equity = new_value / equity

        if pct_of_equity > self.config.max_position_size_pct:
            return RuleResult(
                "max_position_size", False,
                f"would be {pct_of_equity:.1%} of equity, max {self.config.max_position_size_pct:.1%}"
            )
        return RuleResult("max_position_size", True, f"{pct_of_equity:.1%} of equity")

    def _check_max_order_value(self, signal: Signal, current_prices: dict[str, float]) -> RuleResult:
        price = current_prices.get(signal.symbol, 0)
        order_value = signal.suggested_qty * price
        if order_value > self.config.max_order_value_usd:
            return RuleResult(
                "max_order_value", False,
                f"order value ${order_value:.2f} exceeds cap ${self.config.max_order_value_usd:.2f}"
            )
        return RuleResult("max_order_value", True, f"order value ${order_value:.2f}")

    def _check_min_cash_buffer(self, signal: Signal, portfolio, current_prices: dict[str, float]) -> RuleResult:
        if signal.action != "buy":
            return RuleResult("min_cash_buffer", True, "not a buy, skipped")

        price = current_prices.get(signal.symbol, 0)
        order_cost = signal.suggested_qty * price
        equity = portfolio.total_equity(current_prices)
        if equity <= 0:
            return RuleResult("min_cash_buffer", False, "zero or negative equity")

        cash_after = portfolio.cash - order_cost
        buffer_pct_after = cash_after / equity

        if buffer_pct_after < self.config.min_cash_buffer_pct:
            return RuleResult(
                "min_cash_buffer", False,
                f"would leave {buffer_pct_after:.1%} cash buffer, min {self.config.min_cash_buffer_pct:.1%}"
            )
        return RuleResult("min_cash_buffer", True, f"{buffer_pct_after:.1%} buffer after trade")

    def _check_max_open_positions(self, signal: Signal, portfolio) -> RuleResult:
        if signal.action != "buy":
            return RuleResult("max_open_positions", True, "not a buy, skipped")

        is_new_position = signal.symbol not in portfolio.positions
        count_after = portfolio.open_position_count() + (1 if is_new_position else 0)

        if count_after > self.config.max_open_positions:
            return RuleResult(
                "max_open_positions", False,
                f"would open position {count_after}, max {self.config.max_open_positions}"
            )
        return RuleResult("max_open_positions", True, f"{count_after} open positions after trade")

    def _check_daily_loss(self, portfolio, current_prices: dict[str, float]) -> RuleResult:
        equity = portfolio.total_equity(current_prices)
        if equity <= 0:
            return RuleResult("daily_loss_kill_switch", False, "zero or negative equity")

        # daily loss = realized today + unrealized, as a pct of equity
        unrealized = portfolio.unrealized_pl(current_prices)
        daily_pl = portfolio.realized_pl_today + unrealized
        daily_loss_pct = -daily_pl / equity if daily_pl < 0 else 0.0

        if daily_loss_pct > self.config.max_daily_loss_pct:
            self.halt("daily_loss_kill_switch",
                      f"daily loss {daily_loss_pct:.1%} exceeded max {self.config.max_daily_loss_pct:.1%}")
            return RuleResult("daily_loss_kill_switch", False, f"daily loss {daily_loss_pct:.1%}, halting trading")

        return RuleResult("daily_loss_kill_switch", True, f"daily loss {daily_loss_pct:.1%}")

    def _check_daily_trade_count(self) -> RuleResult:
        if self.daily_trade_count >= self.config.max_daily_trade_count:
            return RuleResult(
                "max_daily_trade_count", False,
                f"{self.daily_trade_count} trades today, max {self.config.max_daily_trade_count}"
            )
        return RuleResult("max_daily_trade_count", True, f"{self.daily_trade_count} trades today")


    def _asset_type_for_signal(self, signal: Signal) -> str:
        asset_type = getattr(signal, "asset_type", None)
        if asset_type is None and getattr(signal, "inputs", None):
            asset_type = signal.inputs.get("asset_type")
        if asset_type:
            return asset_type
        return "crypto" if "/" in signal.symbol else "stock"

    def _check_market_hours(self, signal: Signal, now_ts: float) -> RuleResult:
        if not self.config.enforce_market_hours:
            return RuleResult("market_hours", True, "disabled")
        asset_type = self._asset_type_for_signal(signal)
        if is_market_open(asset_type, now_ts, self.config.market_timezone, self.config.allow_extended_hours):
            return RuleResult("market_hours", True, f"{asset_type} market open")
        return RuleResult("market_hours", False, f"{asset_type} market closed")

    def _check_correlated_exposure(self, signal: Signal, portfolio, current_prices: dict[str, float]) -> RuleResult:
        if signal.action != "buy":
            return RuleResult("correlated_exposure", True, "not a buy, skipped")
        price = current_prices.get(signal.symbol)
        if price is None:
            return RuleResult("correlated_exposure", False, "no current price available")
        pct, bucket = projected_bucket_exposure_pct(
            signal.symbol, signal.action, signal.suggested_qty, price, portfolio, current_prices, self.config.correlation_buckets
        )
        if bucket is None:
            return RuleResult("correlated_exposure", True, "no correlation bucket")
        if pct > self.config.max_correlated_exposure_pct:
            return RuleResult(
                "correlated_exposure", False,
                f"bucket {bucket} would be {pct:.1%} of equity, max {self.config.max_correlated_exposure_pct:.1%}"
            )
        return RuleResult("correlated_exposure", True, f"bucket {bucket} {pct:.1%} of equity")

    # ------------------------------------------------------------------
    # Halt / cooldown management
    # ------------------------------------------------------------------

    def halt(self, reason_code: str, detail: str) -> None:
        """Trip the kill switch. Requires manual reset() to resume trading."""
        self.trading_halted = True
        self.halt_reason = f"{reason_code}: {detail}"

    def reset_halt(self) -> None:
        """Manual reset after review. Not called automatically by anything."""
        self.trading_halted = False
        self.halt_reason = None

    def reset_daily_counters(self) -> None:
        """Call once per day (e.g. at market open) to reset trade count."""
        self.daily_trade_count = 0

    def record_trade_executed(self) -> None:
        self.daily_trade_count += 1

    def record_price(self, symbol: str, price: float, now_ts: Optional[float] = None) -> None:
        """Update the price/freshness trackers outside of a full evaluate()
        call. evaluate() only runs on actionable (non-hold) signals, and
        _check_price_sanity only updates last_known_prices/last_price_update_ts
        as a side effect of running — so for an infrequent-signal strategy,
        those trackers would otherwise go stale for as long as the gap
        between trades, making price_sanity/data_freshness compare against a
        weeks-old anchor instead of a recent one. Callers (the live loop)
        should call this on every cycle that does NOT also call evaluate()
        this same cycle — never both, or price_sanity would compare a price
        against itself instead of the previous observation."""
        now_ts = now_ts if now_ts is not None else time.time()
        self.last_known_prices[symbol] = price
        self.last_price_update_ts[symbol] = now_ts

    def _finalize(self, signal: Signal, results: list[RuleResult], now_ts: float) -> RiskDecision:
        approved = all(r.passed for r in results)

        if not approved:
            count = self.consecutive_rejections.get(signal.symbol, 0) + 1
            self.consecutive_rejections[signal.symbol] = count
            if count >= self.config.rejection_cooldown_count:
                self.blacklisted_until[signal.symbol] = now_ts + self.config.rejection_cooldown_seconds
        else:
            self.consecutive_rejections[signal.symbol] = 0
            self.record_trade_executed()

        rule_results = [{"rule_name": r.rule_name, "passed": r.passed, "detail": r.detail} for r in results]
        return RiskDecision(approved=approved, rule_results=rule_results)

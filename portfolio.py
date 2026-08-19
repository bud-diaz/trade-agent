"""
Virtual portfolio state for backtesting.

Tracks cash, open positions, and equity over time. Mirrors the fields in the
`portfolio_snapshots` table so backtest output can be compared apples-to-apples
against live state later.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    asset_type: str  # 'stock' | 'crypto'

    def market_value(self, current_price: float) -> float:
        return self.qty * current_price

    def unrealized_pl(self, current_price: float) -> float:
        return (current_price - self.avg_entry_price) * self.qty


@dataclass
class Fill:
    """A simulated executed order, produced by fills.py"""
    symbol: str
    side: str          # 'buy' | 'sell'
    qty: float
    price: float        # actual fill price, after slippage
    fee: float
    timestamp: int


class Portfolio:
    def __init__(self, starting_cash: float):
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions: dict[str, Position] = {}

        # bookkeeping
        self.realized_pl_total = 0.0
        self.realized_pl_today = 0.0
        self._current_day: Optional[str] = None

        # history for reporting
        self.equity_curve: list[tuple[int, float]] = []  # (timestamp, equity)
        self.trade_log: list[dict] = []

    # ------------------------------------------------------------------
    # Core state queries — these mirror what the live risk gate checks
    # ------------------------------------------------------------------

    def total_equity(self, current_prices: dict[str, float]) -> float:
        """Cash + market value of all open positions."""
        positions_value = sum(
            pos.market_value(current_prices.get(sym, pos.avg_entry_price))
            for sym, pos in self.positions.items()
        )
        return self.cash + positions_value

    def unrealized_pl(self, current_prices: dict[str, float]) -> float:
        return sum(
            pos.unrealized_pl(current_prices.get(sym, pos.avg_entry_price))
            for sym, pos in self.positions.items()
        )

    def open_position_count(self) -> int:
        return len(self.positions)

    def position_value_pct(self, symbol: str, current_prices: dict[str, float]) -> float:
        """What % of total equity a given position represents. Used by risk gate."""
        equity = self.total_equity(current_prices)
        if equity <= 0 or symbol not in self.positions:
            return 0.0
        return self.positions[symbol].market_value(current_prices[symbol]) / equity

    def cash_buffer_pct(self, current_prices: dict[str, float]) -> float:
        equity = self.total_equity(current_prices)
        if equity <= 0:
            return 0.0
        return self.cash / equity

    # ------------------------------------------------------------------
    # Mutating operations
    # ------------------------------------------------------------------

    def apply_fill(self, fill: Fill, asset_type: str = "stock") -> None:
        """Apply a simulated fill to cash and positions."""
        cost = fill.qty * fill.price
        total_cost = cost + fill.fee

        if fill.side == "buy":
            self.cash -= total_cost
            if fill.symbol in self.positions:
                pos = self.positions[fill.symbol]
                new_qty = pos.qty + fill.qty
                # weighted average entry price
                pos.avg_entry_price = (
                    (pos.avg_entry_price * pos.qty) + (fill.price * fill.qty)
                ) / new_qty
                pos.qty = new_qty
            else:
                self.positions[fill.symbol] = Position(
                    symbol=fill.symbol,
                    qty=fill.qty,
                    avg_entry_price=fill.price,
                    asset_type=asset_type,
                )

        elif fill.side == "sell":
            self.cash += cost - fill.fee
            if fill.symbol not in self.positions:
                raise ValueError(f"Attempted to sell {fill.symbol} with no open position")

            pos = self.positions[fill.symbol]
            realized = (fill.price - pos.avg_entry_price) * fill.qty
            self.realized_pl_total += realized
            self.realized_pl_today += realized

            pos.qty -= fill.qty
            if pos.qty <= 1e-9:  # fully closed, avoid float dust
                del self.positions[fill.symbol]
            elif pos.qty < 0:
                raise ValueError(f"Oversold {fill.symbol}: negative position not supported")

        else:
            raise ValueError(f"Unknown side: {fill.side}")

        self.trade_log.append({
            "timestamp": fill.timestamp,
            "symbol": fill.symbol,
            "side": fill.side,
            "qty": fill.qty,
            "price": fill.price,
            "fee": fill.fee,
            "realized_pl": realized if fill.side == "sell" else None,
        })

    def mark_to_market(self, timestamp: int, current_prices: dict[str, float], day_key: str) -> None:
        """Record equity at this point in time. Call once per bar."""
        if self._current_day is not None and day_key != self._current_day:
            self.realized_pl_today = 0.0  # new day, reset daily counter
        self._current_day = day_key

        equity = self.total_equity(current_prices)
        self.equity_curve.append((timestamp, equity))

    def can_afford(self, qty: float, price: float, fee: float = 0.0) -> bool:
        return self.cash >= (qty * price + fee)

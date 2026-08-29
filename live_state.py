"""
Shared state helpers for live_loop.py and dashboard.py. Deliberately knows
nothing about strategy_ma_rsi or risk_gate's internals — just Portfolio/Fill
and raw SQL against orders/system_state/signals. Both the loop and the
dashboard import this so "what do we currently hold" and "are we halted"
have exactly one implementation each, not two that can drift apart.
"""

import sqlite3
from typing import Optional

from portfolio import Portfolio, Fill

SYMBOLS = [
    {"symbol": "AAPL", "asset_type": "stock", "source": "yfinance"},
    {"symbol": "BTC/USD", "asset_type": "crypto", "source": "coinbaseexchange"},
]
STARTING_CASH_PER_SYMBOL = 10_000


def reconstruct_portfolio(
    conn: sqlite3.Connection,
    symbol: str,
    asset_type: str,
    starting_cash: float = STARTING_CASH_PER_SYMBOL,
) -> Portfolio:
    """Replays every filled order for `symbol`, oldest first, through a
    fresh Portfolio via apply_fill(). Single source of truth for current
    holdings — used by live_loop.py at startup and dashboard.py on every
    render. Do not duplicate this logic elsewhere."""
    portfolio = Portfolio(starting_cash=starting_cash)

    rows = conn.execute(
        """
        SELECT symbol, side, filled_qty, filled_price, avg_fill_price, fee, filled_at, submitted_at
        FROM orders
        WHERE symbol = ? AND COALESCE(filled_qty, 0) > 0 AND status IN ('filled', 'partially_filled')
        ORDER BY COALESCE(filled_at, submitted_at) ASC, id ASC
        """,
        (symbol,),
    ).fetchall()

    for row in rows:
        fill = Fill(
            symbol=row["symbol"],
            side=row["side"],
            qty=row["filled_qty"],
            price=row["avg_fill_price"] or row["filled_price"],
            fee=row["fee"] or 0.0,
            timestamp=row["filled_at"] or row["submitted_at"],
        )
        portfolio.apply_fill(fill, asset_type=asset_type)

    return portfolio


def reconstruct_all_portfolios(conn: sqlite3.Connection) -> dict:
    return {
        cfg["symbol"]: reconstruct_portfolio(conn, cfg["symbol"], cfg["asset_type"])
        for cfg in SYMBOLS
    }


def get_last_processed_bar_timestamp(conn: sqlite3.Connection, symbol: str) -> Optional[int]:
    row = conn.execute(
        "SELECT MAX(bar_timestamp) AS ts FROM signals WHERE symbol = ?", (symbol,)
    ).fetchone()
    return row["ts"]


def get_system_state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM system_state WHERE id = 1").fetchone()
    return dict(row) if row else {}


def set_halt(conn: sqlite3.Connection, reason: str, now_ts: int) -> None:
    conn.execute(
        "UPDATE system_state SET trading_halted = 1, halt_reason = ?, halted_at = ? WHERE id = 1",
        (reason, now_ts),
    )
    conn.commit()


def clear_halt(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE system_state SET trading_halted = 0, halt_reason = NULL, halted_at = NULL WHERE id = 1"
    )
    conn.commit()


def sync_risk_gate_halt(conn: sqlite3.Connection, risk_gate) -> None:
    """One-directional: db -> risk_gate. Call at the TOP of every
    live_loop.py cycle, before any evaluate() call this cycle. Always
    adopts whatever system_state currently says. This is safe specifically
    because the loop always calls push_risk_gate_state() (below) at the end
    of every cycle in which the risk gate's own state might have changed —
    so by the time this runs, any mismatch between db and risk_gate can
    only be an external change (the dashboard's Emergency Halt / Reset Kill
    Switch buttons), never a same-process change this function needs to
    push the other way. A single bidirectional "sync" that tried to guess
    which side changed would be ambiguous whenever both sides show
    rg.trading_halted=True/db=False, since that shape means two different
    things depending on which changed most recently — splitting into two
    one-directional functions removes the ambiguity entirely."""
    state = get_system_state(conn)
    if state.get("trading_halted"):
        risk_gate.trading_halted = True
        risk_gate.halt_reason = state.get("halt_reason")
    else:
        risk_gate.trading_halted = False
        risk_gate.halt_reason = None


def push_risk_gate_state(conn: sqlite3.Connection, risk_gate, now_ts: int) -> None:
    """One-directional: risk_gate -> db. Call at the END of every
    live_loop.py cycle (after any evaluate() call that might have tripped
    the kill switch) so system_state reflects the risk gate's current
    in-memory state before the next cycle's sync_risk_gate_halt runs."""
    if risk_gate.trading_halted:
        conn.execute(
            "UPDATE system_state SET trading_halted = 1, halt_reason = ?, "
            "halted_at = COALESCE(halted_at, ?) WHERE id = 1",
            (risk_gate.halt_reason, now_ts),
        )
    else:
        conn.execute(
            "UPDATE system_state SET trading_halted = 0, halt_reason = NULL, halted_at = NULL WHERE id = 1"
        )
    conn.execute(
        "UPDATE system_state SET daily_trade_count = ? WHERE id = 1",
        (risk_gate.daily_trade_count,),
    )
    conn.commit()


def maybe_roll_daily_counters(conn: sqlite3.Connection, risk_gate, today: str) -> None:
    """UTC-day rollover of risk_gate.daily_trade_count + system_state.
    Idempotent — safe to call every cycle."""
    state = get_system_state(conn)
    if state.get("last_reset_date") == today:
        return
    risk_gate.reset_daily_counters()
    conn.execute(
        "UPDATE system_state SET daily_trade_count = 0, last_reset_date = ? WHERE id = 1",
        (today,),
    )
    conn.commit()

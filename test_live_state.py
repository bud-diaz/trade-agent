"""
pytest coverage for live_state.py — portfolio reconstruction, idempotency
watermark, and the halt sync helpers shared by live_loop.py and
dashboard.py. No network involved; uses pytest's tmp_path for a throwaway
db per test.
"""

import pytest

from db import get_connection, init_db
import live_state as ls
from risk_gate import RiskGate, RiskConfig


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.db"))
    init_db(c)
    yield c
    c.close()


def _insert_order(conn, symbol, side, qty, price, fee, filled_at, status="filled"):
    conn.execute(
        """
        INSERT INTO orders (broker, broker_order_id, symbol, side, qty, order_type,
            limit_price, status, submitted_at, filled_at, filled_price, filled_qty, fee)
        VALUES ('paper', NULL, ?, ?, ?, 'market', NULL, ?, ?, ?, ?, ?, ?)
        """,
        (symbol, side, qty, status, filled_at, filled_at, price, qty, fee),
    )
    conn.commit()


def test_reconstruct_portfolio_replays_fills_in_order(conn):
    _insert_order(conn, "TEST", "buy", 10, 100.0, 0.5, filled_at=100)
    _insert_order(conn, "TEST", "sell", 4, 110.0, 0.3, filled_at=200)

    portfolio = ls.reconstruct_portfolio(conn, "TEST", "stock", starting_cash=1000)

    expected_cash = 1000 - (10 * 100 + 0.5) + (4 * 110 - 0.3)
    assert abs(portfolio.cash - expected_cash) < 1e-9
    assert portfolio.positions["TEST"].qty == 6


def test_reconstruct_portfolio_ignores_unfilled_orders(conn):
    _insert_order(conn, "TEST", "buy", 10, 100.0, 0.5, filled_at=100, status="rejected")

    portfolio = ls.reconstruct_portfolio(conn, "TEST", "stock", starting_cash=1000)

    assert portfolio.cash == 1000
    assert portfolio.positions == {}


def test_reconstruct_all_portfolios_covers_every_symbol(conn):
    portfolios = ls.reconstruct_all_portfolios(conn)
    assert set(portfolios.keys()) == {cfg["symbol"] for cfg in ls.SYMBOLS}


def test_last_processed_bar_timestamp_tracks_max(conn):
    assert ls.get_last_processed_bar_timestamp(conn, "AAPL") is None

    conn.execute(
        "INSERT INTO signals (symbol, asset_type, strategy_name, action, confidence, "
        "suggested_qty, inputs_json, created_at, bar_timestamp) "
        "VALUES ('AAPL', 'stock', 'ma_rsi', 'hold', 0, 0, '{}', 1, 500)"
    )
    conn.execute(
        "INSERT INTO signals (symbol, asset_type, strategy_name, action, confidence, "
        "suggested_qty, inputs_json, created_at, bar_timestamp) "
        "VALUES ('AAPL', 'stock', 'ma_rsi', 'buy', 0.5, 1, '{}', 2, 800)"
    )
    conn.commit()

    assert ls.get_last_processed_bar_timestamp(conn, "AAPL") == 800
    assert ls.get_last_processed_bar_timestamp(conn, "BTC/USD") is None


def test_halt_round_trip(conn):
    assert ls.get_system_state(conn)["trading_halted"] == 0

    ls.set_halt(conn, "test reason", 123)
    state = ls.get_system_state(conn)
    assert state["trading_halted"] == 1
    assert state["halt_reason"] == "test reason"
    assert state["halted_at"] == 123

    ls.clear_halt(conn)
    state = ls.get_system_state(conn)
    assert state["trading_halted"] == 0
    assert state["halt_reason"] is None


def test_sync_and_push_are_one_directional_not_ambiguous(conn):
    """Regression test for the bug found during manual testing: a naive
    bidirectional sync couldn't tell 'risk gate just tripped, not yet
    pushed' apart from 'db was externally cleared, risk gate stale' —
    both look like rg.trading_halted=True/db=False. sync_risk_gate_halt
    must always adopt db's state; only push_risk_gate_state writes rg's
    state back out."""
    rg = RiskGate(RiskConfig())

    ls.set_halt(conn, "external halt", 100)
    ls.sync_risk_gate_halt(conn, rg)
    assert rg.trading_halted is True

    ls.clear_halt(conn)  # external actor clears it while rg still thinks halted=True
    ls.sync_risk_gate_halt(conn, rg)
    assert rg.trading_halted is False, "sync must adopt db's cleared state, not repersist rg's stale True"


def test_push_risk_gate_state_persists_self_tripped_halt(conn):
    rg = RiskGate(RiskConfig())
    rg.halt("daily_loss_kill_switch", "test trip")
    rg.daily_trade_count = 3

    ls.push_risk_gate_state(conn, rg, now_ts=999)

    state = ls.get_system_state(conn)
    assert state["trading_halted"] == 1
    assert "daily_loss_kill_switch" in state["halt_reason"]
    assert state["daily_trade_count"] == 3


def test_maybe_roll_daily_counters_is_idempotent(conn):
    rg = RiskGate(RiskConfig())
    rg.daily_trade_count = 5

    ls.maybe_roll_daily_counters(conn, rg, "2099-01-01")
    assert rg.daily_trade_count == 0
    assert ls.get_system_state(conn)["last_reset_date"] == "2099-01-01"

    rg.daily_trade_count = 7  # simulate trades happening after rollover
    ls.maybe_roll_daily_counters(conn, rg, "2099-01-01")  # same day again
    assert rg.daily_trade_count == 7, "must not reset again within the same day"

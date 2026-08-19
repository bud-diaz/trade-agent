"""
Runnable entry point: fetches real historical data, runs the existing
MA/RSI strategy through the existing risk gate, and reports whether it's a
loser — the actual goal of "Data layer + backtester first" from
Trade agent notes.md's suggested build order.

Usage:
    python run_backtest.py
"""

from datetime import datetime, timedelta, timezone
import re

from dotenv import load_dotenv

from db import get_connection, init_db, load_price_history
from datasources import get_data_source, upsert_price_history
from engine import BacktestEngine
from strategy_ma_rsi import make_strategy
from risk_gate import RiskGate, RiskConfig
from report import generate_report

LOOKBACK_YEARS = 2
TIMEFRAME = "1d"
STARTING_CASH = 10_000


def safe_name(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", symbol)


def run_one(symbol: str, asset_type: str, source_name: str, conn) -> dict:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365 * LOOKBACK_YEARS)

    ds = get_data_source(source_name)
    print(f"\nFetching {symbol} ({asset_type}) from {ds.source_name}...")
    raw = ds.fetch_ohlcv(symbol, start, end, timeframe=TIMEFRAME)
    inserted = upsert_price_history(
        conn, raw, symbol=symbol, asset_type=asset_type, source=ds.source_name
    )
    print(f"Inserted {inserted} new bars ({len(raw)} fetched, rest already stored).")

    price_data = load_price_history(conn, symbol=symbol, source=ds.source_name)
    price_data = price_data.assign(symbol=symbol)
    if price_data.empty:
        raise RuntimeError(f"No price history in db for {symbol}/{ds.source_name}")

    # equity_lookup needs the live engine's portfolio, which doesn't exist
    # until the engine is constructed — mutable holder, same pattern
    # test_strategy_and_risk.py already uses.
    engine_ref: dict = {}

    def equity_lookup():
        if "engine" not in engine_ref:
            return STARTING_CASH
        last_close = engine_ref["engine"].price_data.iloc[-1]["close"]
        return engine_ref["engine"].portfolio.total_equity({symbol: last_close})

    strategy_fn = make_strategy(params={"position_size_pct": 0.05}, equity_lookup=equity_lookup)

    # RiskGate is stateful (halt flag, blacklist, daily counters) — a fresh
    # instance per symbol so a kill-switch trip on one run can't silently
    # block the other.
    #
    # data_freshness and price_sanity are live-feed-health checks: they
    # exist to catch a broker's data feed going stale or spitting out a
    # corrupted tick. Neither failure mode can occur when replaying a clean
    # historical dataset, and this engine only updates "last known price"
    # on actionable (non-hold) signal bars — so with an infrequent strategy
    # like MA crossover on daily bars, their live defaults (5 min staleness,
    # 5% deviation) reject essentially every real signal, mistaking normal
    # multi-week price drift for a bad tick. Relaxed here for backtesting
    # only; every portfolio-risk rule (position size, order value cap, cash
    # buffer, open positions, daily-loss kill switch, trade count, cooldown)
    # stays at its real default.
    risk_gate = RiskGate(RiskConfig(
        max_data_staleness_seconds=60 * 60 * 24 * 365 * 10,  # effectively unbounded for backtest
        max_price_deviation_pct=10.0,  # effectively unbounded for backtest
    ))

    engine = BacktestEngine(
        price_data=price_data,
        strategy_fn=strategy_fn,
        risk_gate_fn=risk_gate.evaluate,
        starting_cash=STARTING_CASH,
        asset_type=asset_type,
    )
    engine_ref["engine"] = engine

    result = engine.run()

    metrics = generate_report(result, output_dir=f"backtest_output/{safe_name(symbol)}", label=symbol)

    print(f"\nRejected signals: {len(result.rejected_signals)}")
    print(f"Kill switch tripped: {risk_gate.trading_halted}")
    if risk_gate.trading_halted:
        print(f"Halt reason: {risk_gate.halt_reason}")

    return metrics


def main():
    load_dotenv(".env.local")

    conn = get_connection()
    init_db(conn)

    run_one("AAPL", "stock", "yfinance", conn)
    run_one("BTC/USD", "crypto", "coinbaseexchange", conn)


if __name__ == "__main__":
    main()

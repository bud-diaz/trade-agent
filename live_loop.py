"""
Live paper-trading loop. Continuously polls real market data (yfinance for
AAPL, ccxt/Coinbase for BTC/USD — no broker credentials needed or used) and
runs it through the exact same strategy_ma_rsi + RiskGate + fills.simulate_fill
code the backtester uses. No real broker order is ever placed — approved
signals are simulated fills against the latest fetched price, persisted to
SQLite so live_state.py can reconstruct portfolio state on restart and
dashboard.py can display it.

Usage:
    python live_loop.py            # runs continuously, Ctrl-C to stop
    python live_loop.py --once     # one pass over both symbols, then exit
"""

import argparse
import json
import logging
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

import live_state as ls
from db import get_connection, init_db, load_price_history
from datasources import get_data_source, upsert_price_history
from strategy_ma_rsi import make_strategy
from risk_gate import RiskGate, RiskConfig
from fills import simulate_fill

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("live_loop")

POLL_INTERVAL_SECONDS = 300   # 5 min; bump to 900 if yfinance 429s recur under sustained polling
RECENT_WINDOW_DAYS = 120      # comfortably covers the 32-bar strategy warmup
LIVE_MAX_STALENESS_SECONDS = 1800    # ~6x poll interval, headroom for a couple of failed/retried cycles
LIVE_MAX_PRICE_DEVIATION_PCT = 0.08  # real protective value; record_price keeps this meaningful (see risk_gate.py)
SLIPPAGE_BPS = 5
FEE_BPS = 10
POSITION_SIZE_PCT = 0.05
STRATEGY_NAME = "ma_rsi"


def _install_shutdown_event() -> threading.Event:
    event = threading.Event()

    def _handler(signum, frame):
        logger.info("received signal %s, shutting down after this cycle", signum)
        event.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    return event


def _make_equity_lookup(portfolio, symbol: str, latest_prices: dict):
    def equity_lookup():
        price = latest_prices.get(symbol)
        if price is None:
            return portfolio.starting_cash
        return portfolio.total_equity({symbol: price})

    return equity_lookup


def _insert_signal(conn, row: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO signals
            (symbol, asset_type, strategy_name, action, confidence,
             suggested_qty, inputs_json, created_at, bar_timestamp)
        VALUES
            (:symbol, :asset_type, :strategy_name, :action, :confidence,
             :suggested_qty, :inputs_json, :created_at, :bar_timestamp)
        """,
        row,
    )
    return cur.lastrowid


def _insert_risk_evaluations(conn, signal_id: int, rule_results: list, now_ts: int) -> None:
    for r in rule_results:
        conn.execute(
            "INSERT INTO risk_evaluations (signal_id, rule_name, passed, detail, evaluated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (signal_id, r["rule_name"], r["passed"], r["detail"], now_ts),
        )


def _insert_order(conn, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO orders
            (signal_id, broker, broker_order_id, symbol, side, qty, order_type,
             limit_price, status, submitted_at, filled_at, filled_price, filled_qty, fee)
        VALUES
            (:signal_id, :broker, :broker_order_id, :symbol, :side, :qty, :order_type,
             :limit_price, :status, :submitted_at, :filled_at, :filled_price, :filled_qty, :fee)
        """,
        row,
    )


def _insert_portfolio_snapshot(conn, portfolio, symbol: str, ts: int, current_prices: dict) -> None:
    conn.execute(
        """
        INSERT INTO portfolio_snapshots
            (timestamp, total_equity, cash, unrealized_pl, realized_pl_today, open_position_count, symbol)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts,
            portfolio.total_equity(current_prices),
            portfolio.cash,
            portfolio.unrealized_pl(current_prices),
            portfolio.realized_pl_today,
            portfolio.open_position_count(),
            symbol,
        ),
    )


def _process_new_bar(conn, portfolio, risk_gate, strategy_fn, cfg, history, bar_ts, bar_close, now_ts, day_key):
    symbol = cfg["symbol"]
    signal = strategy_fn(history)

    portfolio.mark_to_market(now_ts, {symbol: bar_close}, day_key)

    rule_results = []
    order_row = None

    if signal.action == "hold":
        # evaluate() won't run this cycle, so keep price_sanity/data_freshness
        # anchored to a recent observation rather than letting them go stale.
        risk_gate.record_price(symbol, bar_close, now_ts)
    else:
        decision = risk_gate.evaluate(signal, portfolio, {symbol: bar_close}, now_ts=now_ts)
        rule_results = list(decision.rule_results)

        if decision.approved:
            fill = simulate_fill(
                symbol=symbol,
                side=signal.action,
                qty=signal.suggested_qty,
                reference_price=bar_close,
                slippage_bps=SLIPPAGE_BPS,
                fee_bps=FEE_BPS,
                timestamp=now_ts,
            )
            if signal.action == "buy" and not portfolio.can_afford(fill.qty, fill.price, fill.fee):
                rule_results.append({"rule_name": "insufficient_cash", "passed": False, "detail": "live guard"})
            else:
                portfolio.apply_fill(fill, asset_type=cfg["asset_type"])
                order_row = {
                    "broker": "paper",
                    "broker_order_id": None,
                    "symbol": symbol,
                    "side": fill.side,
                    "qty": fill.qty,
                    "order_type": "market",
                    "limit_price": None,
                    "status": "filled",
                    "submitted_at": now_ts,
                    "filled_at": fill.timestamp,
                    "filled_price": fill.price,
                    "filled_qty": fill.qty,
                    "fee": fill.fee,
                }

    signal_id = _insert_signal(conn, {
        "symbol": symbol,
        "asset_type": cfg["asset_type"],
        "strategy_name": STRATEGY_NAME,
        "action": signal.action,
        "confidence": signal.confidence,
        "suggested_qty": signal.suggested_qty,
        "inputs_json": json.dumps(signal.inputs),
        "created_at": now_ts,
        "bar_timestamp": bar_ts,
    })
    _insert_risk_evaluations(conn, signal_id, rule_results, now_ts)
    if order_row is not None:
        order_row["signal_id"] = signal_id
        _insert_order(conn, order_row)
    _insert_portfolio_snapshot(conn, portfolio, symbol, now_ts, {symbol: bar_close})
    conn.commit()

    if signal.action != "hold":
        logger.info(
            "%s: %s signal, approved=%s%s",
            symbol, signal.action, order_row is not None,
            "" if order_row else f" rejected={[r['rule_name'] for r in rule_results if not r['passed']]}",
        )


def _run_cycle(conn, cfg: dict, portfolio, risk_gate, strategy_fn, latest_prices: dict) -> None:
    symbol = cfg["symbol"]
    now_ts = int(time.time())

    ds = get_data_source(cfg["source"])
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=RECENT_WINDOW_DAYS)
    raw = ds.fetch_ohlcv(symbol, start, end, timeframe="1d")
    upsert_price_history(conn, raw, symbol=symbol, asset_type=cfg["asset_type"], source=ds.source_name)

    history = load_price_history(conn, symbol=symbol, source=ds.source_name).assign(symbol=symbol)
    if history.empty:
        logger.warning("no price history for %s, skipping cycle", symbol)
        return

    latest = history.iloc[-1]
    bar_ts = int(latest["timestamp"])
    bar_close = float(latest["close"])
    latest_prices[symbol] = bar_close

    today_utc = datetime.now(timezone.utc).date()
    bar_date = datetime.fromtimestamp(bar_ts, tz=timezone.utc).date()
    is_closed_bar = bar_date < today_utc

    last_processed = ls.get_last_processed_bar_timestamp(conn, symbol)
    is_new_bar = is_closed_bar and (last_processed is None or bar_ts > last_processed)
    day_key = today_utc.isoformat()

    if not is_new_bar:
        risk_gate.record_price(symbol, bar_close, now_ts)
        portfolio.mark_to_market(now_ts, {symbol: bar_close}, day_key)
        _insert_portfolio_snapshot(conn, portfolio, symbol, now_ts, {symbol: bar_close})
        conn.commit()
        return

    _process_new_bar(conn, portfolio, risk_gate, strategy_fn, cfg, history, bar_ts, bar_close, now_ts, day_key)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run a single pass over both symbols, then exit")
    args = parser.parse_args()

    load_dotenv(".env.local")
    conn = get_connection()
    init_db(conn)

    portfolios = ls.reconstruct_all_portfolios(conn)
    latest_prices: dict = {}

    risk_gate = RiskGate(RiskConfig(
        max_data_staleness_seconds=LIVE_MAX_STALENESS_SECONDS,
        max_price_deviation_pct=LIVE_MAX_PRICE_DEVIATION_PCT,
    ))
    ls.sync_risk_gate_halt(conn, risk_gate)
    state = ls.get_system_state(conn)
    risk_gate.daily_trade_count = state.get("daily_trade_count") or 0

    strategy_fns = {
        cfg["symbol"]: make_strategy(
            params={"position_size_pct": POSITION_SIZE_PCT},
            equity_lookup=_make_equity_lookup(portfolios[cfg["symbol"]], cfg["symbol"], latest_prices),
        )
        for cfg in ls.SYMBOLS
    }

    shutdown = threading.Event() if args.once else _install_shutdown_event()

    while True:
        today = datetime.now(timezone.utc).date().isoformat()
        ls.maybe_roll_daily_counters(conn, risk_gate, today)
        ls.sync_risk_gate_halt(conn, risk_gate)

        for cfg in ls.SYMBOLS:
            symbol = cfg["symbol"]
            try:
                _run_cycle(conn, cfg, portfolios[symbol], risk_gate, strategy_fns[symbol], latest_prices)
            except Exception:
                logger.exception("cycle failed for %s, continuing", symbol)

        ls.push_risk_gate_state(conn, risk_gate, int(time.time()))

        if args.once:
            break
        if shutdown.wait(POLL_INTERVAL_SECONDS):
            break

    conn.close()
    logger.info("live_loop stopped")


if __name__ == "__main__":
    main()

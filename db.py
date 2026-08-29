"""
SQLite schema + persistence for the trade agent.

Schema is reproduced verbatim from `Trade agent notes.md`. Six tables total:
price_history, signals, risk_evaluations, orders, portfolio_snapshots,
system_state. This increment only populates `price_history` (via the data
layer in datasources.py) — the other five are created here so the schema
lives in one place, but stay empty until the order-manager / live-trading
loop is built. Don't mistake empty tables for missing functionality; they're
intentionally ahead of the code that will write to them.
"""

import sqlite3
from typing import Optional

import pandas as pd

DEFAULT_DB_PATH = "trade_agent.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL,        -- 'stock' | 'crypto'
    timestamp INTEGER NOT NULL,      -- unix epoch, seconds, UTC
    open REAL, high REAL, low REAL, close REAL,
    volume REAL,
    source TEXT,                     -- 'yfinance' | 'coinbaseexchange' | 'alpaca'
    UNIQUE(symbol, timestamp, source)
);

CREATE INDEX IF NOT EXISTS idx_price_history_symbol_ts
    ON price_history(symbol, timestamp);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    action TEXT NOT NULL,            -- 'buy' | 'sell' | 'hold'
    confidence REAL,
    suggested_qty REAL,
    inputs_json TEXT,                -- snapshot of indicator values that drove this
    created_at INTEGER NOT NULL,
    bar_timestamp INTEGER            -- price_history bar that produced this signal; live-loop idempotency watermark
);

CREATE TABLE IF NOT EXISTS risk_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    rule_name TEXT NOT NULL,
    passed BOOLEAN NOT NULL,
    detail TEXT,
    evaluated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    broker TEXT NOT NULL,
    broker_order_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    status TEXT NOT NULL,
    submitted_at INTEGER NOT NULL,
    filled_at INTEGER,
    filled_price REAL,
    filled_qty REAL,
    fee REAL DEFAULT 0.0,            -- needed to replay orders through Portfolio.apply_fill and get correct cash
    client_order_id TEXT UNIQUE,
    broker_status TEXT,
    submitted_broker_at INTEGER,
    updated_broker_at INTEGER,
    last_reconciled_at INTEGER,
    avg_fill_price REAL,
    remaining_qty REAL,
    error_message TEXT,
    raw_broker_json TEXT
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    total_equity REAL NOT NULL,
    cash REAL NOT NULL,
    unrealized_pl REAL,
    realized_pl_today REAL,
    open_position_count INTEGER,
    symbol TEXT                      -- each symbol trades its own independent paper Portfolio
);

CREATE TABLE IF NOT EXISTS system_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    trading_halted BOOLEAN NOT NULL DEFAULT 0,
    halt_reason TEXT,
    halted_at INTEGER,
    daily_trade_count INTEGER DEFAULT 0,
    last_reset_date TEXT,
    last_daily_summary_date TEXT
);
"""


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    # WAL: one long-lived writer (live_loop.py) and a dashboard that rereads
    # constantly is exactly WAL's use case — readers don't block the writer.
    # busy_timeout gives SQLite a few seconds to retry instead of raising
    # "database is locked" on a rare write/write collision.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes if they don't already exist. Safe to call
    every startup."""
    conn.executescript(_SCHEMA)
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS won't retroactively add columns to a table
    that already exists. Safe to call every startup — each step checks
    before acting."""
    signals_cols = {r[1] for r in conn.execute("PRAGMA table_info(signals)")}
    if "bar_timestamp" not in signals_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN bar_timestamp INTEGER")

    orders_cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
    order_migrations = {
        "fee": "ALTER TABLE orders ADD COLUMN fee REAL DEFAULT 0.0",
        "client_order_id": "ALTER TABLE orders ADD COLUMN client_order_id TEXT",
        "broker_status": "ALTER TABLE orders ADD COLUMN broker_status TEXT",
        "submitted_broker_at": "ALTER TABLE orders ADD COLUMN submitted_broker_at INTEGER",
        "updated_broker_at": "ALTER TABLE orders ADD COLUMN updated_broker_at INTEGER",
        "last_reconciled_at": "ALTER TABLE orders ADD COLUMN last_reconciled_at INTEGER",
        "avg_fill_price": "ALTER TABLE orders ADD COLUMN avg_fill_price REAL",
        "remaining_qty": "ALTER TABLE orders ADD COLUMN remaining_qty REAL",
        "error_message": "ALTER TABLE orders ADD COLUMN error_message TEXT",
        "raw_broker_json": "ALTER TABLE orders ADD COLUMN raw_broker_json TEXT",
    }
    for col, sql in order_migrations.items():
        if col not in orders_cols:
            conn.execute(sql)

    state_cols = {r[1] for r in conn.execute("PRAGMA table_info(system_state)")}
    if "last_daily_summary_date" not in state_cols:
        conn.execute("ALTER TABLE system_state ADD COLUMN last_daily_summary_date TEXT")

    snapshot_cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolio_snapshots)")}
    if "symbol" not in snapshot_cols:
        conn.execute("ALTER TABLE portfolio_snapshots ADD COLUMN symbol TEXT")
    # index creation is separate from (and always runs after) the ALTER TABLE
    # above: on a fresh install the column already exists via _SCHEMA's
    # CREATE TABLE, so the ALTER TABLE branch never fires there, but the
    # index still needs creating either way.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_symbol_ts "
        "ON portfolio_snapshots(symbol, timestamp)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_broker_status "
        "ON orders(broker, status)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client_order_id "
        "ON orders(client_order_id) WHERE client_order_id IS NOT NULL"
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO system_state
            (id, trading_halted, halt_reason, halted_at, daily_trade_count, last_reset_date, last_daily_summary_date)
        VALUES (1, 0, NULL, NULL, 0, NULL, NULL)
        """
    )
    conn.commit()


def load_price_history(
    conn: sqlite3.Connection,
    symbol: str,
    source: str,
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> pd.DataFrame:
    """Returns rows shaped exactly how engine.py's BacktestEngine requires:
    columns [timestamp, open, high, low, close, volume, symbol], sorted
    ascending by timestamp.

    `source` is required, not optional: UNIQUE(symbol, timestamp, source)
    allows the same symbol to have rows from more than one source (e.g.
    yfinance today, Alpaca later). Silently blending sources would
    interleave inconsistent prices, so the caller must pick one.
    """
    query = "SELECT timestamp, open, high, low, close, volume, symbol FROM price_history WHERE symbol = ? AND source = ?"
    params: list = [symbol, source]
    if start is not None:
        query += " AND timestamp >= ?"
        params.append(start)
    if end is not None:
        query += " AND timestamp <= ?"
        params.append(end)
    query += " ORDER BY timestamp ASC"

    df = pd.read_sql_query(query, conn, params=params)
    df["timestamp"] = df["timestamp"].astype("int64")
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype("float64")
    df["symbol"] = df["symbol"].astype(str)
    return df

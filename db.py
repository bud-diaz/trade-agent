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
    created_at INTEGER NOT NULL
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
    filled_qty REAL
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    total_equity REAL NOT NULL,
    cash REAL NOT NULL,
    unrealized_pl REAL,
    realized_pl_today REAL,
    open_position_count INTEGER
);

CREATE TABLE IF NOT EXISTS system_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    trading_halted BOOLEAN NOT NULL DEFAULT 0,
    halt_reason TEXT,
    halted_at INTEGER,
    daily_trade_count INTEGER DEFAULT 0,
    last_reset_date TEXT
);
"""


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes if they don't already exist. Safe to call
    every startup."""
    conn.executescript(_SCHEMA)
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

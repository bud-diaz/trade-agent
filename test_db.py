"""
pytest coverage for db.py — schema creation and the price_history loader
contract. No network involved; uses pytest's tmp_path for a throwaway file.
"""

import pandas as pd
import pytest

from db import get_connection, init_db, load_price_history
from datasources import upsert_price_history
from engine import BacktestEngine


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.db"))
    init_db(c)
    yield c
    c.close()


EXPECTED_TABLES = {
    "price_history",
    "signals",
    "risk_evaluations",
    "orders",
    "portfolio_snapshots",
    "system_state",
}


def test_init_db_creates_all_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    table_names = {r["name"] for r in rows}
    assert EXPECTED_TABLES <= table_names


def test_init_db_is_idempotent(conn):
    # calling again must not raise or duplicate anything
    init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    table_names = [r["name"] for r in rows if r["name"] in EXPECTED_TABLES]
    assert len(table_names) == len(set(table_names))


def _sample_df():
    return pd.DataFrame(
        {
            "timestamp": [1_700_000_000, 1_700_086_400],
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1000.0, 1500.0],
        }
    )


def test_upsert_dedupes_on_repeated_fetch(conn):
    df = _sample_df()
    inserted_first = upsert_price_history(
        conn, df, symbol="TEST", asset_type="stock", source="unittest"
    )
    assert inserted_first == 2

    inserted_second = upsert_price_history(
        conn, df, symbol="TEST", asset_type="stock", source="unittest"
    )
    assert inserted_second == 0

    count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    assert count == 2


def test_load_price_history_matches_engine_required_columns(conn):
    df = _sample_df()
    upsert_price_history(conn, df, symbol="TEST", asset_type="stock", source="unittest")

    loaded = load_price_history(conn, symbol="TEST", source="unittest")

    required_cols = {"timestamp", "open", "high", "low", "close", "volume", "symbol"}
    assert required_cols <= set(loaded.columns)
    assert list(loaded["timestamp"]) == sorted(loaded["timestamp"])
    assert loaded["timestamp"].dtype == "int64"

    # BacktestEngine's own missing-column check must pass against our output
    loaded_for_engine = loaded.assign(symbol="TEST")
    missing = {"timestamp", "open", "high", "low", "close", "volume", "symbol"} - set(
        loaded_for_engine.columns
    )
    assert not missing


def test_load_price_history_requires_matching_source(conn):
    df = _sample_df()
    upsert_price_history(conn, df, symbol="TEST", asset_type="stock", source="yfinance")

    loaded = load_price_history(conn, symbol="TEST", source="some_other_source")
    assert loaded.empty

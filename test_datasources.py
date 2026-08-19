"""
pytest coverage for datasources.py. All network calls are mocked — these
tests must pass fully offline.
"""

from datetime import datetime, timezone

import pandas as pd
import pytest

from datasources import YFinanceDataSource, CcxtDataSource, AlpacaDataSource, DataSourceError

REQUIRED_COLS = ["timestamp", "open", "high", "low", "close", "volume"]


# ------------------------------------------------------------------
# YFinanceDataSource
# ------------------------------------------------------------------

class _FakeTicker:
    def __init__(self, history_df):
        self._history_df = history_df

    def history(self, start=None, end=None, interval=None):
        return self._history_df


def _make_yf_history_df():
    index = pd.date_range("2024-01-01", periods=3, freq="D", tz="America/New_York")
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [102.0, 103.0, 104.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [101.0, 102.0, 103.0],
            "Volume": [1000, 1100, 1200],
        },
        index=index,
    )


def test_yfinance_normalizes_to_utc_epoch_seconds(monkeypatch):
    import yfinance as yf

    fake_df = _make_yf_history_df()
    monkeypatch.setattr(yf, "Ticker", lambda symbol: _FakeTicker(fake_df))

    src = YFinanceDataSource(max_retries=1)
    result = src.fetch_ohlcv(
        "AAPL", datetime(2024, 1, 1), datetime(2024, 1, 4), timeframe="1d"
    )

    assert list(result.columns) == REQUIRED_COLS
    assert result["timestamp"].dtype == "int64"
    # first bar is 2024-01-01 00:00 America/New_York == 2024-01-01 05:00 UTC
    expected_first_ts = int(
        pd.Timestamp("2024-01-01 00:00:00", tz="America/New_York")
        .tz_convert("UTC")
        .timestamp()
    )
    assert result.iloc[0]["timestamp"] == expected_first_ts
    assert list(result["timestamp"]) == sorted(result["timestamp"])


def test_yfinance_retries_then_raises_on_empty(monkeypatch):
    import yfinance as yf

    monkeypatch.setattr(yf, "Ticker", lambda symbol: _FakeTicker(pd.DataFrame()))

    src = YFinanceDataSource(max_retries=2, backoff_seconds=0)
    with pytest.raises(DataSourceError):
        src.fetch_ohlcv("AAPL", datetime(2024, 1, 1), datetime(2024, 1, 4))


def test_yfinance_unsupported_timeframe_raises():
    src = YFinanceDataSource()
    with pytest.raises(DataSourceError):
        src.fetch_ohlcv("AAPL", datetime(2024, 1, 1), datetime(2024, 1, 4), timeframe="17m")


# ------------------------------------------------------------------
# CcxtDataSource
# ------------------------------------------------------------------

class _FakeExchange:
    """Simulates paginated fetch_ohlcv: returns full-size pages until
    exhausted, then a short final page."""

    def __init__(self, pages):
        self._pages = pages
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe=None, since=None, limit=None):
        self.calls.append(since)
        if not self._pages:
            return []
        return self._pages.pop(0)


def test_ccxt_paginates_and_normalizes(monkeypatch):
    import ccxt

    day_ms = 86_400_000
    page1 = [[i * day_ms, 100 + i, 101 + i, 99 + i, 100.5 + i, 10 + i] for i in range(300)]
    page2 = [[i * day_ms, 100 + i, 101 + i, 99 + i, 100.5 + i, 10 + i] for i in range(300, 310)]
    fake_exchange = _FakeExchange([page1, page2])

    monkeypatch.setattr(ccxt, "coinbaseexchange", lambda config=None: fake_exchange)

    src = CcxtDataSource()
    start = datetime.fromtimestamp(0, tz=timezone.utc)
    end = datetime.fromtimestamp(400 * 86_400, tz=timezone.utc)
    result = src.fetch_ohlcv("BTC/USD", start, end, timeframe="1d")

    assert list(result.columns) == REQUIRED_COLS
    assert result["timestamp"].dtype == "int64"
    assert len(result) == 310
    assert list(result["timestamp"]) == sorted(result["timestamp"])
    # confirms pagination actually advanced `since` across two calls
    assert len(fake_exchange.calls) == 2


def test_ccxt_raises_on_no_data(monkeypatch):
    import ccxt

    fake_exchange = _FakeExchange([])
    monkeypatch.setattr(ccxt, "coinbaseexchange", lambda config=None: fake_exchange)

    src = CcxtDataSource()
    start = datetime.fromtimestamp(0, tz=timezone.utc)
    end = datetime.fromtimestamp(86_400, tz=timezone.utc)
    with pytest.raises(DataSourceError):
        src.fetch_ohlcv("BTC/USD", start, end)


# ------------------------------------------------------------------
# AlpacaDataSource
# ------------------------------------------------------------------

def test_alpaca_stub_raises_with_key_hint():
    src = AlpacaDataSource()
    with pytest.raises(NotImplementedError, match="ALPACA_API_KEY"):
        src.fetch_ohlcv("AAPL", datetime(2024, 1, 1), datetime(2024, 1, 4))

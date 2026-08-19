"""
Pluggable OHLCV data sources.

Every source normalizes to the same shape: columns [timestamp, open, high,
low, close, volume], `timestamp` as an int unix-epoch in SECONDS, UTC. This
matters because engine.py feeds `timestamp` straight into
`datetime.fromtimestamp(current_row["timestamp"], tz=timezone.utc)` and
`pd.to_datetime(..., unit="s", utc=True)` — milliseconds or tz-naive values
would silently corrupt every bar.

Sources:
- YFinanceDataSource: free, no auth, stocks. Confirmed flaky under load
  (rate-limited requests are common) — retries with backoff, fails loudly
  (raises) rather than returning an empty/wrong backtest.
- CcxtDataSource: ccxt's `coinbaseexchange` id (api.exchange.coinbase.com),
  free, no auth, crypto. Paginates because Coinbase caps ~300 candles/call.
- AlpacaDataSource: stub. Alpaca's market data API requires an API key even
  for historical bars, which we don't have yet. Matches the same interface
  so swapping it in later is a one-line change in get_data_source(), and
  deliberately does not import alpaca-py so it costs nothing until then.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import time

import pandas as pd


class DataSourceError(Exception):
    """Raised when a data source can't return usable data."""


class OHLCVDataSource(ABC):
    source_name: str

    @abstractmethod
    def fetch_ohlcv(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "1d"
    ) -> pd.DataFrame:
        """Returns columns [timestamp, open, high, low, close, volume],
        timestamp as int unix-epoch seconds UTC, sorted ascending."""
        raise NotImplementedError


class YFinanceDataSource(OHLCVDataSource):
    source_name = "yfinance"

    # yfinance's own interval strings — pass-through today, only "1d" is
    # exercised by run_backtest.py. Intraday intervals are capped by Yahoo
    # to roughly the last 60 days of history, a real limitation, not a bug,
    # if this ever gets extended below daily bars.
    _INTERVAL_MAP = {"1d": "1d", "1h": "1h", "5m": "5m", "1m": "1m"}

    def __init__(self, max_retries: int = 3, backoff_seconds: float = 2.0):
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds

    def fetch_ohlcv(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "1d"
    ) -> pd.DataFrame:
        import yfinance as yf

        interval = self._INTERVAL_MAP.get(timeframe)
        if interval is None:
            raise DataSourceError(f"yfinance: unsupported timeframe {timeframe!r}")

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                ticker = yf.Ticker(symbol)
                raw = ticker.history(start=start, end=end, interval=interval)
                if raw.empty:
                    raise DataSourceError(
                        f"yfinance returned no data for {symbol} [{start} - {end}]"
                    )
                return self._normalize(raw, symbol)
            except Exception as exc:  # noqa: BLE001 - genuinely want to retry on anything
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * attempt)

        raise DataSourceError(
            f"yfinance: failed to fetch {symbol} after {self.max_retries} attempts: {last_error}"
        ) from last_error

    @staticmethod
    def _normalize(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
        df = raw.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )[["open", "high", "low", "close", "volume"]].copy()

        index = df.index
        # yfinance's daily-bar index is tz-aware in the exchange's local
        # timezone (e.g. America/New_York); convert to UTC before deriving
        # epoch seconds so bars aren't silently shifted by hours.
        if index.tz is None:
            index = index.tz_localize("UTC")
        else:
            index = index.tz_convert("UTC")

        # Don't assume nanosecond resolution: pandas' default datetime64
        # unit varies (yfinance's own index has come back as [us] and [s]
        # depending on version), and a fixed `// 10**9` silently produces
        # wrong timestamps if the unit isn't actually ns. as_unit("s")
        # normalizes explicitly before the int cast.
        df["timestamp"] = index.as_unit("s").astype("int64")
        df = df.reset_index(drop=True)
        df = df[["timestamp", "open", "high", "low", "close", "volume"]]
        return df.sort_values("timestamp").reset_index(drop=True)


class CcxtDataSource(OHLCVDataSource):
    source_name = "coinbaseexchange"

    # Coinbase Exchange caps candles per request; 300 is the documented
    # ceiling. Paginate rather than assume one call covers the range.
    _PAGE_LIMIT = 300

    def __init__(self, exchange_id: str = "coinbaseexchange"):
        self.exchange_id = exchange_id

    def fetch_ohlcv(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "1d"
    ) -> pd.DataFrame:
        import ccxt

        exchange_cls = getattr(ccxt, self.exchange_id)
        exchange = exchange_cls({"enableRateLimit": True})

        since_ms = int(start.replace(tzinfo=start.tzinfo or timezone.utc).timestamp() * 1000)
        end_ms = int(end.replace(tzinfo=end.tzinfo or timezone.utc).timestamp() * 1000)

        all_rows: list = []
        cursor = since_ms
        while cursor < end_ms:
            try:
                batch = exchange.fetch_ohlcv(
                    symbol, timeframe=timeframe, since=cursor, limit=self._PAGE_LIMIT
                )
            except Exception as exc:  # noqa: BLE001
                raise DataSourceError(
                    f"ccxt/{self.exchange_id}: failed to fetch {symbol}: {exc}"
                ) from exc

            if not batch:
                break

            all_rows.extend(batch)
            last_ts = batch[-1][0]
            if last_ts <= cursor:
                # exchange didn't advance — avoid an infinite loop
                break
            cursor = last_ts + 1

            if len(batch) < self._PAGE_LIMIT:
                break

        if not all_rows:
            raise DataSourceError(
                f"ccxt/{self.exchange_id} returned no data for {symbol} [{start} - {end}]"
            )

        return self._normalize(all_rows, end_ms)

    @staticmethod
    def _normalize(rows: list, end_ms: int) -> pd.DataFrame:
        df = pd.DataFrame(
            rows, columns=["timestamp_ms", "open", "high", "low", "close", "volume"]
        )
        df = df[df["timestamp_ms"] <= end_ms]
        df["timestamp"] = (df["timestamp_ms"] // 1000).astype("int64")
        df = df.drop(columns=["timestamp_ms"])
        df = df.drop_duplicates(subset="timestamp")
        df = df[["timestamp", "open", "high", "low", "close", "volume"]]
        return df.sort_values("timestamp").reset_index(drop=True)


class AlpacaDataSource(OHLCVDataSource):
    """Stub. Alpaca's market data API requires ALPACA_API_KEY/
    ALPACA_SECRET_KEY even for historical bars, which aren't set up yet.
    Deliberately does not import alpaca-py so it costs nothing until this
    gets implemented for real."""

    source_name = "alpaca"

    def fetch_ohlcv(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "1d"
    ) -> pd.DataFrame:
        raise NotImplementedError(
            "AlpacaDataSource not yet implemented — set ALPACA_API_KEY and "
            "ALPACA_SECRET_KEY in .env.local, add alpaca-py to requirements.txt, "
            "then implement this method."
        )


def get_data_source(name: str) -> OHLCVDataSource:
    sources = {
        "yfinance": YFinanceDataSource,
        "coinbaseexchange": CcxtDataSource,
        "alpaca": AlpacaDataSource,
    }
    cls = sources.get(name)
    if cls is None:
        raise ValueError(f"Unknown data source {name!r}, expected one of {list(sources)}")
    return cls()


def upsert_price_history(
    conn,
    df: pd.DataFrame,
    symbol: str,
    asset_type: str,
    source: str,
) -> int:
    """INSERT OR IGNORE into price_history so re-running a fetch is
    idempotent — dedupes on UNIQUE(symbol, timestamp, source). Returns the
    number of rows actually inserted (executemany's rowcount isn't reliable
    with OR IGNORE, so we diff COUNT(*) before/after instead)."""
    before = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]

    rows = [
        (
            symbol,
            asset_type,
            int(r.timestamp),
            float(r.open),
            float(r.high),
            float(r.low),
            float(r.close),
            float(r.volume),
            source,
        )
        for r in df.itertuples(index=False)
    ]

    conn.executemany(
        """
        INSERT OR IGNORE INTO price_history
            (symbol, asset_type, timestamp, open, high, low, close, volume, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()

    after = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    return after - before

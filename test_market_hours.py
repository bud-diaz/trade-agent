from datetime import datetime
from zoneinfo import ZoneInfo
from market_hours import is_market_open


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo("America/New_York")).timestamp()


def test_stock_regular_hours_open():
    assert is_market_open("stock", ts("2024-01-02T10:00:00"))


def test_stock_before_open_closed():
    assert not is_market_open("stock", ts("2024-01-02T08:00:00"))


def test_stock_weekend_closed():
    assert not is_market_open("stock", ts("2024-01-06T10:00:00"))


def test_crypto_always_open():
    assert is_market_open("crypto", ts("2024-01-06T10:00:00"))

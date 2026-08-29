from portfolio import Portfolio, Fill
from exposure import projected_bucket_exposure_pct


def test_projected_bucket_exposure_counts_correlated_positions():
    p = Portfolio(1000)
    p.apply_fill(Fill("MSFT", "buy", 3, 100, 0, 1), asset_type="stock")
    pct, bucket = projected_bucket_exposure_pct("AAPL", "buy", 1, 50, p, {"MSFT": 100, "AAPL": 50})
    assert bucket == "mega_cap_tech"
    assert round(pct, 2) == 0.35


def test_unbucketed_symbol_has_zero_exposure():
    p = Portfolio(1000)
    assert projected_bucket_exposure_pct("XYZ", "buy", 1, 10, p, {"XYZ": 10}) == (0.0, None)

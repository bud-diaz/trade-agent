"""
pytest coverage for the record_price addition to risk_gate.py — the live
loop calls this on cycles that don't also call evaluate(), to keep
price_sanity/data_freshness anchored to a recent observation instead of
going stale for weeks between infrequent MA-crossover signals.
"""

from risk_gate import RiskGate, RiskConfig


def test_record_price_sets_trackers():
    rg = RiskGate(RiskConfig())
    rg.record_price("AAPL", 100.0, now_ts=1_000)

    assert rg.last_known_prices["AAPL"] == 100.0
    assert rg.last_price_update_ts["AAPL"] == 1_000


def test_record_price_defaults_now_ts_to_wall_clock(monkeypatch):
    import risk_gate as risk_gate_module

    monkeypatch.setattr(risk_gate_module.time, "time", lambda: 12_345.0)
    rg = RiskGate(RiskConfig())
    rg.record_price("AAPL", 100.0)

    assert rg.last_price_update_ts["AAPL"] == 12_345.0


def test_record_price_keeps_price_sanity_anchor_fresh():
    """The scenario record_price exists to fix: without it, an anchor set
    once and never refreshed makes a legitimate multi-week price move look
    like an implausible jump."""
    rg = RiskGate(RiskConfig(max_price_deviation_pct=0.05))
    rg.record_price("AAPL", 100.0, now_ts=0)

    # simulate several refresh-only cycles over what would otherwise be a
    # stale multi-week gap, each keeping the anchor close to current price
    for i, price in enumerate([101.0, 102.0, 103.0], start=1):
        rg.record_price("AAPL", price, now_ts=i * 300)

    result = rg._check_price_sanity(
        signal=type("S", (), {"symbol": "AAPL"})(),
        current_prices={"AAPL": 104.0},
        now_ts=1200,
    )
    # 104 vs last recorded 103 is within 5%, even though 104 vs the very
    # first price (100) would have exceeded it
    assert result.passed

from config import load_config


def test_config_defaults_to_simulated(monkeypatch):
    monkeypatch.delenv("EXECUTION_MODE", raising=False)
    cfg = load_config()
    assert cfg.execution_mode == "simulated"
    assert not cfg.broker_execution_enabled


def test_config_enables_broker_execution(monkeypatch):
    monkeypatch.setenv("EXECUTION_MODE", "paper_broker")
    cfg = load_config()
    assert cfg.broker_execution_enabled


def test_config_parses_exposure_cap(monkeypatch):
    monkeypatch.setenv("MAX_CORRELATED_EXPOSURE_PCT", "0.25")
    assert load_config().max_correlated_exposure_pct == 0.25


def test_invalid_execution_mode_fails_fast(monkeypatch):
    import pytest
    monkeypatch.setenv("EXECUTION_MODE", "papre_broker")
    with pytest.raises(ValueError, match="EXECUTION_MODE"):
        load_config()


def test_paper_broker_rejects_alpaca_live_endpoint(monkeypatch):
    import pytest
    monkeypatch.setenv("EXECUTION_MODE", "paper_broker")
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(ValueError, match="ALPACA_PAPER"):
        load_config()


def test_live_mode_requires_explicit_confirmation(monkeypatch):
    import pytest
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.delenv("LIVE_TRADING_CONFIRM", raising=False)
    with pytest.raises(ValueError, match="LIVE_TRADING_CONFIRM"):
        load_config()

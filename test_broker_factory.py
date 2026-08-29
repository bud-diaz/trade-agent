from dataclasses import replace
import pytest
from broker_factory import make_broker_for_asset
from config import AppConfig


def test_paper_broker_crypto_requires_sandbox():
    cfg = AppConfig(execution_mode="paper_broker", ccxt_api_key="k", ccxt_secret="s", ccxt_sandbox=False)
    with pytest.raises(ValueError, match="CCXT_SANDBOX"):
        make_broker_for_asset("crypto", cfg)


def test_live_crypto_allows_non_sandbox_with_confirmation():
    cfg = AppConfig(execution_mode="live", ccxt_api_key="k", ccxt_secret="s", ccxt_sandbox=False, live_trading_confirm="I_UNDERSTAND_THIS_TRADES_REAL_MONEY")
    broker = make_broker_for_asset("crypto", cfg)
    assert broker.broker_name == "ccxt"

from alpaca_broker import AlpacaBrokerClient
from ccxt_broker import CcxtBrokerClient


def make_broker_for_asset(asset_type: str, cfg):
    if asset_type == "stock":
        paper = True if cfg.execution_mode == "paper_broker" else cfg.alpaca_paper
        return AlpacaBrokerClient(cfg.alpaca_api_key, cfg.alpaca_secret_key, paper=paper)
    if asset_type == "crypto":
        if cfg.execution_mode == "paper_broker" and not cfg.ccxt_sandbox:
            raise ValueError("Refusing crypto broker execution in paper_broker mode unless CCXT_SANDBOX=true")
        return CcxtBrokerClient(
            cfg.crypto_exchange_id, cfg.ccxt_api_key, cfg.ccxt_secret, cfg.ccxt_password, sandbox=cfg.ccxt_sandbox
        )
    raise ValueError(f"unsupported asset_type {asset_type!r}")

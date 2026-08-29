from dataclasses import dataclass
import os

TRUE_VALUES = {"1", "true", "yes", "on"}
ALLOWED_MODES = {"simulated", "paper_broker", "live"}
BROKER_MODES = {"paper_broker", "live"}
LIVE_CONFIRMATION = "I_UNDERSTAND_THIS_TRADES_REAL_MONEY"


@dataclass(frozen=True)
class AppConfig:
    execution_mode: str = "simulated"
    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    alpaca_paper: bool = True
    crypto_exchange_id: str = "coinbaseexchange"
    ccxt_api_key: str | None = None
    ccxt_secret: str | None = None
    ccxt_password: str | None = None
    ccxt_sandbox: bool = False
    discord_webhook_url: str | None = None
    allow_extended_hours: bool = False
    max_correlated_exposure_pct: float = 0.35
    market_timezone: str = "America/New_York"
    live_trading_confirm: str | None = None

    @property
    def broker_execution_enabled(self) -> bool:
        return self.execution_mode in BROKER_MODES


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in TRUE_VALUES


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else float(raw)


def validate_config(cfg: AppConfig) -> AppConfig:
    if cfg.execution_mode not in ALLOWED_MODES:
        raise ValueError(f"EXECUTION_MODE must be one of {sorted(ALLOWED_MODES)}, got {cfg.execution_mode!r}")
    if cfg.execution_mode == "paper_broker" and not cfg.alpaca_paper:
        raise ValueError("EXECUTION_MODE=paper_broker requires ALPACA_PAPER=true")
    if cfg.execution_mode == "live" and cfg.live_trading_confirm != LIVE_CONFIRMATION:
        raise ValueError("EXECUTION_MODE=live requires LIVE_TRADING_CONFIRM=I_UNDERSTAND_THIS_TRADES_REAL_MONEY")
    return cfg


def load_config() -> AppConfig:
    cfg = AppConfig(
        execution_mode=os.getenv("EXECUTION_MODE", "simulated"),
        alpaca_api_key=os.getenv("ALPACA_API_KEY"),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY"),
        alpaca_paper=_bool("ALPACA_PAPER", True),
        crypto_exchange_id=os.getenv("CRYPTO_EXCHANGE_ID", "coinbaseexchange"),
        ccxt_api_key=os.getenv("CCXT_API_KEY"),
        ccxt_secret=os.getenv("CCXT_SECRET"),
        ccxt_password=os.getenv("CCXT_PASSWORD"),
        ccxt_sandbox=_bool("CCXT_SANDBOX", False),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL"),
        allow_extended_hours=_bool("ALLOW_EXTENDED_HOURS", False),
        max_correlated_exposure_pct=_float("MAX_CORRELATED_EXPOSURE_PCT", 0.35),
        market_timezone=os.getenv("MARKET_TIMEZONE", "America/New_York"),
        live_trading_confirm=os.getenv("LIVE_TRADING_CONFIRM"),
    )
    return validate_config(cfg)

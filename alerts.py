import logging
import requests

logger = logging.getLogger(__name__)


class DiscordAlertClient:
    def __init__(self, webhook_url: str | None, timeout_seconds: float = 5.0):
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds

    def send(self, content: str, embeds: list[dict] | None = None) -> bool:
        if not self.webhook_url:
            return False
        resp = requests.post(self.webhook_url, json={"content": content, "embeds": embeds or []}, timeout=self.timeout_seconds)
        resp.raise_for_status()
        return True

    def safe_send(self, content: str, embeds: list[dict] | None = None) -> bool:
        try:
            return self.send(content, embeds)
        except Exception:
            logger.exception("Discord alert failed")
            return False


def alert_fill(client: DiscordAlertClient, order_state) -> bool:
    return client.safe_send(f"✅ Fill: {order_state.symbol} {order_state.status} qty={order_state.filled_qty} avg={order_state.avg_fill_price}")


def alert_error(client: DiscordAlertClient, symbol: str, exc: Exception | str) -> bool:
    return client.safe_send(f"⚠️ Trade-agent error for {symbol}: {exc}")


def alert_daily_summary(client: DiscordAlertClient, summary: dict) -> bool:
    return client.safe_send("📊 Daily trade-agent summary", embeds=[{"title": "Daily Summary", "description": "\n".join(f"{k}: {v}" for k, v in summary.items())}])


def alert_startup(client: DiscordAlertClient, cfg) -> bool:
    return client.safe_send(f"🟢 trade-agent started in {cfg.execution_mode} mode")


def alert_shutdown(client: DiscordAlertClient, reason: str = "normal shutdown") -> bool:
    return client.safe_send(f"🔴 trade-agent stopped: {reason}")

from brokers import BrokerOrderState, OrderIntent
from orders import normalize_status


class BrokerConfigError(RuntimeError):
    pass


class CcxtBrokerClient:
    broker_name = "ccxt"

    def __init__(self, exchange_id="coinbaseexchange", api_key=None, secret=None, password=None, exchange=None, sandbox: bool = False):
        self.exchange_id = exchange_id
        self.api_key = api_key
        self.secret = secret
        self.password = password
        self.sandbox = sandbox
        self._exchange = exchange
        if exchange is None and (not api_key or not secret):
            raise BrokerConfigError("ccxt broker missing CCXT_API_KEY/CCXT_SECRET")

    @property
    def exchange(self):
        if self._exchange is None:
            import ccxt
            cls = getattr(ccxt, self.exchange_id)
            cfg = {"enableRateLimit": True, "apiKey": self.api_key, "secret": self.secret}
            if self.password:
                cfg["password"] = self.password
            self._exchange = cls(cfg)
            if self.sandbox and hasattr(self._exchange, "set_sandbox_mode"):
                self._exchange.set_sandbox_mode(True)
        return self._exchange

    def submit_order(self, intent: OrderIntent) -> BrokerOrderState:
        if self.sandbox and hasattr(self.exchange, "set_sandbox_mode"):
            self.exchange.set_sandbox_mode(True)
        params = {"clientOrderId": intent.client_order_id}
        raw = self.exchange.create_order(intent.symbol, intent.order_type, intent.side, intent.qty, None, params)
        return self._state(raw, intent.client_order_id, intent.symbol)

    def get_order(self, broker_order_id: str | None = None, client_order_id: str | None = None, symbol: str | None = None) -> BrokerOrderState:
        if broker_order_id is None:
            raise ValueError("broker_order_id required for ccxt reconciliation")
        raw = self.exchange.fetch_order(broker_order_id, symbol)
        return self._state(raw, client_order_id, symbol)

    def list_open_orders(self) -> list[BrokerOrderState]:
        return [self._state(o) for o in self.exchange.fetch_open_orders()]

    def _state(self, raw: dict, fallback_client_order_id: str | None = None, fallback_symbol: str | None = None) -> BrokerOrderState:
        status = normalize_status(raw.get("status", "submitted"))
        filled = float(raw.get("filled") or raw.get("filled_qty") or 0)
        amount = float(raw.get("amount") or 0)
        avg = raw.get("average") or raw.get("price")
        client_id = raw.get("clientOrderId") or raw.get("client_order_id") or fallback_client_order_id or ""
        return BrokerOrderState(
            broker=self.broker_name, broker_order_id=str(raw.get("id") or "") or None, client_order_id=str(client_id),
            symbol=str(raw.get("symbol") or fallback_symbol or ""), status=status, filled_qty=filled,
            avg_fill_price=float(avg) if avg not in (None, "") else None,
            remaining_qty=float(raw.get("remaining")) if raw.get("remaining") is not None else (max(0.0, amount-filled) if amount else None),
            raw=raw, error_message=raw.get("error"),
        )

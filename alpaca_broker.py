from brokers import BrokerOrderState, OrderIntent
from orders import normalize_status


class BrokerConfigError(RuntimeError):
    pass


class AlpacaBrokerClient:
    broker_name = "alpaca"

    def __init__(self, api_key: str | None, secret_key: str | None, paper: bool = True, client=None):
        if not api_key or not secret_key:
            raise BrokerConfigError("alpaca broker missing ALPACA_API_KEY/ALPACA_SECRET_KEY")
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = paper
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                from alpaca.trading.client import TradingClient
            except Exception as exc:  # pragma: no cover
                raise BrokerConfigError("alpaca-py is not installed") from exc
            self._client = TradingClient(self.api_key, self.secret_key, paper=self.paper)
        return self._client

    def submit_order(self, intent: OrderIntent) -> BrokerOrderState:
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            side = OrderSide.BUY if intent.side == "buy" else OrderSide.SELL
            req = MarketOrderRequest(
                symbol=intent.symbol, qty=intent.qty, side=side, time_in_force=TimeInForce.DAY, client_order_id=intent.client_order_id
            )
            order = self.client.submit_order(req)
            return self._state(order, intent.client_order_id)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "client_order_id" in msg or "duplicate" in msg:
                return self.get_order(client_order_id=intent.client_order_id)
            raise

    def get_order(self, broker_order_id: str | None = None, client_order_id: str | None = None, symbol: str | None = None) -> BrokerOrderState:
        if client_order_id and hasattr(self.client, "get_order_by_client_order_id"):
            return self._state(self.client.get_order_by_client_order_id(client_order_id), client_order_id)
        if broker_order_id is None:
            raise ValueError("broker_order_id or client_order_id required")
        return self._state(self.client.get_order_by_id(broker_order_id), client_order_id)

    def list_open_orders(self) -> list[BrokerOrderState]:
        if not hasattr(self.client, "get_orders"):
            return []
        return [self._state(o, getattr(o, "client_order_id", "")) for o in self.client.get_orders()]

    def _state(self, order, fallback_client_order_id: str | None = None) -> BrokerOrderState:
        raw = order if isinstance(order, dict) else getattr(order, "__dict__", {})
        def val(name, default=None):
            return raw.get(name, default) if isinstance(raw, dict) else getattr(order, name, default)
        status = normalize_status(str(val("status", "submitted")))
        filled_qty = float(val("filled_qty", 0) or 0)
        qty = float(val("qty", 0) or 0)
        avg = val("filled_avg_price", None) or val("avg_fill_price", None)
        return BrokerOrderState(
            broker=self.broker_name, broker_order_id=str(val("id", "")) or None,
            client_order_id=str(val("client_order_id", fallback_client_order_id or "")),
            symbol=str(val("symbol", "")), status=status, filled_qty=filled_qty,
            avg_fill_price=float(avg) if avg not in (None, "") else None,
            remaining_qty=max(0.0, qty - filled_qty) if qty else None, raw=raw if isinstance(raw, dict) else {},
        )

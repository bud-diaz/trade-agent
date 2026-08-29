from dataclasses import dataclass
from typing import Literal, Protocol

Side = Literal["buy", "sell"]
OrderStatus = Literal["submitted", "partially_filled", "filled", "cancelled", "rejected", "error"]


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    asset_type: str
    side: Side
    qty: float
    order_type: str
    client_order_id: str


@dataclass(frozen=True)
class BrokerOrderState:
    broker: str
    broker_order_id: str | None
    client_order_id: str
    symbol: str
    status: OrderStatus
    filled_qty: float
    avg_fill_price: float | None
    remaining_qty: float | None
    raw: dict
    error_message: str | None = None


class BrokerClient(Protocol):
    broker_name: str
    def submit_order(self, intent: OrderIntent) -> BrokerOrderState: ...
    def get_order(self, broker_order_id: str | None = None, client_order_id: str | None = None, symbol: str | None = None) -> BrokerOrderState: ...
    def list_open_orders(self) -> list[BrokerOrderState]: ...

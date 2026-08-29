import time
from brokers import OrderIntent
from portfolio import Fill
import orders as order_repo


def reconcile_open_orders(conn, broker_clients: dict[str, object], portfolios: dict[str, object] | None = None, asset_types: dict[str, str] | None = None, now_ts: int | None = None) -> list[dict]:
    now_ts = now_ts or int(time.time())
    changes = []
    for row in order_repo.list_open_orders(conn):
        broker = row["broker"]
        client = broker_clients.get(broker)
        if client is None:
            continue
        before_status = row["status"]
        before_filled = float(row["filled_qty"] or 0.0)
        try:
            if row["status"] == "pending_submit" and not row.get("broker_order_id"):
                state = client.submit_order(OrderIntent(
                    symbol=row["symbol"],
                    asset_type=(asset_types or {}).get(row["symbol"], "stock"),
                    side=row["side"],
                    qty=float(row["qty"]),
                    order_type=row["order_type"],
                    client_order_id=row["client_order_id"],
                ))
                order_repo.mark_order_submitted(
                    conn,
                    client_order_id=row["client_order_id"],
                    broker_order_id=state.broker_order_id,
                    broker_status=state.status,
                    raw_broker_json=state.raw,
                    now_ts=now_ts,
                )
            else:
                state = client.get_order(row.get("broker_order_id"), row.get("client_order_id"), row.get("symbol"))
            updated = order_repo.update_order_from_broker(
                conn, broker=broker, broker_order_id=state.broker_order_id, client_order_id=state.client_order_id or row.get("client_order_id"),
                broker_status=state.status, filled_qty=state.filled_qty, avg_fill_price=state.avg_fill_price, remaining_qty=state.remaining_qty,
                raw_broker_json=state.raw, now_ts=now_ts, error_message=state.error_message,
            )
            delta = float(state.filled_qty or 0.0) - before_filled
            if portfolios is not None and delta > 1e-9 and state.avg_fill_price is not None:
                symbol = updated["symbol"] if updated else row["symbol"]
                portfolios[symbol].apply_fill(Fill(symbol=symbol, side=row["side"], qty=delta, price=state.avg_fill_price, fee=0.0, timestamp=now_ts), asset_type=(asset_types or {}).get(symbol, "stock"))
            if before_status != (updated or {}).get("status") or delta > 1e-9:
                changes.append({"before": row, "after": updated, "state": state, "filled_delta": delta})
        except Exception as exc:  # noqa: BLE001
            order_repo.update_order_from_broker(
                conn, broker=broker, broker_order_id=row.get("broker_order_id"), client_order_id=row.get("client_order_id"),
                broker_status="error", filled_qty=before_filled, avg_fill_price=row.get("avg_fill_price"), remaining_qty=row.get("remaining_qty"),
                raw_broker_json=row.get("raw_broker_json"), now_ts=now_ts, error_message=str(exc),
            )
            changes.append({"before": row, "after": order_repo.get_order_by_client_order_id(conn, row.get("client_order_id")), "error": str(exc), "filled_delta": 0.0})
    return changes

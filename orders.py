import json
import sqlite3
from typing import Any

OPEN_STATUSES = {"pending_submit", "submitted", "partially_filled"}
TERMINAL_STATUSES = {"filled", "cancelled", "rejected", "error"}


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def create_pending_order(conn, *, signal_id, broker, symbol, side, qty, order_type, client_order_id, submitted_at, limit_price=None) -> dict:
    conn.execute(
        """
        INSERT OR IGNORE INTO orders
            (signal_id, broker, broker_order_id, client_order_id, symbol, side, qty, order_type,
             limit_price, status, broker_status, submitted_at, filled_at, filled_price, filled_qty,
             fee, avg_fill_price, remaining_qty, last_reconciled_at, raw_broker_json)
        VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'pending_submit', NULL, ?, NULL, NULL, 0.0,
                0.0, NULL, ?, NULL, NULL)
        """,
        (signal_id, broker, client_order_id, symbol, side, qty, order_type, limit_price, submitted_at, qty),
    )
    conn.commit()
    return get_order_by_client_order_id(conn, client_order_id)


def get_order_by_client_order_id(conn, client_order_id: str) -> dict | None:
    return _row_to_dict(conn.execute("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)).fetchone())


def mark_order_submitted(conn, *, client_order_id, broker_order_id, broker_status, raw_broker_json=None, now_ts=None) -> None:
    local_status = normalize_status(broker_status)
    conn.execute(
        """
        UPDATE orders
        SET broker_order_id = ?, broker_status = ?, status = ?, raw_broker_json = ?,
            submitted_broker_at = COALESCE(submitted_broker_at, ?), updated_broker_at = ?, last_reconciled_at = ?
        WHERE client_order_id = ?
        """,
        (broker_order_id, broker_status, local_status, _json(raw_broker_json), now_ts, now_ts, now_ts, client_order_id),
    )
    conn.commit()


def update_order_from_broker(conn, *, broker, broker_order_id=None, client_order_id=None, broker_status, filled_qty=0.0, avg_fill_price=None, remaining_qty=None, raw_broker_json=None, now_ts=None, error_message=None) -> dict | None:
    local_status = normalize_status(broker_status)
    filled_at_expr = "COALESCE(filled_at, ?)" if local_status in {"filled", "partially_filled"} and (filled_qty or 0) > 0 else "filled_at"
    sql = f"""
        UPDATE orders
        SET broker_status = ?, status = ?, filled_qty = ?, avg_fill_price = ?,
            filled_price = COALESCE(?, filled_price), remaining_qty = ?, raw_broker_json = ?,
            updated_broker_at = ?, last_reconciled_at = ?, error_message = ?, filled_at = {filled_at_expr}
        WHERE broker = ? AND (broker_order_id = ? OR client_order_id = ?)
    """
    params = (broker_status, local_status, float(filled_qty or 0.0), avg_fill_price, avg_fill_price, remaining_qty, _json(raw_broker_json), now_ts, now_ts, error_message, now_ts if 'COALESCE' in filled_at_expr else None, broker, broker_order_id, client_order_id)
    # remove unused timestamp param when filled_at is unchanged
    if 'COALESCE' not in filled_at_expr:
        params = params[:10] + params[11:]
    conn.execute(sql, params)
    conn.commit()
    if client_order_id:
        return get_order_by_client_order_id(conn, client_order_id)
    return _row_to_dict(conn.execute("SELECT * FROM orders WHERE broker = ? AND broker_order_id = ?", (broker, broker_order_id)).fetchone())


def list_open_orders(conn, broker: str | None = None) -> list[dict]:
    params: list[Any] = []
    sql = "SELECT * FROM orders WHERE status IN ('pending_submit', 'submitted', 'partially_filled')"
    if broker is not None:
        sql += " AND broker = ?"
        params.append(broker)
    sql += " ORDER BY submitted_at ASC, id ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def normalize_status(status: str | None) -> str:
    s = (status or "submitted").lower()
    mapping = {
        "new": "submitted", "accepted": "submitted", "pending_new": "submitted", "open": "submitted",
        "partially_filled": "partially_filled", "partial": "partially_filled",
        "filled": "filled", "closed": "filled",
        "canceled": "cancelled", "cancelled": "cancelled", "expired": "cancelled",
        "rejected": "rejected", "stopped": "rejected",
        "error": "error",
    }
    return mapping.get(s, "submitted")


def _json(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, sort_keys=True)

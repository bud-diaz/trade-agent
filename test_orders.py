from db import get_connection, init_db
import orders


def _conn(tmp_path):
    conn = get_connection(str(tmp_path / "orders.db"))
    init_db(conn)
    return conn


def test_create_pending_order_is_idempotent_by_client_order_id(tmp_path):
    conn = _conn(tmp_path)
    try:
        first = orders.create_pending_order(conn, signal_id=None, broker="alpaca", symbol="AAPL", side="buy", qty=1, order_type="market", client_order_id="cid-1", submitted_at=100)
        second = orders.create_pending_order(conn, signal_id=None, broker="alpaca", symbol="AAPL", side="buy", qty=1, order_type="market", client_order_id="cid-1", submitted_at=100)
        assert first["id"] == second["id"]
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    finally:
        conn.close()


def test_update_order_from_broker_moves_to_filled_and_closes(tmp_path):
    conn = _conn(tmp_path)
    try:
        orders.create_pending_order(conn, signal_id=None, broker="alpaca", symbol="AAPL", side="buy", qty=2, order_type="market", client_order_id="cid-2", submitted_at=100)
        orders.mark_order_submitted(conn, client_order_id="cid-2", broker_order_id="bo-2", broker_status="new", now_ts=101)
        row = orders.update_order_from_broker(conn, broker="alpaca", broker_order_id="bo-2", client_order_id="cid-2", broker_status="filled", filled_qty=2, avg_fill_price=10, remaining_qty=0, now_ts=102)
        assert row["status"] == "filled"
        assert row["filled_qty"] == 2
        assert orders.list_open_orders(conn) == []
    finally:
        conn.close()


def test_reconcile_resubmits_pending_order_by_client_id(tmp_path):
    from brokers import BrokerOrderState
    from reconcile import reconcile_open_orders

    conn = _conn(tmp_path)
    try:
        orders.create_pending_order(conn, signal_id=None, broker="fake", symbol="AAPL", side="buy", qty=2, order_type="market", client_order_id="cid-3", submitted_at=100)

        class FakeBroker:
            broker_name = "fake"
            def submit_order(self, intent):
                return BrokerOrderState("fake", "bo-3", intent.client_order_id, intent.symbol, "filled", 2, 10.0, 0, {"id": "bo-3"})

        changes = reconcile_open_orders(conn, {"fake": FakeBroker()}, now_ts=101)
        row = orders.get_order_by_client_order_id(conn, "cid-3")
        assert len(changes) == 1
        assert row["broker_order_id"] == "bo-3"
        assert row["status"] == "filled"
    finally:
        conn.close()

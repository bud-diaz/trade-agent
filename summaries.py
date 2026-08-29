from datetime import datetime, timezone
import live_state as ls
from alerts import alert_daily_summary


def daily_summary(conn, portfolios: dict, current_prices: dict, date: str | None = None) -> dict:
    date = date or datetime.now(timezone.utc).date().isoformat()
    state = ls.get_system_state(conn)
    since_ts = int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp())
    orders = conn.execute("SELECT status, COUNT(*) c FROM orders WHERE submitted_at >= ? GROUP BY status", (since_ts,)).fetchall()
    counts = {r["status"]: r["c"] for r in orders}
    total_equity = sum(p.total_equity(current_prices) for p in portfolios.values()) if portfolios else 0.0
    cash = sum(p.cash for p in portfolios.values()) if portfolios else 0.0
    return {
        "date": date,
        "total_equity": round(total_equity, 2),
        "cash": round(cash, 2),
        "realized_pl_today": round(sum(p.realized_pl_today for p in portfolios.values()), 2) if portfolios else 0.0,
        "unrealized_pl": round(sum(p.unrealized_pl(current_prices) for p in portfolios.values()), 2) if portfolios else 0.0,
        "filled_order_count": counts.get("filled", 0),
        "rejected_error_order_count": counts.get("rejected", 0) + counts.get("error", 0),
        "trading_halted": bool(state.get("trading_halted")),
        "halt_reason": state.get("halt_reason"),
    }


def maybe_send_daily_summary(conn, alert_client, portfolios: dict, current_prices: dict, now=None) -> bool:
    now = now or datetime.now(timezone.utc)
    # Send once per UTC day after 21:00 UTC, which is after regular US market close.
    if now.hour < 21:
        return False
    today = now.date().isoformat()
    state = ls.get_system_state(conn)
    if state.get("last_daily_summary_date") == today:
        return False
    sent = alert_daily_summary(alert_client, daily_summary(conn, portfolios, current_prices, today))
    conn.execute("UPDATE system_state SET last_daily_summary_date = ? WHERE id = 1", (today,))
    conn.commit()
    return sent

"""
Streamlit dashboard for the live paper-trading loop (live_loop.py).

Read-only against the trading logic — this file never imports risk_gate.py
or strategy_ma_rsi.py, and never trades. It only reads SQLite tables and
writes to system_state via live_state.py's halt/clear helpers, exactly the
same functions live_loop.py uses, so there's one implementation of "what
does halted mean" shared by both processes.

Usage:
    streamlit run dashboard.py
"""

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import live_state as ls
from db import get_connection, init_db

st.set_page_config(page_title="Trade Agent — Live Paper Trading", layout="wide")


def _conn():
    conn = get_connection()
    init_db(conn)
    return conn


def _fmt_ts(ts) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


st.title("Trade Agent — Live Paper Trading")
st.caption("Simulated fills against real market data. No broker order is ever placed.")

# ---------------------------------------------------------------------
# Top-level halt controls — outside any fragment, so a click reruns the
# whole page immediately instead of waiting for the next fragment tick.
# ---------------------------------------------------------------------
with _conn() as conn:
    state = ls.get_system_state(conn)

col_status, col_halt, col_reset = st.columns([3, 1, 1])

with col_status:
    if state.get("trading_halted"):
        st.error(f"TRADING HALTED — {state.get('halt_reason') or 'no reason recorded'} "
                  f"(since {_fmt_ts(state.get('halted_at'))})")
    else:
        st.success("Trading active")

with col_halt:
    if st.button("Emergency Halt", width='stretch', disabled=bool(state.get("trading_halted"))):
        with _conn() as conn:
            ls.set_halt(conn, "manual dashboard halt", int(datetime.now(timezone.utc).timestamp()))
        st.rerun()

with col_reset:
    reviewed = st.checkbox("Reviewed recent orders/rejections", value=False)
    if st.button("Reset Kill Switch", width='stretch',
                 disabled=not (state.get("trading_halted") and reviewed)):
        with _conn() as conn:
            ls.clear_halt(conn)
        st.rerun()

st.divider()


# ---------------------------------------------------------------------
# Live panel: per-symbol price/P&L/equity curve. Refreshes on its own —
# this is the part meant to feel "live" without any user interaction.
# ---------------------------------------------------------------------
@st.fragment(run_every="30s")
def live_panel():
    with _conn() as conn:
        cols = st.columns(len(ls.SYMBOLS))
        for cfg, col in zip(ls.SYMBOLS, cols):
            symbol, asset_type = cfg["symbol"], cfg["asset_type"]
            with col:
                st.subheader(symbol)

                snap = conn.execute(
                    "SELECT * FROM portfolio_snapshots WHERE symbol = ? ORDER BY timestamp DESC LIMIT 1",
                    (symbol,),
                ).fetchone()

                if snap is None:
                    st.info("No data yet — start live_loop.py.")
                    continue

                portfolio = ls.reconstruct_portfolio(conn, symbol, asset_type)
                open_qty = portfolio.positions[symbol].qty if symbol in portfolio.positions else 0.0

                m1, m2, m3 = st.columns(3)
                m1.metric("Equity", f"${snap['total_equity']:,.2f}")
                m2.metric("Unrealized P&L", f"${(snap['unrealized_pl'] or 0):,.2f}")
                m3.metric("Position", f"{open_qty:g}")
                st.caption(f"Cash: ${snap['cash']:,.2f}  ·  Realized P&L today: "
                           f"${(snap['realized_pl_today'] or 0):,.2f}  ·  as of {_fmt_ts(snap['timestamp'])}")

                history_df = pd.read_sql_query(
                    "SELECT timestamp, total_equity FROM portfolio_snapshots "
                    "WHERE symbol = ? ORDER BY timestamp ASC",
                    conn, params=(symbol,),
                )
                if len(history_df) > 1:
                    history_df["time"] = pd.to_datetime(history_df["timestamp"], unit="s", utc=True)
                    st.line_chart(history_df.set_index("time")["total_equity"], height=200)

        daily_count = ls.get_system_state(conn).get("daily_trade_count", 0)
        st.caption(f"Trades today (account-wide): {daily_count}")


live_panel()

st.divider()


# ---------------------------------------------------------------------
# History panel: recent signals (with why they were accepted/rejected)
# and the trade blotter. Changes ~once/day per symbol on this strategy,
# so a slower refresh is enough.
# ---------------------------------------------------------------------
@st.fragment(run_every="120s")
def history_panel():
    with _conn() as conn:
        st.subheader("Recent signals")
        signals_df = pd.read_sql_query(
            "SELECT id, symbol, action, confidence, suggested_qty, inputs_json, created_at "
            "FROM signals ORDER BY created_at DESC LIMIT 20",
            conn,
        )
        if signals_df.empty:
            st.caption("No signals recorded yet.")
        else:
            rows = []
            for _, sig in signals_df.iterrows():
                rejections = conn.execute(
                    "SELECT rule_name FROM risk_evaluations WHERE signal_id = ? AND passed = 0",
                    (int(sig["id"]),),
                ).fetchall()
                rows.append({
                    "time": _fmt_ts(sig["created_at"]),
                    "symbol": sig["symbol"],
                    "action": sig["action"],
                    "confidence": round(sig["confidence"], 3) if sig["confidence"] is not None else None,
                    "rejected_by": ", ".join(r["rule_name"] for r in rejections) or ("-" if sig["action"] != "hold" else ""),
                })
            st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

        st.subheader("Trade blotter")
        orders_df = pd.read_sql_query(
            "SELECT filled_at, symbol, side, filled_qty, filled_price, fee "
            "FROM orders WHERE status = 'filled' ORDER BY filled_at DESC LIMIT 20",
            conn,
        )
        if orders_df.empty:
            st.caption("No filled orders yet.")
        else:
            orders_df["time"] = orders_df["filled_at"].apply(_fmt_ts)
            st.dataframe(
                orders_df[["time", "symbol", "side", "filled_qty", "filled_price", "fee"]],
                width='stretch', hide_index=True,
            )

        st.subheader("Price history")
        for cfg in ls.SYMBOLS:
            price_df = pd.read_sql_query(
                "SELECT timestamp, close FROM price_history WHERE symbol = ? AND source = ? "
                "ORDER BY timestamp ASC",
                conn, params=(cfg["symbol"], cfg["source"]),
            )
            if len(price_df) > 1:
                price_df["time"] = pd.to_datetime(price_df["timestamp"], unit="s", utc=True)
                st.caption(cfg["symbol"])
                st.line_chart(price_df.set_index("time")["close"], height=150)


history_panel()

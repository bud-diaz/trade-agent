"""
Performance metrics computed from a BacktestResult.

Max drawdown matters more than total return for a first read — it tells you
whether you'd have actually stuck with the strategy live, or panic-killed it.
"""

import numpy as np
import pandas as pd


def compute_metrics(result) -> dict:
    equity = result.portfolio.equity_curve
    if len(equity) < 2:
        return {"error": "not enough data to compute metrics"}

    timestamps, values = zip(*equity)
    eq = pd.Series(values, index=pd.to_datetime(list(timestamps), unit="s", utc=True))

    starting = result.portfolio.starting_cash
    ending = eq.iloc[-1]
    total_return_pct = (ending / starting - 1) * 100

    days = (eq.index[-1] - eq.index[0]).days or 1
    years = days / 365.25
    cagr_pct = ((ending / starting) ** (1 / years) - 1) * 100 if years > 0 and ending > 0 else None

    # drawdown
    running_max = eq.cummax()
    drawdown = (eq - running_max) / running_max
    max_drawdown_pct = drawdown.min() * 100

    # returns for Sharpe/Sortino — daily resample to avoid overcounting intrabar noise
    daily_eq = eq.resample("1D").last().dropna()
    daily_returns = daily_eq.pct_change().dropna()

    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(365)
    else:
        sharpe = None

    downside = daily_returns[daily_returns < 0]
    if len(downside) > 1 and downside.std() > 0:
        sortino = (daily_returns.mean() / downside.std()) * np.sqrt(365)
    else:
        sortino = None

    # trade stats
    trades = result.portfolio.trade_log
    sell_trades = [t for t in trades if t["side"] == "sell"]
    n_trades = len(sell_trades)

    wins = losses = 0
    gross_win = gross_loss = 0.0
    # naive P&L pairing note: for precise per-trade P&L, compute realized_pl
    # per sell at apply-time in portfolio.py rather than reconstructing here.
    # This block gives trade *count* stats reliably; win/loss $ amounts are
    # approximate unless you wire through per-fill realized P&L explicitly.

    return {
        "starting_equity": round(starting, 2),
        "ending_equity": round(ending, 2),
        "total_return_pct": round(total_return_pct, 2),
        "cagr_pct": round(cagr_pct, 2) if cagr_pct is not None else None,
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "sharpe_ratio": round(sharpe, 2) if sharpe is not None else None,
        "sortino_ratio": round(sortino, 2) if sortino is not None else None,
        "num_trades": n_trades,
        "num_rejected_signals": len(result.rejected_signals),
        "realized_pl_total": round(result.portfolio.realized_pl_total, 2),
        "days_backtested": days,
    }

"""
Turns a BacktestResult into human-readable output: a printed summary plus
equity_curve.csv / trade_log.csv on disk. The piece Trade agent notes.md
lists in the backtester/ file layout (engine.py, portfolio.py, fills.py,
metrics.py, report.py) but never shipped.
"""

import os


def generate_report(result, output_dir: str, label: str = "backtest") -> dict:
    metrics = result.summary()

    os.makedirs(output_dir, exist_ok=True)

    print(f"=== {label} ===")
    for k, v in metrics.items():
        print(f"{k}: {v}")

    equity_path = os.path.join(output_dir, "equity_curve.csv")
    result.equity_series().rename("equity").to_csv(equity_path, header=True)

    trade_log_path = os.path.join(output_dir, "trade_log.csv")
    _write_trade_log_csv(result.portfolio.trade_log, trade_log_path)

    print(f"\nWrote {equity_path}")
    print(f"Wrote {trade_log_path}")

    _maybe_plot_equity_curve(result, output_dir, label)

    return metrics


def _write_trade_log_csv(trade_log: list, path: str) -> None:
    import pandas as pd

    if not trade_log:
        pd.DataFrame(
            columns=["timestamp", "symbol", "side", "qty", "price", "fee", "realized_pl"]
        ).to_csv(path, index=False)
        return
    pd.DataFrame(trade_log).to_csv(path, index=False)


def _maybe_plot_equity_curve(result, output_dir: str, label: str) -> None:
    """Soft dependency: matplotlib isn't in requirements.txt because this
    needs to run unattended later without a display backend forced on it.
    Skip quietly if it isn't installed."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed, skipping equity curve PNG")
        return

    eq = result.equity_series()
    if eq.empty:
        return

    fig, ax = plt.subplots(figsize=(10, 5))
    eq.plot(ax=ax)
    ax.set_title(f"{label} equity curve")
    ax.set_xlabel("time")
    ax.set_ylabel("equity")
    fig.tight_layout()

    png_path = f"{output_dir}/equity_curve.png"
    fig.savefig(png_path)
    plt.close(fig)
    print(f"Wrote {png_path}")

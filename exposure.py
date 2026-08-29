CORRELATION_BUCKETS = {
    "mega_cap_tech": {"AAPL", "MSFT", "NVDA", "GOOGL", "META", "TSLA"},
    "crypto_major": {"BTC/USD", "ETH/USD", "BTC/USDT", "ETH/USDT"},
}


def buckets_for_symbol(symbol: str, buckets: dict[str, set[str]] | None = None) -> list[str]:
    buckets = buckets or CORRELATION_BUCKETS
    return [name for name, symbols in buckets.items() if symbol in symbols]


def _portfolios(portfolio):
    linked = getattr(portfolio, "linked_portfolios", None)
    if linked:
        return list(linked.values())
    return [portfolio]


def projected_bucket_exposure_pct(symbol: str, side: str, qty: float, price: float, portfolio, current_prices: dict[str, float], buckets: dict[str, set[str]] | None = None) -> tuple[float, str | None]:
    bucket_names = buckets_for_symbol(symbol, buckets)
    if not bucket_names:
        return 0.0, None
    bucket = bucket_names[0]
    symbols = (buckets or CORRELATION_BUCKETS)[bucket]
    portfolios = _portfolios(portfolio)
    equity = sum(p.total_equity(current_prices) for p in portfolios)
    if equity <= 0:
        return 1.0, bucket
    value = 0.0
    for p in portfolios:
        for sym, pos in p.positions.items():
            if sym in symbols:
                value += pos.market_value(current_prices.get(sym, pos.avg_entry_price))
    if side == "buy":
        value += qty * price
    elif side == "sell":
        value = max(0.0, value - qty * price)
    return value / equity, bucket

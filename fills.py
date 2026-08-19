"""
Simulated execution: turns an approved signal into a Fill with realistic
slippage and fees baked in. A strategy that only looks profitable at zero
cost is not a strategy — it's a rounding error.
"""

from portfolio import Fill


def simulate_fill(
    symbol: str,
    side: str,
    qty: float,
    reference_price: float,
    slippage_bps: float,
    fee_bps: float,
    timestamp: int,
) -> Fill:
    """
    reference_price: the price we're filling against (next bar's open).
    slippage_bps: basis points of adverse price movement applied on fill.
    fee_bps: basis points charged as a fee, applied to notional value.
    """
    slippage_mult = slippage_bps / 10_000
    # Buys fill slightly worse (higher), sells fill slightly worse (lower) —
    # slippage always works against you, never in your favor.
    if side == "buy":
        fill_price = reference_price * (1 + slippage_mult)
    elif side == "sell":
        fill_price = reference_price * (1 - slippage_mult)
    else:
        raise ValueError(f"Unknown side: {side}")

    notional = qty * fill_price
    fee = notional * (fee_bps / 10_000)

    return Fill(
        symbol=symbol,
        side=side,
        qty=qty,
        price=fill_price,
        fee=fee,
        timestamp=timestamp,
    )

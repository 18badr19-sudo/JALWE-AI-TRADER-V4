"""A single conservative BUY ceiling used by sizing and execution."""
from decimal import Decimal, ROUND_FLOOR


def protected_entry_limit(reference_price, slippage_pct):
    reference = Decimal(str(reference_price))
    slippage = Decimal(str(slippage_pct))
    if not reference.is_finite() or reference <= 0:
        raise ValueError("Entry reference price must be finite and positive.")
    if not slippage.is_finite() or not 0 <= slippage <= 5:
        raise ValueError("MAX_ENTRY_SLIPPAGE_PCT must be finite and between 0 and 5.")
    cap = reference * (1 + slippage / 100)
    tick = Decimal('0.01') if cap >= 1 else Decimal('0.0001')
    limit = cap.quantize(tick, rounding=ROUND_FLOOR)
    if limit <= 0:
        raise ValueError("Entry ceiling is below the minimum price increment.")
    return float(limit)

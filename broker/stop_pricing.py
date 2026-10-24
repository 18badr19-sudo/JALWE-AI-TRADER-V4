"""One price contract for protective-stop preparation, submission and reads."""
import math


def normalize_stop_price(value):
    if isinstance(value, bool):
        raise ValueError('Stop price must be finite and positive.')
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Stop price must be finite and positive.') from exc
    if not math.isfinite(price) or price <= 0:
        raise ValueError('Stop price must be finite and positive.')
    # Preserve the existing broker rounding convention; do not widen stops.
    price = round(price, 2 if price >= 1.0 else 4)
    if price <= 0:
        raise ValueError('Stop price rounds below the minimum price increment.')
    return price


def observed_stop_price(value):
    normalized = normalize_stop_price(value)
    price = float(value)
    if not math.isclose(price, normalized, rel_tol=0., abs_tol=1e-10):
        raise ValueError('Broker stop price has invalid tick precision.')
    return price

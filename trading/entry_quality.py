"""Native last-stage entry checks. Advisory APEX scores never authorize an entry."""
from datetime import datetime, timezone
import math


def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def assess_entry(features, quote, entry, stop, feed, *, now, min_rvol,
                 max_spread_pct, max_spread_r=.25):
    result = {"approved": False, "scope": "NATIVE_ENTRY_CHECK", "feed": feed,
              "min_rvol": min_rvol, "max_spread_pct": max_spread_pct,
              "max_spread_r": max_spread_r, "reason": "INVALID_EVIDENCE"}
    entry, stop = finite(entry), finite(stop)
    rvol = finite(getattr(features, "rvol", None))
    volume = finite(getattr(features, "volume", None))
    dollars = finite(getattr(features, "dollar_volume", None))
    liquidity = finite(getattr(features, "liquidity_score", None))
    result.update(rvol=rvol, volume=volume, dollar_volume=dollars,
                  liquidity_score=liquidity, dollar_volume_scope="latest_completed_bar_only")
    if entry is None or stop is None or not entry > stop > 0:
        return result
    if any(x is None or x <= 0 for x in (rvol, volume, dollars, liquidity)):
        result["reason"] = "MISSING_CURRENT_ACTIVITY"
        return result
    if rvol < min_rvol:
        result["reason"] = "CURRENT_RVOL_BELOW_MIN"
        return result
    if not isinstance(quote, dict) or quote.get("feed") != feed:
        result["reason"] = "QUOTE_FEED_MISMATCH"
        return result
    try:
        at = quote["timestamp"]
        if not isinstance(at, datetime):
            at = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        if at.tzinfo is None:
            raise ValueError("No timezone")
        age = (now - at.astimezone(timezone.utc)).total_seconds()
        if not -5 <= age <= 30:
            raise ValueError("Stale quote")
    except (KeyError, ValueError, TypeError):
        result["reason"] = "STALE_OR_MISSING_QUOTE"
        return result
    bid, ask = finite(quote.get("bid")), finite(quote.get("ask"))
    if bid is None or ask is None or not ask >= bid > 0:
        result["reason"] = "INVALID_QUOTE"
        return result
    spread = ask - bid
    result.update(quote_timestamp=at.isoformat(), checked_at=now.isoformat(),
                  quote_age_seconds=age, bid=bid, ask=ask,
                  spread_pct=100 * spread / ((ask + bid) / 2), spread_r=spread / (entry - stop))
    if result["spread_pct"] > max_spread_pct or result["spread_r"] > max_spread_r:
        result["reason"] = "SPREAD_TOO_LARGE_FOR_RISK"
        return result
    result.update(approved=True, reason="CURRENT_ACTIVITY_AND_QUOTE_PASSED")
    return result

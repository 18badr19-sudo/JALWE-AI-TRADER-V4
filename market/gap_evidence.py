"""Bounded historical trade samples explain missing bars, never synthesize prices."""
from datetime import datetime, timedelta, timezone
import math


def probe_minute(symbol, minute, feed, fetch_page, *, max_pages=2):
    end = minute + timedelta(minutes=1)
    result = {"minute": minute.isoformat(), "feed": feed, "complete": False,
              "count": 0, "regular": 0, "non_price": 0, "unknown": 0,
              "classification": "INCOMPLETE_TRADE_READ"}
    token, seen = None, set()
    for _ in range(max_pages):
        page = fetch_page({"symbols": symbol, "start": minute.isoformat(),
                           "end": end.isoformat(), "feed": feed, "sort": "asc",
                           "limit": 1000, "page_token": token})
        if not isinstance(page, dict) or "trades" not in page:
            raise ValueError("Missing trade response")
        rows = page["trades"] if page["trades"] is not None else {}
        if not isinstance(rows, dict) or not isinstance(rows.get(symbol, []), list):
            raise ValueError("Malformed historical trades")
        for row in rows.get(symbol, []):
            try:
                at = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
                price, size = float(row["p"]), float(row["s"])
                if at.tzinfo is None or not math.isfinite(price) or not math.isfinite(size) or price <= 0 or size <= 0:
                    raise ValueError("Invalid trade")
                # Alpaca's end can be inclusive; the next minute is outside this sample.
                if at == end:
                    continue
                if not minute <= at < end:
                    raise ValueError("Outside sample")
                conditions = row.get("c")
                if not isinstance(conditions, list) or not all(isinstance(c, str) for c in conditions):
                    raise ValueError("Unknown conditions")
            except (KeyError, ValueError, TypeError, OverflowError):
                result["unknown"] += 1
                continue
            result["count"] += 1
            codes = set(conditions)
            if codes == {"@"}:
                result["regular"] += 1
            elif "I" in codes and codes <= {"@", "I"}:
                result["non_price"] += 1
            else:
                result["unknown"] += 1
        token = page.get("next_page_token")
        if not token:
            result["complete"] = True
            break
        if not isinstance(token, str) or token in seen:
            break
        seen.add(token)
    if result["complete"]:
        if result["unknown"]:
            result["classification"] = "UNKNOWN_TRADE_CONDITIONS"
        elif result["regular"]:
            result["classification"] = "REGULAR_TRADES_WITHOUT_BAR"
        elif result["count"]:
            result["classification"] = "ONLY_NON_PRICE_UPDATING_TRADES"
        else:
            result["classification"] = "NO_TRADES_IN_FEED"
    return result

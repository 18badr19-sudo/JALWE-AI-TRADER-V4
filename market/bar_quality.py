"""Compare completed intraday intervals only; retain genuine missing intervals."""
import pandas as pd

INTRADAY_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


def completed_intraday_bars(bars, timeframe, as_of):
    minutes = INTRADAY_MINUTES.get(str(timeframe).strip().lower())
    if minutes is None:
        # Daily sessions require an exchange calendar, not a fixed 24-hour span.
        return bars
    starts = pd.to_datetime(bars.index, utc=True, errors="coerce")
    closed = starts.notna() & (starts + pd.Timedelta(minutes=minutes) <= pd.Timestamp(as_of))
    result = bars.loc[closed].copy()
    result.attrs["completed_bar_diagnostics"] = {
        "timeframe": timeframe,
        "as_of": pd.Timestamp(as_of).isoformat(),
        "excluded_rows": int((~closed).sum()),
        "completed_intraday_only": True,
        "latest_bar_start": starts[closed][-1].isoformat() if closed.any() else None,
    }
    return result

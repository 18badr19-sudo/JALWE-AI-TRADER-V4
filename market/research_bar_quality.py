"""Reject incomplete, un-timestamped, stale or invalid research bars."""
import numpy as np
import pandas as pd

MINUTES = {'1Min': 1, '5Min': 5, '15Min': 15, '30Min': 30, '1Hour': 60}

def validated_research_bars(frame, timeframe, as_of):
    if frame.empty:
        return frame
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError('Research bars require timestamps.')
    now = pd.Timestamp(as_of)
    now = now.tz_localize('UTC') if now.tzinfo is None else now.tz_convert('UTC')
    starts = pd.to_datetime(frame.index, utc=True, errors='coerce')
    frame = frame.copy()
    frame.index = starts
    frame = frame.loc[starts.notna()].sort_index()
    frame = frame.loc[~frame.index.duplicated(keep='last')]
    if timeframe == '1Day':
        local_starts = frame.index.tz_convert('America/New_York')
        closes = local_starts.normalize() + pd.Timedelta(hours=16)
        frame = frame.loc[closes.tz_convert('UTC') <= now]
        max_age = pd.Timedelta(days=10)
    else:
        minutes = MINUTES[timeframe]
        frame = frame.loc[frame.index + pd.Timedelta(minutes=minutes) <= now]
        ny = now.tz_convert('America/New_York')
        regular = ny.dayofweek < 5 and (ny.hour * 60 + ny.minute) >= 570 and (ny.hour * 60 + ny.minute) < 960
        max_age = pd.Timedelta(minutes=max(15, minutes * 3)) if regular else pd.Timedelta(days=4)
    if frame.empty or now - frame.index[-1] > max_age:
        raise ValueError('Research bars are stale or no completed bar is available.')
    values = frame[['open', 'high', 'low', 'close', 'volume']].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values[:, :4] <= 0).any() or (values[:, 4] < 0).any():
        raise ValueError('Invalid research OHLCV values.')
    if (frame['high'] < frame[['open', 'close', 'low']].max(axis=1)).any() or (frame['low'] > frame[['open', 'close', 'high']].min(axis=1)).any():
        raise ValueError('Inconsistent research OHLC values.')
    return frame

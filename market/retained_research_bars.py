"""Preserve provider-returned SIP bars across bounded historical rechecks only."""
from __future__ import annotations

import copy
import math

import pandas as pd

COLUMNS = ['open', 'high', 'low', 'close', 'volume']
PROTOCOL = 'retained-sip-minute-evidence-v1'


def merge_evidence(frame, previous, *, symbol, start, end, checked_at):
    """Return actual observed rows, retaining conflicting copies for rejection.

    This does not request data, fill gaps, choose a revised price, or touch the
    decision feed. At most two distinct copies per minute suffice to preserve a
    conflict. Evidence is scoped to one symbol and the exact fixed SIP window.
    """
    context = dict(protocol=PROTOCOL, symbol=symbol, feed='sip', timeframe='1Min',
                   reader='get_research_observation_bars',
                   window_start=start.isoformat(), window_end=end.isoformat())
    if frame.attrs.get('data_feed') != 'sip':
        raise ValueError('Research observation feed mismatch')
    if previous is not None:
        if not isinstance(previous, dict) or any(previous.get(k) != v for k, v in context.items()):
            raise ValueError('Saved research evidence context mismatch')
        entries = copy.deepcopy(previous.get('minutes'))
        if not isinstance(entries, dict) or len(entries) > 60:
            raise ValueError('Invalid saved research evidence')
    else:
        entries = {}

    def stamp_at(value):
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            raise ValueError('Saved research evidence requires timezone')
        stamp = stamp.tz_convert('UTC')
        if not start <= stamp < end or stamp != stamp.floor('min'):
            raise ValueError('Saved research evidence outside fixed window')
        return stamp

    for at, entry in entries.items():
        stamp_at(at)
        if (not isinstance(entry, dict) or not isinstance(entry.get('copies'), list)
                or not 1 <= len(entry['copies']) <= 2):
            raise ValueError('Invalid saved research evidence copies')
        for values in entry['copies']:
            if (not isinstance(values, list) or len(values) != len(COLUMNS)
                    or any(v is not None and (isinstance(v, bool)
                           or not isinstance(v, (int, float)) or not math.isfinite(v))
                           for v in values)):
                raise ValueError('Invalid saved research OHLCV evidence')

    if not frame.empty:
        if not set(COLUMNS).issubset(frame.columns):
            raise ValueError('Missing research OHLCV evidence')
        current = frame[COLUMNS].copy()
        current.index = pd.to_datetime(current.index, utc=True, errors='coerce')
        current = current.loc[(current.index >= start) & (current.index < end)
                              & (current.index == current.index.floor('min'))]
        for column in COLUMNS:
            current[column] = pd.to_numeric(current[column], errors='coerce')
        for at, values in zip(current.index, current.to_numpy(dtype=float)):
            # null preserves invalid data without non-standard JSON NaN/Infinity.
            values = [float(v) if math.isfinite(v) else None for v in values]
            entry = entries.setdefault(at.isoformat(), dict(copies=[], first_seen=checked_at.isoformat()))
            if values not in entry['copies'] and len(entry['copies']) < 2:
                entry['copies'].append(values)

    stamps, values = [], []
    for at, entry in sorted(entries.items()):
        for row in entry['copies']:
            stamps.append(stamp_at(at))
            values.append(row)
    merged = pd.DataFrame(values, columns=COLUMNS,
                          index=pd.DatetimeIndex(stamps, tz='UTC'), dtype=float)
    merged.attrs = dict(frame.attrs)
    evidence = {**context, 'minutes': entries, 'last_read_at': checked_at.isoformat()}
    return merged, evidence

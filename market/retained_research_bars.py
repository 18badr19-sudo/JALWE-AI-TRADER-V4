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
    return _merge_window(frame, previous, context=context, start=start, end=end,
                         checked_at=checked_at, max_minutes=60)


def merge_session_evidence(frame, previous, *, symbol, start, end, checked_at):
    """MoveProvider's split-adjusted SIP session; preserve its duplicate policy."""
    if (frame.attrs.get('data_feed', 'sip') != 'sip'
            or frame.attrs.get('adjustment', 'split') != 'split'):
        raise ValueError('Session research feed or adjustment mismatch')
    if (not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None):
        raise ValueError('Session research requires timezone-aware minute bars')
    context = dict(protocol='retained-sip-session-evidence-v1', symbol=symbol,
                   feed='sip', adjustment='split', timeframe='1Min', reader='MoveProvider.intraday',
                   window_start=start.isoformat(), window_end=end.isoformat())
    return _merge_window(frame, previous, context=context, start=start, end=end,
                         checked_at=checked_at, max_minutes=390, preserve_duplicates=True)


def _merge_window(frame, previous, *, context, start, end, checked_at,
                  max_minutes, preserve_duplicates=False):
    minutes = (end-start).total_seconds()/60
    if not 0 < minutes <= max_minutes or not minutes.is_integer():
        raise ValueError('Invalid retained research window length')
    if previous is not None:
        if not isinstance(previous, dict) or any(previous.get(k) != v for k, v in context.items()):
            raise ValueError('Saved research evidence context mismatch')
        entries = copy.deepcopy(previous.get('minutes'))
        if not isinstance(entries, dict) or len(entries) > max_minutes:
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
        repeated = set(current.index[current.index.duplicated(keep=False)])
        for at, values in zip(current.index, current.to_numpy(dtype=float)):
            # null preserves invalid data without non-standard JSON NaN/Infinity.
            values = [float(v) if math.isfinite(v) else None for v in values]
            entry = entries.setdefault(at.isoformat(), dict(copies=[], first_seen=checked_at.isoformat()))
            if values not in entry['copies'] and len(entry['copies']) < 2:
                entry['copies'].append(values)
        if preserve_duplicates:
            for at in repeated:
                copies = entries[at.isoformat()]['copies']
                if len(copies) == 1:
                    copies.append(list(copies[0]))

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

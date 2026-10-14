"""Retrospective, one-minute jump paths. Observed association, never causal proof."""
from datetime import timedelta
import math

import pandas as pd

from market_move_research import NY, UP_PCT, utc, valid_bar, daily_features, _before_move

VERSION = 'historical-jump-path-v1'
LOOKBACK_DAYS = 30
SCAN_VERSION = 'historical-jump-selection-v3'


def candidates(symbols, daily, sessions, now):
    """All daily range/gap candidates, including gains that faded before close.

    Daily extrema are screening bounds: their order requires intraday evidence.
    No portfolio membership, IPO age, or post-move closing-gain requirement.
    """
    cutoff = now.astimezone(NY).date() - timedelta(days=LOOKBACK_DAYS)
    closed = [s for s in sessions if utc(s['close']) + timedelta(minutes=16) <= now]
    for symbol in symbols:
        bars = daily.get(symbol, [])
        by_day, duplicate_days = {}, set()
        for bar in bars:
            try:
                if not valid_bar(bar):
                    continue
                day = utc(bar['t']).astimezone(NY).date().isoformat()
                if day in by_day:
                    duplicate_days.add(day)
                by_day[day] = bar
            except (ValueError, TypeError, KeyError):
                continue
        for prior, session in zip(closed, closed[1:]):
            if utc(session['open']).astimezone(NY).date() < cutoff:
                continue
            current = by_day.get(session['date'])
            if current is None or session['date'] in duplicate_days or prior['date'] in duplicate_days:
                continue
            prior_bar = by_day.get(prior['date'])
            if prior_bar is None:
                continue
            try:
                before = daily_features(symbol, [prior_bar, current], session, prior)
                price = float(prior_bar['c'])
                dollars = price * float(prior_bar['v'])
                current_dollars = float(current['c']) * float(current['v'])
                screen = 100 * (float(current['h']) / min(float(current['l']), price) - 1)
                if (before is None or max(dollars,current_dollars) < 100000
                        or not math.isfinite(current_dollars)
                        or not math.isfinite(screen) or screen < UP_PCT):
                    continue
                # Inputs are explicitly retrospective; never frozen forward features.
                yield dict(symbol=symbol, evidence_day=prior['date'], close=price,
                    dollar_volume=dollars, available_after=(utc(prior['close'])+timedelta(minutes=16)).isoformat(),
                    source_feed='sip', jump_protocol=VERSION, selection='DAILY_RANGE_OR_GAP_BOUND_NOT_ORDER',
                    selection_version=SCAN_VERSION, current_dollar_volume=current_dollars,
                    daily_screen_bound_pct=screen,
                    daily_up_bound_pct=max(0.,100*(float(current['h'])/min(float(current['o']),price)-1)),
                    daily_open=float(current['o']),
                    daily_high=float(current['h']), prior_close=price), session
            except (ValueError, TypeError, KeyError, OverflowError):
                continue


def jump_path(frame, session, *, full_session_complete=False):
    """Maximum observed rise from a prior minute's low (or opening price) to a later high.

    Same-bar low-to-high order is never assumed. Each contiguous segment is
    independent; a gap cannot connect an invented start to a later peak.
    """
    result = dict(protocol=VERSION, status='UNAVAILABLE',
        scope='RETROSPECTIVE_OBSERVED_PATH_NOT_CAUSE_OR_PNL',
        onset_method='PRIOR_MINUTE_LOW_OR_SEGMENT_OPEN_TO_LATER_HIGH',
        full_session_complete=bool(full_session_complete), threshold_pct=UP_PCT)
    cols = ['open', 'high', 'low', 'close', 'volume']
    if (not set(cols).issubset(frame.columns) or not isinstance(frame.index, pd.DatetimeIndex)
            or frame.index.tz is None):
        return result
    data = frame.copy()
    data.index = pd.to_datetime(data.index, utc=True, errors='coerce')
    data = data.loc[(data.index >= utc(session['open'])) & (data.index < utc(session['close']))]
    for col in cols:
        data[col] = pd.to_numeric(data[col], errors='coerce')
    duplicate_stamps = data.index[data.index.duplicated(keep=False)]
    valid = data[cols].map(lambda v: pd.notna(v) and math.isfinite(v) and v > 0).all(axis=1)
    valid &= ((data.high >= data[['open','low','close']].max(axis=1))
              & (data.low <= data[['open','close']].min(axis=1)))
    # Exclude both copies of a disputed minute and non-minute-aligned timestamps.
    data = data.loc[valid & ~data.index.isin(duplicate_stamps)
                    & (data.index == data.index.floor('min'))].sort_index()
    if data.empty:
        return result
    expected = pd.date_range(utc(session['open']),utc(session['close']),freq='min',inclusive='left')
    result['full_session_complete'] = bool(full_session_complete and data.index.equals(expected))
    groups = (data.index.to_series().diff() != pd.Timedelta(minutes=1)).cumsum()
    best = None
    for _, segment in data.groupby(groups):
        low, onset, source = float(segment.open.iloc[0]), segment.index[0], 'SEGMENT_OPEN'
        for stamp, bar in segment.iterrows():
            high = float(bar.high)
            gain = 100 * (high / low - 1)
            if math.isfinite(gain) and (best is None or gain > best[0]):
                best = (gain, onset, stamp, low, high, source, segment)
            # A low can support only subsequent minutes, never this bar's high.
            if float(bar.low) < low:
                low, onset, source = float(bar.low), stamp, 'PRIOR_MINUTE_LOW'
    if best is None:
        return result
    gain, onset, peak, base, high, source, segment = best
    path = segment.loc[onset:peak]
    phase_volume = float(path.volume.sum())
    if not math.isfinite(phase_volume):
        return result
    result.update(status='COMPLETE' if result['full_session_complete'] else 'OBSERVED_SEGMENT',
        strong_jump=bool(gain >= UP_PCT), observed_rise_pct=gain,
        start_at=onset.isoformat(), start_price=base, start_price_source=source,
        peak_at=peak.isoformat(), peak_price=high,
        duration_minutes=int((peak-onset).total_seconds()/60),
        one_minute_onset_estimate=True, intrabar_time_and_order_unknown=True,
        phase_volume=phase_volume,
        pre_jump_context=_before_move(segment, onset),
        trajectory=[dict(at=t.isoformat(), **{c:float(row[c]) for c in cols})
                    for t,row in path.iterrows()])
    from market_jump_profiles import build_profile
    result['profile'] = build_profile(result,segment=segment)
    return result

"""Explicit retrospective requests and bounded study scheduling; no trade authority."""
from collections import Counter
import hashlib
import json
import math

from market_move_research import utc
from market_jump_paths import VERSION

REQUEST_ID = 'badr-week-2026-10-05-09-v1'
REQUEST_SYMBOLS = ('MI', 'WFF', 'SAIQ', 'APUS', 'PFAI', 'VEEA')
REQUEST_START, REQUEST_END = '2026-10-05', '2026-10-09'


def requested_keys(sessions, now):
    return {(symbol, s['date']) for s in sessions
            if REQUEST_START <= s['date'] <= REQUEST_END
            and utc(s['close']).timestamp()+960 <= now.timestamp()
            for symbol in REQUEST_SYMBOLS}


def queue_requested(store, sessions, now):
    keys = requested_keys(sessions, now)
    by_day = {s['date']:s for s in sessions}
    # Existing historical keys/outcomes are reused; ownership, tradability,
    # prior-day liquidity and daily availability do not veto an explicit read.
    studies = [(dict(symbol=symbol, jump_protocol=VERSION,
                     selection='EXPLICIT_RETROSPECTIVE_REQUEST_NOT_BUY_SIGNAL',
                     request_id=REQUEST_ID, source_feed='sip'), by_day[day])
               for symbol, day in sorted(keys)]
    digest = hashlib.sha256(json.dumps(sorted(keys)).encode()).hexdigest()
    manifest = store.enqueue_jumps(studies, 'jump_request:'+REQUEST_ID+':'+digest, now, 30)
    store.state('jump_request', dict(request_id=REQUEST_ID, symbols=list(REQUEST_SYMBOLS),
        start=REQUEST_START, end=REQUEST_END, symbol_days=[list(k) for k in sorted(keys)],
        requested_at=manifest['created_at'], automatic_application=False))
    return manifest


def valid_bound(value):
    if type(value) not in (float,int):
        return 0.
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else 0.
    except (ValueError,OverflowError):
        return 0.


def screen_bound(row):
    return valid_bound(row.get('features',{}).get('daily_screen_bound_pct'))


def requested_bounds(daily, sessions, now):
    # Requests are inserted before the daily read and may deliberately have no
    # daily features. Use a separate retrospective scheduling overlay rather
    # than rewriting persisted features, outcomes or frozen forward evidence.
    from market_jump_paths import candidates
    keys = requested_keys(sessions,now)
    return {f['symbol']+'|'+s['date']:valid_bound(f['daily_screen_bound_pct'])
            for f,s in candidates(REQUEST_SYMBOLS,daily,sessions,now)
            if (f['symbol'],s['date']) in keys}


def schedule_studies(rows, keys, limit=12, *, bounds=None):
    """Forward observations first; up to 8 requested seats, remaining discovery.

    Daily range/gap bounds order reads, never prove an ordered price path.
    First observations precede retries within each lane. No API budget increase.
    """
    def rank(r, *, requested=False):
        bound = screen_bound(r)
        if requested:
            bound = max(bound,valid_bound((bounds or {}).get(r['symbol']+'|'+r['session_day'])))
        return (r['status'] != 'PENDING', -bound,
                -int(r['session_day'].replace('-','')), r['attempts'], r['episode_key'])
    forward = sorted((r for r in rows if r['prospective']),key=rank)
    requested = sorted((r for r in rows if not r['prospective'] and r['role']=='JUMP_CASE'
                        and (r['symbol'],r['session_day']) in keys),key=lambda r:rank(r,requested=True))
    other = sorted((r for r in rows if not r['prospective'] and not
                    (r['role']=='JUMP_CASE' and (r['symbol'],r['session_day']) in keys)),key=rank)
    selected = forward[:limit]
    take = min(8,max(0,limit-len(selected)))
    selected += requested[:take]
    selected += other[:max(0,limit-len(selected))]
    selected += requested[take:take+max(0,limit-len(selected))]
    return selected


def request_summary(rows, manifest):
    if not manifest:
        return {}
    keys = {tuple(k) for k in manifest.get('symbol_days',[])}
    requested = [r for r in rows if r['role']=='JUMP_CASE' and not r['prospective']
                 and (r['symbol'],r['session_day']) in keys]
    return dict(**manifest, queued=len(requested), counts=dict(Counter(r['status'] for r in requested)),
        symbols_status=[dict(symbol=s, counts=dict(Counter(r['status'] for r in requested if r['symbol']==s)))
                        for s in manifest['symbols']])

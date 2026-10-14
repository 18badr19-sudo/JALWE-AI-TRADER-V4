"""Evidence profiles for retrospective paths, never a predictive or trade score."""
from collections import Counter
from datetime import timedelta
import math
import json

import pandas as pd

from market_move_research import utc

PROTOCOL = 'historical-jump-profile-v1'
COLS = ('open','high','low','close','volume')
FACTORS = {'VOLUME_BUILDUP':'تسارع حجم قبل البداية', 'POSITIVE_PRE_MOVE':'زخم قبل البداية',
           'ABOVE_CONTEXT_VWAP':'إغلاق عند/فوق VWAP السياق السابق', 'TIGHT_PRE_RANGE':'نطاق سابق ضيق'}


def number(value):
    if type(value) not in (float,int):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError,OverflowError):
        return None


def pct(a,b):
    result = 100*(a/b-1)
    return result if math.isfinite(result) else None


def path_frame(path):
    rows = path.get('trajectory')
    if not isinstance(rows,list) or not 1 <= len(rows) <= 390:
        raise ValueError('INVALID_PATH')
    values, stamps = [], []
    for row in rows:
        at = utc(row['at'])
        if at.second or at.microsecond:
            raise ValueError('INVALID_MINUTE')
        v = [number(row.get(c)) for c in COLS]
        if any(n is None or n <= 0 for n in v) or v[1] < max(v[0],v[2],v[3]) or v[2] > min(v[0],v[3]):
            raise ValueError('INVALID_OHLCV')
        stamps.append(at); values.append(v)
    data = pd.DataFrame(values,index=pd.DatetimeIndex(stamps),columns=COLS)
    expected = pd.date_range(utc(path['start_at']),utc(path['peak_at']),freq='min')
    base, peak = number(path.get('start_price')), number(path.get('peak_price'))
    source = path.get('start_price_source')
    if (not data.index.equals(expected) or base is None or peak is None or base <= 0
            or peak <= 0 or source not in {'PRIOR_MINUTE_LOW','SEGMENT_OPEN'}):
        raise ValueError('INVALID_PATH_ANCHOR')
    anchor = data.low.iloc[0] if source=='PRIOR_MINUTE_LOW' else data.open.iloc[0]
    if not math.isclose(base,anchor,rel_tol=1e-9) or not math.isclose(peak,data.high.iloc[-1],rel_tol=1e-9):
        raise ValueError('PATH_PRICE_MISMATCH')
    return data


def before_context(path, segment):
    onset = utc(path['start_at'])
    result = dict(status='UNAVAILABLE',cutoff=onset.isoformat())
    if segment is not None:
        expected = pd.date_range(onset-pd.Timedelta(minutes=20),onset-pd.Timedelta(minutes=1),freq='min')
        before = segment.loc[segment.index.isin(expected)]
        if before.index.equals(expected):
            total = float(before.volume.sum())
            if not math.isfinite(total) or total<=0:
                return dict(status='UNAVAILABLE_NONFINITE_AGGREGATES',cutoff=onset.isoformat(),bars=20)
            typical = (before.high+before.low+before.close)/3
            vwap = float((typical*(before.volume/total)).sum())
            if not math.isfinite(vwap) or vwap<=0:
                return dict(status='UNAVAILABLE_NONFINITE_AGGREGATES',cutoff=onset.isoformat(),bars=20)
            result.update(status='RAW_20M_COMPLETE',window_start=expected[0].isoformat(),bars=20,
                return_pct=pct(float(before.close.iloc[-1]),float(before.open.iloc[0])),
                volume_acceleration=float(before.volume.tail(5).mean()/before.volume.head(15).mean()),
                close_above_vwap=bool(before.close.iloc[-1]>=vwap),
                range_pct=100*float(before.high.max()-before.low.min())/float(before.close.iloc[-1]),
                average_minute_volume=float(before.volume.mean()))
        return result
    # Reuse only temporally marked derived evidence already saved by jump_path.
    # Do not pretend an unavailable raw pre-window has been recovered.
    saved = path.get('pre_jump_context',{})
    if saved.get('status')=='COMPLETE' and saved.get('bars')==20 and saved.get('cutoff')==onset.isoformat():
        result.update(status='SAVED_DERIVED_CONTEXT_ONLY',bars=20,
            return_pct=number(saved.get('return_pct')),volume_acceleration=number(saved.get('volume_acceleration')),
            close_above_vwap=saved.get('close_above_vwap') if type(saved.get('close_above_vwap')) is bool else None)
    return result


def build_profile(path, *, segment=None):
    result = dict(protocol=PROTOCOL,status='UNAVAILABLE',scope='RETROSPECTIVE_DESCRIPTION_NOT_CAUSE_OR_PNL',
                  automatic_application=False,execution_authority=False,
                  onset_selected_after_move=True,selection_bias='SELECTED_JUMPS_WITHOUT_CONTROL_BASE_RATE')
    try:
        data = path_frame(path)
        if segment is not None:
            if (not isinstance(segment.index,pd.DatetimeIndex) or segment.index.tz is None
                    or not set(COLS).issubset(segment.columns) or segment.index.has_duplicates
                    or not segment.index.is_monotonic_increasing
                    or not segment.index.equals(segment.index.floor('min'))):
                raise ValueError('INVALID_CONTEXT_SEGMENT')
            valid = segment[list(COLS)].map(lambda v:pd.notna(v) and not isinstance(v,bool)
                                            and math.isfinite(v) and v>0).all(axis=1)
            valid &= (segment.high>=segment[['open','low','close']].max(axis=1))
            valid &= (segment.low<=segment[['open','close']].min(axis=1))
            if not valid.all():
                raise ValueError('INVALID_CONTEXT_OHLCV')
            matching = segment.loc[segment.index.isin(data.index),list(COLS)]
            if not matching.index.equals(data.index) or not matching.eq(data).all(axis=None):
                raise ValueError('CONTEXT_PATH_MISMATCH')
        before = before_context(path,segment)
        for key in ('return_pct','volume_acceleration','range_pct','average_minute_volume'):
            if key in before:
                before[key] = number(before[key])
        predicates = {'VOLUME_BUILDUP':('volume_acceleration',lambda v:v>=2),
            'POSITIVE_PRE_MOVE':('return_pct',lambda v:v>=1),
            'ABOVE_CONTEXT_VWAP':('close_above_vwap',lambda v:v is True),
            'TIGHT_PRE_RANGE':('range_pct',lambda v:v<=2)}
        factors = []
        for code,(key,check) in predicates.items():
            value = before.get(key)
            factors.append(dict(code=code,state='UNKNOWN' if value is None else 'PRESENT' if check(value) else 'ABSENT',
                                evidence_ref='before_context.'+key))
        total = float(data.volume.sum())
        if not math.isfinite(total):
            raise ValueError('NONFINITE_VOLUME')
        phases = []
        if len(data)>=3:
            boundaries = (0,len(data)//3,2*len(data)//3,len(data))
            for label,start,end in zip(('EARLY','MIDDLE','LATE'),boundaries,boundaries[1:]):
                part = data.iloc[start:end]
                phases.append(dict(phase=label,bars=len(part),volume=float(part.volume.sum()),
                    volume_share_pct=float(part.volume.sum())/total*100))
        # A prior-minute low cannot be ordered before that same minute's high.
        measured = data.iloc[1:] if path['start_price_source']=='PRIOR_MINUTE_LOW' else data
        milestones = []
        for gain in (8,20,50,100):
            hits = measured.loc[measured.high >= path['start_price']*(1+gain/100)]
            at = None if hits.empty else hits.index[0]
            milestones.append(dict(threshold_pct=gain,at=at.isoformat() if at is not None else None,
                elapsed_minutes=int((at-utc(path['start_at'])).total_seconds()/60) if at is not None else None))
        prior_peak, drawdown = None, 0.
        for _,bar in measured.iterrows():
            if prior_peak is not None:
                drawdown = max(drawdown,100*(1-float(bar.low)/prior_peak))
            prior_peak = max(prior_peak or 0.,float(bar.high))
        after = []
        for horizon in (5,15,30,60):
            window = dict(minutes=horizon,status='UNKNOWN_NOT_RECORDED')
            if segment is not None:
                peak = utc(path['peak_at'])
                expected = pd.date_range(peak+pd.Timedelta(minutes=1),periods=horizon,freq='min')
                following = segment.loc[segment.index.isin(expected)]
                window.update(status='UNKNOWN_INCOMPLETE_WINDOW',observed_minutes=len(following))
                if following.index.equals(expected):
                    window.update(status='COMPLETE_WINDOW',
                        close_vs_peak_pct=pct(float(following.close.iloc[-1]),float(path['peak_price'])),
                        max_decline_from_peak_pct=max(0.,100*(1-float(following.low.min())/float(path['peak_price']))))
            after.append(window)
        result.update(status='AVAILABLE',coverage='COMPLETE_SESSION' if path.get('full_session_complete') is True
                      and path.get('status')=='COMPLETE' else 'OBSERVED_SEGMENT_ONLY',
            evidence_source='CURRENT_SEGMENT_AND_SAVED_PATH' if segment is not None else 'SAVED_PATH_ONLY',
            before_context=before,factors=factors,milestones=milestones,volume_phases=phases,
            max_prior_minute_peak_to_later_low_decline_pct=drawdown,
            decline_order='PRIOR_MINUTE_HIGH_TO_LATER_MINUTE_LOW_NO_INTRABAR_ASSUMPTION',
            phase_volume_scope='WHOLE_ONSET_TO_PEAK_MINUTE_BARS_NOT_EXACT_TICK_ONSET',
            after_peak=after)
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,ZeroDivisionError):
        result['reason']='INVALID_OR_INSUFFICIENT_PROFILE_EVIDENCE'
    return result


def upgrade_saved_profiles(store, now, limit=24):
    from market_jump_common import enrich_profile, PROTOCOL as COMMON_PROTOCOL
    rows = [r for r in store.episodes() if r['role']=='JUMP_CASE' and not r['prospective']
            and utc(r['session']['close'])+timedelta(minutes=16)<=now
            and r['status'] in {'COMPLETE','PARTIAL_DATA'}
            and r['result'].get('jump_path',{}).get('status') in {'COMPLETE','OBSERVED_SEGMENT'}
            and (r['result']['jump_path'].get('profile',{}).get('protocol')!=PROTOCOL
                 or r['result']['jump_path'].get('profile',{}).get('common_protocol')!=COMMON_PROTOCOL)]
    rows.sort(key=lambda r:(r['status']!='COMPLETE',-r['result']['jump_path'].get('observed_rise_pct',0),r['episode_key']))
    done = 0
    daily = (store.state('daily_cache') or {}).get('rows',{})
    sessions = (store.state('jump_session_calendar') or {}).get('sessions',[])
    for row in rows[:limit]:
        existing = row['result']['jump_path'].get('profile',{})
        profile = existing if existing.get('protocol')==PROTOCOL else build_profile(row['result']['jump_path'])
        profile = enrich_profile(profile,row,daily,sessions)
        done += store.enrich_jump_profile(row['episode_key'],row['result'],profile,now)
    return dict(upgraded=done,pending=max(0,len(rows)-done),provider_reads=0)


def summarize_profiles(rows):
    profiles = [(r,r['result'].get('jump_path',{}).get('profile',{})) for r in rows
                if r['role']=='JUMP_CASE' and not r['prospective']]
    observed = [(r,p) for r,p in profiles if p.get('protocol')==PROTOCOL and p.get('status')=='AVAILABLE'
                and r['result']['jump_path'].get('strong_jump') is True]
    eligible = [(r,p) for r,p in observed if r['status']=='COMPLETE' and p.get('coverage')=='COMPLETE_SESSION']
    groups = {}
    for r,p in eligible:
        groups.setdefault((r['symbol'],r['session_day']),[]).append((r,p))
    completed, conflicts = [], 0
    for group in groups.values():
        if len({json.dumps(p,sort_keys=True) for _,p in group})==1:
            completed.append(group[0])
        else:
            conflicts += 1
    factors = []
    for code in FACTORS:
        eligible = [(r,next((f['state'] for f in p['factors'] if f['code']==code),'UNKNOWN')) for r,p in completed]
        counts = Counter(state for _,state in eligible)
        factors.append(dict(code=code,known=counts['PRESENT']+counts['ABSENT'],present=counts['PRESENT'],
                            absent=counts['ABSENT'],unknown=counts['UNKNOWN'],
                            examples=sorted({r['symbol'] for r,state in eligible if state=='PRESENT'})[:3]))
    from market_jump_common import summarize_common
    return dict(protocol=PROTOCOL,scope='RETROSPECTIVE_SELECTED_CASES_NO_PREDICTIVE_EDGE',
        completed_profiles=len(completed),partial_profiles=sum(p.get('coverage')!='COMPLETE_SESSION' for _,p in observed),
        conflicting_symbol_sessions=conflicts,factors=factors,common_factors=summarize_common(completed,FACTORS),
        automatic_application=False,execution_authority=False,control_group_count=0,
        trading_changes_require_separate_validation_and_approval=True)

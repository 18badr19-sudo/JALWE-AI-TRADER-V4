"""Pre-onset associations in selected historical jumps; no trading authority."""
from itertools import combinations
import copy

from market_move_research import daily_features, utc, valid_bar, NY

PROTOCOL = 'historical-jump-common-factors-v1'
FACTORS = {'PRIOR_DAY_VOLUME':'حجم اليوم السابق ≥2×',
           'PRIOR_DAY_MOMENTUM':'صعود اليوم السابق ≥3%',
           'PRIOR_DAY_RANGE':'نطاق اليوم السابق ≥8%'}


def enrich_profile(profile, row, daily, sessions):
    """Keep raw minute evidence intact; daily evidence must precede session open.

    Calendar adjacency is required: missing bars never substitute older days.
    Today's volume/high/close are never a pre-jump explanatory input.
    """
    result = copy.deepcopy(profile)
    result['common_protocol'] = PROTOCOL
    if result.get('status') != 'AVAILABLE':
        return result
    context = dict(status='UNAVAILABLE',source_feed='sip',adjustment='split')
    try:
        target = next(i for i,s in enumerate(sessions) if s['date']==row['session_day'])
        if target < 2:
            raise ValueError('MISSING_PRIOR_SESSIONS')
        prior, older = sessions[target-1], sessions[target-2]
        bars = [b for b in daily.get(row['symbol'],[]) if
                utc(b['t']).astimezone(NY).date().isoformat() in {prior['date'],older['date']}]
        if (any(type(b.get(k)) not in (float,int) for b in bars for k in ('o','h','l','c','v'))
                or any(not valid_bar(b) for b in bars)):
            raise ValueError('INVALID_NUMERIC_EVIDENCE')
        evidence = daily_features(row['symbol'],bars,prior,older)
        onset = utc(row['result']['jump_path']['start_at'])
        cutoff = utc(row['session']['open'])
        if (evidence is None or utc(evidence['available_after']) >= cutoff
                or cutoff > onset or onset >= utc(row['session']['close'])
                or sessions[target]['open'] != row['session']['open']
                or prior['date'] >= row['session_day']):
            raise ValueError('INVALID_PRE_SESSION_EVIDENCE')
        context.update(status='AVAILABLE',evidence_day=prior['date'],
            comparison_day=older['date'],available_after=evidence['available_after'],
            cutoff=cutoff.isoformat(),volume_ratio_previous_day=evidence['volume_ratio_previous_day'],
            daily_return_pct=evidence['daily_return_pct'],range_pct=evidence['range_pct'])
    except (ValueError,TypeError,KeyError,AttributeError,IndexError,StopIteration,OverflowError):
        pass
    tests = {'PRIOR_DAY_VOLUME':('volume_ratio_previous_day',2.),
             'PRIOR_DAY_MOMENTUM':('daily_return_pct',3.), 'PRIOR_DAY_RANGE':('range_pct',8.)}
    result.update(prior_day_context=context,
        prior_day_factors=[dict(code=code,state='UNKNOWN' if key not in context else
            'PRESENT' if context[key]>=threshold else 'ABSENT',evidence_ref='prior_day_context.'+key)
            for code,(key,threshold) in tests.items()])
    return result


def summarize_common(completed, minute_factors):
    """Known-denominator counts, including co-occurrences, not causal inference.

    Groups overlap (a 100% path also belongs to 25%/50%). Only complete,
    conflict-free symbol/session paths supplied by summarize_profiles qualify.
    """
    from market_jump_profiles import number
    codes = tuple(minute_factors)+tuple(FACTORS)
    cases = []
    for row,profile in completed:
        gain = number(row['result']['jump_path'].get('observed_rise_pct'))
        if gain is None:
            continue
        states, seen = {}, set()
        prior = profile.get('prior_day_factors',[]) if profile.get('common_protocol')==PROTOCOL else []
        for f in profile.get('factors',[])+prior:
            code = f.get('code')
            states[code] = 'UNKNOWN' if code in seen else f.get('state')
            seen.add(code)
        cases.append((row,gain,{code:states.get(code,'UNKNOWN') for code in codes}))

    def group(items, threshold):
        factors, shared = [], []
        for selection in [(c,) for c in codes]+list(combinations(codes,2))+list(combinations(codes,3)):
            known = [(r,all(s[c]=='PRESENT' for c in selection)) for r,_,s in items
                     if all(s[c] in {'PRESENT','ABSENT'} for c in selection)]
            present = [r for r,yes in known if yes]
            value = dict(codes=list(selection),known=len(known),present=len(present),
                absent=len(known)-len(present),unknown=len(items)-len(known),
                present_pct=100*len(present)/len(known) if known else None,
                symbols=sorted({r['symbol'] for r in present})[:5])
            (factors if len(selection)==1 else shared).append(value)
        shared.sort(key=lambda v:(-v['present'],-v['known'],v['codes']))
        return dict(min_rise_pct=threshold,cases=len(items),
            unique_symbols=len({r['symbol'] for r,_,_ in items}),
            unique_days=len({r['session_day'] for r,_,_ in items}),
            factors=factors,cooccurrences=[v for v in shared if len(v['codes'])==2],
            triple_cooccurrences=[v for v in shared if len(v['codes'])==3],
            prominent_cooccurrences=[v for v in shared if v['known']>=3 and v['present']>=2][:3])
    return dict(protocol=PROTOCOL,scope='DESCRIPTIVE_SELECTED_WINNERS_NOT_CAUSES',
        all_jumps=group(cases,8),large_jump_groups=[group([v for v in cases if v[1]>=n],n) for n in (25,50,100)],
        group_overlap=True,control_group_count=0,automatic_application=False,execution_authority=False,
        live_predictive_validity='UNPROVEN',thresholds_are_research_only=True)

"""Daily-bar factor benchmarks, including non-jump outcomes; no trading authority.

Daily highs are a coarse, provider-bar endpoint, not a regular-session minute
path, a first-barrier result, an executable fill, or PnL. Historical cohorts
are retrospective; separately sealed pre-open cohorts supply future evidence.
"""
from collections import defaultdict
from datetime import timedelta
import hashlib
import json
import math

from market_move_research import NY, bucket, daily_features, utc, valid_bar
import market_factor_statistics as statistics

PROTOCOL = 'daily-factor-benchmark-v1'
RULES = {'RANGE8': {'range_pct':8.}, 'VOLUME2': {'volume_ratio_previous_day':2.},
         'MOMENTUM3': {'daily_return_pct':3.},
         'ALL_THREE': {'range_pct':8., 'volume_ratio_previous_day':2., 'daily_return_pct':3.}}
SPEC = hashlib.sha256(json.dumps(RULES,sort_keys=True).encode()).hexdigest()
PREFIX = 'daily_factor_cohort_v1:'
RESULT_PREFIX = 'daily_factor_result_v1:'
DELAY = timedelta(hours=24, minutes=16)


def signals(features):
    keys = {key for rule in RULES.values() for key in rule}
    if any(type(features.get(k)) not in (float,int) or not math.isfinite(features[k]) for k in keys):
        return None
    return {rule:all(features[k]>=n for k,n in tests.items()) for rule,tests in RULES.items()}


def record(features):
    flags = signals(features)
    close, dollars = features.get('close'),features.get('dollar_volume')
    if (flags is None or type(close) not in (float,int) or type(dollars) not in (float,int)
            or not math.isfinite(close) or not math.isfinite(dollars)
            or not .5<=close<=100 or dollars<100000 or features.get('source_feed')!='sip'):
        return None
    return dict(symbol=features['symbol'], group=list(bucket(features)), signals=flags,
                features=dict(features))


def daily_index(daily):
    indexed = {}
    for symbol,bars in daily.items():
        days = {}
        for bar in bars:
            try:
                day = utc(bar['t']).astimezone(NY).date().isoformat()
                good = (all(type(bar.get(k)) in (int,float) for k in ('o','h','l','c','v')) and valid_bar(bar))
                # Never choose a winner among duplicate/corrupt rows.
                days[day] = bar if day not in days and good else None
            except (KeyError,TypeError,ValueError,OverflowError):
                continue
        indexed[symbol] = days
    return indexed


def paired(records, day, rule):
    """Seal matching using pre-day price/liquidity buckets, never target outcomes."""
    groups = defaultdict(lambda: ([],[]))
    for row in records:
        groups[tuple(row['group'])][0 if row['signals'][rule] else 1].append(row['symbol'])
    def order(symbol):
        return hashlib.sha256(f'{PROTOCOL}|{day}|{rule}|{symbol}'.encode()).hexdigest()
    return [pair for yes,no in groups.values()
            for pair in zip(sorted(yes,key=order),sorted(no,key=order))]


def daily_outcomes(records, session, indexed):
    outcomes = {}
    for row in records:
        bar = indexed.get(row['symbol'],{}).get(session['date'])
        outcomes[row['symbol']] = None if bar is None else float(bar['h']) >= float(bar['o'])*1.08
    return outcomes


def measure(records, session, indexed, *, outcomes=None, returns=None, market_return=None):
    outcomes = daily_outcomes(records,session,indexed) if outcomes is None else outcomes
    result = {}
    for rule in RULES:
        groups = {}
        for label,match in (('matching',True),('nonmatching',False)):
            symbols = [r['symbol'] for r in records if r['signals'][rule] is match]
            groups[label] = dict(total=len(symbols), reached=sum(outcomes[s] is True for s in symbols),
                not_reached=sum(outcomes[s] is False for s in symbols),unknown=sum(outcomes[s] is None for s in symbols))
        pairs = paired(records,session['date'],rule)
        complete = [(outcomes[a],outcomes[b]) for a,b in pairs if outcomes[a] is not None and outcomes[b] is not None]
        groups['pairs'] = dict(selected=len(pairs), complete=len(complete),
            matching_reached=sum(a for a,b in complete),control_reached=sum(b for a,b in complete),
            day_edge=sum(int(a)-int(b) for a,b in complete)/len(complete) if complete else None)
        result[rule] = groups
    returns=statistics.observe_returns(records,session['date'],indexed) if returns is None else returns
    evidence=statistics.measure(records,session['date'],outcomes,returns,market_return,
        {rule:paired(records,session['date'],rule) for rule in RULES})
    return dict(day=session['date'],rules=result,statistics=evidence)


def aggregate(days):
    result = dict(days=len(days), first_day=min((d['day'] for d in days),default=None),
                  last_day=max((d['day'] for d in days),default=None),rules={})
    for rule in RULES:
        value = {}
        for group in ('matching','nonmatching'):
            counts = {key:sum(d['rules'][rule][group][key] for d in days)
                      for key in ('total','reached','not_reached','unknown')}
            known = counts['reached']+counts['not_reached']
            counts['known_coverage'] = known/counts['total'] if counts['total'] else None
            counts['reached_frequency'] = counts['reached']/known if known else None
            value[group] = counts
        p = {key:sum(d['rules'][rule]['pairs'][key] for d in days)
             for key in ('selected','complete','matching_reached','control_reached')}
        edges = [d['rules'][rule]['pairs']['day_edge'] for d in days if d['rules'][rule]['pairs']['day_edge'] is not None]
        p.update(coverage=p['complete']/p['selected'] if p['selected'] else None,days_with_pairs=len(edges),
                 day_weighted_frequency_difference=sum(edges)/len(edges) if edges else None)
        value['pairs']=p; result['rules'][rule]=value
    result['statistics']=statistics.aggregate([d['statistics'] for d in days if 'statistics' in d],RULES)
    return result


def freeze_next(store, now, defined_at, *, clock=None):
    """Copy only already-sealed pre-open inputs; immutable independent membership."""
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        days = [r[0] for r in conn.execute('''SELECT DISTINCT session_day FROM move_pattern_features
            WHERE session_day>=?''',(now.astimezone(NY).date().isoformat(),))]
        for day in days:
            key = PREFIX+day
            if conn.execute('SELECT 1 FROM move_state WHERE key=?',(key,)).fetchone():
                continue
            records,session = [],None
            frozen = clock() if clock else now
            for encoded, in conn.execute('SELECT payload_json FROM move_pattern_features WHERE session_day=?',(day,)):
                try:
                    value=json.loads(encoded); f,s=value['features'],value['session']
                    if not (s['date']==day and utc(defined_at)<=frozen<utc(s['open'])
                            and utc(f['available_after'])<=utc(value['prepared_at'])<=frozen
                            and utc(value['prepared_at'])<utc(s['open']) and f['evidence_day']<day):
                        continue
                    row=record(f)
                    if row is not None:
                        if session is not None and s!=session:
                            raise RuntimeError('CONFLICTING_BENCHMARK_CALENDAR')
                        session=s; records.append(row)
                except (KeyError,TypeError,ValueError,OverflowError):
                    continue
            if not records:
                continue
            if len({r['symbol'] for r in records})!=len(records):
                raise ValueError('DUPLICATE_BENCHMARK_MEMBERS')
            final = clock() if clock else now
            if final>=utc(session['open']):
                conn.rollback(); return
            payload = dict(protocol=PROTOCOL,spec_hash=SPEC,defined_at=defined_at,frozen_at=final.isoformat(),
                           session=session,records=sorted(records,key=lambda r:r['symbol']))
            conn.execute('INSERT INTO move_state VALUES (?,?)',(key,json.dumps(payload,allow_nan=False)))
        cutoff=(now.astimezone(NY).date()-timedelta(days=60)).isoformat()
        conn.execute('DELETE FROM move_state WHERE key LIKE ? AND key<?',(PREFIX+'%',PREFIX+cutoff))
        conn.execute('DELETE FROM move_state WHERE key LIKE ? AND key<?',(RESULT_PREFIX+'%',RESULT_PREFIX+cutoff))


def update(store, daily, sessions, symbols, now, *, clock=None, source_session=None):
    fixed=store.state('daily_factor_definition')
    if fixed is None:
        fixed=dict(protocol=PROTOCOL,spec_hash=SPEC,defined_at=now.isoformat(),rules=RULES)
        with store.connect() as conn:
            conn.execute('INSERT OR IGNORE INTO move_state VALUES (?,?)',('daily_factor_definition',json.dumps(fixed)))
        fixed=store.state('daily_factor_definition')
    if fixed.get('spec_hash')!=SPEC or fixed.get('protocol')!=PROTOCOL:
        raise ValueError('BENCHMARK_DEFINITION_MISMATCH')
    statistics_definition=store.state('daily_factor_statistics_definition')
    if statistics_definition is None:
        statistics_definition=dict(spec_hash=statistics.SPEC,defined_at=now.isoformat())
        with store.connect() as conn:
            conn.execute('INSERT OR IGNORE INTO move_state VALUES (?,?)',
                ('daily_factor_statistics_definition',json.dumps(statistics_definition)))
        statistics_definition=store.state('daily_factor_statistics_definition')
    if statistics_definition.get('spec_hash')!=statistics.SPEC:
        raise ValueError('STATISTICS_DEFINITION_MISMATCH')
    freeze_next(store,now,fixed['defined_at'],clock=clock)
    mature=[s for s in sessions if utc(s['close'])+DELAY<=now]
    with store.connect() as conn:
        cohort_keys=[r[0] for r in conn.execute('SELECT key FROM move_state WHERE key LIKE ? ORDER BY key',(PREFIX+'%',))]
    cache_key=dict(spec_hash=SPEC,statistics_spec=statistics.SPEC,mature_through=mature[-1]['date'] if mature else None,
                   source_session=source_session,cohorts=cohort_keys)
    saved=store.state('daily_factor_benchmark')
    if saved and saved.get('cache_key')==cache_key:
        return saved
    indexed=daily_index(daily)
    historical=[]
    cutoff=(now.astimezone(NY).date()-timedelta(days=30)).isoformat()
    universe=sorted(set(symbols))
    for i,s in enumerate(sessions):
        if i<2 or s['date']<cutoff or utc(s['close'])+DELAY>now:
            continue
        prior,older=sessions[i-1],sessions[i-2]
        records=[]
        for symbol in universe:
            bars=indexed.get(symbol,{})
            a,b=bars.get(prior['date']),bars.get(older['date'])
            if a is None or b is None:
                continue
            try:
                f=daily_features(symbol,[a,b],prior,older)
                row=record(f) if f and utc(f['available_after'])<utc(s['open']) else None
                if row is not None:
                    records.append(row)
            except (ValueError,TypeError,KeyError,OverflowError):
                continue
        market_return=statistics.close_return(indexed.get(statistics.BENCHMARK,{}).get(s['date']))
        measured=measure(records,s,indexed,market_return=market_return)
        measured['unknown_or_ineligible_inputs']=len(universe)-len(records)
        historical.append(measured)
    forward,pending=[],0
    for key in cohort_keys:
        cohort=store.state(key)
        if cohort.get('protocol')!=PROTOCOL or cohort.get('spec_hash')!=SPEC:
            continue
        if not utc(fixed['defined_at'])<=utc(cohort['frozen_at'])<utc(cohort['session']['open']):
            continue
        if utc(cohort['session']['close'])+DELAY>now:
            pending+=len(cohort['records']); continue
        result_key=RESULT_PREFIX+cohort['session']['date']
        previous=store.state(result_key) or {}
        preserved=(previous.get('outcomes',{}) if previous.get('protocol')==PROTOCOL
            and previous.get('spec_hash')==SPEC and previous.get('frozen_at')==cohort['frozen_at'] else {})
        observed=daily_outcomes(cohort['records'],cohort['session'],indexed)
        # A shrinking daily cache must never erase already observed outcomes.
        outcomes={symbol:preserved[symbol] if type(preserved.get(symbol)) is bool else value
                  for symbol,value in observed.items()}
        observed_returns=statistics.observe_returns(cohort['records'],cohort['session']['date'],indexed)
        same=(previous.get('protocol')==PROTOCOL and previous.get('spec_hash')==SPEC
              and previous.get('frozen_at')==cohort['frozen_at'] and previous.get('statistics_spec')==statistics.SPEC)
        old_returns=previous.get('close_returns',{}) if same else {}
        returns={symbol:old_returns[symbol] if statistics.number(old_returns.get(symbol)) else value
                 for symbol,value in observed_returns.items()}
        market_return=(previous.get('benchmark_return_pct') if same and statistics.number(previous.get('benchmark_return_pct'))
                       else statistics.close_return(indexed.get(statistics.BENCHMARK,{}).get(cohort['session']['date'])))
        store.state(result_key,dict(protocol=PROTOCOL,spec_hash=SPEC,frozen_at=cohort['frozen_at'],
            updated_at=now.isoformat(),outcomes=outcomes,statistics_spec=statistics.SPEC,
            close_returns=returns,benchmark_return_pct=market_return))
        measured=measure(cohort['records'],cohort['session'],indexed,outcomes=outcomes,
                         returns=returns,market_return=market_return)
        if utc(statistics_definition['defined_at'])>=utc(cohort['session']['open']):
            measured.pop('statistics')
        forward.append(measured)
    split=max(1,len(historical)*2//3) if historical else 0
    result=dict(protocol=PROTOCOL,spec_hash=SPEC,rules=RULES,updated_at=now.isoformat(),cache_key=cache_key,
        endpoint='PROVIDER_DAILY_HIGH_GE_OPEN_PLUS_8_NOT_PNL_OR_FIRST_BARRIER',
        retrospective=dict(all_days=aggregate(historical),earlier_period=aggregate(historical[:split]),
                           later_period=aggregate(historical[split:]),
                           excluded_inputs=sum(d['unknown_or_ineligible_inputs'] for d in historical)),
        forward=dict(state='FROZEN_FORWARD_DAILY_DATA' if forward else 'WAITING_FORWARD_DAILY_DATA',
                     pending_symbol_days=pending,cohort_days=len(cohort_keys),evidence=aggregate(forward)),
        statistics_method=dict(protocol=statistics.PROTOCOL,spec_hash=statistics.SPEC,
            defined_at=statistics_definition['defined_at'],
            benchmark=statistics.BENCHMARK,return_endpoint='PROVIDER_DAILY_OPEN_TO_CLOSE_MINUS_SAME_DAY_SPY_BETA_ONE',
            confidence=.95,min_days=statistics.MIN_DAYS,min_pairs=statistics.MIN_PAIRS,
            block_days=statistics.BLOCK_DAYS,resamples=statistics.RESAMPLES,
            scope='DESCRIPTIVE_NOT_PNL_NOT_CAUSAL_NOT_VALIDATED_ALPHA'),
        automatic_application=False,execution_authority=False,validated_edge=False,
        limitations=['Current asset catalog survivorship bias','Retrospective periods are not unseen validation',
            'Daily bar session scope is provider-defined, not verified regular-session minute coverage',
            'Missing bars remain unknown; no stop/target chronology or executable return',
            'Controls matched on coarse prior-price/liquidity buckets; other confounders remain',
            'SPY subtraction assumes beta=1, not estimated risk-adjusted alpha; no execution costs',
            'Bootstrap resamples blocks of five observed days; gaps and longer dependence remain',
            'Intervals are marginal and exploratory, not adjusted for multiple rules or a promotion gate'])
    store.state('daily_factor_benchmark',result)
    return result

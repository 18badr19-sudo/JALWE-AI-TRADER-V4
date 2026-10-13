"""Learn descriptive prior-day associations; apply only PAPER analysis priority.

Retrospective winners propose hypotheses. A sealed training winner is tested
once on twenty later frozen session cohorts. No entry, risk or order API exists.
"""
import hashlib
import json
import math
import sqlite3
from datetime import timedelta
from pathlib import Path

from market_move_research import utc, NY, read_packet, default_path

PROTOCOL = 'prior-day-move-patterns-v1'
MODE = 'PAPER_SCAN_PRIORITY_ONLY'
RULES = {
    'VOLUME_RANGE': dict(volume_ratio_previous_day=2., range_pct=5.),
    'RANGE_MOMENTUM': dict(range_pct=8., daily_return_pct=3.),
    'VOLUME_MOMENTUM': dict(volume_ratio_previous_day=3., daily_return_pct=2.),
}
LABELS = {'VOLUME_RANGE':'نشاط حجم مع نطاق واسع', 'RANGE_MOMENTUM':'نطاق واسع مع زخم يومي',
          'VOLUME_MOMENTUM':'تسارع حجم مع زخم يومي'}
SPEC = hashlib.sha256(json.dumps(RULES,sort_keys=True).encode()).hexdigest()
MIN_TRAIN_PAIRS, MIN_TRAIN_DAYS = 40, 10
VALIDATION_DAYS, MIN_VALIDATION_PAIRS, MIN_MATCHED_DAYS = 20, 40, 10
MIN_COVERAGE = .90


def matches(features, rule):
    """Unavailable inputs stay unknown, not a negative/neutral feature."""
    values = []
    for key, threshold in RULES[rule].items():
        raw = features.get(key)
        if isinstance(raw,bool) or not isinstance(raw,(int,float)) or not math.isfinite(raw):
            return None
        values.append(raw >= threshold)
    return all(values)


def definition(store, now):
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT OR IGNORE INTO move_state VALUES (?,?)',('pattern_definition',json.dumps(
            dict(protocol=PROTOCOL,spec_hash=SPEC,defined_at=now.isoformat(),rules=RULES))))
        result = json.loads(conn.execute("SELECT value FROM move_state WHERE key='pattern_definition'").fetchone()[0])
    if result.get('protocol') != PROTOCOL or result.get('spec_hash') != SPEC:
        raise ValueError('Pattern definition differs from this version')
    return result


def eligible_rows(rows, started, defined):
    accepted = []
    for row in rows:
        f = row['features']
        try:
            good = (row['prospective'] and row['role'] in {'ACTIVITY','CONTROL'}
                and f.get('pattern_protocol') == PROTOCOL and f.get('pattern_spec_hash') == SPEC
                and f.get('pattern_defined_at') == defined
                and f.get('source_feed') == 'sip'
                and utc(f['available_after']) <= utc(row['frozen_at']) < utc(row['session']['open'])
                and utc(row['frozen_at']) >= utc(started))
        except (KeyError,ValueError,TypeError):
            good = False
        if good:
            accepted.append(row)
    return accepted


def paired(rows):
    groups = {}
    for row in rows:
        groups.setdefault((row['session_day'],row['pair_key']),{}).setdefault(row['role'],[]).append(row)
    # Duplicate roles and orphan pairs remain in coverage denominators.
    return list(groups.values())


def assessment(groups, rule):
    complete, known, edges = 0, 0, []
    seen = set()
    for group in groups:
        if set(group) != {'ACTIVITY','CONTROL'} or any(len(v)!=1 for v in group.values()):
            continue
        a,b = group['ACTIVITY'][0],group['CONTROL'][0]
        identities = {(r['session_day'],r['symbol']) for r in (a,b)}
        if len(identities)!=2 or seen.intersection(identities):
            continue
        seen.update(identities)
        ma,mb = matches(a['features'],rule),matches(b['features'],rule)
        known += ma is not None and mb is not None
        valid = all(r['status']=='COMPLETE' and r['result'].get('outcome') in
                    {'UP8_FIRST','DOWN3_FIRST','NO_BARRIER'} for r in (a,b))
        complete += valid
        if not valid or ma is None or mb is None or ma == mb:
            continue
        selected,control = (a,b) if ma else (b,a)
        edge = int(selected['result']['outcome']=='UP8_FIRST')-int(control['result']['outcome']=='UP8_FIRST')
        edges.append((a['session_day'],edge))
    days = sorted({d for d,_ in edges})
    daily = [sum(v for d,v in edges if d==day)/sum(d==day for d,_ in edges) for day in days]
    return dict(matched_pairs=len(edges),matched_days=len(days),
        complete_pair_coverage=complete/len(groups) if groups else 0,
        known_feature_coverage=known/len(groups) if groups else 0,
        daily_edge=sum(daily)/len(daily) if daily else None,
        metric='PAIRED_UP8_FIRST_FREQUENCY_NOT_PNL')


def qualified(evidence, pairs, days):
    return (evidence['matched_pairs'] >= pairs and evidence['matched_days'] >= days
        and evidence['complete_pair_coverage'] >= MIN_COVERAGE
        and evidence['known_feature_coverage'] >= MIN_COVERAGE)


def finalized(rows, now):
    return all(now >= utc(r['session']['close'])+timedelta(minutes=16)
        and r['status'] != 'PENDING' and (r['status']=='COMPLETE' or r['attempts']>=3
             or now > utc(r['session']['close'])+timedelta(hours=48)) for r in rows)


def validation_sample(policy, future):
    """Seal calendar days and membership before reading outcomes; never replace lost rows."""
    plan = policy.setdefault('validation_plan',[])
    registered = {entry['day'] for entry in plan}
    new_days = sorted({r['session_day'] for r in future}-registered)
    for day in new_days[:max(0,VALIDATION_DAYS-len(plan))]:
        members = [dict(symbol=r['symbol'],role=r['role'],pair_key=r['pair_key'],
                        frozen_at=r['frozen_at'],session=r['session'])
                   for r in future if r['session_day']==day]
        plan.append(dict(day=day,members=members))
    available = {}
    for row in future:
        identity = (row['session_day'],row['pair_key'],row['role'],row['symbol'],row['frozen_at'])
        available.setdefault(identity,[]).append(row)
    sample = []
    for entry in plan:
        for member in entry['members']:
            key=(entry['day'],member['pair_key'],member['role'],member['symbol'],member['frozen_at'])
            rows=available.get(key,[])
            if len(rows)==1:
                sample.append(rows[0])
            else:
                sample.append(dict(member,session_day=entry['day'],features={},status='DATA_UNAVAILABLE',
                    attempts=0,result={'outcome':None,'reason':'REGISTERED_VALIDATION_EVIDENCE_UNAVAILABLE'}))
    return [entry['day'] for entry in plan],sample


def public_policy(policy):
    # The durable holdout manifest belongs in SQLite, not repeated packet/log payloads.
    return {k:v for k,v in policy.items() if k!='validation_plan'}


def register_frozen_validation(conn, values):
    """Register holdout membership in the same transaction as the cohort freeze."""
    saved=conn.execute("SELECT value FROM move_state WHERE key='pattern_policy'").fetchone()
    if not saved:
        return
    policy=json.loads(saved[0])
    if policy.get('state')!='WAITING_FRESH_VALIDATION' or policy.get('protocol')!=PROTOCOL:
        return
    fixed=conn.execute("SELECT value FROM move_state WHERE key='pattern_definition'").fetchone()
    if not fixed:
        return
    rows=[dict(symbol=v[2],role=v[3],pair_key=v[4],prospective=bool(v[5]),frozen_at=v[6],
        session_day=v[1],features=json.loads(v[7]),session=json.loads(v[8])) for v in values]
    future=eligible_rows(rows,policy['trained_at'],json.loads(fixed[0])['defined_at'])
    future=[r for r in future if utc(r['frozen_at'])>utc(policy['trained_at'])]
    validation_sample(policy,future)
    conn.execute("UPDATE move_state SET value=? WHERE key='pattern_policy'",(json.dumps(policy),))


def update(store, rows, now):
    fixed = definition(store,now)
    policy = store.state('pattern_policy')
    if policy is None:
        policy = dict(protocol=PROTOCOL,spec_hash=SPEC,generation=1,started_at=fixed['defined_at'],
            state='COLLECTING_TRAINING',mode=MODE,rule=None)
    if policy.get('protocol') != PROTOCOL or policy.get('spec_hash') != SPEC:
        raise ValueError('Pattern policy version mismatch')
    if policy['state'] in {'FAILED_VALIDATION','SUSPENDED','EXPIRED'}:
        if now < utc(policy['retired_at'])+timedelta(days=7):
            return public_policy(policy)
        policy = dict(protocol=PROTOCOL,spec_hash=SPEC,generation=policy['generation']+1,
            started_at=now.isoformat(),state='COLLECTING_TRAINING',mode=MODE,rule=None)
    candidates = eligible_rows(rows,policy['started_at'],fixed['defined_at'])
    mature = [r for r in candidates if now >= utc(r['session']['close'])+timedelta(minutes=16)]
    discovery = {}
    hot = [r for r in rows if not r['prospective'] and r['role']=='HOT_CASE'
           and r['status']=='COMPLETE' and r['result'].get('outcome')=='UP8_FIRST'
           and now >= utc(r['session']['close'])+timedelta(minutes=16)]
    for rule in RULES:
        supporting = [r for r in hot if matches(r['features'],rule) is True]
        discovery[rule] = dict(cases=len(supporting),days=len({r['session_day'] for r in supporting}),
            scope='RETROSPECTIVE_HYPOTHESIS_SUPPORT_NOT_CAUSE_OR_VALIDATION')
    policy['discovery'] = discovery
    if policy['state']=='COLLECTING_TRAINING':
        assessments = {rule:assessment(paired(mature),rule) for rule in RULES}
        policy['candidates'] = assessments
        ready = [(e['daily_edge'],rule) for rule,e in assessments.items()
                 if qualified(e,MIN_TRAIN_PAIRS,MIN_TRAIN_DAYS) and e['daily_edge'] >= .10
                 and discovery[rule]['cases']>=3 and discovery[rule]['days']>=3]
        if ready:
            _,winner = sorted(ready,key=lambda x:(-x[0],x[1]))[0]
            policy.update(state='WAITING_FRESH_VALIDATION',rule=winner,trained_at=now.isoformat(),
                training=assessments[winner],alpha=.01/(policy['generation']*(policy['generation']+1)))
    elif policy['state']=='WAITING_FRESH_VALIDATION':
        future = [r for r in candidates if utc(r['frozen_at']) > utc(policy['trained_at'])]
        days,validation = validation_sample(policy,future)
        policy['validation_sessions_seen'] = len(days)
        if len(days)==VALIDATION_DAYS and finalized(validation,now):
            evidence = assessment(paired(validation),policy['rule'])
            lower = (evidence['daily_edge']-math.sqrt(2*math.log(1/policy['alpha'])/evidence['matched_days'])
                     if evidence['matched_days'] else None)
            evidence.update(lower_bound=lower,alpha=policy['alpha'],session_days=days,
                assumption='INDEPENDENT_DAYS_NOT_PROVEN',method='DAY_LEVEL_HOEFFDING')
            fresh = days[-1] >= (now.astimezone(NY).date()-timedelta(days=7)).isoformat()
            passed = qualified(evidence,MIN_VALIDATION_PAIRS,MIN_MATCHED_DAYS) and lower > 0 and fresh
            policy.update(state='ACTIVE_PRIORITY' if passed else 'FAILED_VALIDATION',validation=evidence)
            if passed:
                policy.update(activated_at=now.isoformat(),expires_at=(now+timedelta(days=14)).isoformat(),
                    policy_id=hashlib.sha256(f"{PROTOCOL}|{policy['generation']}|{policy['rule']}|{now.isoformat()}".encode()).hexdigest())
            else:
                policy['retired_at'] = now.isoformat()
    elif policy['state']=='ACTIVE_PRIORITY':
        forward = [r for r in mature if utc(r['frozen_at']) > utc(policy['activated_at'])]
        monitor = assessment(paired(forward),policy['rule'])
        policy['forward_monitor'] = monitor
        if qualified(monitor,20,5) and monitor['daily_edge'] <= 0:
            policy.update(state='SUSPENDED',retired_at=now.isoformat(),reason='NONPOSITIVE_FORWARD_ASSOCIATION')
        elif now >= utc(policy['expires_at']):
            policy.update(state='EXPIRED',retired_at=now.isoformat(),reason='POLICY_AGE_LIMIT')
    policy['updated_at'] = now.isoformat()
    store.state('pattern_policy',policy)
    return public_policy(policy)


def active_proof(model, now):
    """Reject malformed/nonfinite proof and verify the sealed policy identity."""
    try:
        if (model.get('state')!='ACTIVE_PRIORITY' or model.get('protocol')!=PROTOCOL
            or model.get('spec_hash')!=SPEC or model.get('mode')!=MODE or model.get('rule') not in RULES):
            return False
        def finite(value):
            return type(value) in (int,float) and math.isfinite(value)
        train,val=model['training'],model['validation']
        for evidence in (train,val):
            if any(type(evidence[k]) is not int or evidence[k]<0 for k in ('matched_pairs','matched_days')):
                return False
            if any(not finite(evidence[k]) or not 0<=evidence[k]<=1
                   for k in ('complete_pair_coverage','known_feature_coverage')):
                return False
            if not finite(evidence['daily_edge']) or not -1<=evidence['daily_edge']<=1:
                return False
        generation=model['generation']
        if type(generation) is not int or generation<1:
            return False
        alpha=.01/(generation*(generation+1))
        if (not finite(model['alpha']) or model['alpha']!=alpha or val.get('alpha')!=alpha
            or not finite(val.get('lower_bound')) or val['lower_bound']<=0
            or not qualified(train,MIN_TRAIN_PAIRS,MIN_TRAIN_DAYS) or train['daily_edge']<.10
            or not qualified(val,MIN_VALIDATION_PAIRS,MIN_MATCHED_DAYS)):
            return False
        days=val['session_days']
        if not isinstance(days,list) or len(days)!=VALIDATION_DAYS or days!=sorted(set(days)):
            return False
        bound=val['daily_edge']-math.sqrt(2*math.log(1/alpha)/val['matched_days'])
        if abs(bound-val['lower_bound'])>1e-9:
            return False
        expected=hashlib.sha256(f"{PROTOCOL}|{generation}|{model['rule']}|{model['activated_at']}".encode()).hexdigest()
        return (model.get('policy_id')==expected
            and utc(model['trained_at']) < utc(model['activated_at']) <= now < utc(model['expires_at'])
            and utc(model['updated_at']) <= now)
    except (KeyError,TypeError,ValueError,OverflowError):
        return False


def _prioritize(items, *, now, symbol_of, paper, allow_live, path=None):
    """Stable permutation only. Native eligibility, sizing, stops and exits remain native."""
    baseline = list(items)
    audit = dict(mode='BASELINE',reason='NO_ACTIVE_PATTERN')
    if not paper or allow_live:
        return baseline,dict(mode='BASELINE',reason='PAPER_ONLY')
    packet = read_packet(now=now,path=path)
    if not packet or packet['health'].get('state') != 'READY':
        return baseline,audit
    model = packet.get('patterns') or {}
    if not active_proof(model,now):
        return baseline,audit
    target = default_path() if path is None else path
    conn = sqlite3.connect(Path(target).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
    try:
        row = conn.execute("SELECT value FROM move_state WHERE key='pattern_policy'").fetchone()
        current = json.loads(row[0]) if row else {}
        if not active_proof(current,now) or current.get('policy_id') != model.get('policy_id'):
            return baseline,audit
        symbols = sorted({str(symbol_of(item)).upper() for item in baseline})
        if not symbols:
            return baseline,audit
        placeholders = ','.join('?' for _ in symbols)
        records = conn.execute(f'''SELECT symbol,payload_json FROM move_pattern_features
            WHERE session_day=? AND symbol IN ({placeholders})''',
            (now.astimezone(NY).date().isoformat(),*symbols)).fetchall()
    finally:
        conn.close()
    preferred = set()
    for symbol,encoded in records:
        value = json.loads(encoded)
        f,s = value['features'],value['session']
        if (utc(f['available_after']) <= utc(value['prepared_at']) < utc(s['open']) <= now < utc(s['close'])
            and f.get('source_feed')=='sip' and .5 <= f['close'] <= 100 and f['dollar_volume']>=100000
            and matches(f,model['rule']) is True):
            preferred.add(symbol)
    ordered = sorted(baseline,key=lambda item:str(symbol_of(item)).upper() not in preferred)
    return ordered,dict(mode=MODE,policy_id=model['policy_id'],rule=model['rule'],
                        prioritized_symbols=sorted(preferred),scope='PRIORITY_NOT_ENTRY_AUTHORITY')


def prioritize(items, **kwargs):
    baseline = list(items)
    try:
        return _prioritize(baseline,**kwargs)
    except Exception as exc:
        return baseline,dict(mode='BASELINE',reason='PATTERN_READ_UNAVAILABLE',error_type=type(exc).__name__)

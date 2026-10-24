"""Local, versioned chronological classifier benchmark. Never a trading authority.

Original implementation informed by Qlib experiment recording and FreqAI's
training/inference separation. No third-party framework code or model is loaded.
"""
from collections import Counter
from datetime import timedelta
import hashlib
import json
import math
import platform
from pathlib import Path
import warnings

from market_move_research import utc, observe_session
from market.retained_research_bars import COLUMNS, merge_session_evidence

PROTOCOL='jalwe-frozen-panel-classifier-lab-v1'
FEATURES=('range_pct','daily_return_pct','volume_ratio_previous_day','close','dollar_volume')
SPEC=dict(protocol=PROTOCOL,features=list(FEATURES),family='STANDARD_SCALER_LOGISTIC_C_0_1',
    train_sessions=10,embargo_sessions=1,test_sessions=5,min_train_pairs=30,min_test_pairs=15,
    min_coverage=.90,min_train_class=5,min_test_class=2,
    label='SIP_FULL_SESSION_UP8_BEFORE_DOWN3_NOT_EXECUTED_PNL',
    scope='OUT_OF_TIME_BACKTEST_NOT_FORWARD_PREDICTIONS',execution_authority=False)
SPEC_HASH=hashlib.sha256(json.dumps(SPEC,sort_keys=True).encode()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def vector(features):
    values=[]
    for name in FEATURES:
        value=features.get(name)
        if type(value) not in (int,float) or not math.isfinite(value) or abs(value)>1e20:
            return None
        if name in {'range_pct','volume_ratio_previous_day'} and value<0:return None
        if name in {'close','dollar_volume'} and value<=0:return None
        values.append(math.log1p(value) if name in {'close','dollar_volume','volume_ratio_previous_day'} else value)
    return values


def dataset(store, definition, now):
    """Decode original captured SIP only; unknown pairs stay in the denominator."""
    import pandas as pd
    with store.connect() as conn:
        panels=conn.execute('SELECT * FROM focused_collection_sessions ORDER BY session_day').fetchall()
        result=[];provenance=[]
        for panel in panels:
            manifest=json.loads(panel['manifest_json']);session=manifest['session']
            opening,close=utc(session['open']),utc(session['close'])
            if opening<utc(definition['defined_at']):continue
            if not utc(manifest['frozen_at'])<opening<close:raise ValueError('INVALID_PREOPEN_PANEL')
            members=[m for m in manifest['members'] if m.get('prospective') and m.get('role') in {'ACTIVITY','CONTROL'}]
            pairs={};identities=Counter(m['symbol'] for m in members)
            for m in members:pairs.setdefault(m['pair_key'],[]).append(m)
            records=[]
            for pair,members in sorted(pairs.items()):
                rows=[]
                for member in members:
                    record=dict(day=session['date'],pair_key=pair,symbol=member['symbol'],x=None,y=None,
                                reason='UNKNOWN_OR_INCOMPLETE_EVIDENCE')
                    item=conn.execute('SELECT * FROM move_episodes WHERE episode_key=?',(member['episode_key'],)).fetchone()
                    saved=conn.execute('SELECT * FROM focused_collection_bars WHERE session_day=? AND symbol=?',
                                       (session['date'],member['symbol'])).fetchone()
                    try:
                        if (item and item['prospective'] and item['role']==member['role']
                                and item['pair_key']==pair and item['symbol']==member['symbol']
                                and item['session_day']==session['date'] and identities[member['symbol']]==1
                                and json.loads(item['session_json'])==session):
                            f=json.loads(item['features_json'])
                            x=vector(f)
                            if (x is not None and f.get('source_feed')=='sip' and f.get('evidence_day','9999')<session['date']
                                    and utc(f['available_after'])<=utc(item['frozen_at'])<=utc(manifest['frozen_at'])<opening
                                    and item['frozen_at']==member['frozen_at']):
                                record['x']=x
                                if now<close+timedelta(minutes=16):record['reason']='WAITING_SESSION'
                                elif saved and saved['expires_at']>=now.isoformat():
                                    evidence=json.loads(saved['evidence_json'])
                                    coverage=json.loads(saved['coverage_json'])
                                    if close+timedelta(minutes=16)<=utc(coverage['as_of'])<=now:
                                        if (utc(evidence['last_read_at'])>utc(coverage['as_of']) or
                                                any(utc(v['first_seen'])>utc(coverage['as_of'])
                                                    for v in evidence['minutes'].values())):
                                            raise ValueError('FUTURE_CAPTURE_EVIDENCE')
                                        empty=pd.DataFrame(columns=COLUMNS,index=pd.DatetimeIndex([],tz='UTC'))
                                        empty.attrs.update(data_feed='sip',adjustment='split')
                                        raw,_=merge_session_evidence(empty,evidence,symbol=member['symbol'],start=opening,end=close,checked_at=now)
                                        observed=observe_session(raw,session)
                                        outcome=observed.get('outcome')
                                        if observed['status']=='COMPLETE' and outcome in {'UP8_FIRST','DOWN3_FIRST','NO_BARRIER'}:
                                            record.update(y=int(outcome=='UP8_FIRST'),reason='KNOWN_RESEARCH_LABEL')
                                        elif outcome=='AMBIGUOUS':record['reason']='AMBIGUOUS_LABEL'
                                        provenance.append([member['episode_key'],digest(evidence),digest(f)])
                    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
                        record.update(x=None,y=None,reason='INVALID_SAVED_EVIDENCE')
                    rows.append(record)
                valid=(len(rows)==2 and {m['role'] for m in members}=={'ACTIVITY','CONTROL'}
                       and all(r['x'] is not None and r['y'] is not None for r in rows))
                records.append(dict(pair_key=pair,usable=valid,rows=rows))
            result.append(dict(day=session['date'],closed=now>=close+timedelta(minutes=16),pairs=records))
            if len(result)>=16:break  # fixed first windows; no replacement by outcome.
    return result,digest([result,provenance])


def evaluate(days, now, fingerprint):
    """Fixed day split and train-only scaling; no search/promotion on the holdout."""
    ordered=sorted(days,key=lambda d:d['day'])
    if len({d['day'] for d in ordered})!=len(ordered):raise ValueError('DUPLICATE_SESSION')
    base=dict(protocol=PROTOCOL,spec_hash=SPEC_HASH,scope=SPEC['scope'],
              execution_authority=False,automatic_promotion=False,live_allowed=False,
              dataset_hash=fingerprint,available_sessions=sum(d['closed'] for d in ordered),
              required_sessions=16,requirements=SPEC,metrics=None,artifact=None)
    if len(ordered)<16 or not all(d['closed'] for d in ordered[:16]):
        return dict(base,state='WAITING_CLOSED_PANELS')
    train,test=ordered[:10],ordered[11:16]
    base.update(train_days=[d['day'] for d in train],embargo_days=[ordered[10]['day']],
                test_days=[d['day'] for d in test])
    parts=[]
    for name,part,minimum in (('train',train,30),('test',test,15)):
        pairs=[p for d in part for p in d['pairs']]
        known=[p for p in pairs if p['usable']]
        coverage=len(known)/len(pairs) if pairs else 0
        rows=[r for p in known for r in p['rows']]
        for day in part:
            for pair in day['pairs']:
                if not pair['usable']:continue
                if (len(pair['rows'])!=2 or any(r['day']!=day['day'] or r['pair_key']!=pair['pair_key']
                        or type(r['y']) is not int or r['y'] not in (0,1)
                        or not isinstance(r['x'],list) or len(r['x'])!=len(FEATURES)
                        or any(type(v) not in (int,float) or not math.isfinite(v) for v in r['x'])
                        for r in pair['rows'])):
                    return dict(base,state='BLOCKED_INPUT_CONTRACT')
        base[name+'_pairs']=len(known);base[name+'_coverage']=coverage
        if len(known)<minimum or coverage<.90:
            return dict(base,state='BLOCKED_INCOMPLETE_DATA')
        # Same stock/day cannot count twice across overlapping research roles.
        if len({(r['day'],r['symbol']) for r in rows})!=len(rows):
            return dict(base,state='BLOCKED_DUPLICATE_EVIDENCE')
        counts=Counter(r['y'] for r in rows)
        required=5 if name=='train' else 2
        if set(counts)!={0,1} or min(counts.values())<required:
            return dict(base,state='BLOCKED_CLASS_VARIATION')
        parts.append(rows)
    import sklearn
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.exceptions import ConvergenceWarning
    training,testing=parts
    model=make_pipeline(StandardScaler(),LogisticRegression(C=.1,max_iter=200,solver='lbfgs',random_state=0))
    with warnings.catch_warnings():
        warnings.simplefilter('error',ConvergenceWarning)
        try:model.fit([r['x'] for r in training],[r['y'] for r in training])
        except ConvergenceWarning:return dict(base,state='BLOCKED_NONCONVERGENCE')
    predicted=model.predict_proba([r['x'] for r in testing])[:,1]
    prior=sum(r['y'] for r in training)/len(training)
    brier=sum((float(p)-r['y'])**2 for p,r in zip(predicted,testing))/len(testing)
    baseline=sum((prior-r['y'])**2 for r in testing)/len(testing)
    scaler,lr=model.steps[0][1],model.steps[1][1]
    artifact=dict(family=SPEC['family'],features=list(FEATURES),transform='LOG1P_CLOSE_DOLLAR_VOLUME_RVOL',
                  coefficients=lr.coef_[0].tolist(),intercept=float(lr.intercept_[0]),
                  mean=scaler.mean_.tolist(),scale=scaler.scale_.tolist(),
                  python=platform.python_version(),sklearn=sklearn.__version__,created_at=now.isoformat(),
                  implementation_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  expires_at=(now+timedelta(days=7)).isoformat(),probability_kind='UNCALIBRATED_LOGISTIC_OUTPUT',
                  inference_authority=False)
    artifact['artifact_hash']=digest(artifact)
    return dict(base,state='BACKTEST_RECORDED',artifact=artifact,
        predictions=[dict(day=r['day'],symbol=r['symbol'],observed_label=r['y'],
                          uncalibrated_score=float(p)) for p,r in zip(predicted,testing)],
        metrics=dict(candidate_brier=brier,constant_baseline_brier=baseline,
                     brier_difference=baseline-brier,baseline_training_prevalence=prior,
                     train_samples=len(training),test_samples=len(testing),
                     beats_constant_baseline_descriptively=brier<baseline,
                     native_bot_pnl_comparison=False,forward_validation_complete=False,
                     prediction_scope='FROZEN_PANEL_RESEARCH_EVENT_NOT_PROFIT_OR_NATIVE_BUY'))


def refresh(store, now):
    """Run in the research child at most hourly. No model is imported by trading."""
    with store.connect() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS ai_research_experiments (id TEXT PRIMARY KEY,created_at TEXT NOT NULL,payload_json TEXT NOT NULL)')
        conn.execute('INSERT OR IGNORE INTO move_state VALUES (?,?)',('ai_lab_definition',json.dumps(
            dict(protocol=PROTOCOL,spec_hash=SPEC_HASH,defined_at=now.isoformat()),sort_keys=True)))
        definition=json.loads(conn.execute("SELECT value FROM move_state WHERE key='ai_lab_definition'").fetchone()[0])
        if definition['spec_hash']!=SPEC_HASH:raise ValueError('MODEL_LAB_PROTOCOL_CHANGED')
        row=conn.execute('SELECT created_at,payload_json FROM ai_research_experiments ORDER BY created_at DESC LIMIT 1').fetchone()
        checkpoint=conn.execute("SELECT value FROM move_state WHERE key='ai_lab_last_scan'").fetchone()
        if row and checkpoint and now<utc(json.loads(checkpoint[0])['at'])+timedelta(hours=1):
            return json.loads(row[1])
        conn.execute('INSERT OR REPLACE INTO move_state VALUES (?,?)',
                     ('ai_lab_last_scan',json.dumps(dict(at=now.isoformat()))))
    days,checksum=dataset(store,definition,now)
    if row and json.loads(row[1]).get('dataset_hash')==checksum:
        return json.loads(row[1])
    result=evaluate(days,now,checksum)
    result.update(defined_at=definition['defined_at'],checked_at=now.isoformat())
    result['experiment_id']=digest([SPEC_HASH,checksum,now.isoformat()])
    with store.connect() as conn:
        conn.execute('INSERT OR IGNORE INTO ai_research_experiments VALUES (?,?,?)',
                     (result['experiment_id'],now.isoformat(),json.dumps(result,sort_keys=True,allow_nan=False)))
        conn.execute('DELETE FROM ai_research_experiments WHERE id NOT IN (SELECT id FROM ai_research_experiments ORDER BY created_at DESC LIMIT 30)')
    store.state('ai_model_lab_status',result)
    return result

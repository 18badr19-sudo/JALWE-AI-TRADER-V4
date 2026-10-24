"""Frozen native-candidate quality benchmark. No trading authority.

The recorder stores facts only. Fitting runs in the separate research child;
commands read its saved result. No provider calls, trade labels or promotion.
"""
from collections import Counter
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib,json,math,sqlite3,warnings,logging

NY=ZoneInfo('America/New_York')
PROTOCOL='frozen-grade-liquidity-holdout-v1'
SPEC=dict(protocol=PROTOCOL,train_sessions=10,embargo_sessions=1,test_sessions=5,
    min_train_entries=60,min_test_entries=30,min_train_class=10,min_test_class=5,
    min_coverage=.90,liquidity_filter=55.,costs=[.001,.0025],
    models=['GRADE_SCORE','GRADE_SCORE_PLUS_LIQUIDITY'],family='SCALER_LOGISTIC_C_0_1',
    base_features=['opportunity_score','grade_A_PLUS','grade_A','grade_B'],
    added_feature='liquidity_score',risk_dollars=1.,cash_dollars=100.,target_r=2.,
    endpoint='POSITIVE_COMMON_ENTRY_CURRENT_STOP_PROXY_AFTER_ASSUMED_COSTS',
    unit='FIRST_ELIGIBLE_SYMBOL_SESSION',decision_feed='iex',observation_feed='sip',
    scope='OUT_OF_TIME_SIMULATION_NOT_FORWARD_PREDICTIONS_OR_BROKER_PNL',
    execution_authority=False,automatic_promotion=False)
digest=lambda v:hashlib.sha256(json.dumps(v,sort_keys=True,allow_nan=False).encode()).hexdigest()
SPEC_HASH=digest(SPEC)


def utc(v):
    v=datetime.fromisoformat(str(v).replace('Z','+00:00'))
    if v.tzinfo is None:raise ValueError('NAIVE_TIME')
    return v.astimezone(timezone.utc)


def number(v):
    try:return float(v) if type(v) in (int,float) and math.isfinite(v) else None
    except (OverflowError,TypeError,ValueError):return None


def entry_facts(payload):
    """Whitelist the original score, grade and IEX/SIP liquidity proxy only."""
    try:
        d=payload['jalwe'];m=d['metadata'];f=m['decision_features'];at=utc(payload['timestamp'])
        minutes={'1m':1,'5m':5,'15m':15}.get((m.get('quality_inputs') or {}).get('timeframe'))
        score,liq=number(d.get('opportunity_score')),number(f.get('liquidity_score'))
        grade=d.get('setup_grade');bar=utc(f['timestamp'])
        if (minutes is None or not bar+timedelta(minutes=minutes)<=at<=bar+timedelta(minutes=minutes+max(3,minutes))
                or f.get('data_quality_ok') is not True or f.get('data_is_stale') is not False
                or score is None or liq is None or not 0<=score<=100 or not 0<=liq<=100
                or grade not in {'A+','A','B','C'} or m.get('decision_data_feed') not in {'iex','sip'}):
            return None
        return dict(score=score,grade=grade,liquidity_score=liq,bar_at=bar.isoformat(),
            timeframe=(m.get('quality_inputs') or {})['timeframe'],feed=m['decision_data_feed'],
            liquidity_scope='ORIGINAL_PROVIDER_SCORE_NOT_TOTAL_MARKET_LIQUIDITY')
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):return None


class Recorder:
    def __init__(self,db,now=None,clock=None):
        self.db=db;self.clock=clock or (lambda:datetime.now(timezone.utc));now=now or self.clock()
        with db.connection() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS entry_quality_definitions (
                protocol TEXT PRIMARY KEY,spec_hash TEXT NOT NULL,defined_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entry_quality_trials (
                trial_key TEXT PRIMARY KEY,protocol TEXT NOT NULL,symbol TEXT NOT NULL,
                day TEXT NOT NULL,decided_at TEXT NOT NULL,decision_feed TEXT NOT NULL,
                facts_json TEXT NOT NULL,UNIQUE(protocol,symbol,day,decision_feed));''')
            c.execute('INSERT OR IGNORE INTO entry_quality_definitions VALUES (?,?,?)',
                (PROTOCOL,SPEC_HASH,now.isoformat()))
            self.definition=dict(c.execute('SELECT * FROM entry_quality_definitions WHERE protocol=?',(PROTOCOL,)).fetchone())
        if self.definition['spec_hash']!=SPEC_HASH:raise ValueError('QUALITY_PROTOCOL_CHANGED')
        logging.getLogger(__name__).info('ENTRY_QUALITY_STUDY_READY %s',json.dumps(dict(
            protocol=PROTOCOL,defined_at=self.definition['defined_at'],execution_authority=False,
            automatic_promotion=False),sort_keys=True))

    def record(self,c,key,payload):
        at=utc(payload['timestamp']);feed=(payload['jalwe'].get('metadata') or {}).get('decision_data_feed')
        if not utc(self.definition['defined_at'])<=at<=self.clock()+timedelta(seconds=5) or feed!='iex':return
        c.execute('INSERT OR IGNORE INTO entry_quality_trials VALUES (?,?,?,?,?,?,?)',
            (key,PROTOCOL,payload['symbol'],str(at.astimezone(NY).date()),at.isoformat(),feed,
             json.dumps(entry_facts(payload),allow_nan=False)))


def collect(path,now):
    """Read first frozen cohort, not latest good outcomes or historical winners."""
    if not Path(path).exists():return None,[]
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2);c.row_factory=sqlite3.Row
    try:
        d=c.execute('SELECT * FROM entry_quality_definitions WHERE protocol=?',(PROTOCOL,)).fetchone()
        if not d:return None,[]
        definition=dict(d)
        if definition['spec_hash']!=SPEC_HASH:raise ValueError('QUALITY_PROTOCOL_CHANGED')
        rows=c.execute('''SELECT q.*,t.plan_json FROM entry_quality_trials q
            LEFT JOIN loss_reduction_trials t USING(trial_key)
            WHERE q.protocol=? AND q.decision_feed='iex' AND q.day IN (
                SELECT day FROM entry_quality_trials WHERE protocol=? AND decision_feed='iex'
                GROUP BY day ORDER BY day LIMIT 16)
            ORDER BY q.day,q.decided_at,q.trial_key LIMIT 2001''',(PROTOCOL,PROTOCOL)).fetchall()
        if len(rows)>2000:raise ValueError('QUALITY_READ_LIMIT_EXCEEDED')
        days=sorted({r['day'] for r in rows})[:16];out=[]
        from loss_reduction_study import validate_plan,evaluate,PROTOCOL as LOSS_PROTOCOL
        import pandas as pd
        for r in rows:
            if r['day'] not in days:continue
            item=dict(day=r['day'],symbol=r['symbol'],decision_feed=r['decision_feed'],
                trial_key=r['trial_key'],facts=None,path_known=False,closed=False,results=None)
            try:
                at=utc(r['decided_at'])
                if at<utc(definition['defined_at']) or str(at.astimezone(NY).date())!=r['day']:raise ValueError('INVALID_FROZEN_DATE')
                item['facts']=json.loads(r['facts_json'])
                plan=json.loads(r['plan_json'])
                # An invalid optional stop context must not masquerade as an open session.
                cal=plan['calendar'];opening,close=utc(cal['session']['open']),utc(cal['session']['close'])
                if (not utc(cal['frozen_at'])<opening<=at<close
                        or cal['source']!='FOCUSED_PREOPEN_ALPACA_CALENDAR'):raise ValueError('INVALID_CALENDAR')
                item['closed']=now>=close+timedelta(minutes=16)
                _,_,_,_,end,close=validate_plan(plan)
                if utc(plan['decided_at'])!=at:raise ValueError('PLAN_TIME_MISMATCH')
                item['closed']=now>=close+timedelta(minutes=16)
                saved=c.execute('SELECT result_json FROM loss_reduction_results WHERE trial_key=? AND feed=?',
                                (r['trial_key'],'sip')).fetchone()
                result=json.loads(saved[0]) if saved else {}
                if (result.get('state')=='COMPLETE' and result.get('protocol')==LOSS_PROTOCOL
                        and end+timedelta(minutes=16)<=utc(result['observed_at'])<=now):
                    # Replay only the saved bars through the fixed simulator, not indicators.
                    bars=result['bar_evidence']
                    frame=pd.DataFrame([b['values'] for b in bars],columns=['open','high','low','close','volume'],
                        index=pd.DatetimeIndex([utc(b['at']) for b in bars]));frame.attrs['data_feed']='sip'
                    replay=evaluate(plan,frame,'sip',utc(result['observed_at']))
                    if (replay['state']=='COMPLETE' and replay['data_hash']==result.get('data_hash')
                            and replay['rules']==result.get('rules')):
                        item.update(path_known=True,results={str(cost):replay['rules'][str(cost)]['CURRENT'] for cost in SPEC['costs']})
            except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
                item.update(facts=None,path_known=False,results=None)
            out.append(item)
        return definition,out
    finally:c.close()


def vector(facts,liquidity=False):
    try:
        score,liq=number(facts['score']),number(facts['liquidity_score']);grade=facts['grade']
        if score is None or liq is None or not 0<=score<=100 or not 0<=liq<=100 or grade not in {'A+','A','B','C'}:return None
        return [score]+[float(grade==g) for g in ('A+','A','B')]+([liq] if liquidity else [])
    except (KeyError,TypeError):return None


def evaluate_cohort(rows,now):
    days=sorted({r['day'] for r in rows})[:16]
    base=dict(protocol=PROTOCOL,spec_hash=SPEC_HASH,requirements=SPEC,scope=SPEC['scope'],
        state='WAITING_NEW_COHORT',recorded=len(rows),required_sessions=16,
        available_sessions=sum(all(r['closed'] for r in rows if r['day']==d) for d in days),
        decision_feed='iex',observation_feed='sip',execution_authority=False,automatic_promotion=False,
        native_trade_pnl=False,metrics=None,artifact=None)
    if len(days)<16 or base['available_sessions']<16:return base
    train_days,test_days=days[:10],days[11:16]
    base.update(train_days=train_days,embargo_days=[days[10]],test_days=test_days)
    parts=[]
    for name,selected,minimum,per_class in [('train',train_days,60,10),('test',test_days,30,5)]:
        all_rows=[r for r in rows if r['day'] in selected]
        if any(r.get('decision_feed')!='iex' or type(r.get('closed')) is not bool
                or type(r.get('path_known')) is not bool for r in all_rows):
            return dict(base,state='BLOCKED_INPUT_CONTRACT')
        if len({(r['day'],r['symbol'],r['decision_feed']) for r in all_rows})!=len(all_rows):
            return dict(base,state='BLOCKED_DUPLICATE_EVIDENCE')
        paths=[r for r in all_rows if r['path_known']]
        try:
            if any(type(r['results'][str(cost)]['traded']) is not bool
                or number(r['results'][str(cost)]['net_pnl']) is None
                for r in paths for cost in SPEC['costs']):raise ValueError('INVALID_LABEL')
        except (ValueError,KeyError,TypeError):return dict(base,state='BLOCKED_INPUT_CONTRACT')
        entries=[r for r in paths if r['results']['0.001']['traded'] is True]
        usable=[r for r in entries if vector(r['facts']) is not None]
        path_coverage=len(paths)/len(all_rows) if all_rows else 0
        feature_coverage=len(usable)/len(entries) if entries else 0
        base[name]=dict(recorded=len(all_rows),known_paths=len(paths),no_entry=len(paths)-len(entries),
            usable_entries=len(usable),path_coverage=path_coverage,feature_coverage=feature_coverage)
        if path_coverage<.90 or feature_coverage<.90 or len(usable)<minimum:
            return dict(base,state='BLOCKED_INCOMPLETE_DATA')
        labels=[int(r['results']['0.001']['net_pnl']>0) for r in usable]
        if min(Counter(labels).get(k,0) for k in (0,1))<per_class:
            return dict(base,state='BLOCKED_CLASS_VARIATION')
        parts.append(usable)
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.exceptions import ConvergenceWarning
    training,testing=parts
    y=[int(r['results']['0.001']['net_pnl']>0) for r in training]
    test_y=[int(r['results']['0.001']['net_pnl']>0) for r in testing]
    prior=sum(y)/len(y);metrics={'constant_brier':sum((prior-v)**2 for v in test_y)/len(test_y)};artifacts={}
    for name,add_liquidity in [('GRADE_SCORE',False),('GRADE_SCORE_PLUS_LIQUIDITY',True)]:
        model=make_pipeline(StandardScaler(),LogisticRegression(C=.1,max_iter=200,random_state=0))
        with warnings.catch_warnings():
            warnings.simplefilter('error',ConvergenceWarning)
            try:model.fit([vector(r['facts'],add_liquidity) for r in training],y)
            except ConvergenceWarning:return dict(base,state='BLOCKED_NONCONVERGENCE')
        predicted=model.predict_proba([vector(r['facts'],add_liquidity) for r in testing])[:,1]
        metrics[name]=dict(brier=sum((float(p)-v)**2 for p,v in zip(predicted,test_y))/len(test_y),
            predictions=[dict(trial_key=r['trial_key'],uncalibrated_score=float(p),label=v) for r,p,v in zip(testing,predicted,test_y)])
        scaler,lr=model.steps[0][1],model.steps[1][1]
        artifacts[name]=dict(mean=scaler.mean_.tolist(),scale=scaler.scale_.tolist(),
            coefficients=lr.coef_[0].tolist(),intercept=float(lr.intercept_[0]))
    sensitivity={}
    for cost in SPEC['costs']:
        baseline=challenger=0.;avoided=missed=0
        for r in testing:
            pnl=r['results'][str(cost)]['net_pnl']
            if r['facts']['grade']!='A+':continue
            baseline+=pnl
            if r['facts']['liquidity_score']>=55:challenger+=pnl
            else:avoided+=pnl<0;missed+=pnl>0
        sensitivity[str(cost)]=dict(a_plus_proxy_pnl=baseline,a_plus_liquidity55_proxy_pnl=challenger,
            difference=challenger-baseline,avoided_losses=avoided,missed_winners=missed)
    grades={}
    for g in ('A+','A','B','C'):
        selected=[r for r in testing if r['facts']['grade']==g]
        grades[g]=dict(entries=len(selected),positive_after_assumed_costs=sum(r['results']['0.001']['net_pnl']>0 for r in selected))
    artifact=dict(models=artifacts,created_at=now.isoformat(),expires_at=(now+timedelta(days=7)).isoformat(),
        spec_hash=SPEC_HASH,train_days=train_days,fit_scope='TRAIN_ONLY_SCALING_AND_LABELS',inference_authority=False)
    artifact['artifact_hash']=digest(artifact)
    return dict(base,state='HOLDOUT_RECORDED',metrics=metrics,artifact=artifact,sensitivity=sensitivity,
        holdout_grades=grades,forward_predictions_validated=False,causal_explanation='UNPROVEN')


def refresh(store,now,native_path=None):
    """Research-child only, at most hourly; never called by the command reader."""
    from core.storage import database_path
    old=store.state('entry_quality_status') or {}
    if old.get('spec_hash')==SPEC_HASH and old.get('state')=='HOLDOUT_RECORDED':return old
    try:
        if old.get('spec_hash')==SPEC_HASH and now<utc(old['checked_at'])+timedelta(hours=1):return old
    except (ValueError,KeyError,TypeError):old={}
    try:
        definition,rows=collect(native_path or database_path(),now)
        checksum=digest([definition,rows])
        if old.get('spec_hash')==SPEC_HASH and old.get('dataset_hash')==checksum:
            result=dict(old,checked_at=now.isoformat())
        else:result=evaluate_cohort(rows,now)
        result.update(defined_at=(definition or {}).get('defined_at'),dataset_hash=checksum,checked_at=now.isoformat())
    except (sqlite3.Error,OSError,ValueError,TypeError,KeyError):
        result=dict(protocol=PROTOCOL,spec_hash=SPEC_HASH,state='EVIDENCE_UNAVAILABLE',checked_at=now.isoformat(),
            execution_authority=False,automatic_promotion=False,metrics=None,artifact=None)
    store.state('entry_quality_status',result)
    return result


def text(result):
    labels={'WAITING_NEW_COHORT':'ينتظر العينة الجديدة','BLOCKED_INCOMPLETE_DATA':'بيانات أو عينات غير كافية',
        'BLOCKED_CLASS_VARIATION':'تنوع النتائج غير كاف','HOLDOUT_RECORDED':'اختبار زمني محفوظ',
        'BLOCKED_NONCONVERGENCE':'التدريب لم يتقارب','BLOCKED_DUPLICATE_EVIDENCE':'تكرار أدلة؛ الاختبار محجوب'}
    r=result or {};lines=['🔬 معايرة الدرجة والسيولة — بحث فقط',labels.get(r.get('state'), 'الدليل المحفوظ غير متاح'),
        f"مرشحون مجمدون {r.get('recorded',0)} | جلسات مكتملة {r.get('available_sessions',0)}/16",
        'نموذج بحثي من الدرجة والسكور مقابل نفسهما مع السيولة؛ 10 أيام تدريب، يوم فصل، 5 أيام اختبار.',
        'قرارات IEX فقط، تُقيّم بمسار SIP متأخر مستقل.']
    if r.get('checked_at'):
        try:lines.append('وقت القراءة المحفوظ: '+utc(r['checked_at']).astimezone(ZoneInfo('Asia/Riyadh')).strftime('%Y-%m-%d %H:%M')+' السعودية')
        except (ValueError,TypeError):lines.append('وقت القراءة المحفوظ غير صالح؛ لا نفترض حداثة النتيجة.')
    m=r.get('metrics') or {}
    if m:
        lines.append(f"خطأ Brier: الدرجة {m['GRADE_SCORE']['brier']:.4f} | مع السيولة {m['GRADE_SCORE_PLUS_LIQUIDITY']['brier']:.4f} | الثابت {m['constant_brier']:.4f}؛ الأقل أفضل.")
        for cost,v in r['sensitivity'].items():
            lines.append(f"فلتر سيولة 55، تكلفة {float(cost)*100:.2f}% لكل جانب: فرق افتراضي ${v['difference']:+.2f} | خسائر متجنبة {v['avoided_losses']} | أرباح فائتة {v['missed_winners']}")
    lines+=['60 دخولًا للتدريب و30 للاختبار، تغطية ≥90% وتنوع نتائج؛ عدم الاكتمال يبقى مجهولًا.',
        'الصفقات الثماني القديمة مستبعدة؛ هذه محاكاة لمرشحين وليست PnL الوسيط أو اختبار تنبؤات أمامية.',
        'درجة السيولة تخص المزوّد الأصلي؛ ليست سيولة السوق كلها. لا اعتماد تلقائي أو تغيير شراء/وقف.']
    return '\n'.join(lines)


def read_report(path=None):
    from ai_system import read_state
    from core.storage import data_directory
    return text(read_state(path or data_directory()/'market_move_research.db','entry_quality_status'))

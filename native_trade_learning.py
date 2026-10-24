"""Local regression learned from audited native trades, without external AI.

No pretrained model, provider call, broker SDK, automatic promotion or orders.
"""
from collections import Counter
from datetime import datetime,timedelta,timezone
import hashlib,json,math

PROTOCOL='native-ledger-local-ridge-r-v2'
SOURCE_PROTOCOL='exact-native-entry-audited-ledger-v2'
FEATURES=('liquidity_score','rvol','momentum_score','atr_pct','distance_from_vwap_pct','above_vwap')
SPEC=dict(protocol=PROTOCOL,features=FEATURES,family='LOCAL_STANDARDIZED_RIDGE',alpha=10.,
    min_train=30,min_test=10,min_train_days=10,test_days=5,min_coverage=.90,
    primary_cost_each_side=.001,stress_cost_each_side=.0025,train_reward_clip_r=10.,
    filter_threshold_r=0.,unit='NATIVE_CLOSED_TRADE',decision_feed='iex',
    objective='REALIZED_LEDGER_RETURN_R_AFTER_ASSUMED_EXECUTION_COSTS',
    scope='OUT_OF_TIME_BACKTEST_NOT_FORWARD_PROFIT_VALIDATION',automatic_promotion=False,
    coverage_population='AUDITED_CLOSED_TRADES_INCLUDING_ORIGINAL_EVIDENCE_EXCLUSIONS',
    day_count='ELIGIBLE_VECTORS_WITH_NONOVERLAPPING_LABELS')
digest=lambda v:hashlib.sha256(json.dumps(v,sort_keys=True,allow_nan=False).encode()).hexdigest()
SPEC_HASH=digest(SPEC)


def utc(v):
    t=datetime.fromisoformat(str(v).replace('Z','+00:00'))
    if t.tzinfo is None:raise ValueError('NAIVE_TIME')
    return t.astimezone(timezone.utc)


def num(v):
    try:return float(v) if type(v) in (int,float) and math.isfinite(v) else None
    except (OverflowError,TypeError,ValueError):return None


def vector(snapshot):
    try:
        a,b,c,d,e=(num(snapshot[k]) for k in FEATURES[:-1]);above=snapshot['above_vwap']
        if (any(v is None for v in (a,b,c,d,e)) or type(above) is not bool
                or not 0<=a<=100 or not 0<=b<=1e6 or not 0<=c<=100
                or not 0<d<=100 or abs(e)>1000):return None
        return [a/100,math.log1p(b),c/100,d/10,e/10,float(above)]
    except (KeyError,TypeError):return None


def load_samples(path=None,limit=50,as_of=None):
    from trade_chain_audit import audit
    from trade_factor_review import stamp
    now=as_of or datetime.now(timezone.utc);a=audit(path=path,limit=min(limit,50))
    rows=[];cohort=[];excluded=Counter();counts=Counter(t.get('trade_id') for t in a.get('trades',[]))
    for t in a.get('trades',[]):
        # Keep the original closed-trade population before evidence exclusions.
        # Missing decision/feed/time cannot silently improve measured coverage.
        facts=t.get('entry_factors') or {}
        if t.get('stage')=='CLOSED' and facts.get('decision_feed')!='sip':
            at=stamp(facts.get('entry_at'))
            cohort.append(dict(trade_id=t.get('trade_id'),entry_at=at.isoformat() if at else None))
        try:
            if counts[t['trade_id']]!=1:raise ValueError('DUPLICATE_TRADE')
            if t.get('stage')!='CLOSED':raise ValueError('OPEN_TRADE')
            if not t.get('ledger_complete') or t.get('findings'):raise ValueError('LEDGER_INCOMPLETE_OR_MISMATCHED')
            f=t['entry_factors'];entry=stamp(f['entry_at']);closed=stamp(t['closed_at'],database_time=True)
            if not entry or not closed or not entry<closed<=now:raise ValueError('INVALID_OR_FUTURE_TRADE_TIME')
            if not f['exact_entry_decision_available']:raise ValueError('EXACT_ENTRY_LINK_MISSING')
            bar=stamp(f.get('feature_timestamp'));minutes={'1m':1,'5m':5,'15m':15}.get(f.get('feature_timeframe'))
            if (not minutes or not bar or not bar+timedelta(minutes=minutes)<=entry<=bar+timedelta(minutes=minutes+max(3,minutes))
                    or f.get('feature_quality_ok') is not True or f.get('feature_is_stale') is not False):
                raise ValueError('ORIGINAL_CLOSED_FEATURE_EVIDENCE_MISSING')
            pnl,price,qty,risk=(num(t.get(k)) for k in ('realized_pnl_before_broker_fees','entry_fill_price','initial_quantity','initial_risk_dollars'))
            if any(v is None for v in (pnl,price,qty,risk)) or min(price,qty,risk)<=0:raise ValueError('INVALID_NATIVE_REWARD')
            notional=price*qty;exit_notional=notional+pnl
            if not math.isfinite(notional) or exit_notional<=0:raise ValueError('INVALID_NATIVE_REWARD')
            snapshot=dict(f['features'],above_vwap=f.get('above_vwap'),market_regime=f.get('market_regime'))
            rows.append(dict(trade_id=t['trade_id'],symbol=t['symbol'],strategy=f.get('strategy'),
                entry_at=entry.isoformat(),closed_at=closed.isoformat(),decision_feed=f.get('decision_feed'),
                pnl_pct=max(-25.,min(25.,pnl/notional*100)),pnl_before_fees=pnl,
                entry_notional=notional,exit_notional=exit_notional,initial_risk_dollars=risk,snapshot=snapshot,
                unknown_execution_evidence=t.get('unknown',[])))
        except (KeyError,TypeError,ValueError,OverflowError) as exc:
            reason=str(exc) if isinstance(exc,ValueError) else 'MISSING_ORIGINAL_INPUTS';excluded[reason]+=1
    return dict(protocol=SOURCE_PROTOCOL,samples=rows,cohort=cohort,excluded=dict(excluded),audit_state=a['state'],
        audited_trades=len(a.get('trades',[])),truncated=a.get('truncated',False),limit=min(limit,50))


def reward(row,cost):
    return (row['pnl_before_fees']-cost*(row['entry_notional']+row['exit_notional']))/row['initial_risk_dollars']


def fit_evaluate(samples,now,cohort=None):
    base=dict(protocol=PROTOCOL,spec_hash=SPEC_HASH,state='COLLECTING_NATIVE_TRADES',requirements=SPEC,
        source_trades=len(samples),metrics=None,artifact=None,execution_authority=False,
        automatic_promotion=False,fees='UNAVAILABLE_COSTS_ARE_ASSUMPTIONS',forward_validation_complete=False)
    rows=sorted([r for r in samples if r.get('decision_feed')=='iex'],key=lambda r:(utc(r['entry_at']),r['trade_id']))
    if len({r['trade_id'] for r in rows})!=len(rows):return dict(base,state='BLOCKED_DUPLICATE_TRADES')
    from zoneinfo import ZoneInfo
    ny=ZoneInfo('America/New_York')
    day=lambda r:str(utc(r['entry_at']).astimezone(ny).date())
    population=[dict(trade_id=r['trade_id'],entry_at=r['entry_at']) for r in rows] if cohort is None else cohort
    try:
        identity={r['trade_id']:utc(r['entry_at']) for r in population}
        if (len(identity)!=len(population) or any(not isinstance(k,str) or not k for k in identity)
                or any(identity.get(r['trade_id'])!=utc(r['entry_at']) for r in rows)):
            return dict(base,state='BLOCKED_COHORT_EVIDENCE')
        days=sorted({day(r) for r in population})
    except (KeyError,TypeError,ValueError):return dict(base,state='BLOCKED_COHORT_EVIDENCE')
    usable=[r for r in rows if vector(r['snapshot']) is not None]
    base.update(known_vectors=len(usable),entry_days=len(days))
    if len(days)<15:return base
    boundary=days[-5];start=min(utc(r['entry_at']) for r in population if day(r)>=boundary)
    parts=[]
    for name,minimum in [('train',30),('test',10)]:
        selected=[r for r in rows if (day(r)<boundary)==(name=='train')]
        recorded=sum((day(r)<boundary)==(name=='train') for r in population)
        # Labels closing in/after the test cannot train the model, even for an older entry.
        known=[r for r in selected if vector(r['snapshot']) is not None and (name=='test' or utc(r['closed_at'])<start)]
        coverage=len(known)/recorded if recorded else 0
        eligible_days=len({day(r) for r in known})
        base[name]=dict(eligible=len(known),recorded=recorded,coverage=coverage,eligible_days=eligible_days)
        if len(known)<minimum or coverage<.90:return dict(base,state='BLOCKED_SAMPLE_OR_COVERAGE')
        if eligible_days<(SPEC['min_train_days'] if name=='train' else SPEC['test_days']):
            return dict(base,state='BLOCKED_ELIGIBLE_DAYS')
        rewards=[reward(r,.001) for r in known]
        if any(not math.isfinite(y) or utc(r['closed_at'])>now for r,y in zip(known,rewards)):
            return dict(base,state='BLOCKED_REWARD_OR_TIME')
        required=5 if name=='train' else 2
        if min(sum(y>0 for y in rewards),sum(y<0 for y in rewards))<required:
            return dict(base,state='BLOCKED_OUTCOME_VARIATION')
        parts.append(known)
    import numpy as np
    train,test=parts;x=np.asarray([vector(r['snapshot']) for r in train]);y=np.clip([reward(r,.001) for r in train],-10,10)
    mean=x.mean(axis=0);scale=x.std(axis=0);scale[scale<1e-12]=1.
    z=(x-mean)/scale;intercept=float(y.mean())
    coef=np.linalg.solve(z.T@z+10.*np.eye(len(FEATURES)),z.T@(y-intercept))
    predictions=np.clip((np.asarray([vector(r['snapshot']) for r in test])-mean)/scale@coef+intercept,-10,10)
    truth=np.asarray([reward(r,.001) for r in test]);candidate=float(np.mean((predictions-truth)**2));baseline=float(np.mean((intercept-truth)**2))
    comparisons={}
    for cost in (.001,.0025):
        values=[r['pnl_before_fees']-cost*(r['entry_notional']+r['exit_notional']) for r in test]
        kept=[v for p,v in zip(predictions,values) if p>0]
        comparisons[str(cost)]=dict(native_ledger_after_assumed_costs=sum(values),filtered_proxy=sum(kept),
            difference=sum(kept)-sum(values),avoided_losses=int(sum(v<0 and p<=0 for p,v in zip(predictions,values))),
            missed_winners=int(sum(v>0 and p<=0 for p,v in zip(predictions,values))),kept=int(sum(p>0 for p in predictions)))
    artifact=dict(protocol=PROTOCOL,spec_hash=SPEC_HASH,features=FEATURES,mean=mean.tolist(),scale=scale.tolist(),
        coefficients=coef.tolist(),intercept=intercept,trained_through=max(r['closed_at'] for r in train),
        created_at=now.isoformat(),expires_at=(now+timedelta(days=7)).isoformat(),inference_authority='OBSERVATION_ONLY')
    artifact['artifact_hash']=digest(artifact)
    return dict(base,state='NATIVE_HOLDOUT_RECORDED',artifact=artifact,
        metrics=dict(candidate_mse_r2=candidate,constant_mse_r2=baseline,test_trades=len(test),
            hypothesis_supported=candidate<baseline and all(v['difference']>0 and v['filtered_proxy']>0
                and v['avoided_losses']>0 and v['kept']>=max(3,math.ceil(.3*len(test))) for v in comparisons.values()),
            comparisons=comparisons,baseline_scope='FILTER_PROXY_ON_SAME_EXECUTED_TRADES_NOT_NEW_PORTFOLIO'),
        dataset_hash=digest(samples),cohort_hash=digest(population),coefficients_are_causal=False)


def refresh(store,now,path=None):
    old=store.state('native_local_model') or {}
    try:
        if old.get('spec_hash')==SPEC_HASH and utc(old['checked_at'])<=now<utc(old['checked_at'])+timedelta(hours=1):return old
    except (KeyError,ValueError,TypeError):pass
    source=load_samples(path,as_of=now)
    try:result=fit_evaluate(source['samples'],now,cohort=source.get('cohort'))
    except (ValueError,KeyError,TypeError,OverflowError):result=dict(protocol=PROTOCOL,spec_hash=SPEC_HASH,state='INVALID_ORIGINAL_INPUTS',artifact=None,metrics=None,execution_authority=False)
    result.update(checked_at=now.isoformat(),source_audit={k:v for k,v in source.items() if k not in ('samples','cohort')},
        coverage_population=len(source.get('cohort',[])))
    store.state('native_local_model',result);return result


def forecast(payload,path=None):
    from ai_system import read_state
    from core.storage import data_directory
    r=read_state(path or data_directory()/'market_move_research.db','native_local_model') or {};a=r.get('artifact') or {}
    base=dict(protocol=PROTOCOL,state='NO_VALIDATED_LOCAL_MODEL',expected_r=None,execution_authority=False)
    try:
        at=utc(payload['timestamp'])
        if (r.get('spec_hash')!=SPEC_HASH or not (r.get('metrics') or {}).get('hypothesis_supported')
                or a.get('artifact_hash')!=digest({k:v for k,v in a.items() if k!='artifact_hash'})
                or not utc(a['trained_through'])<utc(a['created_at'])<=at<utc(a['expires_at'])):return base
        if (a.get('protocol')!=PROTOCOL or a.get('spec_hash')!=SPEC_HASH or list(a.get('features',[]))!=list(FEATURES)
                or any(len(a[k])!=len(FEATURES) for k in ('mean','scale','coefficients'))
                or any(num(v) is None for k in ('mean','scale','coefficients') for v in a[k])
                or num(a.get('intercept')) is None or any(s<=0 for s in a['scale'])):return base
        d=payload['jalwe'];m=d['metadata'];f=m['decision_features'];minutes={'1m':1,'5m':5,'15m':15}.get((m.get('quality_inputs') or {}).get('timeframe'))
        if (m.get('decision_data_feed')!='iex' or f.get('data_quality_ok') is not True or f.get('data_is_stale') is not False
                or not minutes or not utc(f['timestamp'])+timedelta(minutes=minutes)<=at<=utc(f['timestamp'])+timedelta(minutes=minutes+max(3,minutes))):return base
        x=vector(f)
        if x is None:return base
        values=[sum(c*(v-mu)/s for c,v,mu,s in zip(a['coefficients'],x,a['mean'],a['scale']))+a['intercept']]
        if not all(math.isfinite(v) for v in values):return base
        return dict(base,state='LOCAL_FORECAST_RECORDED',expected_r=max(-10.,min(10.,values[0])),
            artifact_hash=a['artifact_hash'],preference='REVIEW_SKIP' if values[0]<=0 else 'REVIEW_KEEP',
            scope='LOCAL_UNCALIBRATED_RETURN_ESTIMATE_NOT_AN_ORDER')
    except (KeyError,ValueError,TypeError,ZeroDivisionError,OverflowError):return base


def status_text(path=None,symbol=None,native_path=None):
    from ai_system import read_state
    from core.storage import data_directory
    r=read_state(path or data_directory()/'market_move_research.db','native_local_model') or {};s=r.get('source_audit') or {}
    lines=['🧠 JALWE — نموذج تداول محلي مستقل',
        'الحالة: '+str(r.get('state','الدليل المحفوظ غير متاح')),
        f"سجل مقروء {s.get('audited_trades','غير متاح')} | عينات موثقة {r.get('source_trades','غير متاحة')} | متجهات مكتملة {r.get('known_vectors','غير متاحة')}",
        'يتعلم عائد الصفقة نسبةً إلى مخاطرتها الأولية، بعد تكاليف مفترضة؛ لا يستخدم نموذجًا أو خدمة AI خارجية.',
        'الحد الأدنى: 30 صفقة تدريب و10 اختبار لاحق، 10 أيام تدريب و5 اختبار، تغطية ≥90% وتنوع نتائج.']
    if not r.get('artifact'):lines.append('التدريب لم يكتمل؛ لا توجد توقعات نموذج معتمد للمقارنة بعد.')
    reasons={'ORIGINAL_CLOSED_FEATURE_EVIDENCE_MISSING':'دليل الشمعة الأصلية أو فترتها غير مكتمل',
        'EXACT_ENTRY_LINK_MISSING':'ربط قرار الدخول غير متاح',
        'LEDGER_INCOMPLETE_OR_MISMATCHED':'سجل التنفيذ ناقص أو غير متطابق',
        'OPEN_TRADE':'صفقة مفتوحة', 'INVALID_OR_FUTURE_TRADE_TIME':'توقيت الصفقة غير صالح'}
    for reason,count in sorted((s.get('excluded') or {}).items()):
        lines.append(f'مستبعد {count}: {reasons.get(reason,reason)}')
    for name,label in (('train','التدريب'),('test','الاختبار اللاحق')):
        part=r.get(name) or {}
        if part:lines.append(f"{label}: {part['eligible']}/{part['recorded']} عينة | تغطية {part['coverage']:.1%} | أيام صالحة {part.get('eligible_days','غير متاحة')}")
    if s.get('truncated'):lines.append('التغطية لآخر 50 سجل صفقة فقط؛ ليست كل السجل التاريخي.')
    metrics=r.get('metrics') or {}
    if metrics:
        lines.append(f"متوسط مربع خطأ العائد (بوحدة R²): النموذج {metrics['candidate_mse_r2']:.3f} | الثابت {metrics['constant_mse_r2']:.3f}")
        for cost,v in metrics['comparisons'].items():lines.append(f"تكلفة {float(cost)*100:.2f}% لكل جانب: فرق ترشيح افتراضي ${v['difference']:+.2f} | خسائر متجنبة {v['avoided_losses']} | أرباح فائتة {v['missed_winners']}")
        a=r.get('artifact') or {}
        if a.get('artifact_hash')==digest({k:v for k,v in a.items() if k!='artifact_hash'}):
            top=sorted(zip(a['features'],a['coefficients']),key=lambda pair:-abs(pair[1]))[:3]
            lines.append('أقوى ارتباطات النموذج المعيارية: '+' | '.join(f'{k}: {v:+.3f}R' for k,v in top))
    if r.get('checked_at'):
        from zoneinfo import ZoneInfo
        try:lines.append('وقت الفحص: '+utc(r['checked_at']).astimezone(ZoneInfo('Asia/Riyadh')).strftime('%Y-%m-%d %H:%M:%S')+' السعودية')
        except (ValueError,TypeError):lines.append('وقت الفحص المحفوظ غير صالح')
    if symbol:
        import re
        from ai_system import latest_decision
        from core.storage import database_path
        symbol=str(symbol).strip().upper()
        p=latest_decision(native_path or database_path(),symbol) if re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}',symbol) else None
        f=(p or {}).get('local_model_forecast') or {}
        estimate=num(f.get('expected_r'))
        if f.get('state')=='LOCAL_FORECAST_RECORDED' and estimate is not None:
            lines.append(f'{symbol}: توقع محلي محفوظ {estimate:+.3f}R | {f.get("preference")} | {(p or {}).get("timestamp")}؛ ليس احتمال نجاح أو أمرًا.')
        else:lines.append(f'{symbol}: لا يوجد توقع محلي محفوظ في آخر 500 قرار مقروء؛ لا نفترض أنه خضع للنموذج.')
    lines+=['الأوزان تتعلم من البيانات؛ الأنماط ارتباطات وليست ثغرات مضمونة أو أسبابًا مثبتة.',
        'لا اعتماد تلقائي؛ التوقع يُحفظ للمقارنة المستقبلية، والمخاطرة والشراء والوقف تحت ضوابط JALWE الحالية.']
    return '\n'.join(lines)[:3800]
